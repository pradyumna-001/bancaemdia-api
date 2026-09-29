"""Compatibility adapter for callers of the previous unversioned comparator."""

from dataclasses import dataclass
from typing import Any

from bancaemdia.domain.cruzamento import compare, normalize


@dataclass(frozen=True)
class Veredicto:
    resultado: str
    motivo: str | None = None


def comparar(nova: dict[str, Any], existente: dict[str, Any]) -> Veredicto:
    result = compare(normalize(nova), normalize(existente))
    return Veredicto(
        {"exact": "igual", "probable": "duvida", "incompatible": "nova"}[result.classification],
        result.explanation,
    )
