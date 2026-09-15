from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

# A casa manda instantes absolutos, e o produto guarda a hora local de quem apostou, como o
# export do Telegram. O Brasil não tem horário de verão desde 2019, e `zoneinfo` exigiria `tzdata`
# no Windows.
FUSO_DA_CASA = timezone(timedelta(hours=-3))
SEM_ESCOLHA_LIDA = "(sem escolha lida)"


class ColetaInvalidaError(ValueError):
    pass


@dataclass(frozen=True)
class Escolha:
    descricao: str
    mercado: str | None = None
    mercado_id: int | None = None
    linha: float | None = None
    odd: float | None = None
    resultado: str | None = None
    evento_id: str | None = None
    evento: str | None = None


@dataclass(frozen=True)
class Coletada:
    identidade: str
    casa: str
    tipo: str
    odd: float
    stake_centavos: int
    data_aposta: str
    escolhas: tuple[Escolha, ...]
    evento: str | None = None
    mercado_bruto: str | None = None
    descricao: str = ""
    comeca_em: str | None = None
    estado: str = "PENDENTE"
    retorno_centavos: int | None = None
    observacao: str | None = None
    motivo_retencao: str | None = None
    bruto: dict[str, Any] = field(default_factory=dict, repr=False)


def instante(ms: object, campo: str) -> str:
    if not isinstance(ms, (int, float)) or ms <= 0:
        raise ColetaInvalidaError(f"o campo {campo} não veio como instante: {ms!r}")
    quando = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return quando.astimezone(FUSO_DA_CASA).replace(tzinfo=None).isoformat(timespec="seconds")


def instante_iso(texto: object, campo: str) -> str:
    if not isinstance(texto, str) or not texto.strip():
        raise ColetaInvalidaError(f"o campo {campo} não veio como instante: {texto!r}")
    try:
        quando = datetime.fromisoformat(texto.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ColetaInvalidaError(f"o campo {campo} não veio como instante: {texto!r}") from None
    if quando.tzinfo is None:
        # Sem fuso declarado não se adivinha: a casa que mandar assim vira decisão nova, medida.
        raise ColetaInvalidaError(
            f"o campo {campo} veio sem fuso ({texto!r}) — não sei de que relógio ele é"
        )
    return quando.astimezone(FUSO_DA_CASA).replace(tzinfo=None).isoformat(timespec="seconds")


def mais_cedo(instantes: list[str | None]) -> str | None:
    # A ordem de um array não é contrato: pegar o primeiro jogo do JSON punha a mesma aposta em
    # dias diferentes conforme a casa reordenasse. O texto ISO local compara em ordem cronológica.
    return min((i for i in instantes if i), default=None)


def data_que_conta(comeca_em: str | None, colocacao: str) -> str:
    # Decisão do dono (10/08/2026): a aposta da casa conta no dia do JOGO, e a colocação é o
    # reserva. Vale só para esta origem; as outras não mudam dinheiro já gravado.
    return comeca_em or colocacao


def ja_acabou(
    bruto: dict[str, Any], *, resultado_em: str = "finalBetResult", liquidada_em: str = "settledAt"
) -> bool:
    # O código de cashout descreve a OFERTA, que existe enquanto a aposta corre: perguntá-lo
    # primeiro fez 34 apostas em curso virarem cashout, 15 delas com retorno zero.
    return bool(bruto.get(resultado_em)) or bool(bruto.get(liquidada_em))


def centavos(valor: object, campo: str) -> int:
    if not isinstance(valor, (int, float)):
        raise ColetaInvalidaError(f"o campo {campo} não veio como número: {valor!r}")
    return round(float(valor) * 100)


def tipo_da_aposta(escolhas: list[Escolha]) -> str:
    # O rótulo da casa não serve: veio "Simples" com duas escolhas do mesmo jogo, que é Criar
    # Aposta. Quem decide é o evento de cada escolha.
    if len(escolhas) <= 1:
        return "SIMPLES"
    ids = {e.evento_id for e in escolhas if e.evento_id}
    if len(ids) <= 1:
        return "CRIAR_APOSTA"
    return "MULTIPLA"


def descricao_das_escolhas(escolhas: list[Escolha]) -> str:
    partes = [
        e.descricao + (f" ({e.mercado})" if e.mercado else "") for e in escolhas if e.descricao
    ]
    return " + ".join(partes) if partes else SEM_ESCOLHA_LIDA


def primeiro_evento(escolhas: list[Escolha]) -> str | None:
    return next((e.evento for e in escolhas if e.evento), None)


def primeiro_mercado(escolhas: list[Escolha]) -> str | None:
    return next((e.mercado for e in escolhas if e.mercado), None)
