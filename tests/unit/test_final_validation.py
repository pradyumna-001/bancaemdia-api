import json
from dataclasses import replace
from pathlib import Path

import pytest

from bancaemdia.cli.replay import ResultadoReplay
from scripts import conferir_numeros, final_validation

SHA = "a" * 40
URL = "https://example.test/evidence"


def _manifest(base: Path) -> dict:
    (base / "integrity.json").write_text(
        json.dumps({
            "source": "conferir_numeros.py",
            "environment": "staging",
            "mode": "staging-gate",
            "release_sha": SHA,
            "passed": True,
            "bancas": 5,
            "apostas_reprocessadas": 16_000,
            "divergencias": 0,
            "erros": 0,
        }),
        encoding="utf-8",
    )
    (base / "contracts.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.contract.test_openapi" '
        'name="test_health"/>'
        '<testcase classname="tests.contract.test_openapi" '
        'name="test_schema"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    (base / "load.json").write_text(
        json.dumps({
            "bancaemdia": {"profile": "all", "environment": "staging", "release_sha": SHA},
            "state": {"testRunDurationMs": 3_300_000},
            "metrics": {
                "http_req_duration": {
                    "values": {"p(95)": 999},
                    "thresholds": {"p(95)<1000": {"ok": True}},
                },
                "http_req_failed": {
                    "values": {"rate": 0.009},
                    "thresholds": {"rate<0.01": {"ok": True}},
                },
                "http_reqs": {"values": {"count": 100}},
            },
        }),
        encoding="utf-8",
    )
    checks = {name: {"passed": True, "evidence": URL} for name in final_validation.SIMPLE_CHECKS}
    checks.update({
        "rds_failover": {"recovery_seconds": 29, "evidence": URL},
        "deploy": {"healthy_seconds": 599, "evidence": URL},
        "rollback": {"healthy_seconds": 299, "evidence": URL},
        "rls": {"cross_tenant_rows": 0, "evidence": URL},
        "open_sev1_sev2": {"count": 0, "evidence": URL},
        "rate_limit": {"status": 429, "headers": {"Retry-After": "60"}, "evidence": URL},
        "auth_expired": {"status": 401, "evidence": URL},
        "auth_invalid": {"status": 401, "evidence": URL},
        "security_headers": {
            "headers": final_validation.EXPECTED_HEADERS,
            "evidence": URL,
        },
    })
    return {
        "environment": "staging",
        "release_sha": SHA,
        "artifacts": {
            "integrity": "integrity.json",
            "contracts": "contracts.xml",
            "load": "load.json",
        },
        "contracts": {"release_sha": SHA, "run_url": URL},
        "checks": checks,
        "cost": {
            "observed_hours": 24,
            "infrastructure_usd": 1,
            "ai_usd": 1,
            "bets_processed": 1000,
            "approved_infra_monthly_ceiling_usd": 30,
            "approved_ai_per_bet_ceiling_usd": 0.001,
            "evidence": URL,
            "approval_evidence": URL,
        },
    }


def _failed(manifest: dict, base: Path) -> set[str]:
    return {check.name for check in final_validation.evaluate(manifest, base) if not check.passed}


def test_go_live_gate_requires_every_piece_of_evidence(tmp_path: Path) -> None:
    assert _failed({}, tmp_path)
    manifest = _manifest(tmp_path)
    assert _failed(manifest, tmp_path) == set()
    assert "GO CANDIDATE" in final_validation.render(final_validation.evaluate(manifest, tmp_path))


def test_go_live_gate_rejects_wrong_sha_thresholds_and_missing_approval(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["release_sha"] = "b" * 40
    assert {"integridade", "contratos API", "carga k6"} <= _failed(manifest, tmp_path)
    manifest["release_sha"] = SHA

    load = json.loads((tmp_path / "load.json").read_text(encoding="utf-8"))
    load["metrics"]["http_req_duration"]["values"]["p(95)"] = 1000
    (tmp_path / "load.json").write_text(json.dumps(load), encoding="utf-8")
    assert "carga k6" in _failed(manifest, tmp_path)

    manifest["cost"]["approval_evidence"] = ""
    assert "custos 24h" in _failed(manifest, tmp_path)
    manifest["checks"]["stakeholder_signoff"]["evidence"] = ""
    assert "stakeholder_signoff" in _failed(manifest, tmp_path)


def test_go_live_gate_rejects_skipped_contracts_and_weak_proof(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / "contracts.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.contract.test_openapi" '
        'name="test_schema"><skipped/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    assert "contratos API" in _failed(manifest, tmp_path)
    manifest["checks"]["trace_chain"]["evidence"] = "https://user:secret@example.test/path"
    assert "trace_chain" in _failed(manifest, tmp_path)


def test_only_known_ungeneratable_negative_contract_is_accepted(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / "contracts.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.contract.test_openapi" '
        'name="test_health"/>'
        '<testcase classname="tests.contract.test_openapi" '
        'name="test_schemathesis_invalid_protected_requests[DELETE /api/v1/usuario/me]">'
        '<skipped message="Impossible to generate negative test cases"/>'
        "</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    assert "contratos API" not in _failed(manifest, tmp_path)


def test_replay_verification_detects_rows_that_dry_run_would_restore() -> None:
    result = ResultadoReplay(usuario_id=1, seco=True)
    assert not conferir_numeros._divergiu(result)
    assert conferir_numeros._divergiu(replace(result, recriadas=1))
    assert conferir_numeros._divergiu(replace(result, movimentos_restaurados=1))
    assert conferir_numeros._divergiu(replace(result, alteradas=1))
    assert conferir_numeros.Conferencia(bancas=5, apostas_reprocessadas=16_000).passou(
        staging_gate=True
    )
    assert not conferir_numeros.Conferencia(bancas=4, apostas_reprocessadas=16_000).passou(
        staging_gate=True
    )


def test_staging_gate_requires_staging_and_immutable_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["conferir_numeros.py", "--todos", "--staging-gate"])
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("RELEASE_SHA", raising=False)
    with pytest.raises(SystemExit) as error:
        conferir_numeros.main()
    assert error.value.code == 2
