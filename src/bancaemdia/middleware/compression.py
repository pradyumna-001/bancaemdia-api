from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Allowlist explícita, nunca prefixo: rota nova só é comprimida depois de conferida contra BREACH
# (docs/contracts/compressao-leituras.md). Nenhuma destas respostas leva token, cookie, CSRF ou
# código temporário, e os parâmetros são tipados: nada digitado pelo cliente volta no corpo.
COMPRESSIBLE_READS = frozenset({
    "/api/v1/apostas",
    "/api/v1/painel",
    "/api/v1/painel/analises",
    "/api/v1/painel/filtrado",
    "/api/v1/painel/filtrado/diario",
    "/api/v1/painel/filtrado/grupos",
    "/api/v1/painel/filtrado/metricas",
    "/api/v1/resultados/fila",
})
MINIMUM_BYTES = 1024
# Nível 6, o mesmo do nginx do frontend: o 9 custa CPU na carga sem ganho que mude o LCP.
COMPRESS_LEVEL = 6


def compressible(scope: Scope) -> bool:
    # Mutations ficam de fora por cautela, mesmo nas rotas da allowlist.
    if scope["type"] != "http" or scope["method"] != "GET":
        return False
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path + "/"):
        path = path[len(root_path) :]
    return path in COMPRESSIBLE_READS


class ReadCompressionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=MINIMUM_BYTES, compresslevel=COMPRESS_LEVEL)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not compressible(scope):
            await self.app(scope, receive, send)
            return

        async def send_vary(message: Message) -> None:
            # O Starlette só declara Vary quando o corpo passa do mínimo; a representação desta rota
            # depende de Accept-Encoding em qualquer tamanho.
            if message["type"] == "http.response.start":
                vary = Headers(raw=message["headers"]).get("vary", "")
                if "accept-encoding" not in {item.strip().lower() for item in vary.split(",")}:
                    message = {**message, "headers": list(message["headers"])}
                    MutableHeaders(raw=message["headers"]).add_vary_header("Accept-Encoding")
            await send(message)

        await self.gzip(scope, receive, send_vary)
