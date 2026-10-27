import json
import re
from pathlib import Path

import jsonschema_rs
import pytest

from bancaemdia.api.collection_contract import CONTRACT_PATHS
from bancaemdia.api.policies import EXTENSION, FORA_DO_USUARIO, POLITICAS
from bancaemdia.domain.access import requires_full_write

SCHEMA = json.loads(
    (Path(__file__).parent / "schemas" / "openapi.json").read_text(encoding="utf-8")
)
METHODS = ("get", "post", "put", "patch", "delete")


def _operations() -> list[tuple[str, str, dict[str, object]]]:
    return [
        (method, path, operation)
        for path, item in SCHEMA["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    ]


def _concreto(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_operations_outside_the_user_gate_are_real_routes() -> None:
    # Exclusão obsoleta esconderia uma operação da prova de runtime.
    assert FORA_DO_USUARIO <= set(SCHEMA["paths"])


def test_every_operation_declares_one_valid_policy() -> None:
    # jsonschema_rs chega com o schemathesis das dependências de desenvolvimento.
    validator = jsonschema_rs.validator_for(SCHEMA["components"]["schemas"]["BancaemdiaPolicy"])
    operations = _operations()
    assert {(method, path) for method, path, _ in operations} == set(POLITICAS)
    for method, path, operation in operations:
        politica = operation[EXTENSION]
        erros = [erro.message for erro in validator.iter_errors(politica)]
        assert not erros, (method, path, erros)
        assert not [chave for chave in operation if chave.startswith("x-") and chave != EXTENSION]


@pytest.mark.parametrize(("method", "path", "operation"), _operations())
def test_policy_matches_responses_parameters_and_recovery(
    method: str, path: str, operation: dict[str, object]
) -> None:
    politica = operation[EXTENSION]
    assert isinstance(politica, dict)
    responses = operation["responses"]
    assert isinstance(responses, dict)
    # 402 publicado só onde o gate comercial pode recusar; o artefato da extensão (v2) é congelado.
    if path not in CONTRACT_PATHS:
        assert ("402" in responses) == (politica["access_modes"] == ["FULL_WRITE"])
    if method == "get":
        assert politica["automatic_retry"]["allowed"] or path.startswith("/auth/")
    identidade = politica["idempotency"]["identity"]
    parametros = {
        (str(item["in"]), str(item["name"]))
        for item in operation.get("parameters", [])  # type: ignore[attr-defined]
    }
    if identidade.startswith("header:"):
        assert ("header", identidade.removeprefix("header:")) in parametros
    if identidade.startswith("path:"):
        assert ("path", identidade.removeprefix("path:")) in parametros
    if identidade == "none" and politica["automatic_retry"]["allowed"] and method != "get":
        # Escrita sem identidade só se repete sozinha quando é cálculo puro.
        assert path.startswith("/api/v1/calculadoras/")
    recuperacao = politica["idempotency"]["recovery"]
    if recuperacao and recuperacao.startswith("GET "):
        alvo = recuperacao.removeprefix("GET ")
        assert "get" in SCHEMA["paths"][alvo]


@pytest.mark.parametrize(
    ("method", "path"),
    [(method, path) for method, path, _ in _operations() if path not in FORA_DO_USUARIO],
)
def test_declared_access_is_the_runtime_gate_of_user_operations(method: str, path: str) -> None:
    politica = POLITICAS[method, path]
    exige = requires_full_write(method.upper(), _concreto(path))
    assert (politica.acesso == ("FULL_WRITE",)) == exige, (method, path)
