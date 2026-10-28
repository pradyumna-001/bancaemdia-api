"""Consumer examples must validate against the whole published OpenAPI tree."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema_rs import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "name,schema_name", [("grupos", "HierarquiaSaida"), ("diario", "DiarioSaida")]
)
def test_complete_examples_and_exact_money_encoding(name: str, schema_name: str) -> None:
    document = json.loads((ROOT / "tests/contract/schemas/openapi.json").read_text())
    sample = json.loads((ROOT / f"docs/contracts/painel-detalhado-{name}.example.json").read_text())
    validator = Draft202012Validator({
        "components": document["components"],
        "$ref": f"#/components/schemas/{schema_name}",
    })
    validator.validate(sample)
    for invalid in (1.5, 100, "1.5", "1e2"):
        bad = copy.deepcopy(sample)
        bad["total_periodo"]["lucro_centavos"] = invalid
        with pytest.raises(ValidationError):
            validator.validate(bad)
    if name == "grupos":
        bad = copy.deepcopy(sample)
        bad["grupos"][0]["id"] = 2**53 + 1
        with pytest.raises(ValidationError):
            validator.validate(bad)


@pytest.mark.parametrize("suffix", ["diario", "grupos", "detalhado/export"])
def test_new_reads_publish_selection_errors_and_bounds(suffix: str) -> None:
    document: dict[str, Any] = json.loads(
        (ROOT / "tests/contract/schemas/openapi.json").read_text()
    )
    operation = document["paths"][f"/api/v1/painel/filtrado/{suffix}"]["get"]
    assert {"401", "422", "429", "503"} <= operation["responses"].keys()
    assert operation["security"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert {
        "periodo",
        "grupo_id",
        "banca_id",
        "conta_casa_id",
        "titular_id",
        "origem",
        "visibilidade",
        "fresh",
    } <= params.keys()
    if suffix == "diario":
        assert params["dias_por_pagina"]["schema"]["maximum"] == 366
    if suffix == "grupos":
        assert params["page_size"]["schema"]["maximum"] == 100
