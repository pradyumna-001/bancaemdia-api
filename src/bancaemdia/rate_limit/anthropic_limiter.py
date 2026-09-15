import time
from collections.abc import Callable
from functools import lru_cache

import redis

from bancaemdia.config import get_settings
from bancaemdia.observability.metrics import rate_limit_errors, rate_limit_exceeded, rate_limit_wait

USER_KEY = "rl:anthropic:user:{user_id}"
GLOBAL_KEY = "rl:anthropic:global"
TIMEOUT_SECONDS = 2.0

TOKEN_BUCKET = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local tokens = tonumber(ARGV[3])
local clock = redis.call("TIME")
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local bucket = redis.call("HMGET", KEYS[1], "tokens", "updated_at")
local available = limit
if bucket[1] and bucket[2] then
    local refill = math.max(0, now - tonumber(bucket[2])) * limit / window
    available = math.min(limit, tonumber(bucket[1]) + refill)
end
available = available - tokens
redis.call("HSET", KEYS[1], "tokens", available, "updated_at", now)
redis.call("PEXPIRE", KEYS[1], math.ceil((limit - available) * window * 1000 / limit))
return math.max(0, math.floor(-available * window * 1000 / limit))
"""


class AnthropicLimiter:
    def __init__(
        self,
        client: redis.Redis,
        user_limit: int,
        global_limit: int,
        window: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.user_limit = user_limit
        self.global_limit = global_limit
        self.window = window
        self.sleep = sleep
        self.script = client.register_script(TOKEN_BUCKET)

    def acquire(self, user_id: int, tokens: int = 1) -> float:
        if tokens < 1:
            raise ValueError(f"tokens must be at least 1, got {tokens}")
        waited = 0.0
        for scope, key, limit in (
            ("user", USER_KEY.format(user_id=user_id), self.user_limit),
            ("global", GLOBAL_KEY, self.global_limit),
        ):
            wait = self._reserve(key, limit, tokens)
            if wait > 0:
                rate_limit_exceeded.labels(scope=scope).inc()
                self.sleep(wait)
                waited += wait
        rate_limit_wait.observe(waited)
        return waited

    def _reserve(self, key: str, limit: int, tokens: int) -> float:
        try:
            wait_ms = self.script(keys=[key], args=[limit, self.window, tokens])
        except redis.RedisError:
            rate_limit_errors.inc()
            return 0.0
        return int(wait_ms) / 1000


@lru_cache
def get_limiter() -> AnthropicLimiter:
    settings = get_settings()
    client = redis.Redis.from_url(
        settings.REDIS_URL,
        socket_timeout=TIMEOUT_SECONDS,
        socket_connect_timeout=TIMEOUT_SECONDS,
    )
    return AnthropicLimiter(
        client,
        settings.ANTHROPIC_RATE_USER,
        settings.ANTHROPIC_RATE_GLOBAL,
        settings.ANTHROPIC_WINDOW,
    )
