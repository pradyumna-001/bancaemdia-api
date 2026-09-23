from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bancaemdia.config import get_settings
from bancaemdia.core.body_limits import (
    COLETA_MAX_BYTES,
    JSON_MAX_BYTES,
    PLANILHA_MAX_BYTES,
    UPLOAD_FORM_MARGIN,
)
from bancaemdia.integrations.telegram.webhook import MAX_WEBHOOK_BYTES, WEBHOOK_PATH

SECURITY_HEADERS = (
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'",
    ),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains; preload"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in SECURITY_HEADERS}
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() not in names
                    ]
                    + list(SECURITY_HEADERS),
                }
            await send(message)

        await self.app(scope, receive, send_headers)


class EndpointBodyLimitMiddleware:
    """Apply the same streamed limit as Starlette, with a smaller default."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == WEBHOOK_PATH:
            limit = MAX_WEBHOOK_BYTES
        elif path == "/api/v1/upload":
            limit = get_settings().UPLOAD_MAX_BYTES + UPLOAD_FORM_MARGIN
        elif path == "/api/v1/apostas/importar-planilha":
            limit = PLANILHA_MAX_BYTES + UPLOAD_FORM_MARGIN
        elif path in {"/coleta", "/api/v1/coleta"}:
            limit = COLETA_MAX_BYTES
        else:
            limit = JSON_MAX_BYTES
        middleware = RequestBodyLimitMiddleware(self.app, max_body_size=limit)
        await middleware(scope, receive, send)
