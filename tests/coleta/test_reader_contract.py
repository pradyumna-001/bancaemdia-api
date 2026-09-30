"""Ephemeral algorithm inputs, never a reviewed capture fixture pack or production evidence."""

import copy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bancaemdia.coleta.readers.base import (
    ReaderEnvelope,
    SourceLocation,
    canonical_bytes,
    decode_raw,
    digest,
)
from bancaemdia.coleta.readers.errors import ReaderError, SchemaDriftError, UnsafePayloadError
from bancaemdia.coleta.readers.reference import decimal_number, money, reference_registry
from bancaemdia.coleta.readers.registry import DEFAULT_REGISTRY, ReaderRegistry
from bancaemdia.coleta.readers.safety import check_safe
from tests.coleta.harness import GoldenReplay


def source(**changes):
    bet = {
        "ticket_id": "unit-ticket",
        "placed_at": "2026-01-01T10:00:00-03:00",
        "revised_at": "2026-01-01T10:00:00-03:00",
        "status": "OPEN",
        "stake": "12,34",
        "odds": "2.500000",
        "currency": "BRL",
        "selections": [
            {
                "event_id": "unit-event",
                "event": "Equipe teste A x B",
                "starts_at": "2026-01-03T23:00:00-03:00",
                "selection": "Resultado teste",
            }
        ],
    }
    bet.update(changes)
    return {"source_schema": "example-1", "bets": [bet]}


def envelope(raw=None, **changes):
    text = canonical_bytes(source() if raw is None else raw).decode()
    value = {
        "envelope_schema": 1,
        "brand": "example",
        "hostname": "reader.example.invalid",
        "source": {"channel": "fetch", "direction": "observed_response", "endpoint": "/history"},
        "captured_at": "2026-01-06T12:00:00Z",
        "raw_schema_version": "example-1",
        "content_type": "application/json",
        "payload_text": text,
        "content_hash": digest(text.encode()),
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("status", "returned", "state"),
    [
        ("OPEN", None, "PENDENTE"),
        ("WIN", "30,85", "GREEN"),
        ("LOSE", "0", "RED"),
        ("VOID", "12,34", "ANULADA"),
        ("CASHOUT", "8,27", "CASHOUT"),
        ("HALF_WIN", "18,51", "MEIO_GREEN"),
        ("HALF_LOSE", "6,17", "MEIO_RED"),
    ],
)
def test_state_and_authoritative_money(status, returned, state):
    parsed = reference_registry().read(envelope(source(status=status, returned=returned)))
    bet = parsed.bets[0]
    assert bet.state.value == state
    assert bet.stake_centavos == 1234
    assert bet.odds == Decimal("2.5")
    assert bet.return_centavos == (
        None
        if returned is None
        else {"30,85": 3085, "0": 0, "12,34": 1234, "8,27": 827, "18,51": 1851, "6,17": 617}[
            returned
        ]
    )
    assert bet.note is None and bet.selections[0].market is None
    assert bet.game_at == datetime(2026, 1, 4, 2, tzinfo=UTC)
    assert bet.game_at != bet.placed_at


def test_multiple_order_and_timezone_are_deterministic():
    raw = source()
    raw["bets"][0]["selections"].append({
        "event_id": "aaa",
        "event": "Outro teste",
        "starts_at": "2026-01-02T00:00:00Z",
        "selection": "Teste",
        "market": "Total",
    })
    first = reference_registry().read(envelope(raw)).bets[0]
    raw["bets"][0]["selections"].reverse()
    second = reference_registry().read(envelope(raw)).bets[0]
    assert first.canonical_hash == second.canonical_hash
    assert first.game_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert first.selections[0].event_id == "aaa"


