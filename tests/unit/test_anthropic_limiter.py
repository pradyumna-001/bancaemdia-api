from __future__ import annotations

import math
import threading
import time

import pytest
import redis
from prometheus_client import REGISTRY

from bancaemdia.config import get_settings
from bancaemdia.rate_limit import anthropic_limiter


def _clock(participants=1):
    class Clock:
        def __init__(self):
            self.now = 0.0
            self.running = participants
            self.sleeping = []
            self.lock = threading.Lock()

        def sleep(self, seconds):
            wake = threading.Event()
            with self.lock:
                self.sleeping.append((self.now + seconds, wake))
                self._stop()
            wake.wait()

        def finish(self):
            with self.lock:
                self._stop()

        def _stop(self):
            self.running -= 1
            if self.running or not self.sleeping:
                return
            self.now = min(when for when, _ in self.sleeping)
            ready = [s for s in self.sleeping if s[0] <= self.now]
            for s in ready:
                self.sleeping.remove(s)
                s[1].set()
            self.running = len(ready)

    return Clock()


def _redis(clock, failure=None):
    class Redis:
        def __init__(self):
            self.script = None
            self.buckets = {}
            self.expiry = {}
            self.reservations = []
            self.lock = threading.Lock()

        def register_script(self, script):
            self.script = script

            def run(keys, args):
                if failure is not None:
                    raise failure
                with self.lock:
                    return self._take(keys[0], *args)

            return run

        def _take(self, key, limit, window, tokens):
            available = limit
            if key in self.buckets:
                stored, updated_at = self.buckets[key]
                refill = max(0, clock.now - updated_at) * limit / window
                available = min(limit, stored + refill)
            available -= tokens
            self.buckets[key] = (available, clock.now)
            self.expiry[key] = math.ceil((limit - available) * window * 1000 / limit)
            self.reservations.append((key, threading.current_thread()))
            return max(0, math.floor(-available * window * 1000 / limit))

    return Redis()


def _limiter(clock, user_limit=10, global_limit=100, window=60, failure=None, sleep=None):
    client = _redis(clock, failure)
    limiter = anthropic_limiter.AnthropicLimiter(
        client, user_limit, global_limit, window, sleep=sleep or clock.sleep
    )
    return limiter, client


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_keys_follow_the_issue() -> None:
    assert anthropic_limiter.USER_KEY.format(user_id=7) == "rl:anthropic:user:7"
    assert anthropic_limiter.GLOBAL_KEY == "rl:anthropic:global"


def test_full_bucket_lets_a_burst_through_without_waiting() -> None:
    clock = _clock()
    limiter, client = _limiter(clock)

    assert [limiter.acquire(7) for _ in range(10)] == [0.0] * 10
    assert clock.now == pytest.approx(0.0)
    assert client.script == anthropic_limiter.TOKEN_BUCKET


def test_request_over_the_limit_waits_for_one_refill() -> None:
    clock = _clock()
    limiter, _ = _limiter(clock)
    exceeded = _sample("rate_limit_exceeded_total", scope="user")
    for _ in range(10):
        limiter.acquire(7)

    assert limiter.acquire(7) == pytest.approx(6.0)
    assert clock.now == pytest.approx(6.0)
    assert _sample("rate_limit_exceeded_total", scope="user") == pytest.approx(exceeded + 1)


def test_bucket_refills_at_limit_over_window_per_second() -> None:
    clock = _clock()
    limiter, _ = _limiter(clock)
    for _ in range(10):
        limiter.acquire(7)

    clock.now = 3.0

    assert limiter.acquire(7) == pytest.approx(3.0)


def test_idle_bucket_never_holds_more_than_its_limit() -> None:
    clock = _clock()
    limiter, _ = _limiter(clock)
    limiter.acquire(7)
    clock.now = 3600.0

    assert [limiter.acquire(7) for _ in range(10)] == [0.0] * 10
    assert limiter.acquire(7) == pytest.approx(6.0)


def test_waiting_requests_of_one_user_are_served_in_arrival_order() -> None:
    limiter, _ = _limiter(_clock(), sleep=lambda _: None)
    for _ in range(10):
        limiter.acquire(7)

    assert [limiter.acquire(7) for _ in range(3)] == pytest.approx([6.0, 12.0, 18.0])


def test_one_user_never_spends_another_users_quota() -> None:
    limiter, _ = _limiter(_clock(), sleep=lambda _: None)

    assert [limiter.acquire(1) for _ in range(30)][-1] == pytest.approx(120.0)
    assert limiter.acquire(2) == pytest.approx(0.0)


def test_global_bucket_is_shared_by_every_user() -> None:
    clock = _clock()
    limiter, _ = _limiter(clock, global_limit=3)
    exceeded = _sample("rate_limit_exceeded_total", scope="global")

    assert [limiter.acquire(user) for user in (1, 2, 3)] == [0.0, 0.0, 0.0]
    assert limiter.acquire(4) == pytest.approx(20.0)
    assert _sample("rate_limit_exceeded_total", scope="global") == pytest.approx(exceeded + 1)


