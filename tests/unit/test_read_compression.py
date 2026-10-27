import gzip
import json

import pytest

from bancaemdia.middleware import compression
from bancaemdia.middleware.compression import COMPRESSIBLE_READS, ReadCompressionMiddleware

SECRET = "csrf-ou-codigo-que-nunca-pode-sair-comprimido"
# Corpo grande o bastante para passar do mínimo, com o mesmo Vary privado das rotas reais.
BODY = json.dumps(
    {
        "csrf_token": SECRET,
        "data": [{"chave": f"aposta-{i}", "lucro_centavos": i} for i in range(200)],
    },
    separators=(",", ":"),
).encode()


async def downstream(scope, receive, send):
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(scope["state"]["body"])).encode()),
            (b"vary", b"Authorization, Cookie"),
            (b"cache-control", b"private, no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": scope["state"]["body"]})


async def call(method, path, accept="gzip, deflate, br", body=BODY, root_path=""):
    messages = []
    headers = [(b"host", b"api.test")]
    if accept is not None:
        headers.append((b"accept-encoding", accept.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": root_path + path,
        "root_path": root_path,
        "headers": headers,
        "query_string": b"",
        "state": {"body": body},
    }

    async def receive():  # ruff: ignore[unused-async] - ASGI protocol
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # ruff: ignore[unused-async] - ASGI protocol
        messages.append(message)

    await ReadCompressionMiddleware(downstream)(scope, receive, send)
    start = messages[0]
    raw = b"".join(m.get("body", b"") for m in messages[1:])
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}, raw


@pytest.mark.parametrize("path", sorted(COMPRESSIBLE_READS))
async def test_allowlisted_read_is_gzip_and_decompresses_to_the_same_bytes(path):
    headers, raw = await call("GET", path)
    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(raw) == BODY
    assert int(headers["content-length"]) == len(raw) < len(BODY)
    assert headers["vary"] == "Authorization, Cookie, Accept-Encoding"
    assert headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("accept", [None, "", "identity", "br"])
async def test_without_gzip_in_accept_encoding_the_read_stays_plain(accept):
    headers, raw = await call("GET", "/api/v1/painel/analises", accept=accept)
    assert "content-encoding" not in headers
    assert raw == BODY
    assert headers["vary"] == "Authorization, Cookie, Accept-Encoding"


async def test_small_read_stays_plain_but_still_varies_by_encoding():
    small = b'{"data":[]}'
    headers, raw = await call("GET", "/api/v1/resultados/fila", body=small)
    assert "content-encoding" not in headers and raw == small
    assert headers["vary"] == "Authorization, Cookie, Accept-Encoding"


async def test_root_path_is_removed_before_matching_the_allowlist():
    headers, raw = await call("GET", "/api/v1/painel", root_path="/edge")
    assert headers["content-encoding"] == "gzip" and gzip.decompress(raw) == BODY


@pytest.mark.parametrize(
    "method,path",
    [
        # Sessão, refresh, logout e callback carregam csrf_token, cookie ou código do provedor.
        ("GET", "/auth/session"),
        ("POST", "/auth/refresh"),
        ("POST", "/auth/logout"),
        ("GET", "/auth/callback"),
        ("GET", "/auth/start"),
        # Código temporário de vínculo do Telegram e credenciais/pareamento da Coleta.
        ("POST", "/api/v1/telegram/link-codes"),
        ("GET", "/api/v1/telegram/link"),
        ("POST", "/api/v1/coleta/pairing-codes"),
        ("POST", "/api/v1/coleta/pairing-exchange"),
        ("POST", "/api/v1/coleta/installations/7/rotate"),
        ("GET", "/api/v1/coleta/installations"),
        ("POST", "/api/v1/coleta/sessions"),
        # Binários já comprimidos.
        ("GET", "/api/v1/painel/export"),
        ("GET", "/api/v1/painel/filtrado/export"),
        ("GET", "/api/v1/painel/filtrado/detalhado/export"),
        ("GET", "/api/v1/usuario/me/export"),
        ("GET", "/api/v1/revisao/7/foto"),
        # Mutations, inclusive nas próprias rotas da allowlist.
        *[
            (method, path)
            for path in sorted(COMPRESSIBLE_READS)
            for method in ("POST", "PUT", "PATCH", "DELETE")
        ],
        ("HEAD", "/api/v1/painel"),
        # Só o caminho exato: detalhe, barra final e caixa diferente não entram.
        ("GET", "/api/v1/apostas/aposta-1"),
        ("GET", "/api/v1/apostas/"),
        ("GET", "/API/v1/apostas"),
        ("GET", "/api/v1/resultados/apostas/aposta-1"),
    ],
)
async def test_route_outside_the_allowlist_is_never_compressed(method, path):
    headers, raw = await call(method, path)
    assert "content-encoding" not in headers
    assert raw == BODY and SECRET.encode() in raw
    assert headers["vary"] == "Authorization, Cookie"


async def test_response_already_encoded_by_the_route_passes_untouched():
    async def encoded(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json"), (b"content-encoding", b"br")],
        })
        await send({"type": "http.response.body", "body": BODY})

    messages = []

    async def send(message):  # ruff: ignore[unused-async] - ASGI protocol
        messages.append(message)

    async def receive():  # ruff: ignore[unused-async] - ASGI protocol
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/apostas",
        "headers": [(b"accept-encoding", b"gzip")],
        "query_string": b"",
    }
    await ReadCompressionMiddleware(encoded)(scope, receive, send)
    headers = dict(messages[0]["headers"])
    assert headers[b"content-encoding"] == b"br" and messages[1]["body"] == BODY


def test_allowlist_holds_only_exact_json_reads_without_exports_or_identity():
    assert COMPRESSIBLE_READS
    for path in COMPRESSIBLE_READS:
        assert path.startswith("/api/v1/") and "{" not in path and not path.endswith("/")
        assert "export" not in path and "foto" not in path
        assert not path.startswith(("/api/v1/telegram", "/api/v1/coleta", "/api/v1/billing"))
    assert compression.MINIMUM_BYTES == 1024
