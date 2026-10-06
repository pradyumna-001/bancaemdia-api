"""Contratos e regras puras do painel financeiro.

O banco entrega apenas contagens e somas exatas. Razoes, janelas de tempo e a
serie acumulada ficam aqui para que HTTP, exportacao e metricas usem a mesma
regra sem converter dinheiro para ``float``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

FUSO_DO_BRASIL = ZoneInfo("America/Sao_Paulo")
CASAS_DECIMAIS_PUBLICAS = 6
ESCALA_BASIS_POINTS = Decimal(10_000)


class PainelInvalidoError(ValueError):
    """Um filtro ou valor agregado nao representa um painel valido."""


class PeriodoPainel(StrEnum):
    SETE_DIAS = "7d"
    TRINTA_DIAS = "30d"
    NOVENTA_DIAS = "90d"
    UM_ANO = "1y"
    TODO = "all"

    @classmethod
    def _missing_(cls, valor: object) -> PeriodoPainel | None:
        if isinstance(valor, str):
            normalizado = valor.strip().lower()
            return next((periodo for periodo in cls if periodo.value == normalizado), None)
        return None


class GranularidadePainel(StrEnum):
    DIA = "dia"
    SEMANA = "semana"
    MES = "mes"


class SecaoExportacao(StrEnum):
    RESUMO = "resumo"
    POR_CASA = "por_casa"
    POR_TIPSTER = "por_tipster"
    POR_MERCADO = "por_mercado"
    POR_PERIODO = "por_periodo"
    EVOLUCAO = "evolucao"


COLUNAS_METRICAS_EXPORTACAO: Final = (
    "total_apostas",
    "pendentes",
    "greens",
    "reds",
    "giro_centavos",
    "base_roi_centavos",
    "retorno_centavos",
    "lucro_centavos",
    "freebets",
    "roi",
    "win_rate",
)

COLUNAS_EXPORTACAO: Final[Mapping[SecaoExportacao, tuple[str, ...]]] = {
    SecaoExportacao.RESUMO: (
        *COLUNAS_METRICAS_EXPORTACAO,
        "saldo_total_centavos",
        "saldo_conhecido_centavos",
        "contas_saldo_desconhecido",
        "saldo_escopo",
    ),
    SecaoExportacao.POR_CASA: ("id", "nome", *COLUNAS_METRICAS_EXPORTACAO),
    SecaoExportacao.POR_TIPSTER: ("id", "nome", *COLUNAS_METRICAS_EXPORTACAO),
    SecaoExportacao.POR_MERCADO: (
        "id",
        "nome",
        "familia",
        *COLUNAS_METRICAS_EXPORTACAO,
    ),
    SecaoExportacao.POR_PERIODO: (
        "periodo_inicio",
        "granularidade",
        *COLUNAS_METRICAS_EXPORTACAO,
    ),
    SecaoExportacao.EVOLUCAO: (
        "periodo_inicio",
        "banca_id",
        "banca_nome",
        "contribuicao_centavos",
        "acumulado_centavos",
        "saldo_centavos",
    ),
}


class EscopoSaldoPainel(StrEnum):
    CONTAS_CASA_ALL_TIME = "contas_casa_all_time"


def _no_brasil(instante: datetime | None) -> datetime:
    if instante is None:
        return datetime.now(FUSO_DO_BRASIL)
    if instante.tzinfo is None:
        return instante.replace(tzinfo=FUSO_DO_BRASIL)
    return instante.astimezone(FUSO_DO_BRASIL)


def _um_ano_antes(dia: date) -> date:
    try:
        return dia.replace(year=dia.year - 1)
    except ValueError:
        # 29/02 nao existe no ano anterior: a janela civil comeca em 28/02.
        return dia.replace(year=dia.year - 1, day=28)


def granularidade_do_periodo(periodo: PeriodoPainel) -> GranularidadePainel:
    if periodo in {PeriodoPainel.SETE_DIAS, PeriodoPainel.TRINTA_DIAS}:
        return GranularidadePainel.DIA
    if periodo == PeriodoPainel.NOVENTA_DIAS:
        return GranularidadePainel.SEMANA
    return GranularidadePainel.MES


@dataclass(frozen=True)
class JanelaPainel:
    """Intervalo civil brasileiro, com fim exclusivo."""

    inicio: date | None
    fim: date
    granularidade: GranularidadePainel

    def __post_init__(self) -> None:
        if self.inicio is not None and self.inicio >= self.fim:
            raise PainelInvalidoError("o inicio do painel precisa ser anterior ao fim")


def janela_do_periodo(
    periodo: PeriodoPainel | str,
    agora: datetime | None = None,
) -> JanelaPainel:
    """Resolve 7d/30d/90d/1y/all no calendario de Sao Paulo.

    As janelas em dias incluem hoje. ``fim`` e amanha, para que toda consulta
    use a mesma comparacao ``inicio <= data < fim`` inclusive perto da meia-noite.
    """

    try:
        periodo_normalizado = PeriodoPainel(periodo)
    except ValueError as exc:
        permitidos = ", ".join(item.value for item in PeriodoPainel)
        raise PainelInvalidoError(f"periodo desconhecido; use {permitidos}") from exc

    hoje = _no_brasil(agora).date()
    fim = hoje + timedelta(days=1)
    inicio: date | None
    match periodo_normalizado:
        case PeriodoPainel.SETE_DIAS:
            inicio = hoje - timedelta(days=6)
        case PeriodoPainel.TRINTA_DIAS:
            inicio = hoje - timedelta(days=29)
        case PeriodoPainel.NOVENTA_DIAS:
            inicio = hoje - timedelta(days=89)
        case PeriodoPainel.UM_ANO:
            inicio = _um_ano_antes(hoje)
        case PeriodoPainel.TODO:
            inicio = None
    return JanelaPainel(inicio, fim, granularidade_do_periodo(periodo_normalizado))


def _id_positivo(valor: int | None, nome: str) -> int | None:
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise PainelInvalidoError(f"{nome} precisa ser um id positivo")
    return valor


@dataclass(frozen=True)
class FiltrosPainel:
    periodo: PeriodoPainel
    janela: JanelaPainel
    casa_id: int | None = None
    tipster_id: int | None = None
    mercado_id: int | None = None

    def __post_init__(self) -> None:
        _id_positivo(self.casa_id, "casa_id")
        _id_positivo(self.tipster_id, "tipster_id")
        _id_positivo(self.mercado_id, "mercado_id")
        if self.janela.granularidade != granularidade_do_periodo(self.periodo):
            raise PainelInvalidoError("a granularidade nao corresponde ao periodo")

    @classmethod
    def criar(
        cls,
        periodo: PeriodoPainel | str = PeriodoPainel.TRINTA_DIAS,
        *,
        casa_id: int | None = None,
        tipster_id: int | None = None,
        mercado_id: int | None = None,
        agora: datetime | None = None,
    ) -> FiltrosPainel:
        try:
            periodo_normalizado = PeriodoPainel(periodo)
        except ValueError as exc:
            permitidos = ", ".join(item.value for item in PeriodoPainel)
            raise PainelInvalidoError(f"periodo desconhecido; use {permitidos}") from exc
        return cls(
            periodo=periodo_normalizado,
            janela=janela_do_periodo(periodo_normalizado, agora),
            casa_id=_id_positivo(casa_id, "casa_id"),
            tipster_id=_id_positivo(tipster_id, "tipster_id"),
            mercado_id=_id_positivo(mercado_id, "mercado_id"),
        )


def _inteiro_exato(valor: object, nome: str) -> int:
    if valor is None:
        return 0
    if isinstance(valor, bool):
        raise PainelInvalidoError(f"{nome} nao pode ser booleano")
    if isinstance(valor, int):
        return valor
    if isinstance(valor, Decimal) and valor == valor.to_integral_value():
        return int(valor)
    raise PainelInvalidoError(f"{nome} precisa ser inteiro exato")


def _inteiro_opcional_exato(valor: object, nome: str) -> int | None:
    return None if valor is None else _inteiro_exato(valor, nome)


def _id_agregado(valor: object, nome: str) -> int | None:
    convertido = _inteiro_exato(valor, nome)
    if convertido == 0:
        return None
    return _id_positivo(convertido, nome)


def _data_exata(valor: object, nome: str) -> date:
    if isinstance(valor, datetime) or not isinstance(valor, date):
        raise PainelInvalidoError(f"{nome} precisa ser uma data civil")
    return valor


def _texto_opcional(valor: object, nome: str) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise PainelInvalidoError(f"{nome} precisa ser texto ou null")
    return valor


def decimal_exato(valor: object, nome: str = "valor") -> Decimal:
    """Converte tipos exatos e recusa ``float`` silencioso."""

    if isinstance(valor, bool):
        raise PainelInvalidoError(f"{nome} nao pode ser booleano")
    if isinstance(valor, Decimal):
        return valor
    if isinstance(valor, int):
        return Decimal(valor)
    if isinstance(valor, str):
        try:
            return Decimal(valor)
        except Exception as exc:
            raise PainelInvalidoError(f"{nome} nao e decimal") from exc
    raise PainelInvalidoError(f"{nome} precisa ser Decimal, int ou texto decimal")


def razao_exata(numerador: int, denominador: int) -> Decimal:
    if denominador == 0:
        return Decimal(0)
    return Decimal(numerador) / Decimal(denominador)


def formatar_decimal(valor: Decimal, casas: int = CASAS_DECIMAIS_PUBLICAS) -> str:
    if isinstance(casas, bool) or not isinstance(casas, int) or not 0 <= casas <= 18:
        raise PainelInvalidoError("casas precisa estar entre 0 e 18")
    passo = Decimal(1).scaleb(-casas)
    return format(valor.quantize(passo, rounding=ROUND_HALF_EVEN), f".{casas}f")


def basis_points(valor: Decimal) -> int:
    return int((valor * ESCALA_BASIS_POINTS).to_integral_value(rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True)
class MetricasPainel:
    total_apostas: int = 0
    pendentes: int = 0
    greens: int = 0
    reds: int = 0
    giro_centavos: int = 0
    base_roi_centavos: int = 0
    retorno_centavos: int = 0
    lucro_centavos: int = 0
    freebets: int = 0

    def __post_init__(self) -> None:
        for campo in fields(self):
            valor = getattr(self, campo.name)
            if isinstance(valor, bool) or not isinstance(valor, int):
                raise PainelInvalidoError(f"{campo.name} precisa ser inteiro")
        for nome in (
            "total_apostas",
            "pendentes",
            "greens",
            "reds",
            "giro_centavos",
            "base_roi_centavos",
            "retorno_centavos",
            "freebets",
        ):
            if getattr(self, nome) < 0:
                raise PainelInvalidoError(f"{nome} nao pode ser negativo")

    @classmethod
    def de_linha(cls, linha: Mapping[str, object]) -> MetricasPainel:
        return cls(**{
            campo.name: _inteiro_exato(linha.get(campo.name), campo.name) for campo in fields(cls)
        })

    @classmethod
    def somar(cls, metricas: Iterable[MetricasPainel]) -> MetricasPainel:
        totais = {campo.name: 0 for campo in fields(cls)}
        for item in metricas:
            for nome in totais:
                totais[nome] += getattr(item, nome)
        return cls(**totais)

    def como_mapeamento(self) -> dict[str, int | Decimal]:
        """Representa as metricas sem arredondar razoes nem converter para ``float``."""

        valores: dict[str, int | Decimal] = {
            campo.name: getattr(self, campo.name) for campo in fields(self)
        }
        valores["roi"] = self.roi
        valores["win_rate"] = self.win_rate
        return valores

    @property
    def roi(self) -> Decimal:
        return razao_exata(self.lucro_centavos, self.base_roi_centavos)

    @property
    def win_rate(self) -> Decimal:
        return razao_exata(self.greens, self.greens + self.reds)

    @property
    def roi_basis_points(self) -> int:
        return basis_points(self.roi)

    @property
    def win_rate_basis_points(self) -> int:
        return basis_points(self.win_rate)


@dataclass(frozen=True)
class GrupoPainel:
    id: int | None
    nome: str | None
    metricas: MetricasPainel
    familia: str | None = None

    @classmethod
    def de_linha(cls, linha: Mapping[str, object]) -> GrupoPainel:
        return cls(
            id=_id_agregado(linha.get("id"), "id"),
            nome=_texto_opcional(linha.get("nome"), "nome"),
            metricas=MetricasPainel.de_linha(linha),
            familia=_texto_opcional(linha.get("familia"), "familia"),
        )


@dataclass(frozen=True)
class SaldoPainel:
    """Saldo conhecido das contas das casas, sem inventar vinculo com ``bancas``."""

    saldo_total_centavos: int | None
    saldo_conhecido_centavos: int
    contas_saldo_desconhecido: int
    escopo: EscopoSaldoPainel = EscopoSaldoPainel.CONTAS_CASA_ALL_TIME

    def __post_init__(self) -> None:
        for nome in ("saldo_conhecido_centavos", "contas_saldo_desconhecido"):
            valor = getattr(self, nome)
            if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
                raise PainelInvalidoError(f"{nome} precisa ser um inteiro nao negativo")
        total = self.saldo_total_centavos
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            raise PainelInvalidoError(
                "saldo_total_centavos precisa ser inteiro nao negativo ou null"
            )
        if self.contas_saldo_desconhecido:
            if total is not None:
                raise PainelInvalidoError(
                    "saldo total nao e conhecido quando alguma conta e incerta"
                )
        elif total is not None and total != self.saldo_conhecido_centavos:
            raise PainelInvalidoError("saldo total precisa coincidir com o saldo conhecido")
        elif total is None and self.saldo_conhecido_centavos:
            raise PainelInvalidoError("saldo conhecido positivo precisa compor um saldo total")

    @classmethod
    def de_linha(cls, linha: Mapping[str, object] | None) -> SaldoPainel:
        if linha is None:
            return cls(None, 0, 0)
        return cls(
            saldo_total_centavos=_inteiro_opcional_exato(
                linha.get("saldo_centavos"), "saldo_centavos"
            ),
            saldo_conhecido_centavos=_inteiro_exato(
                linha.get("saldo_conhecido_centavos"), "saldo_conhecido_centavos"
            ),
            contas_saldo_desconhecido=_inteiro_exato(
                linha.get("contas_saldo_desconhecido"), "contas_saldo_desconhecido"
            ),
        )


@dataclass(frozen=True)
class PontoPeriodo:
    periodo_inicio: date
    granularidade: GranularidadePainel
    metricas: MetricasPainel

    @classmethod
    def de_linha(
        cls,
        linha: Mapping[str, object],
        granularidade: GranularidadePainel,
    ) -> PontoPeriodo:
        return cls(
            periodo_inicio=_data_exata(linha.get("periodo_inicio"), "periodo_inicio"),
            granularidade=granularidade,
            metricas=MetricasPainel.de_linha(linha),
        )


@dataclass(frozen=True)
class ContribuicaoEvolucao:
    periodo_inicio: date
    contribuicao_centavos: int
    banca_id: int | None = None
    banca_nome: str | None = None
    saldo_inicial_centavos: int | None = None
    acumulado_anterior_centavos: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.contribuicao_centavos, bool) or not isinstance(
            self.contribuicao_centavos, int
        ):
            raise PainelInvalidoError("contribuicao_centavos precisa ser inteira")
        _id_positivo(self.banca_id, "banca_id")
        inicial = self.saldo_inicial_centavos
        if inicial is not None and (
            isinstance(inicial, bool) or not isinstance(inicial, int) or inicial < 0
        ):
            raise PainelInvalidoError("saldo_inicial_centavos precisa ser inteiro nao negativo")
        if isinstance(self.acumulado_anterior_centavos, bool) or not isinstance(
            self.acumulado_anterior_centavos, int
        ):
            raise PainelInvalidoError("acumulado_anterior_centavos precisa ser inteiro")

    @classmethod
    def de_linha(cls, linha: Mapping[str, object]) -> ContribuicaoEvolucao:
        return cls(
            periodo_inicio=_data_exata(linha.get("periodo_inicio"), "periodo_inicio"),
            contribuicao_centavos=_inteiro_exato(
                linha.get("contribuicao_centavos"), "contribuicao_centavos"
            ),
            banca_id=_id_agregado(linha.get("banca_id"), "banca_id"),
            banca_nome=_texto_opcional(linha.get("banca_nome"), "banca_nome"),
            saldo_inicial_centavos=_inteiro_opcional_exato(
                linha.get("saldo_inicial_centavos"), "saldo_inicial_centavos"
            ),
            acumulado_anterior_centavos=_inteiro_exato(
                linha.get("acumulado_anterior_centavos"),
                "acumulado_anterior_centavos",
            ),
        )


@dataclass(frozen=True)
class PontoEvolucao:
    periodo_inicio: date
    contribuicao_centavos: int
    acumulado_centavos: int
    banca_id: int | None
    banca_nome: str | None
    saldo_centavos: int | None


class AcumuladorEvolucaoOrdenada:
    """Acumula uma consulta ja ordenada sem reter toda a serie em memoria."""

    def __init__(self) -> None:
        self._iniciado = False
        self._banca_id: int | None = None
        self._banca_nome: str | None = None
        self._saldo_inicial_centavos: int | None = None
        self._acumulado_anterior_centavos = 0
        self._acumulado_centavos = 0
        self._ultimo_periodo: date | None = None
        self._bancas_encerradas: set[int | None] = set()

    def adicionar(self, item: ContribuicaoEvolucao) -> PontoEvolucao:
        metadados = (
            item.banca_nome,
            item.saldo_inicial_centavos,
            item.acumulado_anterior_centavos,
        )
        if not self._iniciado or item.banca_id != self._banca_id:
            if self._iniciado:
                self._bancas_encerradas.add(self._banca_id)
            if item.banca_id in self._bancas_encerradas:
                raise PainelInvalidoError("a evolucao precisa estar ordenada por banca")
            self._iniciado = True
            self._banca_id = item.banca_id
            self._banca_nome = item.banca_nome
            self._saldo_inicial_centavos = item.saldo_inicial_centavos
            self._acumulado_anterior_centavos = item.acumulado_anterior_centavos
            self._acumulado_centavos = item.acumulado_anterior_centavos
            self._ultimo_periodo = None
        elif metadados != (
            self._banca_nome,
            self._saldo_inicial_centavos,
            self._acumulado_anterior_centavos,
        ):
            raise PainelInvalidoError("a mesma banca veio com metadados diferentes")

        if self._ultimo_periodo is not None and item.periodo_inicio < self._ultimo_periodo:
            raise PainelInvalidoError("a evolucao precisa estar ordenada por periodo")
        self._ultimo_periodo = item.periodo_inicio
        self._acumulado_centavos += item.contribuicao_centavos
        saldo = (
            None
            if self._saldo_inicial_centavos is None
            else self._saldo_inicial_centavos + self._acumulado_centavos
        )
        return PontoEvolucao(
            periodo_inicio=item.periodo_inicio,
            contribuicao_centavos=item.contribuicao_centavos,
            acumulado_centavos=self._acumulado_centavos,
            banca_id=item.banca_id,
            banca_nome=item.banca_nome,
            saldo_centavos=saldo,
        )


def acumular_evolucao(
    contribuicoes: Iterable[ContribuicaoEvolucao],
) -> tuple[PontoEvolucao, ...]:
    """Agrupa e acumula contribuicoes separadamente por banca.

    O saldo so existe quando a banca tem saldo inicial conhecido. Isso nao e o
    saldo das contas das casas, cujo contrato separado e :class:`SaldoPainel`.
    """

    por_banca_dia: dict[tuple[int | None, date], int] = {}
    metadados: dict[int | None, tuple[str | None, int | None, int]] = {}
    for item in contribuicoes:
        atuais = metadados.setdefault(
            item.banca_id,
            (
                item.banca_nome,
                item.saldo_inicial_centavos,
                item.acumulado_anterior_centavos,
            ),
        )
        if atuais != (
            item.banca_nome,
            item.saldo_inicial_centavos,
            item.acumulado_anterior_centavos,
        ):
            raise PainelInvalidoError("a mesma banca veio com metadados diferentes")
        chave = (item.banca_id, item.periodo_inicio)
        por_banca_dia[chave] = por_banca_dia.get(chave, 0) + item.contribuicao_centavos

    pontos: list[PontoEvolucao] = []
    for banca_id in sorted(metadados, key=lambda valor: -1 if valor is None else valor):
        banca_nome, saldo_inicial, acumulado = metadados[banca_id]
        da_banca = sorted(
            (dia, valor)
            for (id_da_banca, dia), valor in por_banca_dia.items()
            if id_da_banca == banca_id
        )
        for periodo_inicio, contribuicao in da_banca:
            acumulado += contribuicao
            saldo = None if saldo_inicial is None else saldo_inicial + acumulado
            pontos.append(
                PontoEvolucao(
                    periodo_inicio,
                    contribuicao,
                    acumulado,
                    banca_id,
                    banca_nome,
                    saldo,
                )
            )
    return tuple(pontos)


def _instante_utc(valor: datetime) -> datetime:
    return valor.replace(tzinfo=UTC) if valor.tzinfo is None else valor.astimezone(UTC)


def segundos_decimais(delta: timedelta) -> Decimal:
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(micros) / Decimal(1_000_000)


@dataclass(frozen=True)
class FrescorPainel:
    atualizado_em: datetime | None
    idade_mv_segundos: Decimal | None
    respondido_em: datetime
    replica_atraso_segundos: Decimal | None


def calcular_frescor(
    atualizado_em: datetime | None,
    replica_atraso_segundos: object | None,
    *,
    respondido_em: datetime | None = None,
) -> FrescorPainel:
    """Separa idade da MV, hora da resposta e atraso de replay.

    ``pg_last_xact_replay_timestamp`` nao e horario de refresh. Em primary ou
    ambiente sem standby o atraso recebido e ``None`` e permanece assim.
    """

    resposta = _instante_utc(respondido_em or datetime.now(UTC))
    atualizado = None if atualizado_em is None else _instante_utc(atualizado_em)
    idade = None
    if atualizado is not None:
        idade = max(Decimal(0), segundos_decimais(resposta - atualizado))
    atraso = (
        None
        if replica_atraso_segundos is None
        else max(Decimal(0), decimal_exato(replica_atraso_segundos, "replica_atraso_segundos"))
    )
    return FrescorPainel(atualizado, idade, resposta, atraso)


@dataclass(frozen=True)
class SerieGrafico:
    chave: str
    label: str
    unidade: str
    data: tuple[int, ...]


@dataclass(frozen=True)
class MetricasGraficos:
    granularidade: GranularidadePainel
    labels: tuple[str, ...]
    datasets: tuple[SerieGrafico, ...]


def metricas_para_graficos(
    pontos: Iterable[PontoPeriodo],
    *,
    granularidade_esperada: GranularidadePainel | None = None,
) -> MetricasGraficos:
    ordenados = tuple(sorted(pontos, key=lambda ponto: ponto.periodo_inicio))
    granularidades = {ponto.granularidade for ponto in ordenados}
    if len(granularidades) > 1:
        raise PainelInvalidoError("uma serie nao pode misturar granularidades")
    granularidade = next(iter(granularidades), granularidade_esperada or GranularidadePainel.DIA)
    if granularidade_esperada is not None and granularidade != granularidade_esperada:
        raise PainelInvalidoError("a serie nao corresponde a granularidade esperada")
    labels = tuple(ponto.periodo_inicio.isoformat() for ponto in ordenados)
    return MetricasGraficos(
        granularidade=granularidade,
        labels=labels,
        datasets=(
            SerieGrafico(
                "lucro_centavos",
                "Lucro",
                "centavos",
                tuple(ponto.metricas.lucro_centavos for ponto in ordenados),
            ),
            SerieGrafico(
                "giro_centavos",
                "Giro",
                "centavos",
                tuple(ponto.metricas.giro_centavos for ponto in ordenados),
            ),
            SerieGrafico(
                "total_apostas",
                "Apostas",
                "quantidade",
                tuple(ponto.metricas.total_apostas for ponto in ordenados),
            ),
            SerieGrafico(
                "roi_basis_points",
                "ROI",
                "basis_points",
                tuple(ponto.metricas.roi_basis_points for ponto in ordenados),
            ),
            SerieGrafico(
                "win_rate_basis_points",
                "Taxa de acerto",
                "basis_points",
                tuple(ponto.metricas.win_rate_basis_points for ponto in ordenados),
            ),
        ),
    )


@dataclass(frozen=True)
class Painel:
    resumo: MetricasPainel
    saldo: SaldoPainel
    por_casa: tuple[GrupoPainel, ...]
    por_tipster: tuple[GrupoPainel, ...]
    por_mercado: tuple[GrupoPainel, ...]
    evolucao: tuple[PontoEvolucao, ...]
    frescor: FrescorPainel


@dataclass(frozen=True)
class LinhaExportacao:
    secao: SecaoExportacao
    valores: Mapping[str, object]
