from contextlib import suppress
from functools import lru_cache

import anthropic
import pybreaker
import redis
from prometheus_client import Gauge

from bancaemdia.config import get_settings

ANTHROPIC = "anthropic"
STATE_KEY = "anthropic:pybreaker:state"
FAIL_MAX = 5
RESET_TIMEOUT = 60
TIMEOUT_SECONDS = 2.0
STATE_CODES = {
    pybreaker.STATE_CLOSED: 0,
    pybreaker.STATE_OPEN: 1,
    pybreaker.STATE_HALF_OPEN: 2,
}
ANTHROPIC_EXCLUDE = (
    anthropic.APITimeoutError,
    anthropic.BadRequestError,
    anthropic.RequestTooLargeError,
    anthropic.UnprocessableEntityError,
)

circuit_breaker_state = Gauge(
    "circuit_breaker_state", "Circuit breaker state: 0=closed, 1=open, 2=half-open", ["breaker"]
)


class PrometheusListener(pybreaker.CircuitBreakerListener):
    def state_change(
        self,
        cb: pybreaker.CircuitBreaker,
        old_state: pybreaker.CircuitBreakerState | None,
        new_state: pybreaker.CircuitBreakerState,
    ) -> None:
        circuit_breaker_state.labels(breaker=cb.name).set(STATE_CODES[new_state.name])


class RedisStorage(pybreaker.CircuitRedisStorage):
    def _initialize_redis_state(self, state: str) -> None:
        with suppress(redis.RedisError):
            super()._initialize_redis_state(state)


def new_anthropic_breaker(
    storage: pybreaker.CircuitBreakerStorage | None = None,
) -> pybreaker.CircuitBreaker:
    breaker = pybreaker.CircuitBreaker(
        fail_max=FAIL_MAX,
        reset_timeout=RESET_TIMEOUT,
        exclude=ANTHROPIC_EXCLUDE,
        listeners=[PrometheusListener()],
        state_storage=storage,
        name=ANTHROPIC,
    )
    circuit_breaker_state.labels(breaker=ANTHROPIC).set(STATE_CODES[breaker.current_state])
    return breaker


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().REDIS_URL,
        socket_timeout=TIMEOUT_SECONDS,
        socket_connect_timeout=TIMEOUT_SECONDS,
    )


@lru_cache
def get_anthropic_breaker() -> pybreaker.CircuitBreaker:
    return new_anthropic_breaker(
        RedisStorage(pybreaker.STATE_CLOSED, get_redis(), namespace=ANTHROPIC)
    )


def breaker_states() -> dict[str, str]:
    try:
        stored = get_redis().get(STATE_KEY)
    except redis.RedisError:
        return {ANTHROPIC: "unknown"}
    state = stored.decode() if isinstance(stored, bytes) else str(stored or pybreaker.STATE_CLOSED)
    circuit_breaker_state.labels(breaker=ANTHROPIC).set(STATE_CODES[state])
    return {ANTHROPIC: state}
