from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal, Protocol, cast

import httpx
import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bancaemdia.config import get_settings

ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_CELERY_QUEUES = ("extraction", "materialization", "dead_letter")
# Kombu's Redis transport stores each configured priority in a separate list. These are its
# defaults; keeping the values here prevents a health endpoint from importing/initializing Celery.
CELERY_PRIORITY_STEPS = (0, 3, 6, 9)
CELERY_PRIORITY_SEPARATOR = "\x06\x16"
READINESS_CACHE_TTL_SECONDS = 2.0

PRIMARY_WRITABLE_SQL = text(
    "SELECT NOT pg_is_in_recovery() AND NOT current_setting('transaction_read_only')::boolean"
)
REPLICA_READ_ONLY_SQL = text("SELECT current_setting('transaction_read_only')::boolean")
REPLICA_RECOVERY_SQL = text("SELECT pg_is_in_recovery()")

type JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None
type CheckDetails = dict[str, JsonValue]
type ReadinessCheck = Callable[[], Awaitable[CheckDetails]]
type CheckStatus = Literal["ok", "failed", "degraded"]
type CheckImpact = Literal["required", "report_only"]


class RedisProbe(Protocol):
    async def ping(self) -> object: ...

    async def llen(self, key: str) -> int: ...

    async def aclose(self) -> None: ...


