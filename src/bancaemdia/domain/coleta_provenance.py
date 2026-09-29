"""Conservative source provenance. Receipt/capture clocks never order financial revisions."""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from bancaemdia.coleta.leitura import Coletada
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.domain.vocabulario import CASAS

MAX_ITEM_BYTES = 128 * 1024
MAX_BATCH_BYTES = 1024 * 1024
SECRET_KEY = re.compile(
    r"token|cookie|password|passwd|authorization|secret|session|credential|login|email|cpf|customerid|headers",
    re.I,
)
SECRET_VALUE = re.compile(
    r"(?:cti_|cpc_)[A-Za-z0-9_-]+|Bearer\s+\S+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)
HOSTS = {
    domain: next(
        (
            key
            for key in ("betano", "superbet", "betmgm", "betfair", "kto", "esportiva")
            if casa_canonica(key) == name
        ),
        domain.split(".", maxsplit=1)[0],
    )
    for name, domain in CASAS
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def safe_payload(value: Any, depth: int = 0) -> bool:
    if depth > 20:
        return False
    if isinstance(value, dict):
        return all(
            not SECRET_KEY.search(re.sub(r"[^a-z0-9]", "", str(key).lower()))
            and safe_payload(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(safe_payload(item, depth + 1) for item in value)
    if isinstance(value, str):
        return "\x00" not in value and not SECRET_VALUE.search(value)
    return value is None or isinstance(value, (int, float, bool))


def instant(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = datetime.fromtimestamp(value / 1000, UTC)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                return None
            result = result.astimezone(UTC)
        else:
            return None
        return (
            result
            if datetime(2000, 1, 1, tzinfo=UTC)
            <= result
            <= datetime.now(UTC) + timedelta(minutes=5)
            else None
        )
    except (ValueError, OverflowError, OSError):
        return None


def source_times(casa: str, raw: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    placement = {
        "betano": "placedAt",
        "superbet": "dateReceived",
        "betmgm": "deliveryDate",
        "kto": "placedDate",
        "esportiva": "createdDate",
    }.get(casa)
    occurred = instant(raw.get(placement)) if placement else None
    # Only a measured source revision field is accepted. Other adapters may create
    # an initial snapshot, but conflicting revisions with no clock require review.
    revised = instant(raw.get("settledAt")) if casa == "betano" else None
    if casa == "betano" and raw.get("settledAt") is not None and revised is None:
        return occurred, None
    if revised and occurred and revised < occurred:
        return occurred, None
    return occurred, revised or occurred


def canonical_ticket(coletada: Coletada) -> str:
    parsed = asdict(coletada)
    parsed.pop("bruto")
    return content_hash(parsed)
