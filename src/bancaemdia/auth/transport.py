"""Exact CORS, cookie CSRF and strict private response headers."""

import secrets
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from bancaemdia.api.identity import failure
from bancaemdia.auth.identity_config import identity_settings
from bancaemdia.auth.identity_service import identity_service
from bancaemdia.auth.oidc import IdentityError
from bancaemdia.middleware.rate_limit import request_path

SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
ALLOWED_HEADERS = frozenset({
    "authorization",
    "content-type",
    "x-csrf-token",
    "x-request-id",
    "x-read-replica",
})


class IdentityTransportMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = identity_settings()
        origin = request.headers.get("origin")
        origins = {
            settings.AUTH_FRONTEND_ORIGIN.rstrip("/"),
            settings.AUTH_PUBLIC_URL.rstrip("/"),
        } - {""}
        path = request_path(request)
        legacy = path.startswith("/api/")
        if settings.AUTH_ENABLED and request.method == "OPTIONS" and origin is not None:
            requested = {
                h.strip().lower()
                for h in request.headers.get("access-control-request-headers", "").split(",")
                if h.strip()
            }
            if origin not in origins or not requested <= ALLOWED_HEADERS:
                response: Response = failure(
                    IdentityError("origin_not_allowed", 403), legacy=legacy
                )
            else:
                response = Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Methods": "GET,HEAD,POST,PATCH,DELETE,OPTIONS",
                        "Access-Control-Allow-Headers": ",".join(sorted(ALLOWED_HEADERS)),
                        "Access-Control-Max-Age": "300",
                    },
                )
        else:
            cookie = request.cookies.get(settings.session_cookie, "")
            auth_operation = path in {"/auth/refresh", "/auth/logout"}
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            bearer = scheme.lower() == "bearer" and bool(token.strip())
            if (
                settings.AUTH_ENABLED
                and request.method not in SAFE
                and (auth_operation or (cookie and not bearer))
            ):
                proof = request.headers.get("x-csrf-token", "")
                if (
                    origin not in origins
                    or not cookie
                    or not secrets.compare_digest(proof, identity_service().keys.csrf(cookie))
                ):
                    response = failure(IdentityError("csrf_failed", 403), legacy=legacy)
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        if settings.AUTH_ENABLED and origin in origins:
            response.headers["Access-Control-Allow-Origin"] = str(origin)
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Expose-Headers"] = (
                "X-Auth-Error, X-Request-ID, Retry-After"
            )
            response.headers["Vary"] = ", ".join(
                filter(None, [response.headers.get("Vary"), "Origin"])
            )
        if path.startswith("/auth/") or (settings.AUTH_ENABLED and path.startswith("/api/")):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
        return response
