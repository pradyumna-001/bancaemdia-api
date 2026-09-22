from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bancaemdia.observability.health import (
    CELERY_PRIORITY_SEPARATOR,
    CheckResult,
    DependencyNotReadyError,
    ReadinessChecker,
    ReadinessReport,
    celery_queue_key,
    check_anthropic,
    check_celery_queue_depth,
    check_postgres_primary,
    check_postgres_replica,
    check_redis,
    liveness,
)


class FakeConnection:
    def __init__(self, *scalars: object) -> None:
        self.scalars = list(scalars)
        self.executed: list[str] = []

    async def scalar(self, statement: object) -> object:
        self.executed.append(str(statement))
        return self.scalars.pop(0)


class ConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> ConnectionContext:
        return ConnectionContext(self.connection)


class DownEngine:
    def connect(self) -> ConnectionContext:
        raise ConnectionError("postgresql://admin:secret@primary.internal/database")


class FakeRedis:
    def __init__(self, *, ping: object = True, sizes: dict[str, int] | None = None) -> None:
        self.ping_response = ping
        self.sizes = sizes or {}
        self.keys: list[str] = []
        self.closed = False

    async def ping(self) -> object:
        return self.ping_response

    async def llen(self, key: str) -> int:
        self.keys.append(key)
        return self.sizes.get(key, 0)

    async def aclose(self) -> None:
        self.closed = True


def test_liveness_has_no_dependencies() -> None:
    assert liveness() == {"status": "ok"}


async def test_readiness_runs_all_checks_concurrently() -> None:
    started: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()

    def make_check(name: str):
        async def check() -> dict[str, object]:
            started.add(name)
            if len(started) == 5:
                all_started.set()
            await release.wait()
            return {"name": name}

        return check

    checker = ReadinessChecker(
        {name: make_check(name) for name in ("primary", "replica", "redis", "ai", "queue")},
        timeout_seconds=0.5,
    )

    pending = asyncio.create_task(checker.check())
    await asyncio.wait_for(all_started.wait(), timeout=0.2)
    assert started == {"primary", "replica", "redis", "ai", "queue"}
    release.set()
    report = await pending

    assert report.ready is True
    assert report.status_code == 200
    assert report.as_dict()["status"] == "ready"


async def test_concurrent_readiness_requests_share_one_probe_run() -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def probe() -> dict[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"mode": "ok"}

    checker = ReadinessChecker({"dependency": probe}, timeout_seconds=0.5, cache_ttl_seconds=2.0)
    pending = [asyncio.create_task(checker.check()) for _ in range(10)]
    await asyncio.wait_for(started.wait(), timeout=0.2)
    release.set()
    reports = await asyncio.gather(*pending)

    assert calls == 1
    assert all(report is reports[0] for report in reports)
    assert await checker.check() is reports[0]
    assert calls == 1


async def test_readiness_times_out_and_never_exposes_exception_messages() -> None:
    async def ok() -> dict[str, object]:
        await asyncio.sleep(0)
        return {"mode": "safe"}

    async def stalled() -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    async def leaks_if_serialized() -> dict[str, object]:
        await asyncio.sleep(0)
        raise RuntimeError("postgresql://admin:secret@example.internal/database")

    checker = ReadinessChecker(
        {"ok": ok, "slow": stalled, "error": leaks_if_serialized}, timeout_seconds=0.01
    )

    report = await checker.check()
    payload = report.as_dict()
    serialized = json.dumps(payload)

    assert report.ready is False
    assert report.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["slow"]["details"] == {"reason": "timeout"}
    assert payload["checks"]["error"]["details"] == {"reason": "internal_error"}
    assert "secret" not in serialized
    assert "example.internal" not in serialized


async def test_primary_down_makes_readiness_return_503_without_exposing_the_dsn() -> None:
    checker = ReadinessChecker(
        {"postgres_primary": lambda: check_postgres_primary(DownEngine())},  # type: ignore[arg-type]
        timeout_seconds=0.1,
    )

    report = await checker.check()
    payload = report.as_dict()

    assert report.status_code == 503
    assert payload["checks"]["postgres_primary"]["details"] == {"reason": "internal_error"}
    assert "primary.internal" not in json.dumps(payload)


