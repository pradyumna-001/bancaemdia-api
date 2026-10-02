import asyncio
from contextvars import ContextVar

import pytest

from bancaemdia.workers import coleta_runtime


class Engine:
    def __init__(self):
        self.disposals = 0

    async def dispose(self):
        await asyncio.sleep(0)
        self.disposals += 1


def test_reuses_loop_but_copies_each_callers_context_and_closes_once():
    engine = Engine()
    runtime = coleta_runtime.CollectionRuntime(engine)
    context = ContextVar("task_identity", default=0)

    async def read_and_mutate():
        await asyncio.sleep(0)
        value = context.get()
        context.set(999)
        return value, asyncio.get_running_loop()

    try:
        context.set(7)
        first, first_loop = runtime.run(read_and_mutate())
        assert first == 7 and context.get() == 7
        context.set(8)
        second, second_loop = runtime.run(read_and_mutate())
        assert second == 8 and context.get() == 8
        assert first_loop is second_loop
    finally:
        runtime.close()
        runtime.close()
    assert first_loop.is_closed()
    assert engine.disposals == 1


def test_failed_task_cancels_its_background_work_before_next_task():
    runtime = coleta_runtime.CollectionRuntime(Engine())
    events = []
    background_tasks = []

    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    async def fail():
        background_tasks.append(asyncio.create_task(background()))
        await asyncio.sleep(0)
        raise RuntimeError("failed collection")

    async def next_task():
        await asyncio.sleep(0)
        return "next collection"

    try:
        with pytest.raises(RuntimeError, match="failed collection"):
            runtime.run(fail())
        assert events == ["cancelled"]
        assert background_tasks[0].cancelled()
        assert not asyncio.all_tasks(runtime.runner.get_loop())
        assert runtime.run(next_task()) == "next collection"
    finally:
        runtime.close()


@pytest.mark.parametrize("boundary", ["process", "thread"])
def test_rejects_cross_boundary_use_without_running_or_leaking_coroutine(monkeypatch, boundary):
    runtime = coleta_runtime.CollectionRuntime(Engine())

    async def forbidden():
        await asyncio.sleep(0)
        raise AssertionError("wrong owner executed task")

    try:
        with monkeypatch.context() as patch:
            module = coleta_runtime.os if boundary == "process" else coleta_runtime.threading
            name = "getpid" if boundary == "process" else "get_ident"
            original = getattr(module, name)()
            patch.setattr(module, name, lambda: original + 1)
            with pytest.raises(RuntimeError, match="boundary"):
                runtime.run(forbidden())
    finally:
        runtime.close()


def test_parent_initialization_preserves_direct_call_path():
    assert coleta_runtime.initialize_collection_runtime() is None
    assert coleta_runtime.get_collection_runtime() is None


def test_rejects_nested_tasks_and_calls_after_shutdown():
    runtime = coleta_runtime.CollectionRuntime(Engine())

    async def inner():
        await asyncio.sleep(0)
        raise AssertionError("rejected coroutine executed")

    async def outer():
        with pytest.raises(RuntimeError, match="nested task"):
            runtime.run(inner())
        await asyncio.sleep(0)

    try:
        runtime.run(outer())
    finally:
        runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.run(inner())


def test_child_initialization_is_lazy_idempotent_and_shutdown_disposes(monkeypatch):
    engine = Engine()
    options = []
    monkeypatch.setattr(coleta_runtime, "_parent_pid", coleta_runtime.os.getpid() + 1)

    def create_engine(url, **kwargs):
        options.append(kwargs)
        return engine

    monkeypatch.setattr(coleta_runtime, "create_async_engine", create_engine)
    try:
        first = coleta_runtime.initialize_collection_runtime()
        assert first is not None
        assert coleta_runtime.initialize_collection_runtime() is first
        assert coleta_runtime.get_collection_runtime() is first
        assert len(options) == 1
        assert options[0]["pool_size"] == 1 and options[0]["max_overflow"] == 0
        assert options[0]["pool_pre_ping"] is True
    finally:
        coleta_runtime.close_collection_runtime()
    assert coleta_runtime.get_collection_runtime() is None
    assert engine.disposals == 1
