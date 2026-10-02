"""Temporary numeric-only timing probe around the unchanged staging application."""

import json
import os
import sys
import time
from collections import defaultdict
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

stages: ContextVar[dict[str, float] | None] = ContextVar("ingest_timings", default=None)


def timed(name: str, function: Any, *, asynchronous: bool = True) -> Any:
    def record(started: float) -> None:
        values = stages.get()
        if values is not None:
            values[name] = values.get(name, 0) + time.perf_counter() - started

    @wraps(function)
    async def async_call(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return await function(*args, **kwargs)
        finally:
            record(started)

    @wraps(function)
    def sync_call(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            record(started)

    return async_call if asynchronous else sync_call


def instrument() -> Any:
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession

    from bancaemdia.api.v1 import coleta
    from bancaemdia.db.session import engine
    from bancaemdia.main import app as backend
    from bancaemdia.middleware.rate_limit import ALL_LIMITERS

    for name in ("registrar", "_set_current_user"):
        setattr(coleta, name, timed(name, getattr(coleta, name)))
    coleta._enfileirar = timed("publish", coleta._enfileirar, asynchronous=False)
    for cls, method in (
        (coleta.ColetaTokenRepo, "get_usuario_id_by_hash"),
        (coleta.ColetaCasaRepo, "count_received_since"),
        (coleta.CasaRepo, "get_id_by_nome"),
        (AsyncSession, "commit"),
    ):
        setattr(cls, method, timed(method, getattr(cls, method)))
    for limiter in ALL_LIMITERS:
        for method in ("_check_request_limit", "_inject_headers"):
            setattr(limiter, method, timed(method, getattr(limiter, method), asynchronous=False))

    def connected(*args: Any) -> None:
        values = stages.get()
        if values is not None:
            values["new_connections"] = values.get("new_connections", 0) + 1

    event.listen(engine.sync_engine, "connect", connected)
    path = Path(os.environ["K6_STAGING_DIR"]) / f"timings-{os.getpid()}.jsonl"
    handle = path.open("a", encoding="utf-8")
    path.chmod(0o600)

    async def application(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] != "/coleta":
            await backend(scope, receive, send)
            return
        values: dict[str, float] = {}
        token = stages.set(values)
        started = time.perf_counter()
        try:
            await backend(scope, receive, send)
        finally:
            values["total"] = time.perf_counter() - started
            values["timestamp"] = time.time()
            handle.write(json.dumps(values) + "\n")
            handle.flush()
            stages.reset(token)

    return application


if __name__ == "__main__":
    rows = []
    for path in Path(os.environ["K6_STAGING_DIR"]).glob("timings-*.jsonl"):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    Path("k6-ingest-profile.json").write_text(json.dumps(rows), encoding="utf-8")
    samples: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for key, value in row.items():
            if key != "timestamp":
                samples[key].append(value)
    sys.stdout.write(
        json.dumps({
            key: {"n": len(v), "avg": sum(v) / len(v), "p95": sorted(v)[int(len(v) * 0.95)]}
            for key, v in samples.items()
        })
        + "\n"
    )
else:
    app = instrument()
