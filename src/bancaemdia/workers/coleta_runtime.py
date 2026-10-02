"""One loop and PostgreSQL connection for sequential collection tasks in each prefork child."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bancaemdia.config import get_settings

_parent_pid = os.getpid()
_runtime: CollectionRuntime | None = None


@dataclass
class CollectionRuntime:
    engine: AsyncEngine
    pid: int = field(default_factory=os.getpid)
    thread_id: int = field(default_factory=threading.get_ident)
    runner: asyncio.Runner = field(default_factory=asyncio.Runner)
    closed: bool = False

    def _check_owner(self) -> None:
        if self.pid != os.getpid() or self.thread_id != threading.get_ident():
            raise RuntimeError("Collection runtime cannot cross a process or thread boundary")

    def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        try:
            self._check_owner()
            if self.closed:
                raise RuntimeError("Collection runtime is closed")
            loop = self.runner.get_loop()
            if loop.is_running():
                raise RuntimeError("Collection runtime cannot run a nested task")
        except BaseException:
            coroutine.close()
            raise
        previous = asyncio.all_tasks(loop)
        # Match asyncio.run's fresh task context, including the current Celery trace context.
        task = loop.create_task(coroutine, context=copy_context())
        try:
            return loop.run_until_complete(task)
        finally:
            pending = asyncio.all_tasks(loop) - previous
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def close(self) -> None:
        if self.closed:
            return
        self._check_owner()
        try:
            self.run(self.engine.dispose())
        finally:
            self.runner.close()
            self.closed = True


def get_collection_runtime() -> CollectionRuntime | None:
    runtime = _runtime
    if runtime is None or runtime.pid != os.getpid() or runtime.thread_id != threading.get_ident():
        return None
    return runtime


def initialize_collection_runtime() -> CollectionRuntime | None:
    global _runtime
    # Imported by celery_app before forking. Outside prefork children, direct calls, CLI/eager
    # tasks and thread/solo pools retain asyncio.run + NullPool. No parent-owned loop is inherited.
    if os.getpid() == _parent_pid:
        return None
    runtime = get_collection_runtime()
    if runtime is not None:
        return runtime
    if _runtime is not None:
        raise RuntimeError("Collection runtime must be initialized in its prefork child")
    settings = get_settings()
    _runtime = CollectionRuntime(
        create_async_engine(
            settings.DATABASE_URL,
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        )
    )
    return _runtime


def close_collection_runtime() -> None:
    global _runtime
    runtime = get_collection_runtime()
    if runtime is not None:
        try:
            runtime.close()
        finally:
            _runtime = None
