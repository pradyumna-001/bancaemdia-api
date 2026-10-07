"""Reject insecure credential transport and redirects without reflecting inputs."""

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bancaemdia.auth.middleware import route_path


class CollectionCredentialMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = route_path(request).rstrip("/")
        catalog = path == "/api/v1/coleta/catalogo" and request.method == "GET"
        private = (
            path in {"/coleta", "/api/v1/coleta"}
            or path.startswith("/api/v1/coleta/")
            or "x-coleta-token" in request.headers
        )
        if not private:
            return await call_next(request)
        # Only the ASGI scheme is authoritative. Forwarded headers must be handled
        # by the deployment's explicitly trusted proxy, never by client input here.
        if request.scope.get("scheme") != "https":
            return JSONResponse(
                {"detail": "HTTPS required"}, status_code=400, headers={"Cache-Control": "no-store"}
            )
        if request.query_params and (
            not catalog
            or set(request.query_params) - {"client_version", "environment", "known_version"}
            or len(request.query_params.multi_items()) != len(request.query_params)
            or any(len(value) > 40 for value in request.query_params.values())
        ):
            return JSONResponse(
                {"detail": "Query parameters are not accepted"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        if catalog and response.status_code in {200, 304} and response.headers.get("ETag"):
            return response
        if 300 <= response.status_code < 400:
            response = JSONResponse(
                {"detail": "Credential redirects are forbidden"}, status_code=400
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response
