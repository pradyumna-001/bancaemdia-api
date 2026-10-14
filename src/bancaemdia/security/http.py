from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bancaemdia.config import get_settings
from bancaemdia.core.body_limits import (
    COLETA_MAX_BYTES,
    JSON_MAX_BYTES,
    PLANILHA_MAX_BYTES,
    UPLOAD_FORM_MARGIN,
)
from bancaemdia.domain.foto_aposta import photo_path
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
                private_path = scope.get("path", "").removeprefix(scope.get("root_path", ""))
                if (
                    private_path.startswith("/api/v1/revisao")
                    or photo_path(private_path)
                    or private_path.startswith((
                        "/api/v1/prints/",
                        "/api/v1/planilhas/",
                        "/api/v1/resultados/",
                    ))
                    or (
                        private_path.startswith("/api/v1/revisao/")
                        and private_path.endswith("/foto")
                    )
                ):
                    from starlette.datastructures import MutableHeaders

                    headers = MutableHeaders(scope=message)
                    headers["Cache-Control"] = "private, no-store"
                    headers["Pragma"] = "no-cache"
                    headers["Referrer-Policy"] = "no-referrer"
                    headers["Content-Security-Policy"] = (
                        "default-src 'none'; frame-ancestors 'none'"
                    )
                    vary = [
                        value.strip()
                        for value in headers.get("Vary", "").split(",")
                        if value.strip()
                    ]
                    for value in ("Authorization", "Cookie"):
                        if value.lower() not in {existing.lower() for existing in vary}:
                            vary.append(value)
                    headers["Vary"] = ", ".join(vary)
                    if message["status"] == 503 and "Retry-After" not in headers:
                        headers["Retry-After"] = "5"
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

        path = (scope.get("path", "") or "").rstrip("/") or "/"
        if path == WEBHOOK_PATH:
            limit = MAX_WEBHOOK_BYTES
        elif path == "/api/v1/upload":
            limit = get_settings().UPLOAD_MAX_BYTES + UPLOAD_FORM_MARGIN
        elif path.startswith("/api/v1/prints/intencoes/") and not path.endswith("/retry"):
            from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES

            limit = MAX_PHOTO_BYTES + UPLOAD_FORM_MARGIN
        elif path == "/api/v1/apostas/importar-planilha" or (
            path.startswith("/api/v1/planilhas/fontes/") and "/previas/" in path
        ):
            limit = PLANILHA_MAX_BYTES + UPLOAD_FORM_MARGIN
        elif path in {"/coleta", "/api/v1/coleta"}:
            limit = COLETA_MAX_BYTES
        else:
            limit = JSON_MAX_BYTES
        middleware = RequestBodyLimitMiddleware(self.app, max_body_size=limit)
        await middleware(scope, receive, send)
