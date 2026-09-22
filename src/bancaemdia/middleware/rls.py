from collections.abc import Awaitable, Callable

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, SessionTransaction
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from bancaemdia.core.context import current_user_id


class RLSMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        user_id = getattr(request.state, "usuario_id", None)
        token = current_user_id.set(str(user_id)) if user_id else None

        try:
            return await call_next(request)
        finally:
            if token:
                current_user_id.reset(token)


def apply_current_user(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    user_id = current_user_id.get()
    if user_id:
        # set_config(..., true) vale só até o fim da transação: depois de um commit, a transação
        # seguinte do mesmo pedido não veria nenhuma linha pela RLS.
        connection.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": user_id}
        )


event.listen(Session, "after_begin", apply_current_user)
