from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from bancaemdia.domain.coleta_provenance import MAX_BATCH_BYTES


class CollectionBodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.bounded = RequestBodyLimitMiddleware(app, max_body_size=MAX_BATCH_BYTES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        target = self.bounded if scope.get("path", "").endswith("/coleta/batches") else self.app
        await target(scope, receive, send)
