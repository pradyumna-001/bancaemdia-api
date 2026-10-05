import asyncio
import copy
import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bancaemdia.cli import reconciliar_casa_telegram as cli
from bancaemdia.cli.reconciliacao_report import (
    ALGORITHM,
    CLASSES,
    ReconciliationError,
    candidate_id,
    canonical,
    csv_view,
    read_report,
    seal,
    summary,
    verify,
    write_report,
)


def example():
    return seal({
        "contract_version": 1,
        "algorithm_version": ALGORITHM,
        "filters": cli.Filters(7).json(),
        "batch_size": 1,
        "generated_at": "2026-09-30T01:00:00+00:00",
        "source_high_water_mark": {"max_event_id": 9, "sha256": "synthetic"},
        "current_totals": {"Betano": dict.fromkeys(cli.METRICS, 100)},
        "projected_totals": {"Betano": dict.fromkeys(cli.METRICS, 90)},
        "counts": dict.fromkeys(CLASSES, 0),
        "candidates": [
            {
                "candidate_id": candidate_id(7, 10, 11),
                "house_id": 10,
                "telegram_id": 11,
                "score": 100,
                "classification": "exact",
                "action": "consolidate",
                "reason": "exact",
                "house": "Betano",
                "matched": ["ticket"],
                "conflicts": [],
                "removed_contribution": {key: 10 for key in cli.METRICS if key != "bet_count"},
            }
        ],
    })


