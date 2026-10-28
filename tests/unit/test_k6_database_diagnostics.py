"""Database timing evidence must never publish raw statements or bound secrets."""

import importlib.util
import json
import math
from pathlib import Path
from typing import Any


def test_replica_startup_failure_logs_are_bounded_owned_and_redacted(
    tmp_path, monkeypatch, capsys
) -> None:
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[2] / "scripts/k6_staging.py"
    spec = importlib.util.spec_from_file_location("bootstrap_diagnostic", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def logs(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            stdout="FATAL bootstrap rejected SENTINEL-admin SENTINEL-replica", stderr=""
        )

    monkeypatch.setattr(module.subprocess, "run", logs)
    module.database_startup_diagnostics(
        tmp_path,
        {
            "name": "k6-0123456789abcdef",
            "admin_password": "SENTINEL-admin",
            "replica_password": "SENTINEL-replica",
        },
    )
    captured = capsys.readouterr().out
    assert "FATAL bootstrap rejected" in captured and "SENTINEL" not in captured
    assert "SENTINEL" not in (tmp_path / "database-startup.log").read_text(encoding="utf-8")
    assert calls == [
        ["docker", "logs", "--tail", "60", "k6-0123456789abcdef-primary"],
        ["docker", "logs", "--tail", "60", "k6-0123456789abcdef-replica"],
    ]


def test_numeric_database_measurement_discards_queries_literals_and_unknown_fields() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts/k6_staging.py"
    spec = importlib.util.spec_from_file_location("database_diagnostic", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row: dict[str, Any] = {
        "queryid": 9007199254740993,
        "query": "UPDATE coleta_instalacoes SET token_hash='SENTINEL-private-token' /* raw payload */",
        "calls": 5000,
        "rows": 5000,
        "total_exec_time": 100.25,
        "mean_exec_time": 0.02005,
        "max_exec_time": 5.0,
        "shared_blks_read": 4,
        "shared_blks_hit": 6000,
        "temp_blks_written": 0,
        "private_extra": "SENTINEL-private-password",
    }
    result = module.statement_measurement(row)
    assert result["query_id"] == "9007199254740993"
    assert result["operation"] == "UPDATE"
    assert result["resources"] == ["coleta_instalacoes"]
    assert result["calls"] == 5000
    assert math.isclose(result["mean_exec_time"], 0.02005)
    encoded = json.dumps(result)
    assert all(
        value not in encoded
        for value in (
            "SENTINEL",
            "token_hash",
            "private_extra",
            "raw payload",
            'query"',
        )
    )
