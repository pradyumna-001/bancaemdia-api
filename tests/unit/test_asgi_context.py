"""Streaming and cancellation retain tenant/routing context without extra tasks."""

import asyncio

import pytest
from starlette.types import Message, Receive, Scope, Send

from bancaemdia.core.context import current_user_id, use_primary
from bancaemdia.middleware import router
from bancaemdia.middleware.rls import RLSMiddleware
from bancaemdia.middleware.router import RecentWrites, RouterMiddleware


def scope_for(owner: int, method: str = "GET") -> Scope:
    return {
        "type": "http",
        "method": method,
        "path": "/api/v1/apostas",
        "root_path": "",
        "headers": [],
        "query_string": b"",
        "state": {"usuario_id": owner},
    }


async def receive() -> Message:
    await asyncio.sleep(0)
    return {"type": "http.request", "body": b"", "more_body": False}


async def discard(message: Message) -> None:
    pass


async def test_concurrent_streams_keep_each_owner_and_primary_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = RecentWrites()
    monkeypatch.setattr(router, "get_recent_writes", lambda: writes)
    seen: list[tuple[int, str | None, bool]] = []
    sent: list[tuple[str | None, bool, str]] = []

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        owner = scope["state"]["usuario_id"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for index in range(3):
            await asyncio.sleep(0)
            seen.append((owner, current_user_id.get(), use_primary.get()))
            await send({"type": "http.response.body", "body": b"chunk", "more_body": index < 2})

    async def observe_response(message: Message) -> None:
        await asyncio.sleep(0)
        sent.append((current_user_id.get(), use_primary.get(), message["type"]))

    wrapped = RLSMiddleware(RouterMiddleware(endpoint))
    original = (current_user_id.get(), use_primary.get())
    await asyncio.gather(
        wrapped(scope_for(10, "POST"), receive, observe_response),
        wrapped(scope_for(20), receive, observe_response),
    )
    assert [entry for entry in seen if entry[0] == 10] == [(10, "10", True)] * 3
    assert [entry for entry in seen if entry[0] == 20] == [(20, "20", False)] * 3
    assert [entry for entry in sent if entry[0] == "10"] == [
        ("10", True, "http.response.start"),
        *[("10", True, "http.response.body")] * 3,
    ]
    assert [entry for entry in sent if entry[0] == "20"] == [
        ("20", False, "http.response.start"),
        *[("20", False, "http.response.body")] * 3,
    ]
    assert writes.recent(10) and not writes.recent(20)
    assert (current_user_id.get(), use_primary.get()) == original


async def test_cancelled_response_restores_context_in_the_calling_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = RecentWrites()
    monkeypatch.setattr(router, "get_recent_writes", lambda: writes)
    started = asyncio.Event()
    observed: list[tuple[str | None, bool]] = []

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        observed.append((current_user_id.get(), use_primary.get()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await asyncio.Event().wait()

    wrapped = RLSMiddleware(RouterMiddleware(endpoint))

    async def request() -> None:
        owner_token = current_user_id.set("outer")
        primary_token = use_primary.set(False)
        try:
            with pytest.raises(asyncio.CancelledError):
                await wrapped(scope_for(30, "POST"), receive, discard)
            observed.append((current_user_id.get(), use_primary.get()))
        finally:
            current_user_id.reset(owner_token)
            use_primary.reset(primary_token)

    task = asyncio.create_task(request())
    await started.wait()
    task.cancel()
    await task
    assert observed == [("30", True), ("outer", False)]
    assert writes.recent(30)


async def test_non_http_scope_passes_through_without_installing_user_context() -> None:
    observed = []

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0)
        observed.append((scope["type"], current_user_id.get(), use_primary.get()))

    original = (current_user_id.get(), use_primary.get())
    await RLSMiddleware(RouterMiddleware(endpoint))(
        {"type": "websocket", "state": {"usuario_id": 99}}, receive, discard
    )
    assert observed == [("websocket", *original)]
