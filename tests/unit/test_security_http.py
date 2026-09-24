from fastapi import FastAPI
from fastapi.testclient import TestClient

from bancaemdia.security.http import EndpointBodyLimitMiddleware, SecurityHeadersMiddleware


def _client() -> TestClient:
    app = FastAPI()

    @app.post("/{path:path}")
    async def echo(path: str) -> dict[str, str]:
        return {"path": path}

    app.add_middleware(EndpointBodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_security_headers_are_on_success_and_rejection() -> None:
    client = _client()
    for response in (
        client.post("/small", content=b"ok"),
        client.post("/small", content=b"x" * 1_048_577),
    ):
        assert response.headers["content-security-policy"] == (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
        )
        assert response.headers["strict-transport-security"] == (
            "max-age=31536000; includeSubDomains; preload"
        )
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_body_limit_is_specific_to_endpoint() -> None:
    client = _client()
    body = b"x" * 1_048_577
    assert client.post("/api/v1/apostas", content=body).status_code == 413
    assert client.post("/api/v1/upload", content=body).status_code == 200
    assert client.post("/api/v1/apostas/importar-planilha", content=body).status_code == 200
    assert client.post("/api/v1/upload/", content=body).status_code == 200
    assert client.post("/coleta/", content=body).status_code == 200
