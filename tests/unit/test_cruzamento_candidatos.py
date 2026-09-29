import copy
import json
from pathlib import Path

import pytest

from bancaemdia.domain.cruzamento import compare, normalize

FIXTURES = json.loads((Path(__file__).parents[1] / "fixtures/cruzamento/cases.json").read_text())


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["name"])
def test_versioned_sanitized_fixtures_have_stable_scores_classes_and_explanations(case):
    left = normalize(FIXTURES["base"])
    right = normalize({**FIXTURES["base"], **case["changes"]})
    first = compare(left, right)
    assert first.classification == case["class"]
    assert first.score == case["score"]
    assert first == compare(copy.deepcopy(left), copy.deepcopy(right))
    assert first.json() == compare(left, right).json()
    assert "Concordam:" in first.explanation and "Divergem:" in first.explanation


def test_tolerance_and_source_time_boundaries_are_explicit():
    base = normalize(FIXTURES["base"])
    assert compare(base, {**base, "odd": 1.905}).classification == "exact"
    assert compare(base, {**base, "odd": 1.906}).classification == "incompatible"
    assert compare(base, {**base, "ocorrido_em": "2026-09-20T22:35:00Z"}).classification == "exact"
    assert (
        compare(base, {**base, "ocorrido_em": "2026-09-20T22:35:01Z"}).classification == "probable"
    )
    assert compare(base, {**base, "ocorrido_em": None}).classification == "probable"
    assert (
        compare(base, {**base, "ocorrido_em": "2020-01-01T00:00:00Z"}).classification
        == "incompatible"
    )
    with pytest.raises(ValueError):
        compare(base, base, "unknown/2")


def test_only_supplied_canonical_dictionary_can_normalize_names():
    raw = {**FIXTURES["base"], "evento": "Azul FC - Verde"}
    literal = normalize(raw)
    assert literal["evento"] == "Azul FC - Verde"
    known = normalize(raw, {"evento:Azul FC - Verde": "time#1 vs time#2"})
    assert known["evento"] == "time#1 vs time#2"
    assert raw["evento"] == "Azul FC - Verde"


@pytest.mark.parametrize("legs", ["invalid", {"bad": "shape"}, [None], ["invalid"]])
def test_malformed_legacy_legs_never_establish_exact_identity(legs):
    base = normalize(FIXTURES["base"])
    malformed = normalize({**FIXTURES["base"], "selecoes": legs})
    assert compare(base, malformed).classification == "probable"


def test_full_multiple_is_order_independent_but_missing_leg_cannot_be_exact():
    raw = copy.deepcopy(FIXTURES["base"])
    raw["tipo_aposta"] = "MULTIPLA"
    raw["selecoes"].append({
        "evento": "Amarelo - Roxo",
        "mercado": "Vencedor",
        "escolha": "Roxo",
        "linha": None,
    })
    reordered = copy.deepcopy(raw)
    reordered["selecoes"].reverse()
    assert compare(normalize(raw), normalize(reordered)).classification == "exact"
    reordered["selecoes"].pop()
    assert compare(normalize(raw), normalize(reordered)).classification == "probable"


def test_unknown_fields_and_conflicting_settlements_do_not_become_exact():
    base = normalize(FIXTURES["base"])
    for patch in (
        {"moeda": None},
        {"stake_centavos": 0},
        {"odd": float("nan")},
        {"ticket": None},
        {"estrutura_completa": False},
    ):
        assert compare(base, {**base, **patch}).classification != "exact"
    assert (
        compare({**base, "estado": "GREEN"}, {**base, "estado": "RED"}).classification == "probable"
    )