def test_hash_has_independent_canonical_byte_oracle():
    data = example()
    body = {k: v for k, v in data.items() if k != "sha256"}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert data["sha256"] == expected
    verify(data, expected)
    assert canonical({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


@pytest.mark.parametrize(
    "field",
    [
        "filters",
        "candidates",
        "generated_at",
        "current_totals",
        "projected_totals",
        "source_high_water_mark",
        "batch_size",
    ],
)
def test_any_reviewed_field_tampering_is_refused(field):
    data = example()
    changed = copy.deepcopy(data)
    changed[field] = "altered"
    with pytest.raises(ReconciliationError, match="modificado"):
        verify(changed, data["sha256"])


def test_rehash_does_not_replace_explicitly_approved_hash():
    data = example()
    changed = seal({**data, "batch_size": 2})
    with pytest.raises(ReconciliationError, match="aprovado"):
        verify(changed, data["sha256"])


@pytest.mark.parametrize("patch", [{"contract_version": 99}, {"algorithm_version": "future/2"}])
def test_unknown_report_versions_are_rejected(patch):
    data = seal({**example(), **patch})
    with pytest.raises(ReconciliationError, match="Versão"):
        verify(data, data["sha256"])


def test_candidate_identity_is_stable_and_tenant_and_algorithm_scoped():
    assert candidate_id(7, 10, 11) == hashlib.sha256(b'[1,"casa-telegram/1",7,10,11]').hexdigest()
    assert len({candidate_id(7, 10, 11), candidate_id(8, 10, 11), candidate_id(7, 11, 10)}) == 3


def test_report_roundtrip_never_overwrites_reviewed_file(tmp_path):
    path = tmp_path / "review.json"
    data = example()
    write_report(path, data)
    assert read_report(path) == data
    with pytest.raises(FileExistsError):
        write_report(path, seal({**data, "batch_size": 2}))
    assert read_report(path) == data


@pytest.mark.parametrize(
    "content", ['{"sha256":"a","sha256":"b"}', "[]", "{", '{"candidates":[{"id":1,"id":2}]}']
)
def test_ambiguous_or_invalid_json_is_refused(tmp_path, content):
    path = tmp_path / "review.json"
    path.write_text(content)
    with pytest.raises(ReconciliationError):
        read_report(path)


def test_csv_contains_review_metadata_and_defuses_spreadsheet_formulas():
    data = example()
    data["candidates"][0]["reason"] = "=SENSITIVE()"
    row = next(csv.DictReader(io.StringIO(csv_view(data))))
    assert row["reason"] == "'=SENSITIVE()"
    assert row["sha256"] == data["sha256"]
    assert json.loads(row["filters"]) == data["filters"]
    assert json.loads(row["current_totals"]) == data["current_totals"]
    assert json.loads(row["source_high_water_mark"]) == data["source_high_water_mark"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_json_is_not_hashable(value):
    with pytest.raises(ValueError):
        canonical({"score": value})


@pytest.mark.parametrize("user", [0, -1, 2**63])
def test_invalid_user_never_reaches_database(user):
    with pytest.raises(ReconciliationError):
        cli.Filters(user).validate()


def test_range_uses_game_date_and_local_inclusive_calendar_end():
    filters = cli.Filters(7, "2026-09-22", "2026-09-22", "betano")
    filters.validate()
    assert cli.selected(
        {"house": "Betano", "game": datetime(2026, 9, 23, 2, 59, tzinfo=UTC)}, filters
    )
    assert not cli.selected(
        {"house": "Betano", "game": datetime(2026, 9, 23, 3, tzinfo=UTC)}, filters
    )
    assert not cli.selected({"house": "Betano", "game": None}, filters)
    assert not cli.selected(
        {"house": "Superbet", "game": datetime(2026, 9, 22, 4, tzinfo=UTC)}, filters
    )


@pytest.mark.parametrize(
    "filters",
    [
        cli.Filters(7, "bad"),
        cli.Filters(7, "2026-10-01", "2026-09-01"),
        cli.Filters(7, algorithm_version="x"),
        cli.Filters(7, casa=""),
    ],
)
def test_invalid_filters(filters):
    with pytest.raises(ReconciliationError):
        filters.validate()


@pytest.mark.parametrize("batch", [0, -1, 201])
async def test_unbounded_batch_refused_before_opening_connection(batch):
    with pytest.raises(ReconciliationError, match="batch-size"):
        await cli.dry_run(None, cli.Filters(7), batch)


def test_dry_run_is_default_and_apply_is_explicit():
    args = cli.parser().parse_args(["--usuario-id", "7", "--report", "review.json"])
    assert not args.apply and args.sha256 is None
    assert args.batch_size == 100


def test_terminal_summary_has_only_counts_and_hash():
    data = example()
    message = summary(data)
    assert data["sha256"] in message and "Nenhuma escrita" in message
    assert all(cls + "=" in message for cls in CLASSES)
    assert "house_id" not in message and "SENSITIVE" not in message


def test_integrity_projection_preserves_source_count_and_only_subtracts_approved_exact():
    data = example()
    assert cli.expected_totals(data, 0) == data["current_totals"]
    after = cli.expected_totals(data, 1)["Betano"]
    assert after["bet_count"] == 100
    assert all(after[key] == 90 for key in cli.METRICS if key != "bet_count")
    data["candidates"][0]["action"] = "review"
    assert cli.expected_totals(data, 1) == data["current_totals"]


def test_unresolved_profit_is_not_an_invented_loss():
    source = {
        "bet": SimpleNamespace(
            stake_centavos=10000, retorno_centavos=None, estado="PENDENTE", revisao_grave=False
        )
    }
    assert cli.contribution(source) == {
        "financial_fact_count": 1,
        "stake": 10000,
        "return": 0,
        "profit": 0,
        "unresolved_value": 10000,
    }


def test_missing_prerequisite_fails_closed_without_legacy_fallback(monkeypatch):
    def missing(_name):
        raise ImportError("synthetic missing consolidation")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    with pytest.raises(ReconciliationError, match="PR #166"):
        cli.runtime()


def test_operational_exception_never_prints_driver_secrets_or_payloads(monkeypatch, capsys):
    async def failure(_args):
        await asyncio.sleep(0)
        raise RuntimeError("SECRET_SYNTHETIC_TOKEN postgres://private:password@internal RAW_MEDIA")

    monkeypatch.setattr(cli, "run", failure)
    assert cli.main(["--usuario-id", "7", "--report", "review.json"]) == 3
    captured = capsys.readouterr()
    assert "chunk atual revertido" in captured.err
    assert (
        "SECRET" not in captured.err
        and "password" not in captured.err
        and "RAW_MEDIA" not in captured.err
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--apply"],
        ["--apply", "--sha256", "a", "--dry-run"],
        ["--apply", "--sha256", "a", "--csv", "view.csv"],
        ["--sha256", "a"],
        ["--max-chunks", "1"],
        ["--apply", "--sha256", "a", "--max-chunks", "0"],
    ],
)
async def test_inconsistent_write_flags_refused_before_connecting(options):
    args = cli.parser().parse_args(["--usuario-id", "7", "--report", "review.json", *options])
    with pytest.raises(ReconciliationError):
        await cli.run(args)
