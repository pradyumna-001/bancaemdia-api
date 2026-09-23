from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from threading import Lock
from typing import Any, cast
from urllib.parse import urlsplit

import structlog
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bancaemdia.config import get_settings
from bancaemdia.observability.metrics import rate_limiter_fallback_total

COLETA_TOKEN_HEADER = "X-Coleta-Token"
RATE_LIMIT_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "Retry-After",
)


def user_key(request: Request) -> str:
    """Use the authenticated tenant without ever putting a bearer token in storage."""
    usuario_id = getattr(request.state, "usuario_id", None)
    if usuario_id is not None:
        return f"user:{usuario_id}"

    return f"anonymous:{_opaque_digest('client', client_address(request))}"


def _opaque_digest(purpose: str, value: str) -> str:
    secret = get_settings().COLETA_TOKEN_SECRET.encode()
    return hmac.new(secret, f"rate-limit:{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


@lru_cache(maxsize=32)
def _trusted_proxy_networks(raw: str) -> tuple[IPv4Network | IPv6Network, ...]:
    return tuple(ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip())


def client_address(request: Request) -> str:
    """Resolve a forwarded client only when the immediate peer is explicitly trusted."""
    host = request.client.host if request.client is not None else "unknown"
    try:
        peer = ip_address(host)
    except ValueError:
        return host

    trusted = _trusted_proxy_networks(get_settings().RATE_LIMIT_TRUSTED_PROXY_CIDRS)
    if not any(peer in network for network in trusted):
        return str(peer)

    forwarded = request.headers.get("X-Forwarded-For", "")
    for raw_candidate in reversed(forwarded.split(",")):
        try:
            candidate = ip_address(raw_candidate.strip())
        except ValueError:
            continue
        if not any(candidate in network for network in trusted):
            return str(candidate)
    return str(peer)


def coleta_token_digest(request: Request) -> str:
    """Return the non-reversible identifier historically exposed by the coleta module."""
    token = request.headers.get(COLETA_TOKEN_HEADER, "")
    return _opaque_digest("coleta-token", token)


def coleta_token_key(request: Request) -> str:
    """Keep extension tokens out of Redis keys and memory diagnostics."""
    return f"extension:{coleta_token_digest(request)}"


def _api_limit() -> str:
    return get_settings().API_RATE_LIMIT


def _auth_limit() -> str:
    return get_settings().AUTH_RATE_LIMIT


def _upload_limit() -> str:
    return get_settings().UPLOAD_RATE_LIMIT


def _coleta_limit() -> str:
    return get_settings().COLETA_RATE_LIMIT


def _coleta_ip_limit() -> str:
    return get_settings().COLETA_IP_RATE_LIMIT


class ObservableLimiter(Limiter):
    """Report each transition from shared storage to the process-local fallback."""

    def __init__(
        self,
        *,
        fallback_policy: str,
        fallback_backend: str,
        **kwargs: Any,
    ) -> None:
        self._fallback_policy = fallback_policy
        self._fallback_backend = fallback_backend
        self._storage_dead_value = False
        self._fallback_transition_lock = Lock()
        super().__init__(**kwargs)

    @property
    def _storage_dead(self) -> bool:
        return self._storage_dead_value

    @_storage_dead.setter
    def _storage_dead(self, value: bool) -> None:
        # SlowAPI writes this flag from both its request-check and header-injection paths. The
        # lock keeps simultaneous failures from reporting the same transition more than once.
        with self._fallback_transition_lock:
            was_dead = self._storage_dead_value
            self._storage_dead_value = value
            entered_fallback = value and not was_dead
        if entered_fallback:
            rate_limiter_fallback_total.labels(
                policy=self._fallback_policy,
                storage_backend=self._fallback_backend,
            ).inc()
            structlog.get_logger(__name__).warning(
                "rate_limiter_fallback",
                policy=self._fallback_policy,
                storage_backend=self._fallback_backend,
                fallback_backend="memory",
            )


def _limiter(
    limit: Callable[[], str], key_func: Callable[[Request], str], namespace: str
) -> Limiter:
    settings = get_settings()
    storage_backend = urlsplit(settings.RATE_LIMIT_STORAGE).scheme or "memory"
    storage_options = cast(
        dict[str, object],
        {
            "socket_connect_timeout": settings.RATE_LIMIT_STORAGE_TIMEOUT_SECONDS,
            "socket_timeout": settings.RATE_LIMIT_STORAGE_TIMEOUT_SECONDS,
        }
        if settings.RATE_LIMIT_STORAGE.lower().startswith(("redis://", "rediss://"))
        else {},
    )
    if settings.RATE_LIMIT_STORAGE.lower().startswith("redis+cluster://"):
        storage_options = {
            "socket_connect_timeout": settings.RATE_LIMIT_STORAGE_TIMEOUT_SECONDS,
            "socket_timeout": settings.RATE_LIMIT_STORAGE_TIMEOUT_SECONDS,
            "ssl": True,
            "ssl_cert_reqs": "required",
        }
    return ObservableLimiter(
        fallback_policy=namespace,
        fallback_backend=storage_backend,
        key_func=key_func,
        default_limits=[limit],
        headers_enabled=True,
        storage_uri=settings.RATE_LIMIT_STORAGE,
        storage_options=storage_options,
        # A local fallback preserves protection if Redis is briefly unavailable. In a
        # multi-worker deployment it is intentionally only a degraded fallback, not the source
        # of truth; RATE_LIMIT_STORAGE must point to Redis there.
        in_memory_fallback=[limit],
        in_memory_fallback_enabled=True,
        retry_after="delta-seconds",
        key_prefix=f"bancaemdia:{namespace}",
        key_style="endpoint",
        swallow_errors=True,
    )


api_limiter = _limiter(_api_limit, user_key, "api")
auth_limiter = _limiter(_auth_limit, user_key, "auth")
upload_limiter = _limiter(_upload_limit, user_key, "upload")
coleta_limiter = _limiter(_coleta_limit, coleta_token_key, "coleta")
coleta_ip_limiter = _limiter(_coleta_ip_limit, user_key, "coleta-ip")


# SlowAPI includes the endpoint name in a storage key. Stable markers deliberately make every
# path in one policy share a bucket, including the two supported coleta aliases.
def _api_scope() -> None:
    return None


def _auth_scope() -> None:
    return None


def _upload_scope() -> None:
    return None


def _coleta_scope() -> None:
    return None


def _coleta_ip_scope() -> None:
    return None


@dataclass(frozen=True)
class RateLimitPolicy:
    limiter: Limiter
    scope: Callable[[], None]


API_POLICY = RateLimitPolicy(api_limiter, _api_scope)
AUTH_POLICY = RateLimitPolicy(auth_limiter, _auth_scope)
UPLOAD_POLICY = RateLimitPolicy(upload_limiter, _upload_scope)
COLETA_POLICY = RateLimitPolicy(coleta_limiter, _coleta_scope)
COLETA_IP_POLICY = RateLimitPolicy(coleta_ip_limiter, _coleta_ip_scope)
ALL_LIMITERS = (api_limiter, auth_limiter, upload_limiter, coleta_limiter, coleta_ip_limiter)


def request_path(request: Request) -> str:
    path: str = request.scope["path"]
    root_path: str = request.scope.get("root_path", "")
    if root_path and path.startswith(root_path + "/"):
        return path[len(root_path) :]
    return path


def policy_for(request: Request) -> RateLimitPolicy | None:
    path = request_path(request)
    if path in {"/coleta", "/api/v1/coleta"}:
        return COLETA_POLICY if request.method == "POST" else None
    if path == "/api/v1/upload" and request.method == "POST":
        return UPLOAD_POLICY
    if path == "/api/v1" or path.startswith("/api/v1/"):
        return API_POLICY
    if path == "/auth" or path.startswith("/auth/"):
        return AUTH_POLICY
    return None


def _limited_response(
    request: Request, policy: RateLimitPolicy, exc: RateLimitExceeded
) -> Response:
    retry_after = 1
    if exc.limit is not None:
        retry_after = int(exc.limit.limit.get_expiry())
    current_limit = getattr(request.state, "view_rate_limit", None)
    injected: Response | None = None
    if current_limit is not None:
        injected = JSONResponse({"error": "rate_limited", "retry_after": retry_after}, 429)
        policy.limiter._inject_headers(injected, current_limit)
        try:
            retry_after = max(0, int(injected.headers["Retry-After"]))
        except (KeyError, ValueError):
            pass

    response = JSONResponse(
        status_code=429,
        content={"error": "rate_limited", "retry_after": retry_after},
    )
    if injected is not None:
        for header in RATE_LIMIT_HEADERS:
            if header in injected.headers:
                response.headers[header] = injected.headers[header]
    else:
        response.headers["Retry-After"] = str(retry_after)
    return response


async def _apply_policies(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    policies: tuple[RateLimitPolicy, ...],
) -> Response:
    # SlowAPI does not export a public alias for its `(RateLimitItem, identifiers)` tuple.
    active: tuple[RateLimitPolicy, Any] | None = None
    for policy in policies:
        if not policy.limiter.enabled:
            continue
        try:
            # SlowAPI's stock middleware is tied to app.state.limiter and therefore cannot host
            # independent token/user buckets. Its own check/header routines are used here with
            # the selected limiter; uv.lock pins the exercised SlowAPI version.
            policy.limiter._check_request_limit(request, policy.scope, in_middleware=True)
        except RateLimitExceeded as exc:
            return _limited_response(request, policy, exc)
        current_limit = getattr(request.state, "view_rate_limit", None)
        if current_limit is not None:
            active = (policy, current_limit)

    response = await call_next(request)
    if active is not None:
        policy, current_limit = active
        policy.limiter._inject_headers(response, current_limit)
    return response


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Throttle pre-authentication endpoints before JWT validation can short-circuit them."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        policy = policy_for(request)
        if policy is not AUTH_POLICY:
            return await call_next(request)
        return await _apply_policies(request, call_next, (policy,))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Throttle authenticated APIs and extension collection after authentication."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        policy = policy_for(request)
        if policy is None or policy is AUTH_POLICY:
            return await call_next(request)
        policies = (COLETA_IP_POLICY, policy) if policy is COLETA_POLICY else (policy,)
        return await _apply_policies(request, call_next, policies)


def reset_rate_limiters() -> None:
    """Reset buckets in isolated tests; production code never calls this helper."""
    for limiter in ALL_LIMITERS:
        limiter.reset()
        # SlowAPI's public reset only clears the primary storage. A test that exercises a Redis
        # outage can otherwise leak both fallback counters and the dead-backend flag into the
        # next case. These internals are covered here because uv.lock pins the SlowAPI version.
        fallback_storage = getattr(limiter, "_fallback_storage", None)
        if fallback_storage is not None:
            fallback_storage.reset()
        limiter._storage_dead = False
