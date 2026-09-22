from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import schemathesis
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy
from schemathesis.config import HealthCheck
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bancaemdia.api.v1 import coleta
from bancaemdia.db.session import get_db
from bancaemdia.main import app
from bancaemdia.middleware import rate_limit as rate_limit_middleware

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests" / "contract" / "schemas" / "openapi.json"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})

type JsonObject = dict[str, Any]


class NoProductionLifespan:
    """Exercise the real ASGI stack without opening production dependencies in contract tests."""

    def __init__(self, application: ASGIApp) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.application(scope, receive, send)
            return

        message: Message = await receive()
        if message["type"] != "lifespan.startup":
            raise RuntimeError("expected ASGI lifespan startup")
        await send({"type": "lifespan.startup.complete"})
        message = await receive()
        if message["type"] != "lifespan.shutdown":
            raise RuntimeError("expected ASGI lifespan shutdown")
        await send({"type": "lifespan.shutdown.complete"})


@dataclass
class CollectionContractBackend:
    """Deterministic boundary double for the collection endpoint's infrastructure."""

    token_hashes: list[str] = field(default_factory=list)
    house_names: list[str] = field(default_factory=list)
    statements: list[tuple[str, object | None]] = field(default_factory=list)
    queued: list[tuple[int, list[int]]] = field(default_factory=list)
    daily_limit_checks: int = 0
    commits: int = 0


class _ContractSession:
    def __init__(self, backend: CollectionContractBackend) -> None:
        self.backend = backend

    async def execute(self, statement: object, params: object | None = None) -> None:
        self.backend.statements.append((str(statement), params))

    async def commit(self) -> None:
        self.backend.commits += 1


@contextmanager
def _collection_backend() -> Iterator[CollectionContractBackend]:
    backend = CollectionContractBackend()
    monkeypatch = pytest.MonkeyPatch()

    class TokenRepository:
        async def get_usuario_id_by_hash(self, _session: object, token_hash: str) -> int | None:
            backend.token_hashes.append(token_hash)
            expected = coleta.hash_do_token("contract-token")
            return 7 if token_hash == expected else None

    class CollectionRepository:
        async def count_received_since(
            self, _session: object, _usuario_id: int, _desde: object
        ) -> int:
            backend.daily_limit_checks += 1
            return 0

    class HouseRepository:
        async def get_id_by_nome(self, _session: object, name: str) -> int | None:
            backend.house_names.append(name)
            return {"Betano": 1}.get(name)

    def session_provider() -> Iterator[_ContractSession]:
        yield _ContractSession(backend)

    def enqueue(usuario_id: int, collection_ids: list[int]) -> None:
        backend.queued.append((usuario_id, list(collection_ids)))

    had_override = get_db in app.dependency_overrides
    previous_override = app.dependency_overrides.get(get_db)
    try:
        monkeypatch.setattr(coleta, "ColetaTokenRepo", TokenRepository)
        monkeypatch.setattr(coleta, "ColetaCasaRepo", CollectionRepository)
        monkeypatch.setattr(coleta, "CasaRepo", HouseRepository)
        monkeypatch.setattr(coleta, "_enfileirar", enqueue)
        monkeypatch.setattr(coleta.limiter, "enabled", False)
        monkeypatch.setattr(rate_limit_middleware.coleta_ip_limiter, "enabled", False)
        app.dependency_overrides[get_db] = session_provider
        yield backend
    finally:
        if had_override:
            assert previous_override is not None
            app.dependency_overrides[get_db] = previous_override
        else:
            app.dependency_overrides.pop(get_db, None)
        monkeypatch.undo()


def _as_object(value: object) -> JsonObject:
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _operations(document: JsonObject) -> Iterator[tuple[str, str, JsonObject]]:
    for path, path_item_value in _as_object(document["paths"]).items():
        path_item = _as_object(path_item_value)
        for method, operation_value in path_item.items():
            if method in HTTP_METHODS:
                yield method, path, _as_object(operation_value)


