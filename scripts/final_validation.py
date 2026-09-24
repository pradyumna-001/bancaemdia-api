"""Fail-closed go-live gate for issue #44; never deploys or triggers chaos tests."""

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SHA = re.compile(r"[0-9a-f]{40}\Z")
SIMPLE_CHECKS = (
    "trace_chain",
    "custom_metrics",
    "json_logs",
    "alert_to_slack",
    "circuit_breaker",
    "runbooks_reviewed",
    "oncall_confirmed",
    "stakeholder_signoff",
)
PHASE1_CHECKS = (
    *(name for name in SIMPLE_CHECKS if name != "alert_to_slack"),
    "budget_alert",
    "daily_backup",
    "restore_drill",
    "weekly_snapshot",
    "private_storage_access",
)
EXPECTED_HEADERS = {
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'",
    "strict-transport-security": "max-age=31536000; includeSubDomains; preload",
    "x-frame-options": "DENY",
}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _evidence(value: Any, base: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if value.startswith("https://"):
        try:
            url = urlsplit(value)
            return (
                bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
            )
        except ValueError:
            return False
    path = Path(value)
    return not path.is_absolute() and (base / path).is_file()


def _read_json(path: Path) -> dict[str, Any]:
    return _object(json.loads(path.read_text(encoding="utf-8")))


def _artifact_path(manifest: dict[str, Any], key: str, base: Path) -> Path | None:
    value = _object(manifest.get("artifacts")).get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else base / path


def _integrity(manifest: dict[str, Any], base: Path, sha: str, phase1: bool) -> Check:
    path = _artifact_path(manifest, "integrity", base)
    if path is None or not path.is_file():
        return Check("integridade", False, "relatório de conferir_numeros ausente")
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return Check("integridade", False, "relatório inválido")
    banks = _number(data.get("bancas"))
    bets = _number(data.get("apostas_reprocessadas"))
    divergent = _number(data.get("divergencias"))
    errors = _number(data.get("erros"))
    passed = (
        data.get("source") == "conferir_numeros.py"
        and data.get("environment") == ("testing" if phase1 else "staging")
        and data.get("mode") == ("phase1-gate" if phase1 else "staging-gate")
        and data.get("release_sha") == sha
        and data.get("passed") is True
        and banks is not None
        and banks >= 5
        and bets is not None
        and bets >= 16_000
        and divergent == 0
        and errors == 0
    )
    return Check("integridade", passed, f"{banks} bancas; {bets} apostas; {divergent} divergências")


def _contracts(manifest: dict[str, Any], base: Path, sha: str) -> Check:
    path = _artifact_path(manifest, "contracts", base)
    meta = _object(manifest.get("contracts"))
    if path is None or not path.is_file():
        return Check("contratos API", False, "JUnit dos testes de contrato ausente")
    try:
        root = ET.parse(path).getroot()
        cases = list(root.iter("testcase"))
    except (OSError, ET.ParseError):
        return Check("contratos API", False, "JUnit inválido")

    def acceptable(case: ET.Element) -> bool:
        skipped = case.find("skipped")
        known_ungeneratable_negative = (
            case.get("name")
            == "test_schemathesis_invalid_protected_requests[DELETE /api/v1/usuario/me]"
            and skipped is not None
            and skipped.get("message") == "Impossible to generate negative test cases"
        )
        return (
            case.get("classname", "").startswith("tests.contract.")
            and case.find("failure") is None
            and case.find("error") is None
            and (skipped is None or known_ungeneratable_negative)
        )

    passed = (
        meta.get("release_sha") == sha
        and _evidence(meta.get("run_url"), base)
        and bool(cases)
        and any(case.find("skipped") is None for case in cases)
        and all(acceptable(case) for case in cases)
    )
    skipped_count = sum(case.find("skipped") is not None for case in cases)
    return Check("contratos API", passed, f"{len(cases)} casos; {skipped_count} skip justificado")


def _load(manifest: dict[str, Any], base: Path, sha: str, phase1: bool) -> Check:
    path = _artifact_path(manifest, "load", base)
    if path is None or not path.is_file():
        return Check("carga k6", False, "resumo k6 ausente")
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return Check("carga k6", False, "resumo k6 inválido")
    meta = _object(data.get("bancaemdia"))
    metrics = _object(data.get("metrics"))
    duration = _object(metrics.get("http_req_duration"))
    failures = _object(metrics.get("http_req_failed"))
    requests = _object(metrics.get("http_reqs"))
    p95 = _number(_object(duration.get("values")).get("p(95)"))
    error_rate = _number(_object(failures.get("values")).get("rate"))
    count = _number(_object(requests.get("values")).get("count"))
    run_ms = _number(_object(data.get("state")).get("testRunDurationMs"))
    threshold_groups = [
        _object(metric.get("thresholds"))
        for metric in metrics.values()
        if isinstance(metric, dict) and metric.get("thresholds")
    ]
    thresholds_ok = bool(threshold_groups) and all(
        isinstance(threshold, dict) and threshold.get("ok") is True
        for group in threshold_groups
        for threshold in group.values()
    )
    passed = (
        meta.get("profile") == "all"
        and meta.get("environment") == ("testing" if phase1 else "staging")
        and meta.get("release_sha") == sha
        and p95 is not None
        and p95 < 1000
        and error_rate is not None
        and error_rate < 0.01
        and count is not None
        and count > 0
        and run_ms is not None
        and run_ms >= 50 * 60 * 1000
        and thresholds_ok
    )
    return Check("carga k6", passed, f"P95={p95} ms; erros={error_rate}; duração={run_ms} ms")


def _operational(manifest: dict[str, Any], base: Path, phase1: bool) -> list[Check]:
    values = _object(manifest.get("checks"))
    checks: list[Check] = []
    for name in PHASE1_CHECKS if phase1 else SIMPLE_CHECKS:
        item = _object(values.get(name))
        checks.append(
            Check(
                name,
                item.get("passed") is True and _evidence(item.get("evidence"), base),
                "atestado com evidência",
            )
        )
    measured: dict[str, tuple[str, Callable[[float], bool]]] = {
        "deploy": ("healthy_seconds", lambda value: value < 600),
        "rollback": ("healthy_seconds", lambda value: value < 300),
        "rls": ("cross_tenant_rows", lambda value: value == 0),
        "open_sev1_sev2": ("count", lambda value: value == 0),
        "rate_limit": ("status", lambda value: value == 429),
        "auth_expired": ("status", lambda value: value == 401),
        "auth_invalid": ("status", lambda value: value == 401),
    }
    if not phase1:
        measured["rds_failover"] = ("recovery_seconds", lambda value: value < 30)
    for name, (field, condition) in measured.items():
        item = _object(values.get(name))
        value = _number(item.get(field))
        passed = value is not None and condition(value) and _evidence(item.get("evidence"), base)
        if name == "rate_limit":
            headers = _object(item.get("headers"))
            passed = passed and any(key.lower() == "retry-after" for key in headers)
        checks.append(Check(name, passed, f"{field}={value}; evidência exigida"))
    item = _object(values.get("security_headers"))
    headers = {str(key).lower(): value for key, value in _object(item.get("headers")).items()}
    checks.append(
        Check(
            "security_headers",
            all(headers.get(key) == expected for key, expected in EXPECTED_HEADERS.items())
            and _evidence(item.get("evidence"), base),
            "CSP, HSTS e X-Frame-Options exatos; evidência exigida",
        )
    )
    return checks


def _cost(manifest: dict[str, Any], base: Path, phase1: bool) -> Check:
    item = _object(manifest.get("cost"))
    hours = _number(item.get("observed_hours"))
    infra = _number(item.get("infrastructure_usd"))
    ai = _number(item.get("ai_usd"))
    bets = _number(item.get("bets_processed"))
    infra_cap = _number(item.get("approved_infra_monthly_ceiling_usd"))
    ai_cap = _number(item.get("approved_ai_per_bet_ceiling_usd"))
    projected = infra / hours * 24 * 30 if infra is not None and hours and hours > 0 else None
    per_bet = ai / bets if ai is not None and bets and bets > 0 else None
    if phase1:
        fx = _number(item.get("usd_to_brl"))
        total_brl = (
            (infra + ai) / hours * 24 * 30 * fx
            if infra is not None and ai is not None and hours and hours > 0 and fx is not None
            else None
        )
        passed = (
            hours is not None
            and hours >= 24
            and infra is not None
            and infra >= 0
            and ai is not None
            and ai >= 0
            and bets is not None
            and bets > 0
            and fx is not None
            and fx > 0
            and total_brl is not None
            and total_brl <= 200
            and _evidence(item.get("evidence"), base)
            and _evidence(item.get("approval_evidence"), base)
            and _evidence(item.get("fx_evidence"), base)
        )
        return Check("custos 24h", passed, f"total/mês ~R$ {total_brl}")
    passed = (
        hours is not None
        and hours >= 24
        and infra is not None
        and infra >= 0
        and ai is not None
        and ai >= 0
        and bets is not None
        and bets > 0
        and infra_cap is not None
        and infra_cap >= 0
        and ai_cap is not None
        and ai_cap >= 0
        and projected is not None
        and projected <= infra_cap
        and per_bet is not None
        and per_bet <= ai_cap
        and _evidence(item.get("evidence"), base)
        and _evidence(item.get("approval_evidence"), base)
    )
    return Check("custos 24h", passed, f"infra/mês ~US$ {projected}; IA/aposta ~US$ {per_bet}")


def evaluate(manifest: dict[str, Any], base: Path) -> list[Check]:
    phase1 = manifest.get("phase") == "lightsail-phase1"
    sha = manifest.get("release_sha")
    if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
        sha = ""
    checks = [
        Check(
            "identidade da liberação",
            manifest.get("environment") == ("production" if phase1 else "staging") and bool(sha),
            "ambiente e SHA imutável exigidos",
        ),
        _integrity(manifest, base, sha, phase1),
        _contracts(manifest, base, sha),
        _load(manifest, base, sha, phase1),
        *_operational(manifest, base, phase1),
        _cost(manifest, base, phase1),
    ]
    return checks


def render(checks: list[Check]) -> str:
    status = "GO CANDIDATE" if all(check.passed for check in checks) else "NO-GO"
    lines = [
        f"# Validação final — {status}",
        "",
        "Evidências declaradas pelo operador; links exigem revisão humana.",
        "",
    ]
    for check in checks:
        lines.append(f"- {'PASS' if check.passed else 'FAIL'} **{check.name}** — {check.detail}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        manifest = _read_json(args.evidence)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"Cannot read evidence manifest: {error}\n")
        return 2
    checks = evaluate(manifest, args.evidence.resolve().parent)
    output = render(checks)
    if args.report:
        args.report.write_text(output, encoding="utf-8")
    else:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
        sys.stdout.write(output)
    return int(not all(check.passed for check in checks))


if __name__ == "__main__":
    raise SystemExit(main())
