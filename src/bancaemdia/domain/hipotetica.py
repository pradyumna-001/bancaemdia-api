"""Pure historical comparison; confirmed evidence is supplied separately.

Canonical cents, standard financial states and the panel's integer metrics/ROI
are reused. No stake units or binary floating point arithmetic are performed.
"""

from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

from bancaemdia.domain.financeiro import Estado, lucro_exato, retorno_padrao_exato
from bancaemdia.domain.painel import MetricasPainel, formatar_decimal

type Motivo = Literal[
    "vinculo_ambiguo",
    "dica_indisponivel",
    "dica_em_revisao",
    "stake_indicada_invalida",
    "stake_real_zero",
    "odd_real_invalida",
    "cashout_nao_escalavel",
    "historico_financeiro_ausente",
    "taxa_bonus_ou_retorno_fixo",
    "retorno_real_desconhecido",
    "retorno_fora_da_regra_padrao",
    "fora_do_financeiro",
    "pendente",
    "resultado_real_desconhecido",
]


@dataclass(frozen=True)
class Fato:
    id: int
    origem: str
    estado: Estado
    custo: int
    face: int
    retorno: int | None
    odd: str | None
    freebet: bool = False
    revisao: bool = False
    financeiro: bool = True
    duvida: bool = False
    historico: bool = False
    especial: bool = False

    @property
    def liquidada(self) -> bool:
        return (
            self.financeiro
            and not self.revisao
            and self.estado not in {Estado.PENDENTE, Estado.ANULADA}
        )

    def metricas(self) -> MetricasPainel:
        """Same financial eligibility/cents as painel_filtrado._metricas.

        Unknown values are tracked separately and never exposed as these internal
        zero contributions. Freebet face belongs in ROI, never in financial cost.
        """
        if not self.liquidada:
            return MetricasPainel(total_apostas=1, pendentes=int(self.estado == Estado.PENDENTE))
        retorno = self.retorno if self.retorno is not None else 0
        return MetricasPainel(
            total_apostas=1,
            greens=int(self.estado == Estado.GREEN),
            reds=int(self.estado == Estado.RED),
            giro_centavos=self.custo,
            base_roi_centavos=self.face if self.freebet else self.custo,
            retorno_centavos=retorno,
            lucro_centavos=lucro_exato(retorno, self.custo) if self.retorno is not None else 0,
            freebets=int(self.freebet),
        )


@dataclass(frozen=True)
class Dica:
    stake: int
    revisao: bool = False
    freebet: bool = False


def seguindo(fato: Fato, dica: Dica) -> tuple[Fato | None, Motivo | None]:
    if dica.revisao:
        return None, "dica_em_revisao"
    if dica.stake <= 0 or dica.freebet:
        return None, "stake_indicada_invalida"
    if dica.stake == fato.face:
        return fato, None
    if fato.revisao or not fato.financeiro:
        return fato, "fora_do_financeiro"
    if fato.face <= 0 or (not fato.freebet and fato.custo <= 0):
        return None, "stake_real_zero"
    if fato.estado == Estado.CASHOUT:
        return None, "cashout_nao_escalavel"
    if not fato.historico:
        return None, "historico_financeiro_ausente"
    if fato.especial:
        return None, "taxa_bonus_ou_retorno_fixo"
    try:
        odd = Decimal(fato.odd or "NaN")
    except InvalidOperation:
        return None, "odd_real_invalida"
    if not odd.is_finite() or not Decimal(1) <= odd <= Decimal("1000000"):
        return None, "odd_real_invalida"
    if fato.estado != Estado.PENDENTE:
        if fato.retorno is None:
            return None, "retorno_real_desconhecido"
        if fato.retorno != retorno_padrao_exato(fato.face, odd, fato.estado, freebet=fato.freebet):
            return None, "retorno_fora_da_regra_padrao"
    return replace(
        fato,
        face=dica.stake,
        custo=0 if fato.freebet else dica.stake,
        retorno=retorno_padrao_exato(dica.stake, odd, fato.estado, freebet=fato.freebet),
    ), None