def _schema_nodes(schema: JsonObject, location: str) -> Iterator[tuple[str, JsonObject]]:
    yield location, schema
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            yield from _schema_nodes(_as_object(child), f"{location}.properties.{name}")
    for key in ("additionalProperties", "contains", "else", "if", "items", "not", "then"):
        child = schema.get(key)
        if isinstance(child, dict):
            yield from _schema_nodes(_as_object(child), f"{location}.{key}")
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = schema.get(key)
        if isinstance(children, list):
            for index, child in enumerate(children):
                yield from _schema_nodes(_as_object(child), f"{location}.{key}[{index}]")


def _operation_schemas(operation: JsonObject, location: str) -> Iterator[tuple[str, JsonObject]]:
    parameters = operation.get("parameters", [])
    assert isinstance(parameters, list)
    for index, parameter_value in enumerate(parameters):
        parameter = _as_object(parameter_value)
        yield f"{location}.parameters[{index}]", _as_object(parameter["schema"])

    request_body_value = operation.get("requestBody")
    if isinstance(request_body_value, dict):
        request_body = _as_object(request_body_value)
        for media_type, media_value in _as_object(request_body["content"]).items():
            yield (
                f"{location}.requestBody.{media_type}",
                _as_object(_as_object(media_value)["schema"]),
            )

    for code, response_value in _as_object(operation["responses"]).items():
        response = _as_object(response_value)
        content_value = response.get("content")
        if not isinstance(content_value, dict):
            continue
        for media_type, media_value in content_value.items():
            yield (
                f"{location}.responses.{code}.{media_type}",
                _as_object(_as_object(media_value)["schema"]),
            )


@pytest.fixture(scope="module")
def openapi_document() -> JsonObject:
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    return _as_object(response.json())


@pytest.mark.contract
def test_openapi_endpoint_matches_checked_in_snapshot(openapi_document: JsonObject) -> None:
    assert openapi_document["openapi"].startswith("3.1.")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert openapi_document == expected


@pytest.mark.contract
def test_every_operation_has_human_documentation(openapi_document: JsonObject) -> None:
    operations = list(_operations(openapi_document))
    assert len(operations) == 26
    for method, path, operation in operations:
        location = f"{method.upper()} {path}"
        assert str(operation.get("summary", "")).strip(), location
        assert str(operation.get("description", "")).strip(), location
        for parameter_value in operation.get("parameters", []):
            parameter = _as_object(parameter_value)
            assert str(parameter.get("description", "")).strip(), (
                f"{location}: {parameter.get('name')} lacks a description"
            )


@pytest.mark.contract
def test_request_bodies_have_examples(openapi_document: JsonObject) -> None:
    bodies = 0
    for method, path, operation in _operations(openapi_document):
        request_body_value = operation.get("requestBody")
        if not isinstance(request_body_value, dict):
            continue
        bodies += 1
        for media_type, media_value in _as_object(request_body_value["content"]).items():
            media = _as_object(media_value)
            assert media.get("example") is not None or media.get("examples"), (
                f"{method.upper()} {path}: {media_type} lacks an example"
            )
    assert bodies == 8


@pytest.mark.contract
def test_responses_cover_success_and_failures_with_schemas(openapi_document: JsonObject) -> None:
    for method, path, operation in _operations(openapi_document):
        location = f"{method.upper()} {path}"
        responses = _as_object(operation["responses"])
        numeric_codes = {int(code) for code in responses if str(code).isdigit()}
        assert any(200 <= code < 300 for code in numeric_codes), f"{location}: missing 2xx"
        assert any(500 <= code < 600 for code in numeric_codes), f"{location}: missing 5xx"
        if path.startswith("/api/") or operation.get("requestBody") or operation.get("parameters"):
            assert any(400 <= code < 500 for code in numeric_codes), f"{location}: missing 4xx"

        for code, response_value in responses.items():
            response = _as_object(response_value)
            assert str(response.get("description", "")).strip(), f"{location}: {code} description"
            content = response.get("content")
            assert isinstance(content, dict) and content, f"{location}: {code} content"
            for media_type, media_value in content.items():
                schema = _as_object(_as_object(media_value).get("schema"))
                assert schema, f"{location}: {code} {media_type} has an empty schema"


