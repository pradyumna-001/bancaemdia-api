from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, SessionTransaction
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from bancaemdia.core.context import current_user_id


class RLSMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        user_id = getattr(request.state, "usuario_id", None)
        token = current_user_id.set(str(user_id)) if user_id else None

        try:
            await self.app(scope, receive, send)
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
