from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bancaemdia.auth.jwt import (
    InvalidTokenError,
    KeysUnavailableError,
    get_jwks_cache,
    verify_token,
)

# A extensão autentica o /coleta com o próprio token e não manda Bearer; /health e /metrics são
# lidos pela infraestrutura, sem usuário. O aviso de fim de upload vem do trabalhador, que tem o
# segredo do webhook e nenhum token de pessoa.
PUBLIC_PATHS = frozenset({
    "/health",
    "/metrics",
    "/coleta",
    "/api/v1/coleta",
    "/webhook/upload-complete",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
})


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


class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if route_path(request) in PUBLIC_PATHS:
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