def cenario(fatos: list[Fato], *, incompleto: bool = False) -> dict[str, object]:
    metricas = MetricasPainel.somar(f.metricas() for f in fatos)
    desconhecidos = sum(f.liquidada and f.retorno is None for f in fatos)
    liquidadas = sum(f.liquidada for f in fatos)
    pendentes = [f for f in fatos if f.financeiro and not f.revisao and f.estado == Estado.PENDENTE]
    sem_resultado = bool(desconhecidos or (pendentes and not liquidadas))
    result: dict[str, object] = {
        "total_apostas": len(fatos),
        "liquidadas": liquidadas,
        "pendentes": len(pendentes),
        "em_revisao": sum(f.revisao for f in fatos),
        "fora_do_financeiro": sum(not f.financeiro for f in fatos),
        "anuladas": sum(f.estado == Estado.ANULADA for f in fatos),
        "resultados_desconhecidos": desconhecidos,
        "freebets": metricas.freebets,
        "giro_centavos": None if incompleto else str(metricas.giro_centavos),
        "base_roi_centavos": None if incompleto else str(metricas.base_roi_centavos),
        "retorno_centavos": None if incompleto or sem_resultado else str(metricas.retorno_centavos),
        "lucro_centavos": None if incompleto or sem_resultado else str(metricas.lucro_centavos),
        "exposicao_pendente_centavos": None if incompleto else str(sum(f.custo for f in pendentes)),
        "face_pendente_centavos": None if incompleto else str(sum(f.face for f in pendentes)),
        "roi": None
        if incompleto or sem_resultado or not metricas.base_roi_centavos
        else formatar_decimal(metricas.roi),
        "win_rate": None
        if not metricas.greens + metricas.reds
        else formatar_decimal(metricas.win_rate),
    }
    return result


def comparar(fatos: list[Fato], vinculos: dict[int, list[Dica | None]]) -> dict[str, object]:
    hipoteticas = []
    motivos: Counter[Motivo] = Counter()
    vinculadas = alteradas = diferentes = indisponiveis = 0
    elegiveis = 0
    incompleto = False
    for fato in fatos:
        copias = vinculos.get(fato.id, []) if fato.origem == "casa" else []
        nova: Fato | None = fato
        motivo: Motivo | None = None
        if fato.duvida:
            motivo = "vinculo_ambiguo"
        elif copias:
            vinculadas += 1
            elegiveis += int(fato.financeiro and not fato.revisao)
            if len(copias) != 1:
                motivo = "vinculo_ambiguo"
                nova = None
            elif copias[0] is None:
                motivo = "dica_indisponivel"
                nova = None
            else:
                dica = copias[0]
                diferentes += int(dica.stake != fato.face)
                nova, motivo = seguindo(fato, dica)
        if motivo:
            motivos[motivo] += 1
        if nova is None:
            indisponiveis += 1
            incompleto |= fato.liquidada or (
                fato.financeiro and not fato.revisao and fato.estado == Estado.PENDENTE
            )
            nova = fato  # Kept in population/counts; financial totals hidden if unsafe.
        alteradas += int(nova.face != fato.face)
        hipoteticas.append(nova)
    motivos["pendente"] += sum(
        f.financeiro and not f.revisao and f.estado == Estado.PENDENTE for f in fatos
    )
    motivos["resultado_real_desconhecido"] += sum(f.liquidada and f.retorno is None for f in fatos)
    real = cenario(fatos)
    hipotetico = cenario(hipoteticas, incompleto=incompleto)
    campos = (
        "giro_centavos",
        "retorno_centavos",
        "lucro_centavos",
        "base_roi_centavos",
        "exposicao_pendente_centavos",
    )
    diferencas = {}
    for campo in campos:
        a, b = real[campo], hipotetico[campo]
        diferencas[campo] = (
            str(int(b) - int(a)) if isinstance(a, str) and isinstance(b, str) else None
        )
    estado = (
        "sem_apostas"
        if not fatos
        else "parcial"
        if indisponiveis and alteradas
        else "indisponivel"
        if indisponiveis
        else "sem_vinculos_elegiveis"
        if not elegiveis
        else "sem_stakes_diferentes"
        if not diferentes
        else "comparado"
    )
    return {
        "estado": estado,
        "real": real,
        "hipotetico": hipotetico,
        "diferencas": diferencas,
        "apostas_consideradas": len(fatos),
        "vinculos_confirmados": vinculadas,
        "stakes_diferentes": diferentes,
        "apostas_alteradas": alteradas,
        "apostas_indisponiveis": indisponiveis,
        "limitacoes": [{"motivo": m, "quantidade": n} for m, n in sorted(motivos.items()) if n],
    }
