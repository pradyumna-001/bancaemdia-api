"""Exact-centavo rollups of account metrics, cash flow and unreconciled bets."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal

from bancaemdia.domain.painel import formatar_decimal, razao_exata


@dataclass(frozen=True)
class AccountMetrics:
    apostas: int = 0
    pendentes: int = 0
    greens: int = 0
    reds: int = 0
    giro_centavos: int = 0
    base_roi_centavos: int = 0
    retorno_centavos: int = 0
    lucro_centavos: int = 0
    exposicao_aberta_centavos: int = 0
    depositos_centavos: int = 0
    saques_centavos: int = 0
    bonus_centavos: int = 0
    transferencias_centavos: int = 0
    ajustes_centavos: int = 0

    @classmethod
    def from_rows(
        cls, bet: dict[str, int | None] | None, movement: dict[str, int | None] | None
    ) -> AccountMetrics:
        return cls(**{
            field.name: int((bet or {}).get(field.name) or (movement or {}).get(field.name) or 0)
            for field in fields(cls)
        })

    @classmethod
    def sum(cls, items: list[AccountMetrics]) -> AccountMetrics:
        return cls(**{
            field.name: sum(getattr(item, field.name) for item in items) for field in fields(cls)
        })

    def as_dict(self) -> dict[str, int | str]:
        result: dict[str, int | str] = {
            field.name: getattr(self, field.name) for field in fields(self)
        }
        roi: Decimal = razao_exata(self.lucro_centavos, self.base_roi_centavos)
        win_rate: Decimal = razao_exata(self.greens, self.greens + self.reds)
        result["roi"] = formatar_decimal(roi)
        result["win_rate"] = formatar_decimal(win_rate)
        return result
