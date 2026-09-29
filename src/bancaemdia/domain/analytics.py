"""Additional dashboard insights over the canonical per-bet financial contributions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from zoneinfo import ZoneInfo

from bancaemdia.domain.painel import MetricasPainel, decimal_exato, formatar_decimal

ODDS_BANDS = (
    ("lt_1_50", None, Decimal("1.50")),
    ("1_50_1_99", Decimal("1.50"), Decimal("2.00")),
    ("2_00_2_99", Decimal("2.00"), Decimal("3.00")),
    ("3_00_4_99", Decimal("3.00"), Decimal("5.00")),
    ("gte_5_00", Decimal("5.00"), None),
)
PERIODOS_DIA = ("madrugada", "manha", "tarde", "noite")


def _inteiro(linha: Mapping[str, object], campo: str) -> int:
    valor = linha.get(campo)
    if valor is None:
        return 0
    convertido = decimal_exato(valor, campo)
    if convertido != convertido.to_integral_value():
        raise ValueError(f"{campo} precisa ser inteiro")
    return int(convertido)


def _metricas(linhas: Iterable[Mapping[str, object]]) -> MetricasPainel:
    return MetricasPainel.somar(MetricasPainel.de_linha(linha) for linha in linhas)


def _resumo(linhas: list[Mapping[str, object]]) -> dict[str, object]:
    metricas = _metricas(linhas)
    return {
        "total_apostas": metricas.total_apostas,
        "pendentes": metricas.pendentes,
        "greens": metricas.greens,
        "reds": metricas.reds,
        "giro_centavos": metricas.giro_centavos,
        "base_roi_centavos": metricas.base_roi_centavos,
        "lucro_centavos": metricas.lucro_centavos,
        "roi": formatar_decimal(metricas.roi),
        "hit_rate": (
            None if metricas.greens + metricas.reds == 0 else formatar_decimal(metricas.win_rate)
        ),
        "resultado_nao_aplicavel": metricas.total_apostas - metricas.greens - metricas.reds,
    }


def _agrupar(
    linhas: list[Mapping[str, object]],
    chave: Callable[[Mapping[str, object]], object],
    *,
    chaves: Iterable[object] = (),
) -> list[dict[str, object]]:
    grupos: dict[object, list[Mapping[str, object]]] = {item: [] for item in chaves}
    for linha in linhas:
        grupos.setdefault(chave(linha), []).append(linha)
    return [{"chave": item, **_resumo(grupo)} for item, grupo in grupos.items()]


def _odd(linha: Mapping[str, object]) -> Decimal | None:
    valor = linha.get("odd")
    if valor is None:
        return None
    convertido = decimal_exato(valor, "odd")
    return convertido if convertido.is_finite() and convertido > 1 else None


def _faixa_odd(linha: Mapping[str, object]) -> str:
    odd = _odd(linha)
    if odd is None:
        return "unknown"
    for nome, inicio, fim in ODDS_BANDS:
        if (inicio is None or odd >= inicio) and (fim is None or odd < fim):
            return nome
    raise AssertionError("faixas de odds incompletas")


def _instante_local(linha: Mapping[str, object], fuso: ZoneInfo) -> datetime:
    instante = linha.get("instante")
    if not isinstance(instante, datetime):
        raise ValueError("instante da aposta ausente")
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=UTC)
    return instante.astimezone(fuso)


def _periodo_dia(hora: int) -> str:
    return PERIODOS_DIA[hora // 6]


def _quartis(linhas: list[Mapping[str, object]]) -> list[dict[str, object]]:
    valores = sorted(
        _inteiro(linha, "valor_face_centavos")
        for linha in linhas
        if _inteiro(linha, "valor_face_centavos") > 0
    )
    if not valores:
        return _agrupar(linhas, lambda _: "not_applicable", chaves=("not_applicable",))
    limites = sorted({valores[ceil(len(valores) * indice / 4) - 1] for indice in (1, 2, 3)})
    limites = [limite for limite in limites if limite < valores[-1]]

    def chave(linha: Mapping[str, object]) -> str:
        valor = _inteiro(linha, "valor_face_centavos")
        if valor <= 0:
            return "not_applicable"
        indice = next((i for i, limite in enumerate(limites) if valor <= limite), len(limites))
        return f"q{indice + 1}"

    buckets = _agrupar(linhas, chave, chaves=tuple(f"q{i + 1}" for i in range(len(limites) + 1)))
    for indice, bucket in enumerate(buckets[: len(limites) + 1]):
        bucket["valor_face_min_centavos"] = valores[0] if indice == 0 else limites[indice - 1] + 1
        bucket["valor_face_max_centavos"] = (
            limites[indice] if indice < len(limites) else valores[-1]
        )
    return buckets


def analisar_apostas(
    linhas: Iterable[Mapping[str, object]], *, fuso_horario: str
) -> dict[str, object]:
    """Every partition includes the entire filtered population, including null dimensions."""

    apostas = list(linhas)
    fuso = ZoneInfo(fuso_horario)
    total = _resumo(apostas)
    odds = _agrupar(
        apostas,
        _faixa_odd,
        chaves=(*(nome for nome, _, _ in ODDS_BANDS), "unknown"),
    )
    heatmap = _agrupar(
        apostas,
        lambda linha: (
            f"{_instante_local(linha, fuso).weekday()}:{_periodo_dia(_instante_local(linha, fuso).hour)}"
        ),
        chaves=(f"{dia}:{periodo}" for dia in range(7) for periodo in PERIODOS_DIA),
    )
    esportes = _agrupar(
        apostas,
        lambda linha: str(linha.get("esporte_id") or "unknown"),
        chaves=("unknown",),
    )
    nomes_esporte = {
        str(linha.get("esporte_id")): linha.get("esporte_nome")
        for linha in apostas
        if linha.get("esporte_id") is not None
    }
    for esporte in esportes:
        esporte["nome"] = nomes_esporte.get(str(esporte["chave"]))
    quartis = _quartis(apostas)

    liquidadas = [linha for linha in apostas if linha.get("estado") not in {"PENDENTE", "ANULADA"}]
    odds_validas = [odd for linha in liquidadas if (odd := _odd(linha)) is not None]
    lucros = [_inteiro(linha, "lucro_centavos") for linha in apostas]
    ganhos = sum(max(valor, 0) for valor in lucros)
    perdas = -sum(min(valor, 0) for valor in lucros)
    grupos = _agrupar(
        apostas,
        lambda linha: str(linha.get("banca_id") or "unknown"),
        chaves=("unknown",),
    )
    metadados_banca = {
        str(linha.get("banca_id")): (linha.get("banca_nome"), linha.get("capital_banca_centavos"))
        for linha in apostas
        if linha.get("banca_id")
    }
    for grupo in grupos:
        nome, capital = metadados_banca.get(str(grupo["chave"]), (None, None))
        grupo["nome"] = nome
        grupo["capital_banca_centavos"] = capital
        capital_decimal = (
            None if capital is None else decimal_exato(capital, "capital_banca_centavos")
        )
        if capital_decimal is None or capital_decimal <= 0:
            grupo["progressao"] = None
            grupo["motivo_progressao"] = (
                "sem_banca" if grupo["chave"] == "unknown" else "capital_ausente_ou_zero"
            )
        else:
            grupo["progressao"] = formatar_decimal(
                decimal_exato(grupo["lucro_centavos"], "lucro_centavos") / capital_decimal
            )
            grupo["motivo_progressao"] = None
    return {
        "total_filtrado": total,
        "faixas_odds": odds,
        "heatmap": heatmap,
        "por_esporte": esportes,
        "quartis_stake": quartis,
        "por_banca_progressao": grupos,
        "odd_media": (
            None
            if not odds_validas
            else formatar_decimal(sum(odds_validas, Decimal(0)) / len(odds_validas))
        ),
        "odds_desconhecidas": len(liquidadas) - len(odds_validas),
        "odds_nao_aplicaveis": len(apostas) - len(liquidadas),
        "profit_factor": None if perdas == 0 else formatar_decimal(Decimal(ganhos) / perdas),
        "fuso_horario": fuso_horario,
    }