@pytest.mark.contract
def test_all_published_schemas_are_typed_and_strict(openapi_document: JsonObject) -> None:
    roots: list[tuple[str, JsonObject]] = []
    components = _as_object(_as_object(openapi_document["components"])["schemas"])
    roots.extend(
        (f"components.schemas.{name}", _as_object(schema)) for name, schema in components.items()
    )
    for method, path, operation in _operations(openapi_document):
        roots.extend(_operation_schemas(operation, f"paths.{path}.{method}"))

    assert roots
    for root_location, root in roots:
        for location, schema in _schema_nodes(root, root_location):
            assert schema, f"{location}: unconstrained Any schema"
            assert schema.get("type") != "any", f"{location}: Any type"
            additional = schema.get("additionalProperties")
            assert additional is not True, f"{location}: untyped additionalProperties"
            assert additional != {}, f"{location}: untyped additionalProperties"
            if schema.get("type") == "object":
                assert "additionalProperties" in schema, f"{location}: object is not strict"


@pytest.mark.contract
def test_business_input_schemas_preserve_domain_constraints(
    openapi_document: JsonObject,
) -> None:
    components = _as_object(_as_object(openapi_document["components"])["schemas"])

    manual_bet = _as_object(components["ApostaManual"])
    assert set(manual_bet["required"]) == {"casa", "odd", "stake_unidades"}
    manual_properties = _as_object(manual_bet["properties"])
    assert _as_object(manual_properties["odd"])["minimum"] == pytest.approx(1.01)
    assert _as_object(manual_properties["odd"])["maximum"] == 1000
    assert _as_object(manual_properties["stake_unidades"])["exclusiveMinimum"] == 0
    house_enum = _as_object(manual_properties["casa"])["enum"]
    assert isinstance(house_enum, list) and house_enum
    assert {"Betano", "betano", "bet365", "KTO", "kto"} <= set(house_enum)

    result = _as_object(components["Resultado"])
    result_properties = _as_object(result["properties"])
    assert set(_as_object(result_properties["estado"])["enum"]) == {
        "PENDENTE",
        "GREEN",
        "RED",
        "ANULADA",
        "MEIO_GREEN",
        "MEIO_RED",
        "CASHOUT",
    }
    for name in ("retorno_centavos", "cashout_valor_centavos", "comissao_centavos"):
        variants = _as_object(result_properties[name])["anyOf"]
        assert any(_as_object(variant).get("minimum") == 0 for variant in variants)
    assert result["allOf"]

    resolution = _as_object(components["ResolucaoSaida"])
    resolution_properties = _as_object(resolution["properties"])
    assert resolution_properties["aposta"] == {"$ref": "#/components/schemas/BetResponse"}


@pytest.mark.contract
def test_security_and_binary_media_are_explicit(openapi_document: JsonObject) -> None:
    schemes = _as_object(_as_object(openapi_document["components"])["securitySchemes"])
    assert schemes["CollectionToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Coleta-Token",
        "description": "Token opaco da extensão; armazenado no servidor somente como HMAC.",
    }
    paths = _as_object(openapi_document["paths"])
    for path in ("/coleta", "/api/v1/coleta"):
        assert _as_object(_as_object(paths[path])["post"])["security"] == [{"CollectionToken": []}]

    metrics = _as_object(_as_object(_as_object(paths["/metrics"])["get"])["responses"])
    assert "text/plain" in _as_object(_as_object(metrics["200"])["content"])
    export = _as_object(
        _as_object(_as_object(_as_object(paths["/api/v1/painel/export"])["get"])["responses"])[
            "200"
        ]
    )
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in _as_object(
        export["content"]
    )


