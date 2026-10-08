"""Transient algorithm regressions; no unreviewed capture fixtures enter Git."""

from decimal import Decimal, localcontext

import pytest

from bancaemdia.coleta.readers.base import ReaderEnvelope
from bancaemdia.coleta.readers.errors import ReaderError
from bancaemdia.coleta.readers.native import exact_amount, money_text
from bancaemdia.coleta.readers.one_win import OneWinReader
from tests.coleta.test_one_win import envelope, source


def native_source(status=2, **changes):
    raw = source()
    raw["bet"].update(currencyCode="USDT", status=status, profitAmount=30.85 if status == 2 else 0)
    raw["bet"].update(changes)
    raw["selections"][0]["status"] = status
    return raw


def native_bet(raw=None, **changes):
    return OneWinReader().parse_native(
        ReaderEnvelope.model_validate(envelope(native_source() if raw is None else raw, **changes))
    )


def test_native_green_and_red_use_source_return_without_deriving_profit_from_odd():
    raw = native_source(profitAmount=31.73)
    bet = native_bet(raw)
    assert bet.currency == "USDT" and bet.returned == Decimal("31.73")
    assert bet.profit == Decimal("19.39")
    assert bet.game_at != bet.placed_at
    assert bet.source_updated_at is None and bet.revision_policy == "unversioned_conflict_review"
    assert not hasattr(bet, "stake_centavos")
    assert native_bet(native_source(1)).returned == 0


def test_source_precision_is_retained_and_serialized_as_decimal_text():
    import json

    text = json.dumps(native_source()).replace("30.85", "30.850000000000000001")
    bet = native_bet(text=text)
    assert bet.returned == Decimal("30.850000000000000001")
    assert bet.model_dump(mode="json")["returned"] == "30.850000000000000001"
    with localcontext() as context:
        context.prec = 4
        assert bet.profit == Decimal("18.510000000000000001")


@pytest.mark.parametrize(
    "value",
    [True, False, 1.25, "NaN", "1e3", "1,25", Decimal("NaN"), Decimal("1e-31"), Decimal("1e20")],
)
def test_native_money_rejects_rounding_float_and_unknown_formats(value):
    with pytest.raises(ValueError):
        exact_amount(value)


def test_native_text_format_does_not_round_under_low_decimal_context():
    value = Decimal("1234567890123456789.123456789012345678901234567890")
    with localcontext() as context:
        context.prec = 4
        assert (
            money_text(exact_amount(value)) == "1234567890123456789.12345678901234567890123456789"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"freebetAmount": 1},
        {"bonusAmount": 1},
        {"bonusPercent": 1},
        {"currencyCode": "BRL"},
        {"betType": "express"},
    ],
)
def test_unobserved_source_variants_are_retained(changes):
    raw = native_source()
    raw["bet"].update(changes)
    with pytest.raises(ReaderError) as failure:
        native_bet(raw)
    assert failure.value.reason == "native_source_variant_not_evidenced"


@pytest.mark.parametrize("status", [0, 3, 4])
def test_public_enum_does_not_authorize_unobserved_native_settlement(status):
    raw = native_source(status)
    with pytest.raises(ReaderError) as failure:
        native_bet(raw)
    assert failure.value.reason == "native_source_variant_not_evidenced"


def test_half_return_or_missing_selection_result_is_not_inferred():
    for value in (True, None):
        raw = native_source()
        if value is None:
            del raw["selections"][0]["status"]
        else:
            raw["selections"][0]["isHalfReturn"] = value
        with pytest.raises(ReaderError, match="native_source_variant_not_evidenced"):
            native_bet(raw)


def test_multiple_has_stable_canonical_order_and_minimum_game_time():
    import copy

    raw = native_source(betType="express")
    selection = copy.deepcopy(raw["selections"][0])
    selection["match"]["id"] = "another-event"
    selection["odd"]["id"] = "another-odd"
    selection["match"]["startAt"] -= 3600
    raw["selections"].append(selection)
    first = native_bet(raw)
    raw["selections"].reverse()
    second = native_bet(raw)
    assert first.canonical_hash == second.canonical_hash
    assert first.game_at == second.game_at


