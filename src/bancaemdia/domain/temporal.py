from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from bancaemdia.domain.financeiro import Aposta, Estado

VALOR_UNIDADE_PADRAO_CENTAVOS = 10_000


@dataclass(frozen=True)
class Unidade:
    valor_centavos: int
    vigente_de: date
    vigente_ate: date | None = None


def unidade_vigente(
    unidades: Iterable[Unidade],
    data: date,
    padrao_centavos: int = VALOR_UNIDADE_PADRAO_CENTAVOS,
) -> int:
    vigentes = [
        unidade
        for unidade in unidades
        if unidade.vigente_de <= data
        and (unidade.vigente_ate is None or data < unidade.vigente_ate)
    ]
    if not vigentes:
        return padrao_centavos
    return max(vigentes, key=lambda unidade: unidade.vigente_de).valor_centavos


@dataclass(frozen=True)
class ContaCasa:
    casa_id: int
    id: int | None = None
    ativa: bool = True
    desde: date | None = None
    ate: date | None = None

    def valia_em(self, data: date | None) -> bool:
        if data is None:
            return True
        if self.desde is not None and data < self.desde:
            return False
        return not (self.ate is not None and data > self.ate)


def conta_casa_vigente(
    contas: Iterable[ContaCasa], casa_id: int, data: date | None
) -> ContaCasa | None:
    for conta in contas:
        if conta.casa_id == casa_id and conta.ativa and conta.valia_em(data):
            return conta
    return None


class TipoMovimento(StrEnum):
    DEPOSITO = "DEPOSITO"
    SAQUE = "SAQUE"
    TRANSFERENCIA = "TRANSFERENCIA"
    BONUS = "BONUS"
    AJUSTE = "AJUSTE"


@dataclass(frozen=True)
class Movimento:
    tipo: TipoMovimento
    valor_centavos: int
    conta_casa_id: int | None
    ocorrido_em: date


@dataclass(frozen=True)
class SaldoDaCasa:
    conta_casa_id: int
    depositado_centavos: int = 0
    sacado_centavos: int = 0
    bonus_centavos: int = 0
    movido_centavos: int = 0
    apostado_centavos: int = 0
    retornado_centavos: int = 0
    em_jogo_centavos: int = 0
    apostas_pendentes: int = 0
    movimentos: int = 0
    apostado_no_periodo_centavos: int = 0
    retornado_no_periodo_centavos: int = 0
    apostas_antes_do_caixa: int = 0
    desde: date | None = None
    lucro_centavos: int = 0

    @property
    def tem_caixa(self) -> bool:
        return self.movimentos > 0

    @property
    def _bruto(self) -> int:
        return (
            self.depositado_centavos
            - self.sacado_centavos
            + self.bonus_centavos
            + self.movido_centavos
            - self.apostado_no_periodo_centavos
            + self.retornado_no_periodo_centavos
        )

    @property
    def saldo_centavos(self) -> int | None:
        """`None` não é zero: sem lançamento de caixa (ou com conta que não fecha) o saldo é
        desconhecido, e só as apostas dariam um negativo inventado. Regra do dono: nada deve
        ser obrigatório para o usuário."""
        if not self.tem_caixa:
            return None
        return self._bruto if self._bruto >= 0 else None

    @property
    def deposito_faltante_centavos(self) -> int:
        if not self.tem_caixa or self._bruto >= 0:
            return 0
        return -self._bruto


def saldo(
    conta_casa_id: int,
    apostas: Iterable[Aposta],
    movimentos: Iterable[Movimento],
    data_corte: date | None = None,
) -> SaldoDaCasa:
    depositado = sacado = bonus = movido = count = 0
    desde: date | None = None
    for movimento in movimentos:
        if movimento.conta_casa_id != conta_casa_id:
            continue
        if data_corte is not None and movimento.ocorrido_em > data_corte:
            continue
        count += 1
        if desde is None or movimento.ocorrido_em < desde:
            desde = movimento.ocorrido_em
        if movimento.tipo == TipoMovimento.DEPOSITO:
            depositado += movimento.valor_centavos
        elif movimento.tipo == TipoMovimento.SAQUE:
            sacado += abs(movimento.valor_centavos)
        elif movimento.tipo == TipoMovimento.BONUS:
            bonus += movimento.valor_centavos
        else:
            movido += movimento.valor_centavos

    apostado = retornado = em_jogo = pendentes = lucro = 0
    apostado_periodo = retornado_periodo = antes_do_caixa = 0
    for aposta in apostas:
        if aposta.conta_casa_id != conta_casa_id or not aposta.selecionada:
            continue
        data = aposta.data_aposta or aposta.criada_em
        if data_corte is not None and data is not None and data > data_corte:
            continue
        apostado += aposta.stake_centavos
        retornado += aposta.retorno_centavos or 0
        if aposta.estado == Estado.PENDENTE:
            pendentes += 1
            em_jogo += aposta.stake_centavos
        elif aposta.conta_no_giro:
            lucro += (aposta.retorno_centavos or 0) - aposta.stake_centavos
        if desde is not None and data is not None and data < desde:
            antes_do_caixa += 1
            continue
        apostado_periodo += aposta.stake_centavos
        retornado_periodo += aposta.retorno_centavos or 0

    return SaldoDaCasa(
        conta_casa_id=conta_casa_id,
        depositado_centavos=depositado,
        sacado_centavos=sacado,
        bonus_centavos=bonus,
        movido_centavos=movido,
        apostado_centavos=apostado,
        retornado_centavos=retornado,
        em_jogo_centavos=em_jogo,
        apostas_pendentes=pendentes,
        movimentos=count,
        apostado_no_periodo_centavos=apostado_periodo,
        retornado_no_periodo_centavos=retornado_periodo,
        apostas_antes_do_caixa=antes_do_caixa,
        desde=desde,
        lucro_centavos=lucro,
    )