@pytest.mark.contract
def test_collection_envelope_and_body_limit_match_runtime(openapi_document: JsonObject) -> None:
    paths = _as_object(openapi_document["paths"])
    collection = _as_object(_as_object(paths["/api/v1/coleta"])["post"])
    request = _as_object(collection["requestBody"])
    json_media = _as_object(_as_object(request["content"])["application/json"])
    collection_schema = _as_object(json_media["schema"])
    assert collection_schema["additionalProperties"] == {"$ref": "#/components/schemas/JsonValue"}
    properties = _as_object(collection_schema["properties"])
    assert _as_object(properties["apostas"])["items"] == {"$ref": "#/components/schemas/JsonValue"}
    assert properties["capturado_em"] == {
        "$ref": "#/components/schemas/JsonValue",
        "description": (
            "Metadado informativo enviado pela extensão; o servidor o ignora para ordenação e "
            "aceita clientes legados."
        ),
    }

    for _, path, operation in _operations(openapi_document):
        if not isinstance(operation.get("requestBody"), dict):
            continue
        response_413 = _as_object(_as_object(operation["responses"])["413"])
        content = _as_object(response_413["content"])
        assert "text/plain" in content, path

    upload = _as_object(_as_object(paths["/api/v1/upload"])["post"])
    upload_request = _as_object(upload["requestBody"])
    multipart = _as_object(_as_object(upload_request["content"])["multipart/form-data"])
    assert _as_object(multipart["schema"])["additionalProperties"] == {"type": "string"}
    upload_400 = _as_object(_as_object(upload["responses"])["400"])
    assert "application/json" in _as_object(upload_400["content"])
    upload_413 = _as_object(_as_object(upload["responses"])["413"])
    assert set(_as_object(upload_413["content"])) == {"application/json", "text/plain"}


@pytest.mark.contract
def test_ci_rejects_breaking_changes_against_main() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "origin/main:tests/contract/schemas/openapi.json" in workflow
    assert "oasdiff/oasdiff-action/breaking@5e81b5c380accc6b523f9d32a637ca630e33620b" in workflow
    assert "fail-on: WARN" in workflow
    assert "allow-external-refs: false" in workflow
    assert '--source-root "$base_worktree/src"' in workflow
    assert 'github-token: ""' in workflow
    assert "permissions:\n  contents: read" in workflow


contract_app = NoProductionLifespan(app)

positive_config = schemathesis.Config()
positive_config.projects.default.generation.update(
    modes=[schemathesis.GenerationMode.POSITIVE],
    max_examples=10,
    deterministic=True,
)
positive_schema = schemathesis.openapi.from_asgi(
    "/openapi.json", contract_app, config=positive_config
).include(path="/health", method="GET")


@pytest.mark.contract
@positive_schema.parametrize()
def test_schemathesis_valid_public_requests(case: schemathesis.Case) -> None:
    response = case.call_and_validate()
    assert response.status_code == 200


collection_positive_config = schemathesis.Config()
collection_positive_config.projects.default.generation.update(
    modes=[schemathesis.GenerationMode.POSITIVE],
    max_examples=5,
    deterministic=True,
)
collection_positive_config.projects.default.phases.update(phases=["fuzzing"])
collection_positive_schema = schemathesis.openapi.from_asgi(
    "/openapi.json", contract_app, config=collection_positive_config
).include(path="/api/v1/coleta", method="POST")
collection_operation = collection_positive_schema["/api/v1/coleta"]["POST"]


