"""Versioned, pure evidence assessment. Scores never authorize financial writes."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from bancaemdia.coleta.leitura import FUSO_DA_CASA
from bancaemdia.domain.materializar import casa_canonica

VERSION = "casa-telegram/1"
WINDOW_DAYS = 14
MAX_NEIGHBORS = 200
CONFIG = {
    "version": VERSION,
    "probable_min": 60,
    "exact_min": 90,
    "odd_tolerance": "0.005",
    "exact_seconds": 300,
    "window_days": WINDOW_DAYS,
    "stake_tolerance_cents": 0,
    "max_neighbors": MAX_NEIGHBORS,
}


def instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        # Existing parsers explicitly produce local Brazilian wall time.
        return result.replace(tzinfo=result.tzinfo or FUSO_DA_CASA).astimezone(UTC)
    except ValueError:
        return None


def number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def normalize(raw: dict[str, Any], dictionary: dict[str, str] | None = None) -> dict[str, Any]:
    """Only approved dictionary aliases change text; unknown strings remain literal."""
    dictionary = dictionary or {}

    def name(kind: str, value: Any) -> Any:
        return dictionary.get(f"{kind}:{value}", value)

    legs = raw.get("selecoes") or []
    normalized_legs = []
    for leg in legs[:32]:
        normalized_legs.append({
            "evento": name("evento", leg.get("evento") or raw.get("evento")),
            "mercado": name("mercado", leg.get("mercado")),
            "escolha": leg.get("escolha"),
            "linha": str(number(leg["linha"])) if leg.get("linha") is not None else None,
        })
    return {
        "usuario_id": raw.get("usuario_id"),
        "revisao_grave": raw.get("revisao_grave", False),
        "casa": casa_canonica(raw.get("casa")),
        "ticket": raw.get("identidade_bilhete"),
        "ocorrido_em": raw.get("ocorrido_em") or raw.get("comeca_em"),
        "stake_centavos": raw.get("stake_centavos"),
        "moeda": raw.get("moeda"),
        "odd": raw.get("odd"),
        "estado": raw.get("estado"),
        "evento": name("evento", raw.get("evento")),
        "competicao": name("competicao", raw.get("competicao")),
        "esporte": name("esporte", raw.get("esporte")),
        "mercado": name("mercado", raw.get("mercado_bruto")),
        "escolha": raw.get("descricao"),
        "tipo": raw.get("tipo_aposta"),
        "selecoes": sorted(normalized_legs, key=lambda item: json.dumps(item, sort_keys=True)),
        "estrutura_completa": bool(legs)
        and len(legs) <= 32
        and all(
            leg.get("evento") and leg.get("mercado") and leg.get("escolha")
            for leg in normalized_legs
        ),
    }


@dataclass(frozen=True)
class Candidate:
    version: str
    score: int
    classification: str
    matched: tuple[str, ...]
    conflicts: tuple[str, ...]
    missing: tuple[str, ...]
    explanation: str

    def json(self) -> dict[str, Any]:
        return asdict(self)


def compare(left: dict[str, Any], right: dict[str, Any], version: str = VERSION) -> Candidate:
    if version != VERSION:
        raise ValueError("Unsupported matching algorithm version")
    matched: list[str] = []
    conflicts: list[str] = []
    missing: list[str] = []
    score = 0
    hard = False

    def signal(key: str, weight: int, *, decisive: bool = False) -> None:
        nonlocal score, hard
        a, b = left.get(key), right.get(key)
        if a is None or b is None or a == "" or b == "":
            missing.append(key)
        elif a == b:
            matched.append(key)
            score += weight
        else:
            conflicts.append(key)
            hard = hard or decisive

    signal("usuario_id", 0, decisive=True)
    signal("casa", 10, decisive=True)
    signal("ticket", 50, decisive=True)
    signal("stake_centavos", 15, decisive=True)
    signal("moeda", 0, decisive=True)
    a_time, b_time = instant(left.get("ocorrido_em")), instant(right.get("ocorrido_em"))
    seconds = None if a_time is None or b_time is None else abs((a_time - b_time).total_seconds())
    if seconds is None:
        missing.append("ocorrido_em")
    elif seconds <= 300:
        matched.append("ocorrido_em")
        score += 15
    elif seconds <= WINDOW_DAYS * 86400:
        conflicts.append("janela_temporal")
    else:
        conflicts.append("ocorrido_em")
        hard = True
    odd_a, odd_b = number(left.get("odd")), number(right.get("odd"))
    if odd_a is None or odd_b is None or min(odd_a, odd_b) <= 1:
        missing.append("odd")
    elif abs(odd_a - odd_b) <= Decimal("0.005"):
        matched.append("odd")
        score += 10
    else:
        conflicts.append("odd")
        hard = True
    for key, weight in (
        ("evento", 20),
        ("competicao", 0),
        ("esporte", 0),
        ("mercado", 10),
        ("escolha", 10),
        ("tipo", 5),
    ):
        signal(key, weight)
    if left.get("estrutura_completa") and right.get("estrutura_completa"):
        signal("selecoes", 5)
    else:
        missing.append("estrutura")
    states = {left.get("estado"), right.get("estado")}
    valid = {"PENDENTE", "GREEN", "RED", "MEIO_GREEN", "MEIO_RED", "ANULADA", "CASHOUT"}
    if not states <= valid:
        missing.append("lifecycle")
    elif len(states) == 1 or "PENDENTE" in states:
        matched.append("lifecycle")
    else:
        conflicts.append("lifecycle")
    required = {
        "usuario_id",
        "casa",
        "ticket",
        "stake_centavos",
        "moeda",
        "odd",
        "ocorrido_em",
        "estrutura",
        "tipo",
        "lifecycle",
    }
    if any(
        not isinstance(side.get("stake_centavos"), int) or side["stake_centavos"] <= 0
        for side in (left, right)
    ):
        missing.append("stake_centavos")
    if "ticket" not in matched and {"selecoes", "escolha", "mercado"}.intersection(conflicts):
        hard = True
    score = min(score, 100)
    if left.get("revisao_grave") or right.get("revisao_grave"):
        missing.append("aprovacao")
    required.add("aprovacao")
    if hard or "casa" in missing or "usuario_id" in missing or score < 60:
        classification = "incompatible"
    elif score >= 90 and not conflicts and not required.intersection(missing):
        classification = "exact"
    else:
        classification = "probable"
    explanation = (
        f"{VERSION}: {classification}, {score}/100. "
        f"Concordam: {', '.join(matched) or 'nenhum'}. "
        f"Divergem: {', '.join(conflicts) or 'nenhum'}. "
        f"Ausentes: {', '.join(missing) or 'nenhum'}."
    )
    return Candidate(
        VERSION,
        score,
        classification,
        tuple(matched),
        tuple(conflicts),
        tuple(missing),
        explanation,
    )
