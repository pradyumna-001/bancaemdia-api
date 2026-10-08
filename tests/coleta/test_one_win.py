"""Transient source-contract examples; no real capture or reviewed fixture claims."""

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bancaemdia.coleta.readers.base import ReaderEnvelope, digest
from bancaemdia.coleta.readers.errors import ReaderError
from bancaemdia.coleta.readers.one_win import (
    ENDPOINT,
    RESPONSE_HOST,
    V1_SCHEMA,
    V2_SCHEMA,
    OneWinReader,
    candidate_registry,
    game_time,
    source_number,
)
from bancaemdia.coleta.readers.reference import reference_registry
from bancaemdia.coleta.readers.registry import DEFAULT_REGISTRY, ReaderRegistry
from tests.coleta.test_reader_contract import envelope as reference_envelope


def source():
    return {
        "bet": {
            "id": "unit-ticket-one-win",
            "createdAt": "2026-01-01T23:00:00-03:00",
            "currencyCode": "BRL",
            "wallet": "unit-wallet",
            "status": 0,
            "amount": 12.34,
            "cf": 2.5,
            "profitAmount": 0,
            "freebetAmount": 0,
            "bonusAmount": 0,
            "bonusPercent": 0,
            "betType": "ordinary",
        },
        "selections": [
            {
                "match": {
                    "id": "unit-event",
                    "startAt": 1767484800,
                    "competitors": [{"name": "Equipe teste A"}, {"name": "Equipe teste B"}],
                },
                "odd": {
                    "id": "unit-odd",
                    "name": "Resultado teste",
                    "groupName": "Total",
                    "cf": 2.5,
                },
                "status": 0,
                "isHalfReturn": False,
            }
        ],
    }


def envelope(raw=None, *, text=None, **changes):
    payload = (
        text
        if text is not None
        else json.dumps(source() if raw is None else raw, separators=(",", ":"), ensure_ascii=False)
    )
    value = {
        "envelope_schema": 1,
        "brand": "1win",
        "hostname": RESPONSE_HOST,
        "source": {"channel": "fetch", "direction": "observed_response", "endpoint": ENDPOINT},
        "captured_at": "2026-01-06T12:00:00Z",
        "raw_schema_version": V2_SCHEMA,
        "content_type": "application/json",
        "payload_text": payload,
        "content_hash": digest(payload.encode()),
    }
    value.update(changes)
    return value


def inspect(raw=None, **changes):
    return OneWinReader().inspect(ReaderEnvelope.model_validate(envelope(raw, **changes)))


def test_exact_source_values_and_game_day_are_extracted_without_money_claim():
    observation = inspect()
    assert observation.game_at == datetime(2026, 1, 4, tzinfo=UTC)
    assert observation.placed_at == datetime(2026, 1, 2, 2, tzinfo=UTC)
    assert observation.displayed_amount == Decimal("12.34")
    assert observation.odds == Decimal("2.5")
    assert observation.source_profit_amount == 0
    assert observation.currency == "BRL"
    assert observation.selections[0].event == "Equipe teste A x Equipe teste B"
    assert observation.selections[0].event_id == "s:unit-event"
    assert not hasattr(observation, "stake_centavos")
    assert not hasattr(observation, "return_centavos")
    assert not hasattr(observation, "source_updated_at")
    assert not hasattr(observation, "wallet")
    assert not hasattr(observation, "conta_id")


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (0, "PENDENTE"),
        (1, "RED"),
        (2, "GREEN"),
        (3, "ANULADA"),
        (4, "CASHOUT"),
    ],
)
def test_public_enum_does_not_assert_authoritative_settlement(status, state):
    raw = source()
    raw["bet"]["status"] = status
    raw["bet"]["profitAmount"] = 31.73
    raw["selections"][0]["isHalfReturn"] = True
    observation = inspect(raw)
    assert observation.public_state.value == state
    assert observation.source_profit_amount == Decimal("31.73")
    assert observation.half_return_flags == (True,)
    with pytest.raises(ReaderError, match="financial_evidence_pending"):
        candidate_registry().read(envelope(raw))


def test_identity_survives_replay_lifecycle_and_capture_time_change():
    raw = source()
    first = inspect(raw)
    repeated = inspect(raw, captured_at="2026-01-07T12:00:00Z")
    raw["bet"].update(status=2, profitAmount=30.85)
    settled = inspect(raw)
    assert first.identity_hash == repeated.identity_hash == settled.identity_hash
    assert first.source_content_hash == repeated.source_content_hash
    assert first.source_content_hash != settled.source_content_hash


def test_multiple_uses_earliest_game_in_utc_independent_of_source_order():
    raw = source()
    another = copy.deepcopy(raw["selections"][0])
    another["match"].update(id=1, startAt="1767312000.000001")
    raw["selections"].append(another)
    raw["bet"]["betType"] = "express"
    first = inspect(raw)
    raw["selections"].reverse()
    second = inspect(raw)
    assert first.game_at == second.game_at == datetime(2026, 1, 2, 0, 0, 0, 1, tzinfo=UTC)
    assert second.selections[0].event_id == "n:1"
    raw["selections"][0]["match"]["id"] = "1"
    assert inspect(raw).selections[0].event_id == "s:1"


