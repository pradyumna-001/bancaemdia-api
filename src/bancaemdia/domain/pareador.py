"""Conservative house-versus-tip matcher; ambiguous bets never count twice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from bancaemdia.domain.materializar import casa_canonica, chave_de_nome

_SEPARADOR = re.compile(r"\s+(?:x|vs?\.?|-|@)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class Veredicto:
    resultado: Literal["nova", "igual", "duvida"]
    motivo: str | None = None


def _jogo(valor: object) -> frozenset[str]:
    if not isinstance(valor, str):
        return frozenset()
    partes = _SEPARADOR.split(valor.strip())
    if len(partes) != 2:
        return frozenset({chave_de_nome(valor)})
    times = frozenset(chave_de_nome(parte) for parte in partes)
    return times if len(times) == 2 and all(times) else frozenset()


def _data(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None


def comparar(nova: dict[str, Any], existente: dict[str, Any]) -> Veredicto:
    """Require same house, confrontation and a plausible day before touching money."""
    casa_nova = casa_canonica(nova.get("casa"))
    casa_antiga = casa_canonica(existente.get("casa"))
    if casa_nova is None or casa_nova != casa_antiga:
        return Veredicto("nova")
    jogo_novo, jogo_antigo = _jogo(nova.get("evento")), _jogo(existente.get("evento"))
    if not jogo_novo or jogo_novo != jogo_antigo:
        return Veredicto("nova")
    data_nova = _data(nova.get("comeca_em") or nova.get("data_aposta"))
    data_antiga = _data(existente.get("comeca_em") or existente.get("data_aposta"))
    if data_nova is None or data_antiga is None:
        return Veredicto("duvida", "mesma casa e jogo, mas falta a data para confirmar o par")
    dias = abs((data_nova.date() - data_antiga.date()).days)
    if dias > 14:
        return Veredicto("nova")
    if dias > 1:
        return Veredicto("duvida", "mesma casa e jogo, mas as datas divergem")
    mercado_novo = chave_de_nome(str(nova.get("mercado_bruto") or ""))
    mercado_antigo = chave_de_nome(str(existente.get("mercado_bruto") or ""))
    descricao_nova = chave_de_nome(str(nova.get("descricao") or ""))
    descricao_antiga = chave_de_nome(str(existente.get("descricao") or ""))
    if mercado_novo and mercado_antigo and descricao_nova and descricao_antiga:
        if mercado_novo == mercado_antigo and descricao_nova == descricao_antiga:
            return Veredicto("igual")
    return Veredicto("duvida", "mesma casa e jogo, mas mercado ou palpite não confirmados")
