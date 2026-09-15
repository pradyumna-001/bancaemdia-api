from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum


class Estado(StrEnum):
    PENDENTE = "PENDENTE"
    GREEN = "GREEN"
    RED = "RED"
    ANULADA = "ANULADA"
    MEIO_GREEN = "MEIO_GREEN"
    MEIO_RED = "MEIO_RED"
    CASHOUT = "CASHOUT"


SEM_RISCO: frozenset[Estado] = frozenset({Estado.ANULADA})


@dataclass(frozen=True)
class Aposta:
    stake_unidades: float
    valor_unidade_centavos: int
    odd: float | None = None
    estado: Estado = Estado.PENDENTE
    freebet: bool = False
    comissao_centavos: int = 0
    retorno_centavos: int | None = None
    retorno_informado: bool = False
    selecionada: bool = True
    revisao_grave: bool = False
    revisao_motivo: str | None = None
    conta_casa_id: int | None = None
    data_aposta: date | None = None
    criada_em: date | None = None

    @property
    def stake_centavos(self) -> int:
        if self.freebet:
            return 0
        return round(self.stake_unidades * self.valor_unidade_centavos)

    @property
    def valor_aposta_centavos(self) -> int:
        return round(self.stake_unidades * self.valor_unidade_centavos)

    @property
    def conta_no_giro(self) -> bool:
        if self.revisao_grave:
            return False
        return self.estado not in SEM_RISCO and self.estado != Estado.PENDENTE

    @property
    def lucro_centavos(self) -> int | None:
        if self.retorno_centavos is None:
            return None
        return self.retorno_centavos - self.stake_centavos

    @property
    def retorno_se_ganhar_centavos(self) -> int | None:
        odd = self.odd
        if odd is None:
            return None
        return self._gross_payout(odd) - self.comissao_centavos

    def _gross_payout(self, odd: float) -> int:
        base = self.valor_aposta_centavos
        return round(base * odd) - (base if self.freebet else 0)

    def retorno_calculado(self) -> int | None:
        odd = self.odd
        if odd is None or self.estado == Estado.PENDENTE:
            return None
        base = self.valor_aposta_centavos
        gross = self._gross_payout(odd)
        match self.estado:
            case Estado.GREEN:
                return gross - self.comissao_centavos
            case Estado.RED:
                return 0
            case Estado.ANULADA:
                return 0 if self.freebet else base
            case Estado.MEIO_GREEN:
                return round((gross + (0 if self.freebet else base)) / 2) - self.comissao_centavos
            case Estado.MEIO_RED:
                return 0 if self.freebet else round(base / 2)
        return None


def resolver_retorno(aposta: Aposta) -> Aposta:
    if aposta.retorno_informado or aposta.estado == Estado.PENDENTE:
        return aposta
    return replace(aposta, retorno_centavos=aposta.retorno_calculado())


@dataclass(frozen=True)
class ContagemPendentes:
    confiaveis: int = 0
    suspeitas: int = 0
    stake_confiavel_centavos: int = 0
    stake_suspeita_centavos: int = 0

    @property
    def total(self) -> int:
        return self.confiaveis + self.suspeitas

    @property
    def stake_total_centavos(self) -> int:
        return self.stake_confiavel_centavos + self.stake_suspeita_centavos


def contar_pendentes(apostas: Iterable[Aposta], so_selecionadas: bool = True) -> ContagemPendentes:
    confiaveis = suspeitas = stake_confiavel = stake_suspeita = 0
    for aposta in apostas:
        if so_selecionadas and not aposta.selecionada:
            continue
        if aposta.estado != Estado.PENDENTE:
            continue
        if aposta.revisao_grave:
            suspeitas += 1
            stake_suspeita += aposta.stake_centavos
        else:
            confiaveis += 1
            stake_confiavel += aposta.stake_centavos
    return ContagemPendentes(
        confiaveis=confiaveis,
        suspeitas=suspeitas,
        stake_confiavel_centavos=stake_confiavel,
        stake_suspeita_centavos=stake_suspeita,
    )


@dataclass
class Resumo:
    apostas: int = 0
    pendentes: int = 0
    pendentes_suspeitas: int = 0
    greens: int = 0
    reds: int = 0
    giro_centavos: int = 0
    base_roi_centavos: int = 0
    retorno_centavos: int = 0
    lucro_centavos: int = 0
    giro_proprio_centavos: int = 0
    lucro_proprio_centavos: int = 0
    freebets: int = 0
    em_revisao_grave: int = 0
    em_revisao_leve: int = 0
    lucro_a_confirmar_centavos: int = 0

    @property
    def roi(self) -> float:
        if not self.base_roi_centavos:
            return 0.0
        return self.lucro_centavos / self.base_roi_centavos

    @property
    def roi_sem_bonus(self) -> float:
        if not self.giro_proprio_centavos:
            return 0.0
        return self.lucro_proprio_centavos / self.giro_proprio_centavos

    @property
    def taxa_de_acerto(self) -> float:
        decididas = self.greens + self.reds
        return self.greens / decididas if decididas else 0.0


def resumir(apostas: Iterable[Aposta], so_selecionadas: bool = True) -> Resumo:
    items = list(apostas)
    resumo = Resumo()
    for aposta in items:
        if so_selecionadas and not aposta.selecionada:
            continue
        if aposta.revisao_motivo:
            if aposta.revisao_grave:
                resumo.em_revisao_grave += 1
                continue
            resumo.em_revisao_leve += 1
        resumo.apostas += 1
        if aposta.estado == Estado.PENDENTE:
            continue
        if aposta.estado == Estado.GREEN:
            resumo.greens += 1
        elif aposta.estado == Estado.RED:
            resumo.reds += 1
        if not aposta.conta_no_giro:
            continue
        lucro = aposta.lucro_centavos or 0
        if aposta.revisao_motivo:
            resumo.lucro_a_confirmar_centavos += lucro
        resumo.giro_centavos += aposta.stake_centavos
        resumo.retorno_centavos += aposta.retorno_centavos or 0
        resumo.lucro_centavos += lucro
        if aposta.freebet:
            resumo.base_roi_centavos += aposta.valor_aposta_centavos
            resumo.freebets += 1
        else:
            resumo.base_roi_centavos += aposta.stake_centavos
            resumo.giro_proprio_centavos += aposta.stake_centavos
            resumo.lucro_proprio_centavos += lucro

    contagem = contar_pendentes(items, so_selecionadas)
    resumo.pendentes = contagem.confiaveis
    resumo.pendentes_suspeitas = contagem.suspeitas
    return resumo
