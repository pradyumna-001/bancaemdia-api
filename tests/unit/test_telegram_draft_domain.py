"""Draft validation and concise Telegram reply parsing."""

import pytest

from bancaemdia.domain.rascunho_aposta import (
    DraftInputError,
    missing_fields,
    parse_reply,
    summary,
)


def test_only_missing_or_ambiguous_fields_are_requested() -> None:
    values = {
        "odd": 1.82,
        "evento": "Corinthians x Santos",
        "data_aposta": "2026-09-20T21:00:00-03:00",
    }
    missing = missing_fields(values, {"odd": {"source": "extraction", "confidence": 0.95}})
    assert missing == ["casa", "stake_unidades"]
    message = summary(values, missing, "AWAITING_INFORMATION")
    assert "Corinthians x Santos" in message
    assert "1.82" in message
    assert "casa, stake em unidades" in message
    assert "Reenvie" not in message


def test_low_confidence_and_invalid_extraction_are_not_trusted() -> None:
    values = {
        "casa": "Betano",
        "odd": 1.01,
        "stake_unidades": 0,
        "data_aposta": "2026-09-20T21:00:00-03:00",
    }
    metadata = {"odd": {"source": "extraction", "confidence": 0.3}}
    assert missing_fields(values, metadata) == ["odd", "stake_unidades"]
    assert missing_fields(values, metadata, account_needs_choice=True) == [
        "odd",
        "stake_unidades",
        "conta_casa_id",
    ]
    only_odd = summary(values, ["odd"], "AWAITING_INFORMATION")
    assert "odd=1,90" in only_odd
    assert "casa=Betano" not in only_odd


def test_one_reply_can_patch_multiple_fields_or_one_explicit_correction() -> None:
    assert parse_reply("casa Betano, stake 2,5") == {
        "casa": "Betano",
        "stake_unidades": 2.5,
    }
    assert parse_reply("stake=2; odd=1,90") == {"stake_unidades": 2.0, "odd": 1.9}
    assert parse_reply("/corrigir data 23/09/2026") == {"data_aposta": "2026-09-23T00:00:00-03:00"}
    assert parse_reply("evento=Time da casa x visitante; stake=2") == {
        "evento": "Time da casa x visitante",
        "stake_unidades": 2.0,
    }


@pytest.mark.parametrize(
    "reply",
    ["stake=0", "odd=inf", "casa=Casa Inventada", "conta=abc", "stake=2; stake=3"],
)
def test_invalid_reply_is_rejected_before_it_can_touch_the_draft(reply: str) -> None:
    with pytest.raises(DraftInputError):
        parse_reply(reply)
