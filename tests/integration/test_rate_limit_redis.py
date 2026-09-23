from __future__ import annotations

import itertools
import os
import threading
from collections.abc import Iterator
from urllib.parse import quote
from uuid import uuid4

import pytest
import redis
from prometheus_client import REGISTRY
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request

from bancaemdia.config import get_settings
from bancaemdia.middleware.rate_limit import _limiter as api_limiter
from bancaemdia.middleware.rate_limit import user_key
from bancaemdia.rate_limit import anthropic_limiter

pytestmark = pytest.mark.xdist_group("redis")

IMAGE = "redis:7-alpine"


def _docker_available() -> bool:
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception:
        return False
    return True


@pytest.fixture(scope="module")
def server() -> Iterator[redis.Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    if url:
        yield redis.Redis.from_url(url)
        return
    if not _docker_available():
        pytest.skip("integration tests need Docker or TEST_REDIS_URL pointing at a Redis")
    from testcontainers.community.redis import RedisContainer

    with RedisContainer(IMAGE) as container:
        yield container.get_client()


@pytest.fixture
def client(server: redis.Redis) -> redis.Redis:
    users = list(server.scan_iter(match="rl:anthropic:user:*"))
    server.delete(anthropic_limiter.GLOBAL_KEY, *users)
    return server


def _limiter(client, user_limit=10, global_limit=1_000_000, window=60):
    waits = []
    limiter = anthropic_limiter.AnthropicLimiter(
        client, user_limit, global_limit, window, sleep=waits.append
    )
    return limiter, waits


def _seconds(client):
    seconds, microseconds = client.time()
    return seconds + microseconds / 1_000_000


def _connection_url(client: redis.Redis) -> str:
    options = client.connection_pool.connection_kwargs
    host = str(options.get("host", "127.0.0.1"))
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = int(options.get("port", 6379))
    database = int(options.get("db", 0))
    username = options.get("username")
    password = options.get("password")
    credentials = ""
    if username is not None:
        credentials = quote(str(username), safe="")
    if password is not None:
        credentials += f":{quote(str(password), safe='')}"
    if credentials:
        credentials += "@"
    scheme = (
        "rediss" if client.connection_pool.connection_class.__name__ == "SSLConnection" else "redis"
    )
    return f"{scheme}://{credentials}{host}:{port}/{database}"


def _request_for_user(usuario_id: int) -> Request:
    request = Request({
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/apostas",
        "raw_path": b"/api/v1/apostas",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    })
    request.state.usuario_id = usuario_id
    return request


def _shared_api_scope() -> None:
    return None


def test_script_takes_tokens_and_refills_from_the_redis_clock(client) -> None:
    limiter, waits = _limiter(client)

    started = _seconds(client)
    burst = [limiter.acquire(7) for _ in range(10)]
    over = limiter.acquire(7)
    elapsed = _seconds(client) - started

    assert burst == [0.0] * 10
    assert 6.0 - elapsed - 0.002 <= over <= 6.0
    assert waits == [over]
    assert 0 < client.pttl("rl:anthropic:user:7") <= 66000


def test_concurrent_acquires_never_lose_a_reservation(client) -> None:
    pool = redis.BlockingConnectionPool(
        max_connections=4, timeout=30, **client.connection_pool.connection_kwargs
    )
    limiter, _ = _limiter(redis.Redis(connection_pool=pool), window=3600)
    errors = REGISTRY.get_sample_value("rate_limit_error_total") or 0.0
    results = []

    def request():
        results.append(limiter.acquire(7))

    threads = [threading.Thread(target=request) for _ in range(200)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    waits = sorted(results)
    assert (REGISTRY.get_sample_value("rate_limit_error_total") or 0.0) == pytest.approx(errors)
    assert len(waits) == 200
    assert waits[:10] == [0.0] * 10
    gaps = [later - earlier for earlier, later in itertools.pairwise(waits[10:])]
    assert all(gap == pytest.approx(360.0, abs=30.0) for gap in gaps)
    assert float(client.hget("rl:anthropic:user:7", "tokens")) == pytest.approx(-190.0, abs=0.5)


def test_one_user_never_spends_another_users_quota(client) -> None:
    limiter, _ = _limiter(client)

    started = _seconds(client)
    queued = [limiter.acquire(1) for _ in range(30)]
    elapsed = _seconds(client) - started

    assert 120.0 - elapsed - 0.002 <= queued[-1] <= 120.0
    assert limiter.acquire(2) == pytest.approx(0.0)


def test_global_bucket_is_shared_by_every_user(client) -> None:
    limiter, _ = _limiter(client, global_limit=3)

    started = _seconds(client)
    first = [limiter.acquire(user) for user in (1, 2, 3)]
    fourth = limiter.acquire(4)
    elapsed = _seconds(client) - started

    assert first == [0.0, 0.0, 0.0]
    assert 20.0 - elapsed - 0.002 <= fourth <= 20.0


def test_api_quota_is_shared_between_limiter_instances(
    client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = f"integration-{uuid4().hex}"
    with monkeypatch.context() as environment:
        environment.setenv("RATE_LIMIT_STORAGE", _connection_url(client))
        get_settings.cache_clear()
        first_worker = api_limiter(lambda: "2/minute", user_key, namespace)
        second_worker = api_limiter(lambda: "2/minute", user_key, namespace)
    get_settings.cache_clear()

    try:
        first_worker._check_request_limit(
            _request_for_user(7), _shared_api_scope, in_middleware=True
        )
        second_worker._check_request_limit(
            _request_for_user(7), _shared_api_scope, in_middleware=True
        )

        with pytest.raises(RateLimitExceeded):
            first_worker._check_request_limit(
                _request_for_user(7), _shared_api_scope, in_middleware=True
            )
    finally:
        keys = list(client.scan_iter(match=f"*{namespace}*"))
        if keys:
            client.delete(*keys)
        get_settings.cache_clear()