def test_readiness_report_requires_every_check() -> None:
    report = ReadinessReport({
        "one": CheckResult("ok", 1.2),
        "two": CheckResult("failed", 2.3, {"reason": "down"}),
    })

    assert report.ready is False
    assert report.as_dict() == {
        "status": "not_ready",
        "checks": {
            "one": {"status": "ok", "latency_ms": 1.2},
            "two": {
                "status": "failed",
                "latency_ms": 2.3,
                "details": {"reason": "down"},
            },
        },
    }


async def test_primary_probe_checks_read_write_mode_without_mutation() -> None:
    connection = FakeConnection(True)

    details = await check_postgres_primary(FakeEngine(connection))  # type: ignore[arg-type]

    assert details == {"mode": "writable"}
    assert len(connection.executed) == 1
    assert "pg_is_in_recovery" in connection.executed[0]


async def test_primary_probe_rejects_read_only_without_attempting_a_write() -> None:
    connection = FakeConnection(False)

    with pytest.raises(DependencyNotReadyError, match="not_writable"):
        await check_postgres_primary(FakeEngine(connection))  # type: ignore[arg-type]

    assert len(connection.executed) == 1


async def test_replica_probe_requires_a_read_only_connection() -> None:
    read_only = FakeConnection(True, True)
    writable = FakeConnection(False, False)

    assert await check_postgres_replica(FakeEngine(read_only)) == {  # type: ignore[arg-type]
        "mode": "read_only",
        "in_recovery": True,
    }
    with pytest.raises(DependencyNotReadyError, match="not_read_only"):
        await check_postgres_replica(FakeEngine(writable))  # type: ignore[arg-type]


async def test_redis_probe_requires_a_real_pong() -> None:
    assert await check_redis(FakeRedis()) == {"response": "pong"}

    with pytest.raises(DependencyNotReadyError, match="unexpected_response"):
        await check_redis(FakeRedis(ping=False))


def test_celery_queue_key_matches_kombu_priority_layout() -> None:
    assert celery_queue_key("extraction", 0) == "extraction"
    assert celery_queue_key("extraction", 3) == f"extraction{CELERY_PRIORITY_SEPARATOR}3"


async def test_celery_depth_sums_priorities_and_queues() -> None:
    redis = FakeRedis(
        sizes={
            "extraction": 4,
            celery_queue_key("extraction", 3): 1,
            "materialization": 2,
        }
    )

    details = await check_celery_queue_depth(
        redis, queues=("extraction", "materialization"), limit=10
    )

    assert details == {
        "depths": {"extraction": 5, "materialization": 2},
        "total": 7,
        "limit": 10,
    }
    assert len(redis.keys) == 8


async def test_celery_depth_fails_closed_at_the_configured_limit() -> None:
    redis = FakeRedis(sizes={"extraction": 5})

    with pytest.raises(DependencyNotReadyError) as raised:
        await check_celery_queue_depth(redis, queues=("extraction",), limit=5)

    assert raised.value.reason == "threshold_exceeded"
    assert raised.value.details == {
        "depths": {"extraction": 5},
        "total": 5,
        "limit": 5,
    }


async def test_anthropic_uses_an_authenticated_get_on_a_real_api_resource() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.test/", transport=httpx.MockTransport(handler)
    ) as client:
        details = await check_anthropic(client, api_key="top-secret-key")

    assert details == {"http_status": 200}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/models"
    assert dict(requests[0].url.params) == {"limit": "1"}
    assert requests[0].headers["x-api-key"] == "top-secret-key"
    assert requests[0].headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [(401, "authentication_failed"), (429, "rate_limited"), (503, "provider_error")],
)
async def test_anthropic_reports_only_safe_bounded_failures(status_code: int, reason: str) -> None:
    secret = "must-never-appear"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=f"upstream echoed {secret}")

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.test/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(DependencyNotReadyError) as raised:
            await check_anthropic(client, api_key=secret)

    assert raised.value.reason == reason
    assert raised.value.details == {"http_status": status_code}
    assert secret not in json.dumps(raised.value.details)


async def test_anthropic_missing_key_fails_without_an_http_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url="https://api.anthropic.test/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(DependencyNotReadyError, match="not_configured"):
            await check_anthropic(client, api_key=None)

    assert called is False
