from __future__ import annotations

import anthropic
import pybreaker
import pytest
import redis
from prometheus_client import REGISTRY

from bancaemdia.config import get_settings
from bancaemdia.resilience import circuit_breaker


def _redis(failure=None):
    class Pipeline:
        def __init__(self, client):
            self.client = client

        def get(self, key):
            return self.client.get(key)

        def multi(self):
            return None

        def set(self, key, value):
            return self.client.set(key, value)

    class Redis:
        def __init__(self):
            self.data = {}

        def _check(self):
            if failure is not None:
                raise failure

        def setnx(self, key, value):
            self._check()
            if key in self.data:
                return False
            self.data[key] = str(value).encode()
            return True

        def get(self, key):
            self._check()
            return self.data.get(key)

        def set(self, key, value):
            self._check()
            self.data[key] = str(value).encode()
            return True

        def incr(self, key):
            self._check()
            self.data[key] = str(int(self.data.get(key, b"0")) + 1).encode()
            return int(self.data[key])

        def transaction(self, func, *watches):
            self._check()
            func(Pipeline(self))

    return Redis()


def _shared(client):
    return circuit_breaker.new_anthropic_breaker(
        circuit_breaker.RedisStorage(pybreaker.STATE_CLOSED, client, namespace="anthropic")
    )


def _boom():
    raise RuntimeError("503 from upstream")


def _trip(breaker):
    for _ in range(circuit_breaker.FAIL_MAX - 1):
        with pytest.raises(RuntimeError):
            breaker.call(_boom)
    with pytest.raises(pybreaker.CircuitBreakerError):
        breaker.call(_boom)


def _gauge():
    return REGISTRY.get_sample_value("circuit_breaker_state", {"breaker": "anthropic"})


def test_anthropic_breaker_matches_the_issue() -> None:
    breaker = circuit_breaker.new_anthropic_breaker()

    assert breaker.name == "anthropic"
    assert breaker.fail_max == 5
    assert breaker.reset_timeout == 60
    assert anthropic.APITimeoutError in breaker.excluded_exceptions
    assert circuit_breaker.new_anthropic_breaker() is not breaker


def test_state_gauge_follows_every_transition() -> None:
    breaker = circuit_breaker.new_anthropic_breaker()
    assert _gauge() == pytest.approx(0.0)

    _trip(breaker)
    assert _gauge() == pytest.approx(1.0)

    breaker.half_open()
    assert _gauge() == pytest.approx(2.0)

    breaker.close()
    assert _gauge() == pytest.approx(0.0)


def test_breaker_opened_by_one_worker_refuses_calls_in_another() -> None:
    client = _redis()
    worker_a, worker_b = _shared(client), _shared(client)
    calls = []

    _trip(worker_a)
    circuit_breaker.circuit_breaker_state.labels(breaker="anthropic").set(0)
    with pytest.raises(pybreaker.CircuitBreakerError):
        worker_b.call(calls.append, "sent")

    assert calls == []
    assert worker_b.current_state == "open"
    assert _gauge() == pytest.approx(1.0)


def test_trial_call_after_the_reset_timeout_closes_the_breaker_for_everyone() -> None:
    client = _redis()
    worker_a, worker_b = _shared(client), _shared(client)
    _trip(worker_a)
    worker_b.reset_timeout = 0

    assert worker_b.call(lambda: "read") == "read"
    assert worker_a.current_state == "closed"
    assert _gauge() == pytest.approx(0.0)


def test_redis_down_leaves_the_breaker_closed_and_calls_going() -> None:
    breaker = _shared(_redis(failure=redis.ConnectionError("down")))

    for _ in range(circuit_breaker.FAIL_MAX + 1):
        with pytest.raises(RuntimeError):
            breaker.call(_boom)

    assert breaker.current_state == "closed"
    assert breaker.call(lambda: "read") == "read"


def test_get_anthropic_breaker_keeps_its_state_in_redis(monkeypatch) -> None:
    client = _redis()
    opened = {}

    def from_url(url, **options):
        opened.update(url=url, **options)
        return client

    monkeypatch.setenv("REDIS_URL", "redis://breaker-host:6380/4")
    monkeypatch.setattr(circuit_breaker.redis.Redis, "from_url", from_url)
    get_settings.cache_clear()
    circuit_breaker.get_redis.cache_clear()
    circuit_breaker.get_anthropic_breaker.cache_clear()
    try:
        breaker = circuit_breaker.get_anthropic_breaker()
    finally:
        get_settings.cache_clear()
        circuit_breaker.get_redis.cache_clear()
        circuit_breaker.get_anthropic_breaker.cache_clear()

    assert opened == {
        "url": "redis://breaker-host:6380/4",
        "socket_timeout": circuit_breaker.TIMEOUT_SECONDS,
        "socket_connect_timeout": circuit_breaker.TIMEOUT_SECONDS,
    }
    assert breaker.name == "anthropic"
    assert client.data[circuit_breaker.STATE_KEY] == b"closed"


def test_breaker_states_reads_the_shared_state_and_updates_the_gauge(monkeypatch) -> None:
    client = _redis()
    _trip(_shared(client))
    circuit_breaker.circuit_breaker_state.labels(breaker="anthropic").set(0)
    monkeypatch.setattr(circuit_breaker, "get_redis", lambda: client)

    assert circuit_breaker.breaker_states() == {"anthropic": "open"}
    assert _gauge() == pytest.approx(1.0)


def test_breaker_states_is_closed_before_any_breaker_wrote_to_redis(monkeypatch) -> None:
    monkeypatch.setattr(circuit_breaker, "get_redis", _redis)

    assert circuit_breaker.breaker_states() == {"anthropic": "closed"}


def test_breaker_states_is_unknown_when_redis_is_down(monkeypatch) -> None:
    down = _redis(failure=redis.ConnectionError("down"))
    monkeypatch.setattr(circuit_breaker, "get_redis", lambda: down)
    circuit_breaker.circuit_breaker_state.labels(breaker="anthropic").set(1)

    assert circuit_breaker.breaker_states() == {"anthropic": "unknown"}
    assert _gauge() == pytest.approx(1.0)