def test_replay_and_lifecycle_use_the_same_identity_without_financial_persistence_claim():
    registry = reference_registry()
    first = registry.read(envelope())
    replay = GoldenReplay({})
    assert replay.apply(first) == ["created"]
    for _ in range(10):
        assert replay.apply(registry.read(envelope(captured_at="2026-01-07T00:00:00Z"))) == ["noop"]
    update = registry.read(
        envelope(source(status="WIN", returned="30.85", revised_at="2026-01-05T10:00:00Z"))
    )
    assert first.bets[0].identity_hash == update.bets[0].identity_hash
    assert first.content_hash != update.content_hash
    assert first.bets[0].canonical_hash != update.bets[0].canonical_hash
    assert replay.apply(update) == ["updated"]
    assert replay.apply(first) == ["stale_or_conflicting"]
    assert len(replay.bets) == 1


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"hostname": "different.example.invalid"}, "wrong_host"),
        ({"brand": "other"}, "wrong_host"),
        ({"hostname": "*.example.invalid"}, "wrong_host"),
        ({"hostname": "https://reader.example.invalid"}, "wrong_host"),
        ({"hostname": "127.0.0.1"}, "wrong_host"),
        ({"raw_schema_version": "example-2"}, "schema_drift"),
        ({"envelope_schema": 2}, "schema_drift"),
        ({"envelope_schema": True}, "schema_drift"),
        ({"content_type": "text/html"}, "schema_drift"),
        ({"content_hash": "0" * 64}, "schema_drift"),
        ({"captured_at": "2026-01-01"}, "schema_drift"),
        ({"extra": "field"}, "schema_drift"),
        (
            {
                "source": {
                    "channel": "xhr",
                    "direction": "observed_response",
                    "endpoint": "/history",
                }
            },
            "schema_drift",
        ),
        (
            {
                "source": {
                    "channel": "fetch",
                    "direction": "observed_response",
                    "endpoint": "/other",
                }
            },
            "schema_drift",
        ),
    ],
)
def test_exact_routing_and_unknown_contracts_fail_closed(changes, code):
    with pytest.raises(ReaderError) as failure:
        reference_registry().read(envelope(**changes))
    assert failure.value.code.value == code


def test_host_normalization_is_exact_not_suffix_matching():
    assert (
        reference_registry().read(envelope(hostname="READER.EXAMPLE.INVALID.")).bets[0].hostname
        == "reader.example.invalid"
    )
    with pytest.raises(ReaderError):
        reference_registry().read(envelope(hostname="reader.example.invalid.evil.invalid"))
    with pytest.raises(ReaderError, match="wrong_host"):
        DEFAULT_REGISTRY.read(envelope())


def test_registry_ambiguity_is_rejected_at_startup():
    registration = reference_registry().registrations[0]
    with pytest.raises(SchemaDriftError, match="ambiguous_reader_registration"):
        ReaderRegistry((registration, replace(registration, reader_id="another_reader")))
    with pytest.raises(ValueError):
        replace(registration, endpoint="/history?credential=x")
    with pytest.raises(ValueError):
        replace(registration, hostname="*.example.invalid")


def test_passive_frame_requires_exact_reviewed_signature():
    registration = replace(
        reference_registry().registrations[0],
        channel="websocket",
        endpoint="/stream",
        frame_signature="bet.updated.v1",
    )
    registry = ReaderRegistry((registration,))
    value = envelope(
        source={
            "channel": "websocket",
            "direction": "received_frame",
            "endpoint": "/stream",
            "frame_signature": "bet.updated.v1",
        }
    )
    assert registry.read(value).bets
    value["source"]["frame_signature"] = "bet.updated.v2"
    with pytest.raises(SchemaDriftError):
        registry.read(value)
    with pytest.raises(ValueError):
        SourceLocation(channel="websocket", direction="observed_response", endpoint="/stream")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"status": "EXOTIC"}, "unsupported_market"),
        ({"selections": []}, "schema_drift"),
        ({"returned": "2"}, "schema_drift"),
        ({"status": "WIN"}, "schema_drift"),
        ({"status": "LOSE", "returned": "1"}, "schema_drift"),
        ({"revised_at": "2025-01-01T00:00:00Z"}, "schema_drift"),
        ({"stake": "0"}, "schema_drift"),
        ({"stake": "1.005"}, "schema_drift"),
        ({"odds": "2.1234567"}, "schema_drift"),
        ({"currency": "USD"}, "schema_drift"),
    ],
)
def test_invalid_financial_data_cannot_be_guessed(changes, code):
    with pytest.raises(ReaderError) as failure:
        reference_registry().read(envelope(source(**changes)))
    assert failure.value.code.value == code


