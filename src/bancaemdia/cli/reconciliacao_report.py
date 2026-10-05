"""Versioned review artifact: canonical JSON is the authority; CSV is a view."""

import csv
import hashlib
import io
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

CONTRACT = 1
ALGORITHM = "casa-telegram/1"
MAX_BATCH = 200
CLASSES = ("exact", "probable", "incompatible", "competing", "already-consolidated", "error")


class ReconciliationError(RuntimeError):
    """Safe operator diagnostic, without source payloads or connection secrets."""


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("Unsupported report value")


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def seal(report: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in report.items() if key != "sha256"}
    return {**body, "sha256": digest(body)}


def verify(report: dict[str, Any], approved_hash: str) -> None:
    if len(approved_hash) != 64 or report.get("sha256") != approved_hash:
        raise ReconciliationError("Hash aprovado não corresponde ao relatório")
    if seal(report)["sha256"] != approved_hash:
        raise ReconciliationError("Relatório modificado")
    if report.get("contract_version") != CONTRACT or report.get("algorithm_version") != ALGORITHM:
        raise ReconciliationError("Versão do contrato/algoritmo não suportada")


def candidate_id(user: int, house: int, tip: int) -> str:
    return digest([CONTRACT, ALGORITHM, user, house, tip])


def write_report(path: Path, report: dict[str, Any]) -> None:
    # Never overwrite the reviewed artifact, even on an accidental rerun.
    with path.open("xb") as file:
        file.write(canonical(report) + b"\n")
    path.chmod(0o600)


def read_report(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReconciliationError("Chave JSON duplicada")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    except (ValueError, OSError) as error:
        raise ReconciliationError("Relatório JSON inválido ou inacessível") from error
    if not isinstance(value, dict):
        raise ReconciliationError("Relatório deve ser um objeto JSON")
    return value


def csv_view(report: dict[str, Any]) -> str:
    stream = io.StringIO(newline="")
    fields = [
        "candidate_id",
        "house_id",
        "telegram_id",
        "score",
        "classification",
        "matched",
        "conflicts",
        "action",
        "reason",
        "algorithm_version",
        "contract_version",
        "filters",
        "generated_at",
        "source_high_water_mark",
        "current_totals",
        "projected_totals",
        "sha256",
    ]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for candidate in report["candidates"] or [{}]:
        row = {field: candidate.get(field, report.get(field, "")) for field in fields}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                row[key] = canonical(value).decode()
            elif isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                row[key] = "'" + value
        writer.writerow(row)
    return stream.getvalue()


def summary(report: dict[str, Any]) -> str:
    counts = ", ".join(f"{key}={report['counts'][key]}" for key in CLASSES)
    return f"dry-run: {counts}\nSHA-256: {report['sha256']}\nNenhuma escrita no banco.\n"
