from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from bancaemdia.core.context import current_user_id


class RLSMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        user_id = getattr(request.state, "user_id", None)
        token = current_user_id.set(user_id) if user_id else None

        try:
            return await call_next(request)
        finally:
            if token:
                current_user_id.reset(token)
