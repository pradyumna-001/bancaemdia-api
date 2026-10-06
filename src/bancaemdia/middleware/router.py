import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from bancaemdia.auth.middleware import route_path
from bancaemdia.core.context import use_primary

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_WINDOW_SECONDS = 5.0
READ_REPLICA_HEADER = "X-Read-Replica"
PAINEL_PATH = "/api/v1/painel"
# Os valores que o FastAPI lê como verdadeiro num `fresh: bool` (medido): comparar só "true" mandaria
# `fresh=1` à cópia enquanto a rota acha que leu o dado fresco.
TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})


# Memória deste processo, como o limite do slowapi (ADR-008: Redis só com mais de um worker): com mais
# de uma cópia da API atrás do balanceador (#41), a leitura seguinte pode cair na outra cópia.
class RecentWrites:
    def __init__(
        self, ttl: float = WRITE_WINDOW_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl = ttl
        self.clock = clock
        self.expires: dict[int, float] = {}

    def mark(self, usuario_id: int) -> None:
        now = self.clock()
        self.expires = {uid: expires for uid, expires in self.expires.items() if expires > now}
        self.expires[usuario_id] = now + self.ttl

    def recent(self, usuario_id: int) -> bool:
        expires = self.expires.get(usuario_id)
        if expires is None:
            return False
        if self.clock() < expires:
            return True
        del self.expires[usuario_id]
        return False


@lru_cache
def get_recent_writes() -> RecentWrites:
    return RecentWrites()


def needs_primary(request: Request, usuario_id: int | None, writes: RecentWrites) -> bool:
    if request.method not in SAFE_METHODS:
        return True
    if (request.headers.get(READ_REPLICA_HEADER) or "").strip().lower() == "false":
        return True
    fresh = (request.query_params.get("fresh") or "").strip().lower()
    if route_path(request) == PAINEL_PATH and fresh in TRUE_VALUES:
        return True
    # A marca é da pessoa, não da aposta: o roteador não sabe a chave de uma aposta criada, e depois de
    # gravar a pessoa volta à lista e ao saldo, não só ao detalhe (ADR-005, ADR-009).
    return usuario_id is not None and writes.recent(usuario_id)


class RouterMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        usuario_id = getattr(request.state, "usuario_id", None)
        writes = get_recent_writes()
        token = use_primary.set(needs_primary(request, usuario_id, writes))
        try:
            return await call_next(request)
        finally:
            use_primary.reset(token)
            if request.method not in SAFE_METHODS and usuario_id is not None:
                writes.mark(usuario_id)