def invalid_collection_bodies() -> SearchStrategy[JsonObject]:
    invalid_contract = st.one_of(
        st.integers(max_value=0),
        st.integers(min_value=2, max_value=2**31),
    ).map(lambda contract: {"contrato": contract, "casa": "betano", "apostas": []})
    invalid_house = st.one_of(
        st.text(max_size=1),
        st.text(min_size=41, max_size=60),
    ).map(lambda house: {"contrato": 1, "casa": house, "apostas": []})
    invalid_bets = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.text(max_size=20),
    ).map(lambda bets: {"contrato": 1, "casa": "betano", "apostas": bets})
    missing_required = st.sampled_from([
        {},
        {"contrato": 1},
        {"contrato": 1, "casa": "betano"},
    ])
    return st.one_of(invalid_contract, invalid_house, invalid_bets, missing_required)


@collection_positive_schema.auth(refresh_interval=None)
class CollectionTokenAuth:
    def get(self, _case: schemathesis.Case, _context: schemathesis.AuthContext) -> str:
        return "contract-token"

    def set(
        self,
        case: schemathesis.Case,
        token: str,
        _context: schemathesis.AuthContext,
    ) -> None:
        case.headers[coleta.TOKEN_HEADER] = token


def _collection_case(body: JsonObject) -> schemathesis.Case:
    case = collection_operation.Case(body=body, media_type="application/json")
    collection_positive_schema.auth.set(
        case,
        schemathesis.AuthContext(operation=case.operation, app=contract_app),
    )
    return case


@pytest.mark.contract
@collection_positive_schema.parametrize()
def test_schemathesis_valid_collection_requests_match_contract(
    case: schemathesis.Case,
) -> None:
    with _collection_backend() as collection_backend:
        generated_body = _as_object(case.body)
        case.body = {
            **generated_body,
            "contrato": 1,
            "casa": "betano",
            "apostas": [],
        }

        response = case.call_and_validate()

        assert response.status_code == 200
        assert response.json() == {
            "antes_do_inicio": 0,
            "novas_contando": 0,
            "iguais_a_existentes": 0,
            "em_duvida": 0,
            "atualizadas": 0,
            "ja_conhecidas": 0,
            "recusadas": [],
            "sem_leitor": [],
        }
        assert collection_backend.daily_limit_checks == 1
        assert collection_backend.house_names == ["Betano"]
        assert collection_backend.commits == 1
        assert collection_backend.queued == [(7, [])]


@pytest.mark.contract
@settings(max_examples=10, derandomize=True, deadline=None)
@given(body=invalid_collection_bodies())
def test_schemathesis_invalid_collection_requests_match_contract(
    body: JsonObject,
) -> None:
    case = _collection_case(body)
    with _collection_backend() as collection_backend:
        response = case.call_and_validate()

        assert 400 <= response.status_code < 500
        assert "erro" in response.json()
        # A valid token is applied after negative input generation. Reaching the daily-limit check
        # proves that the generated request exercised the collection handler, not an auth 403.
        assert collection_backend.daily_limit_checks == 1
        assert collection_backend.commits == 0
        assert collection_backend.queued == []


negative_config = schemathesis.Config(suppress_health_check=[HealthCheck.filter_too_much])
negative_config.projects.default.generation.update(
    modes=[schemathesis.GenerationMode.NEGATIVE],
    max_examples=5,
    deterministic=True,
    allow_extra_parameters=False,
    with_security_parameters=False,
)
negative_schema = (
    schemathesis.openapi
    .from_asgi("/openapi.json", contract_app, config=negative_config)
    .include(path_regex=r"^/api/v1/(?!coleta$)")
    .exclude(
        # `chave` is an intentionally opaque, unconstrained string. There is no serializable
        # negative string value for that path parameter, so Schemathesis correctly has no strategy.
        path_regex=r"^/api/v1/apostas/\{chave\}",
    )
    # This authenticated stats operation has no request input to invalidate.
    .exclude(path="/api/v1/revisao/stats")
)


@pytest.mark.contract
@negative_schema.parametrize()
def test_schemathesis_invalid_protected_requests(case: schemathesis.Case) -> None:
    response = case.call_and_validate()
    assert response.status_code == 401
