"""Redis clients for local single-node and production cluster-mode caches."""

from typing import cast

import redis
import redis.asyncio as async_redis


def sync_client(url: str, *, cluster: bool, timeout: float) -> redis.Redis:
    constructor = redis.cluster.RedisCluster if cluster else redis.Redis
    return cast(
        redis.Redis,
        constructor.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout),
    )


def async_client(url: str, *, cluster: bool, timeout: float) -> async_redis.Redis:
    constructor = async_redis.cluster.RedisCluster if cluster else async_redis.Redis
    return cast(
        async_redis.Redis,
        constructor.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout),
    )