class DependencyNotReadyError(Exception):
    """A bounded, safe reason that may be included in the public readiness response."""

    def __init__(self, reason: str, details: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    latency_ms: float
    details: Mapping[str, JsonValue] = field(default_factory=dict)
    impact: CheckImpact = "required"

    def as_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "status": self.status,
            "latency_ms": self.latency_ms,
            "impact": self.impact,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class ReadinessReport:
    checks: Mapping[str, CheckResult]

    @property
    def ready(self) -> bool:
        return all(
            result.status == "ok" for result in self.checks.values() if result.impact == "required"
        )

    @property
    def status_code(self) -> int:
        return 200 if self.ready else 503

    def as_dict(self) -> dict[str, JsonValue]:
        checks: dict[str, JsonValue] = {
            name: result.as_dict() for name, result in self.checks.items()
        }
        return {"status": "ready" if self.ready else "not_ready", "checks": checks}


class ReadinessChecker:
    def __init__(
        self,
        checks: Mapping[str, ReadinessCheck],
        *,
        timeout_seconds: float,
        cache_ttl_seconds: float = READINESS_CACHE_TTL_SECONDS,
        report_only: Collection[str] = (),
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not checks:
            raise ValueError("readiness needs at least one check")
        if timeout_seconds <= 0:
            raise ValueError("readiness timeout must be positive")
        if cache_ttl_seconds < 0:
            raise ValueError("readiness cache TTL cannot be negative")
        report_only_names = frozenset(report_only)
        unknown_report_only = report_only_names.difference(checks)
        if unknown_report_only:
            names = ", ".join(sorted(unknown_report_only))
            raise ValueError(f"report-only checks are not configured: {names}")
        if len(report_only_names) == len(checks):
            raise ValueError("readiness needs at least one required check")
        self._checks = tuple(checks.items())
        self._report_only = report_only_names
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached: tuple[float, ReadinessReport] | None = None

    async def check(self) -> ReadinessReport:
        cached = self._fresh_cache()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh_cache()
            if cached is not None:
                return cached
            report = await self._run_checks()
            self._cached = (self._clock() + self._cache_ttl_seconds, report)
            return report

    def _fresh_cache(self) -> ReadinessReport | None:
        if self._cached is None:
            return None
        expires_at, report = self._cached
        return report if self._clock() < expires_at else None

    async def _run_checks(self) -> ReadinessReport:
        # Every dependency starts before any one of them can consume the whole timeout budget.
        results = await asyncio.gather(
            *(
                self._run_check(
                    name,
                    check,
                    impact="report_only" if name in self._report_only else "required",
                )
                for name, check in self._checks
            )
        )
        return ReadinessReport(dict(results))

    async def _run_check(
        self,
        name: str,
        check: ReadinessCheck,
        *,
        impact: CheckImpact,
    ) -> tuple[str, CheckResult]:
        started = self._clock()
        failed_status: CheckStatus = "degraded" if impact == "report_only" else "failed"
        try:
            async with asyncio.timeout(self._timeout_seconds):
                details = await check()
        except TimeoutError:
            result = CheckResult(
                status=failed_status,
                latency_ms=self._elapsed_ms(started),
                details={"reason": "timeout"},
                impact=impact,
            )
        except DependencyNotReadyError as error:
            details = {"reason": error.reason, **error.details}
            result = CheckResult(
                status=failed_status,
                latency_ms=self._elapsed_ms(started),
                details=details,
                impact=impact,
            )
        except Exception:
            # Never serialize exception messages: drivers commonly include host names, DSNs and
            # credentials in them. The full error belongs in internal logs, not a public probe.
            result = CheckResult(
                status=failed_status,
                latency_ms=self._elapsed_ms(started),
                details={"reason": "internal_error"},
                impact=impact,
            )
        else:
            result = CheckResult(
                status="ok",
                latency_ms=self._elapsed_ms(started),
                details=details,
                impact=impact,
            )
        return name, result

    def _elapsed_ms(self, started: float) -> float:
        return round(max(0.0, self._clock() - started) * 1000, 2)


def liveness() -> dict[str, str]:
    """Liveness deliberately has no dependency I/O: a live process is healthy enough to restart."""

    return {"status": "ok"}


async def check_postgres_primary(engine: AsyncEngine) -> CheckDetails:
    """Verify the connection is a read-write primary without mutating catalogs or user data."""

    async with engine.connect() as connection:
        writable = await connection.scalar(PRIMARY_WRITABLE_SQL)
    if writable is not True:
        raise DependencyNotReadyError("not_writable")
    return {"mode": "writable"}


async def check_postgres_replica(engine: AsyncEngine) -> CheckDetails:
    """Run real reads and reject a replica connection that accidentally permits writes."""

    async with engine.connect() as connection:
        read_only = await connection.scalar(REPLICA_READ_ONLY_SQL)
        in_recovery = await connection.scalar(REPLICA_RECOVERY_SQL)
    if read_only is not True:
        raise DependencyNotReadyError("not_read_only")
    return {"mode": "read_only", "in_recovery": in_recovery is True}


async def check_redis(client: RedisProbe) -> CheckDetails:
    if await client.ping() is not True:
        raise DependencyNotReadyError("unexpected_response")
    return {"response": "pong"}


def celery_queue_key(queue: str, priority: int) -> str:
    return f"{queue}{CELERY_PRIORITY_SEPARATOR}{priority}" if priority else queue


async def check_celery_queue_depth(
    client: RedisProbe,
    *,
    queues: Sequence[str] = DEFAULT_CELERY_QUEUES,
    limit: int,
) -> CheckDetails:
    if limit <= 0:
        raise ValueError("Celery queue depth limit must be positive")
    keys = [
        celery_queue_key(queue, priority) for queue in queues for priority in CELERY_PRIORITY_STEPS
    ]
    # Keep the commands on one client sequential: if one fails there are no orphaned commands
    # racing the client's close. The five top-level dependency probes still run concurrently.
    sizes: list[int] = []
    for key in keys:
        sizes.append(await client.llen(key))
    steps = len(CELERY_PRIORITY_STEPS)
    depths = {
        queue: sum(sizes[index * steps : (index + 1) * steps]) for index, queue in enumerate(queues)
    }
    total = sum(depths.values())
    depth_details: dict[str, JsonValue] = dict(depths)
    details: CheckDetails = {"depths": depth_details, "total": total, "limit": limit}
    if total >= limit:
        raise DependencyNotReadyError("threshold_exceeded", details)
    return details


async def check_anthropic(
    client: httpx.AsyncClient,
    *,
    api_key: str | None,
) -> CheckDetails:
    if not api_key:
        raise DependencyNotReadyError("not_configured")
    # HEAD is not part of the Anthropic API contract. Listing one model is a cheap authenticated
    # GET against a real endpoint and does not create content or incur model usage.
    response = await client.get(
        "v1/models",
        params={"limit": 1},
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
    )
    if 200 <= response.status_code < 300:
        return {"http_status": response.status_code}
    details: CheckDetails = {"http_status": response.status_code}
    if response.status_code in {401, 403}:
        raise DependencyNotReadyError("authentication_failed", details)
    if response.status_code == 429:
        raise DependencyNotReadyError("rate_limited", details)
    if response.status_code >= 500:
        raise DependencyNotReadyError("provider_error", details)
    raise DependencyNotReadyError("unexpected_response", details)


def build_readiness_checker(
    *,
    primary_engine: AsyncEngine,
    replica_engine: AsyncEngine,
    redis_url: str,
    celery_broker_url: str,
    anthropic_api_key: str | None,
    timeout_seconds: float,
    celery_queue_depth_limit: int,
    celery_queues: Sequence[str] = DEFAULT_CELERY_QUEUES,
    anthropic_base_url: str = ANTHROPIC_API_BASE_URL,
) -> ReadinessChecker:
    if celery_queue_depth_limit <= 0:
        raise ValueError("Celery queue depth limit must be positive")

    async def redis_check() -> CheckDetails:
        client = cast(
            RedisProbe,
            redis.Redis.from_url(
                redis_url,
                socket_timeout=timeout_seconds,
                socket_connect_timeout=timeout_seconds,
            ),
        )
        try:
            return await check_redis(client)
        finally:
            await client.aclose()

    async def celery_check() -> CheckDetails:
        client = cast(
            RedisProbe,
            redis.Redis.from_url(
                celery_broker_url,
                socket_timeout=timeout_seconds,
                socket_connect_timeout=timeout_seconds,
            ),
        )
        try:
            return await check_celery_queue_depth(
                client, queues=celery_queues, limit=celery_queue_depth_limit
            )
        finally:
            await client.aclose()

    async def anthropic_check() -> CheckDetails:
        async with httpx.AsyncClient(
            base_url=anthropic_base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as client:
            return await check_anthropic(client, api_key=anthropic_api_key)

    return ReadinessChecker(
        {
            "postgres_primary": lambda: check_postgres_primary(primary_engine),
            "postgres_replica": lambda: check_postgres_replica(replica_engine),
            "redis": redis_check,
            "anthropic": anthropic_check,
            "celery_queue_depth": celery_check,
        },
        timeout_seconds=timeout_seconds,
        # These probes remain visible for operators, but neither dependency is required for the
        # API process to serve most request paths. Draining API pods cannot heal an LLM outage or
        # reduce a worker backlog, so their failures must never change the readiness verdict.
        report_only={"anthropic", "celery_queue_depth"},
    )


@lru_cache
def get_readiness_checker() -> ReadinessChecker:
    # Importing the engines lazily keeps this module unit-testable without constructing global pools.
    from bancaemdia.db.session import engine, replica_engine

    settings = get_settings()
    return build_readiness_checker(
        primary_engine=engine,
        replica_engine=replica_engine,
        redis_url=settings.REDIS_URL,
        celery_broker_url=settings.CELERY_BROKER_URL,
        anthropic_api_key=settings.ANTHROPIC_API_KEY,
        timeout_seconds=settings.READINESS_CHECK_TIMEOUT_SECONDS,
        celery_queue_depth_limit=settings.CELERY_QUEUE_DEPTH_LIMIT,
    )