def test_missing_required_fields_and_duplicate_id_are_typed():
    raw = source()
    del raw["bets"][0]["stake"]
    with pytest.raises(ReaderError, match="incomplete_payload"):
        reference_registry().read(envelope(raw))
    raw = source()
    raw["bets"].append(copy.deepcopy(raw["bets"][0]))
    with pytest.raises(SchemaDriftError, match="duplicate_external_identity"):
        reference_registry().read(envelope(raw))
    with pytest.raises(ReaderError, match="incomplete_payload"):
        reference_registry().read({})
    with pytest.raises(ReaderError, match="incomplete_payload"):
        reference_registry().read(None)


@pytest.mark.parametrize(
    "value", [True, 1.2, "1,234.56", "1e3", "-1", "NaN", "Infinity", "R$2,00", {}, Decimal("NaN")]
)
def test_ambiguous_or_inexact_numbers_are_rejected(value):
    with pytest.raises(SchemaDriftError):
        decimal_number(value)


def test_raw_decimal_numbers_are_lossless_and_brazilian_money_is_exact():
    with pytest.raises(SchemaDriftError, match="fractional_centavo"):
        money("100.0000000000000000000000000000000000001")
    assert decode_raw('{"value":1.234567890123456789}') == {
        "value": Decimal("1.234567890123456789")
    }
    assert money("R$ 1.234,56") == 123456
    assert money(Decimal("0.29")) == 29
    assert decimal_number("2,5") == Decimal("2.5")
    with pytest.raises(SchemaDriftError, match="duplicate_json_key"):
        decode_raw('{"x":1,"x":2}')
    with pytest.raises(SchemaDriftError):
        decode_raw("not JSON")


@pytest.mark.parametrize(
    "value",
    [
        {"access_token": "synthetic"},
        {"nested": {"customerId": "42"}},
        {"note": "Bearer synthetic-private"},
        {"note": "synthetic@example.invalid"},
        {"note": "123.456.789-00"},
        {"note": "+55 (11) 99999-9999"},
        {"note": "https://reader.example.invalid/a?key=synthetic"},
        {"note": "https://[bad"},
        {"value": float("nan")},
        {"value": object()},
        {"x": "\ud800"},
        {"x": "\x01"},
        {"x": "aZ8qP1kY4mN7vT2sL9hG6cB0rD5wF3jU"},
    ],
)
def test_safety_rejects_pii_credentials_and_invalid_values(value):
    with pytest.raises(UnsafePayloadError):
        check_safe(value)


def test_bounds_and_unknown_schema_secrets_do_not_bypass_screening():
    check_safe({"__typename": "SportsbookExpandableLegCardGroup"})
    with pytest.raises(UnsafePayloadError):
        check_safe({"__typename": "aZ8qP1kY4mN7vT2sL9hG6cB0rD5wF3jU"})
    with pytest.raises(UnsafePayloadError, match="array_limit"):
        check_safe([0] * 2001)
    with pytest.raises(UnsafePayloadError, match="object_limit"):
        check_safe({str(i): i for i in range(2001)})
    value = "safe"
    for _ in range(22):
        value = [value]
    with pytest.raises(UnsafePayloadError, match="depth_limit"):
        check_safe(value)
    with pytest.raises(UnsafePayloadError):
        reference_registry().read(envelope({"cookie": "synthetic"}, raw_schema_version="future"))
    check_safe({
        "content_hash": digest(b"sample"),
        "event_id": "unit-event",
        "stake": "R$ 1.234,56",
    })


def test_parser_bugs_empty_output_and_changed_provenance_are_closed():
    class Broken:
        def parse(self, value):
            raise RuntimeError("Bearer synthetic-sensitive-error")

    class Empty:
        def parse(self, value):
            return []

    class Foreign:
        def parse(self, value):
            bet = reference_registry().read(value).bets[0]
            return [bet.model_copy(update={"brand": "other"})]

    registration = reference_registry().registrations[0]
    for reader, code in [
        (Broken(), "schema_drift"),
        (Empty(), "incomplete_payload"),
        (Foreign(), "schema_drift"),
    ]:
        with pytest.raises(ReaderError) as failure:
            ReaderRegistry((replace(registration, reader=reader),)).read(envelope())
        assert failure.value.code.value == code
        assert "sensitive" not in str(failure.value)
    assert SchemaDriftError("Bearer secret").reason == "reader_failure"
    # Models also cross the same validation and screening path.
    assert reference_registry().read(ReaderEnvelope.model_validate(envelope())).bets
