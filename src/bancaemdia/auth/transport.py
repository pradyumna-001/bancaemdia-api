"""Exact CORS, cookie CSRF and strict private response headers."""

import secrets

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


class IdentityTransportMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = identity_settings()
        request = Request(scope)
        path = request_path(request)
        if not settings.AUTH_ENABLED and not path.startswith("/auth/"):
            # Disabled identity has no transport policy on these routes. Do not
            # create a task group or buffer a streaming collection response.
            await self.app(scope, receive, send)
            return
        origin = request.headers.get("origin")
        origins = {
            settings.AUTH_FRONTEND_ORIGIN.rstrip("/"),
            settings.AUTH_PUBLIC_URL.rstrip("/"),
        } - {""}
        legacy = path.startswith("/api/")
        response: Response | None = None
        if settings.AUTH_ENABLED and request.method == "OPTIONS" and origin is not None:
            requested = {
                h.strip().lower()
                for h in request.headers.get("access-control-request-headers", "").split(",")
                if h.strip()
            }
            if origin not in origins or not requested <= ALLOWED_HEADERS:
                response = failure(IdentityError("origin_not_allowed", 403), legacy=legacy)
            else:
                response = Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Methods": "GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS",
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
                    or not secrets.compare_digest(
                        proof.encode(), identity_service().keys.csrf(cookie).encode()
                    )
                ):
                    response = failure(IdentityError("csrf_failed", 403), legacy=legacy)

        async def send_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if settings.AUTH_ENABLED and origin in origins:
                    headers["Access-Control-Allow-Origin"] = str(origin)
                    headers["Access-Control-Allow-Credentials"] = "true"
                    headers["Access-Control-Expose-Headers"] = (
                        "X-Auth-Error, X-Request-ID, Retry-After"
                    )
                    headers["Vary"] = ", ".join(filter(None, [headers.get("Vary"), "Origin"]))
                if path.startswith("/auth/") or (
                    settings.AUTH_ENABLED and path.startswith("/api/")
                ):
                    headers["Cache-Control"] = "private, no-store"
                    headers["Pragma"] = "no-cache"
                    headers["Referrer-Policy"] = "no-referrer"
                    headers["X-Content-Type-Options"] = "nosniff"
                    headers["Content-Security-Policy"] = (
                        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                    )
            await send(message)

        if response is not None:
            await response(scope, receive, send_response)
        else:
            await self.app(scope, receive, send_response)