def _concurrently(users):
    clock = _clock(participants=len(users))
    limiter, client = _limiter(clock)
    fired = {}

    def request(user):
        try:
            waited = limiter.acquire(user)
            fired[threading.current_thread()] = (user, waited, clock.now)
        finally:
            clock.finish()

    threads = [threading.Thread(target=request, args=(user,), daemon=True) for user in users]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))

    assert not any(thread.is_alive() for thread in threads)
    return fired, client


def _fired_in_reservation_order(fired, client, key):
    fired_at = [fired[thread][2] for name, thread in client.reservations if name == key]
    return fired_at == sorted(fired_at)


def test_burst_of_200_concurrent_requests_is_queued_not_rejected() -> None:
    users = [1] * 150 + [user for user in range(2, 7) for _ in range(10)]

    fired, client = _concurrently(users)

    assert len(fired) == 200
    waits = {user: sorted(w for u, w, _ in fired.values() if u == user) for user in set(users)}
    assert waits[1] == pytest.approx([0.0] * 10 + [6.0 * n for n in range(1, 141)])
    assert all(waits[user] == [0.0] * 10 for user in range(2, 7))
    assert _fired_in_reservation_order(fired, client, "rl:anthropic:user:1")


def test_burst_of_200_concurrent_requests_queues_on_the_global_bucket() -> None:
    exceeded = _sample("rate_limit_exceeded_total", scope="global")

    fired, client = _concurrently([user for user in range(20) for _ in range(10)])

    assert len(fired) == 200
    waits = sorted(waited for _, waited, _ in fired.values())
    assert waits == pytest.approx([0.0] * 100 + [0.6 * n for n in range(1, 101)])
    assert _sample("rate_limit_exceeded_total", scope="global") == pytest.approx(exceeded + 100)
    assert _fired_in_reservation_order(fired, client, "rl:anthropic:global")


def test_several_tokens_are_taken_at_once() -> None:
    limiter, _ = _limiter(_clock(), user_limit=5, sleep=lambda _: None)

    assert limiter.acquire(7, tokens=4) == pytest.approx(0.0)
    assert limiter.acquire(7, tokens=4) == pytest.approx(36.0)


@pytest.mark.parametrize("tokens", [0, -1])
def test_tokens_must_be_positive(tokens) -> None:
    limiter, client = _limiter(_clock())

    with pytest.raises(ValueError, match="at least 1"):
        limiter.acquire(7, tokens=tokens)

    assert client.reservations == []


def test_bucket_expires_when_it_would_be_full_again() -> None:
    limiter, client = _limiter(_clock())

    limiter.acquire(7)

    assert client.expiry == {"rl:anthropic:user:7": 6000, "rl:anthropic:global": 600}


def test_every_wait_is_observed_in_the_histogram() -> None:
    limiter, _ = _limiter(_clock())
    count = _sample("rate_limit_wait_seconds_count")
    total = _sample("rate_limit_wait_seconds_sum")

    for _ in range(11):
        limiter.acquire(7)

    assert _sample("rate_limit_wait_seconds_count") == pytest.approx(count + 11)
    assert _sample("rate_limit_wait_seconds_sum") == pytest.approx(total + 6.0)


def test_redis_down_blocks_unmetered_provider_calls_and_counts_the_error() -> None:
    clock = _clock()
    limiter, _ = _limiter(clock, failure=redis.ConnectionError("down"))
    errors = _sample("rate_limit_error_total")

    with pytest.raises(redis.ConnectionError):
        limiter.acquire(7)
    assert clock.now == pytest.approx(0.0)
    assert _sample("rate_limit_error_total") == pytest.approx(errors + 1)


def test_get_limiter_reads_settings_with_short_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://limiter-host:6380/3")
    monkeypatch.setenv("ANTHROPIC_RATE_USER", "5")
    monkeypatch.setenv("ANTHROPIC_RATE_GLOBAL", "50")
    monkeypatch.setenv("ANTHROPIC_WINDOW", "30")
    get_settings.cache_clear()
    anthropic_limiter.get_limiter.cache_clear()
    try:
        limiter = anthropic_limiter.get_limiter()
    finally:
        get_settings.cache_clear()
        anthropic_limiter.get_limiter.cache_clear()

    options = limiter.client.connection_pool.connection_kwargs
    assert (options["host"], options["port"], options["db"]) == ("limiter-host", 6380, 3)
    assert options["socket_timeout"] == anthropic_limiter.TIMEOUT_SECONDS
    assert options["socket_connect_timeout"] == anthropic_limiter.TIMEOUT_SECONDS
    assert (limiter.user_limit, limiter.global_limit, limiter.window) == (5, 50, 30)
    assert limiter.sleep is time.sleep
