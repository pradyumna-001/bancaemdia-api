from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from bancaemdia.domain import financeiro, temporal
from bancaemdia.domain.registros import Aposta, Movimento

TIPOS_PUBLICOS = frozenset({"DEPOSITO", "SAQUE", "TRANSFERENCIA", "AJUSTE"})
FUSO_DO_BRASIL = ZoneInfo("America/Sao_Paulo")
BIGINT_MIN = -(2**63)
BIGINT_MAX = 2**63 - 1


class MovimentoInvalidoError(ValueError):
    pass


@dataclass(frozen=True)
class LancamentoPlanejado:
    conta_casa_id: int | None
    tipo: str
    valor_centavos: int
    transferencia_id: UUID | None = None


@dataclass(frozen=True)
class PlanoMovimento:
    tipo: str
    lancamentos: tuple[LancamentoPlanejado, ...]
    transferencia_id: UUID | None = None


def normalizar_tipo(tipo: str) -> str:
    normalizado = tipo.strip().upper() if isinstance(tipo, str) else ""
    if normalizado not in TIPOS_PUBLICOS:
        permitidos = ", ".join(sorted(TIPOS_PUBLICOS))
        raise MovimentoInvalidoError(f"tipo desconhecido: {tipo!r}. Use um de {permitidos}")
    return normalizado


def _conta(valor: int | None, nome: str, *, obrigatoria: bool) -> int | None:
    if valor is None:
        if obrigatoria:
            raise MovimentoInvalidoError(f"{nome} é obrigatória")
        return None
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0 or valor > BIGINT_MAX:
        raise MovimentoInvalidoError(f"{nome} tem de ser um id positivo")
    return valor


def _valor(valor_centavos: int) -> int:
    if isinstance(valor_centavos, bool) or not isinstance(valor_centavos, int):
        raise MovimentoInvalidoError("valor_centavos tem de ser um número inteiro")
    if valor_centavos == 0:
        raise MovimentoInvalidoError("um movimento de zero não é um movimento")
    if not BIGINT_MIN <= valor_centavos <= BIGINT_MAX:
        raise MovimentoInvalidoError("valor_centavos não cabe no intervalo monetário do banco")
    return valor_centavos


def planejar_movimento(
    tipo: str,
    valor_centavos: int,
    conta_casa_id: int | None = None,
    conta_casa_destino_id: int | None = None,
) -> PlanoMovimento:
    """Valida um pedido público e devolve os lançamentos que devem ser persistidos juntos."""
    tipo_normalizado = normalizar_tipo(tipo)
    valor = _valor(valor_centavos)

    if tipo_normalizado == "TRANSFERENCIA":
        origem = _conta(conta_casa_id, "conta_casa_id", obrigatoria=True)
        destino = _conta(conta_casa_destino_id, "conta_casa_destino_id", obrigatoria=True)
        if valor < 0:
            raise MovimentoInvalidoError("a transferência recebe um valor positivo")
        if origem == destino:
            raise MovimentoInvalidoError("origem e destino precisam ser contas diferentes")
        transferencia_id = uuid4()
        return PlanoMovimento(
            tipo=tipo_normalizado,
            transferencia_id=transferencia_id,
            lancamentos=(
                LancamentoPlanejado(
                    conta_casa_id=origem,
                    tipo=tipo_normalizado,
                    valor_centavos=-valor,
                    transferencia_id=transferencia_id,
                ),
                LancamentoPlanejado(
                    conta_casa_id=destino,
                    tipo=tipo_normalizado,
                    valor_centavos=valor,
                    transferencia_id=transferencia_id,
                ),
            ),
        )

    if conta_casa_destino_id is not None:
        raise MovimentoInvalidoError("conta_casa_destino_id só vale para transferência")
    conta = _conta(
        conta_casa_id,
        "conta_casa_id",
        obrigatoria=tipo_normalizado in {"DEPOSITO", "SAQUE"},
    )
    if tipo_normalizado != "AJUSTE" and valor < 0:
        raise MovimentoInvalidoError(f"{tipo_normalizado.lower()} recebe um valor positivo")
    persistido = -valor if tipo_normalizado == "SAQUE" else valor
    return PlanoMovimento(
        tipo=tipo_normalizado,
        lancamentos=(LancamentoPlanejado(conta, tipo_normalizado, persistido),),
    )


def _data_no_brasil(valor: datetime | None) -> date | None:
    if valor is None:
        return None
    localizado = (
        valor.replace(tzinfo=FUSO_DO_BRASIL)
        if valor.tzinfo is None
        else valor.astimezone(FUSO_DO_BRASIL)
    )
    return localizado.date()


def movimento_temporal(movimento: Movimento) -> temporal.Movimento:
    try:
        tipo = temporal.TipoMovimento(movimento.tipo.strip().upper())
    except ValueError as exc:
        raise MovimentoInvalidoError(f"tipo persistido desconhecido: {movimento.tipo!r}") from exc
    ocorrido_em = _data_no_brasil(movimento.ocorrido_em)
    if ocorrido_em is None:  # A anotação do registro não permite, mas protege fontes externas.
        raise MovimentoInvalidoError("movimento sem data")
    return temporal.Movimento(
        tipo=tipo,
        valor_centavos=movimento.valor_centavos,
        conta_casa_id=movimento.conta_casa_id,
        ocorrido_em=ocorrido_em,
    )


def aposta_temporal(aposta: Aposta) -> financeiro.Aposta:
    try:
        estado = financeiro.Estado(aposta.estado.strip().upper())
    except ValueError as exc:
        raise MovimentoInvalidoError(f"estado persistido desconhecido: {aposta.estado!r}") from exc

    # O domínio financeiro normalmente deriva a stake de unidades x valor da unidade. Aqui a linha
    # materializada já traz os centavos oficiais; usar uma unidade evita refazer a conta com float.
    valor_da_unidade = aposta.valor_aposta_centavos if aposta.freebet else aposta.stake_centavos
    return financeiro.Aposta(
        stake_unidades=1,
        valor_unidade_centavos=valor_da_unidade,
        odd=aposta.odd,
        estado=estado,
        freebet=aposta.freebet,
        retorno_centavos=aposta.retorno_centavos,
        retorno_informado=aposta.retorno_centavos is not None,
        selecionada=aposta.selecionada,
        revisao_grave=aposta.revisao_grave,
        conta_casa_id=aposta.conta_casa_id,
        data_aposta=_data_no_brasil(aposta.data_aposta),
        criada_em=_data_no_brasil(aposta.criada_em),
    )


def _data_de_corte(valor: date | datetime | None) -> date | None:
    if isinstance(valor, datetime):
        return _data_no_brasil(valor)
    return valor


def saldo_da_conta(
    conta_casa_id: int,
    apostas: Iterable[Aposta],
    movimentos: Iterable[Movimento],
    data_corte: date | datetime | None = None,
) -> temporal.SaldoDaCasa:
    """Adapta linhas materializadas sem criar uma segunda regra de saldo."""
    return temporal.saldo(
        conta_casa_id,
        (aposta_temporal(aposta) for aposta in apostas),
        (movimento_temporal(movimento) for movimento in movimentos),
        _data_de_corte(data_corte),
    )


def saldo_por_conta(
    conta_casa_id: int,
    apostas: Iterable[Aposta],
    movimentos: Iterable[Movimento],
    data_corte: date | datetime | None = None,
) -> temporal.SaldoDaCasa:
    return saldo_da_conta(conta_casa_id, apostas, movimentos, data_corte)