def test_sha256_metadata_cannot_be_misclassified_as_a_phone_number():
    from bancaemdia.coleta.readers.safety import check_safe

    fingerprint = "a" * 20 + "11987654321" + "b" * 33
    check_safe({"content_hash": fingerprint})
    with pytest.raises(ReaderError, match="sensitive_value"):
        check_safe({"description": fingerprint})
    with pytest.raises(ReaderError, match="sensitive_value"):
        check_safe({"content_hash": "11987654321"})


@pytest.mark.parametrize("status", [1, 2])
def test_source_bet_and_selection_states_cannot_contradict_each_other(status):
    raw = native_source(status)
    raw["selections"][0]["status"] = 1 if status == 2 else 2
    with pytest.raises(ReaderError, match="native_source_variant_not_evidenced"):
        native_bet(raw)


def test_native_harness_checks_independent_golden_and_retains_human_gate(tmp_path):
    from datetime import UTC, datetime

    from bancaemdia.coleta.readers.base import canonical_bytes, digest
    from bancaemdia.coleta.readers.registry import DEFAULT_REGISTRY
    from tests.coleta.harness import FixtureContractError, bundle_digest, run_fixture_set
    from tests.coleta.test_harness import temporary_bundle, write_json

    directory = tmp_path / "native"
    manifest = temporary_bundle(directory)
    value = envelope(native_source())
    expected = {
        "money_contract": 2,
        "reader_id": "1win_history_candidate",
        "reader_version": "0.2.0",
        "external_identity": "unit-ticket-one-win",
        "brand": "1win",
        "hostname": "api-gateway.top-parser.com",
        "currency": "USDT",
        "placed_at": "2026-01-02T02:00:00Z",
        "source_updated_at": None,
        "revision_policy": "unversioned_conflict_review",
        "state": "GREEN",
        "stake": "12.34",
        "returned": "30.85",
        "odds": "2.5",
        "selections": [
            {
                "event_id": "s:unit-event",
                "event": "Equipe teste A x Equipe teste B",
                "starts_at": datetime
                .fromtimestamp(1767484800, UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "description": "Resultado teste",
                "market": "Total",
                "odds": "2.5",
                "result": "GREEN",
            }
        ],
    }
    (directory / "probe.raw.json").write_text(value["payload_text"], encoding="utf-8")
    write_json(directory / "probe.envelope.json", value)
    (directory / "probe.golden.json").write_bytes(canonical_bytes({"bets": [expected]}))
    manifest.update(
        money_contract=2,
        reader_id="1win_history_candidate",
        reader_version="0.2.0",
        brand="1win",
        hostname="api-gateway.top-parser.com",
        raw_schema_version="1win-history-game-v2",
    )
    manifest["fixtures"][0]["covered_state"] = "GREEN"
    manifest["fixtures"][0]["files"] = {
        name: digest((directory / name).read_bytes()) for name in manifest["fixtures"][0]["files"]
    }
    manifest["review"]["bundle_sha256"] = bundle_digest(manifest)
    write_json(directory / "manifest.json", manifest)
    report = run_fixture_set(directory, DEFAULT_REGISTRY)
    assert report["passed"] and report["money_contract"] == 2
    assert report["production_support_proven"] is False
    manifest["review"]["status"] = "pending"
    write_json(directory / "manifest.json", manifest)
    with pytest.raises(FixtureContractError, match="human_review_missing_or_changed"):
        run_fixture_set(directory, DEFAULT_REGISTRY)


def test_native_admission_cannot_promote_an_unreviewed_digest(monkeypatch):
    from scripts import validate_reader_fixtures as scanner

    monkeypatch.setattr(scanner, "REVIEWED_BUNDLE_SHA256", "b" * 64)
    with pytest.raises(ValueError, match="native admission requires"):
        scanner.validate()


@pytest.mark.parametrize("field", ["freebetAmount", "bonusAmount", "bonusPercent"])
def test_missing_bonus_evidence_cannot_be_assumed_zero(field):
    raw = native_source()
    del raw["bet"][field]
    with pytest.raises(ReaderError, match="required_fields_missing"):
        native_bet(raw)


def test_winning_cash_source_cannot_assert_zero_gross_return():
    with pytest.raises(ReaderError, match="native_money_contract_invalid"):
        native_bet(native_source(profitAmount=0))
