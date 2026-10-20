import re
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from bancaemdia.auth.jwt import (
    InvalidTokenError,
    KeysUnavailableError,
    get_jwks_cache,
    verify_token,
)

# A extensão autentica o /coleta com o próprio token e não manda Bearer; /health, /ready e /metrics
# são lidos pela infraestrutura, sem usuário. O aviso de fim de upload vem do trabalhador, que tem
# o segredo do webhook e nenhum token de pessoa.
PUBLIC_PATHS = frozenset({
    "/auth/start",
    "/auth/callback",
    "/auth/session",
    "/auth/refresh",
    "/auth/logout",
    "/auth/jwks",
    "/api/v1/billing/webhook",
    "/health",
    "/ready",
    "/metrics",
    "/coleta",
    "/api/v1/coleta",
    "/api/v1/coleta/pairing-exchange",
    "/api/v1/coleta/status",
    "/api/v1/coleta/sessions",
    "/api/v1/coleta/batches",
    "/api/v1/coleta/contract",
    "/api/v1/coleta/contract/schema",
    "/api/v1/coleta/catalogo",
    "/webhook/upload-complete",
    "/api/v1/integrations/telegram/webhook",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
})

CATALOG_METHODS = {
    "/api/v1/coleta/catalogo": "GET",
    "/api/v1/admin/casas": "GET",
    "/api/v1/admin/casas/export": "GET",
    "/api/v1/catalogo/candidatos": "POST",
}


def route_path(request: Request) -> str:
    # O mesmo caminho que o roteador compara (`get_route_path` do Starlette): `request.url.path` é
    # remontado, e um `%3F` decodificado cortava ali o caminho de uma rota protegida (medido).
    path: str = request.scope["path"]
    root_path: str = request.scope.get("root_path", "")
    if root_path and path.startswith(root_path + "/"):
        return path[len(root_path) :]
    return path


def unauthorized(detail: str, challenge: str) -> JSONResponse:
    return JSONResponse(
        status_code=401, content={"detail": detail}, headers={"WWW-Authenticate": challenge}
    )


def public_path(path: str) -> bool:
    return (
        path in PUBLIC_PATHS
        or re.fullmatch(r"/api/v1/coleta/(sessions|jobs)/[0-9a-fA-F-]{36}", path) is not None
    )


class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            path = route_path(request)
            allowed = CATALOG_METHODS.get(path)
            if public_path(path) and (allowed is None or request.method == allowed):
                # These paths already authenticate at their own boundary, or are
                # public infrastructure. Avoid an extra task/response stream for
                # the same existing bypass; disallowed methods still reach dispatch.
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        allowed = CATALOG_METHODS.get(route_path(request))
        if allowed is not None and request.method != allowed:
            return JSONResponse(
                status_code=405,
                content={"detail": "Method not allowed"},
                headers={"Allow": allowed},
            )
        if public_path(route_path(request)):
            return await call_next(request)

        from bancaemdia.api.identity import failure
        from bancaemdia.auth.identity_config import identity_settings
        from bancaemdia.auth.identity_service import identity_service
        from bancaemdia.auth.oidc import IdentityError

        settings = identity_settings()
        if settings.AUTH_ENABLED:
            try:
                service = identity_service()
                scheme, _, token = (request.headers.get("Authorization") or "").partition(" ")
                if scheme.lower() == "bearer" and token.strip():
                    request.state.usuario_id = await service.authenticate_bearer(token.strip())
                elif request.headers.get("Authorization"):
                    raise IdentityError("not_authenticated")
                else:
                    request.state.usuario_id = await service.authenticate(
                        request.cookies.get(settings.session_cookie, "")
                    )
            except IdentityError as error:
                return failure(error, legacy=True)
            return await call_next(request)

        scheme, _, token = (request.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return unauthorized("Not authenticated", "Bearer")
        try:
            request.state.usuario_id = await verify_token(token.strip(), get_jwks_cache())
        except InvalidTokenError:
            return unauthorized("Invalid token", 'Bearer error="invalid_token"')
        except KeysUnavailableError:
            # Um 401 faria o cliente jogar fora um token bom enquanto o servidor de chaves está fora.
            return JSONResponse(
                status_code=503, content={"detail": "Authentication is temporarily unavailable"}
            )
        return await call_next(request)