def test_raw_decimal_lexemes_are_not_rounded_through_float():
    text = envelope()["payload_text"].replace('"amount":12.34', '"amount":12.340000000000000001')
    observation = inspect(text=text)
    assert observation.displayed_amount == Decimal("12.340000000000000001")
    assert observation.displayed_amount != Decimal(str(float(observation.displayed_amount)))


@pytest.mark.parametrize(
    "value", [True, False, 1.1, "12.34", Decimal("NaN"), Decimal("Infinity"), 10**16]
)
def test_financial_source_types_fail_closed(value):
    with pytest.raises(ReaderError):
        source_number(value)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "game_time_missing"),
        ("2026-01-04", "invalid_game_time"),
        ("1,2", "invalid_game_time"),
        (-1, "invalid_game_time"),
        (0, "invalid_game_time"),
        (Decimal("1.0000001"), "invalid_game_time"),
        (10**12, "invalid_game_time"),
        (True, "financial_number_not_exact"),
        ({}, "financial_number_not_exact"),
    ],
)
def test_no_clock_fallback_or_ambiguous_timestamp(value, reason):
    with pytest.raises(ReaderError, match=reason):
        game_time(value)


@pytest.mark.parametrize("remove", ["startAt", "competitors", "name"])
def test_missing_game_data_is_retained_instead_of_using_placement(remove):
    raw = source()
    selection = raw["selections"][0]
    del selection["odd" if remove == "name" else "match"][remove]
    with pytest.raises(ReaderError, match="incomplete_payload"):
        inspect(raw)


@pytest.mark.parametrize("value", [99, True, "2", 2.0])
def test_unknown_or_coerced_state_is_rejected(value):
    raw = source()
    raw["bet"]["status"] = value
    with pytest.raises(ReaderError, match="schema_drift"):
        inspect(raw)


@pytest.mark.parametrize("value", ["bad-time", "2026-01-01T12:00:00"])
def test_placement_is_not_guessed(value):
    raw = source()
    raw["bet"]["createdAt"] = value
    with pytest.raises(ReaderError, match="invalid_placement_time"):
        inspect(raw)


def test_v1_is_preserved_and_explicitly_reports_missing_game_projection():
    raw = source()["bet"]
    del raw["betType"]
    with pytest.raises(ReaderError, match="game_fields_not_projected"):
        candidate_registry().read(envelope(raw, raw_schema_version=V1_SCHEMA))
    with pytest.raises(ReaderError, match="required_fields_missing"):
        inspect(raw)


@pytest.mark.parametrize("field", ["status", "isHalfReturn"])
def test_optional_fields_may_be_absent_but_not_explicit_null(field):
    raw = source()
    del raw["selections"][0][field]
    inspect(raw)
    raw["selections"][0][field] = None
    with pytest.raises(ReaderError, match="one_win_source_shape_changed"):
        inspect(raw)


def test_duplicate_selection_identity_is_not_counted_twice():
    raw = source()
    raw["selections"].append(copy.deepcopy(raw["selections"][0]))
    with pytest.raises(ReaderError, match="duplicate_source_selection"):
        inspect(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_schema_version": "future"},
        {"envelope_schema": 2},
        {"content_type": "text/html"},
        {
            "source": {
                "channel": "fetch",
                "direction": "observed_response",
                "endpoint": "/bets/history/get",
            }
        },
    ],
)
def test_version_and_source_are_exact(changes):
    with pytest.raises(ReaderError, match="schema_drift"):
        inspect(**changes)


@pytest.mark.parametrize("changes", [{"hostname": "1win.com"}, {"brand": "other"}])
def test_page_host_or_brand_cannot_authorize_response_host(changes):
    with pytest.raises(ReaderError, match="wrong_host"):
        inspect(**changes)


def test_unsafe_unknown_fields_and_duplicate_keys_cannot_be_retained():
    raw = source()
    raw["bet"]["authorization"] = "unit-secret-sentinel"
    with pytest.raises(ReaderError, match="unsafe_payload"):
        inspect(raw)
    with pytest.raises(ReaderError, match="duplicate_json_key"):
        inspect(text='{"bet":{},"bet":{}}')


def test_candidate_is_isolated_and_cannot_promote_production_support():
    with pytest.raises(ReaderError, match="wrong_host"):
        DEFAULT_REGISTRY.read(envelope())
    combined = ReaderRegistry(
        candidate_registry().registrations + reference_registry().registrations
    )
    with pytest.raises(ReaderError, match="financial_evidence_pending"):
        combined.read(envelope())
    assert combined.read(reference_envelope()).bets[0].stake_centavos == 1234


def test_xhr_is_an_independent_exact_registration():
    source_location = {"channel": "xhr", "direction": "observed_response", "endpoint": ENDPOINT}
    with pytest.raises(ReaderError, match="financial_evidence_pending"):
        candidate_registry().read(envelope(source=source_location))
