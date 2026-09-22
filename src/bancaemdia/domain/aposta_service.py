from datetime import datetime
from typing import Any

from bancaemdia.domain.materializar import MOTIVO_APAGADA, EventoNovo
from bancaemdia.models.aposta import ESTADOS

# Só o que a pessoa decide entra por aqui. Nada calculado: `eventos` não se apaga, e um evento com
# campo calculado derrubava a projeção para sempre no projeto antigo.
CAMPOS_CORRIGIVEIS = frozenset({
    "conta_casa_id",
    "tipster_id",
    "time_casa_id",
    "time_fora_id",
    "mercado_id",
    "competicao_id",
    "casa",
    "evento",
    "descricao",
    "odd",
    "stake_unidades",
    "data_aposta",
    "data_jogo",
    "freebet",
    "comissao_centavos",
    # O estado só entra por aqui numa aposta marcada para revisão; a rota confere isso antes.
    "estado",
    "revisao_motivo",
    "revisao_grave",
})
# Uma lista só, usada pela rota e pelo trabalhador: duas cópias divergiram e a competição
# corrigida virava evento sem nunca chegar na linha da aposta.
CAMPOS_DE_ID = (
    "conta_casa_id",
    "tipster_id",
    "time_casa_id",
    "time_fora_id",
    "mercado_id",
    "competicao_id",
)
CAMPOS_DE_DATA = ("data_aposta", "data_jogo")
CAMPOS_DE_TEXTO = ("casa", "evento", "descricao", "revisao_motivo")
ID_BIGINT_MINIMO = -(2**63)
ID_BIGINT_MAXIMO = 2**63 - 1
ODD_MINIMA = 1.01
ODD_MAXIMA = 1000.0
FONTE_DA_PESSOA = "manual"


class ApostaInvalidaError(ValueError):
    pass


def _e_data(valor: object) -> bool:
    if not isinstance(valor, str):
        return False
    try:
        datetime.fromisoformat(valor)
    except ValueError:
        return False
    return True


def _comparavel(valor: object) -> object:
    return valor.isoformat() if isinstance(valor, datetime) else valor


def mudancas(atual: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
    # Só entra no histórico o que mudou: salvar sem mexer em nada não é correção, e um evento vazio
    # ficaria lá para sempre dizendo que houve uma.
    return {
        campo: valor
        for campo, valor in pedido.items()
        if _comparavel(valor) != _comparavel(atual.get(campo))
    }


def _numero(valor: object) -> bool:
    return isinstance(valor, int | float) and not isinstance(valor, bool)


def erro_de_id(campo: str, valor: object) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, int) or isinstance(valor, bool):
        return f"{campo} tem de ser um número"
    if not ID_BIGINT_MINIMO <= valor <= ID_BIGINT_MAXIMO:
        return f"{campo} tem de caber em um BIGINT"
    return None


def validar_correcao(pedido: dict[str, Any], atual: dict[str, Any]) -> None:
    fora = sorted(campo for campo in pedido if campo not in CAMPOS_CORRIGIVEIS)
    if fora:
        raise ApostaInvalidaError(f"campo que não pode ser corrigido: {', '.join(fora)}")
    problemas: list[str] = []
    if "odd" in pedido:
        odd = pedido["odd"]
        if not _numero(odd) or odd < ODD_MINIMA:
            problemas.append(f"a odd tem de ser pelo menos {ODD_MINIMA}")
        elif odd > ODD_MAXIMA:
            problemas.append(f"odd {odd:g} é alta demais para ser real")
    if "stake_unidades" in pedido:
        stake = pedido["stake_unidades"]
        if not _numero(stake) or stake <= 0:
            problemas.append("a stake em unidades tem de ser positiva")
    if "comissao_centavos" in pedido:
        comissao = pedido["comissao_centavos"]
        if not _numero(comissao) or comissao < 0:
            problemas.append("a comissão não pode ser negativa")
    # Cada campo é conferido ANTES de virar evento: `eventos` não se apaga, e um valor com o tipo
    # errado só apareceria na hora de gravar a linha, como erro 500 e com o histórico já sujo.
    for campo in CAMPOS_DE_ID:
        if campo in pedido and (problema := erro_de_id(campo, pedido[campo])) is not None:
            problemas.append(problema)
    for campo in CAMPOS_DE_DATA:
        if campo in pedido and pedido[campo] is not None and not _e_data(pedido[campo]):
            problemas.append(f"{campo} tem de ser uma data como 2026-09-20T21:00:00")
    for campo in CAMPOS_DE_TEXTO:
        if campo in pedido and pedido[campo] is not None and not isinstance(pedido[campo], str):
            problemas.append(f"{campo} tem de ser texto")
    if "estado" in pedido and pedido["estado"] not in ESTADOS:
        problemas.append(f"estado desconhecido: {pedido['estado']!r}")
    for campo in ("freebet", "revisao_grave"):
        if campo in pedido and not isinstance(pedido[campo], bool):
            problemas.append(f"{campo} só aceita sim ou não")
    if problemas:
        raise ApostaInvalidaError("; ".join(problemas))


def validar_resultado(
    estado: str,
    retorno_centavos: int | None,
    cashout_valor_centavos: int | None,
    comissao_centavos: int | None = None,
) -> None:
    problemas: list[str] = []
    # No cashout o valor é o que a casa pagou, já com o que ela tirou: aceitar uma comissão aqui
    # seria guardá-la sem efeito nenhum.
    if estado == "CASHOUT" and comissao_centavos is not None:
        problemas.append("cashout não usa comissão: o valor é o que a casa pagou")
    if estado not in ESTADOS:
        problemas.append(f"estado desconhecido: {estado!r}")
    if cashout_valor_centavos is not None and estado != "CASHOUT":
        problemas.append("cashout_valor_centavos só vale com estado CASHOUT")
    # Cashout não tem fórmula: o valor vem da casa, e sem ele a aposta ficaria com lucro inventado.
    if estado == "CASHOUT" and cashout_valor_centavos is None and retorno_centavos is None:
        problemas.append("cashout precisa do valor que a casa pagou")
    for nome, valor in (
        ("retorno_centavos", retorno_centavos),
        ("cashout_valor_centavos", cashout_valor_centavos),
    ):
        if valor is not None and valor < 0:
            problemas.append(f"{nome} não pode ser negativo")
    if problemas:
        raise ApostaInvalidaError("; ".join(problemas))


def eventos_da_correcao(diferencas: dict[str, Any]) -> list[EventoNovo]:
    if not diferencas:
        return []
    # Um evento, um idioma: `CORRECAO_MANUAL` com fonte `manual` é o que faz a releitura automática
    # nunca desfazer o que a pessoa digitou.
    return [EventoNovo("CORRECAO_MANUAL", FONTE_DA_PESSOA, dict(diferencas))]


def eventos_do_resultado(
    estado: str,
    retorno_centavos: int | None = None,
    comissao_centavos: int | None = None,
) -> list[EventoNovo]:
    if estado == "CASHOUT":
        return [
            EventoNovo(
                "CASHOUT_REGISTRADO", FONTE_DA_PESSOA, {"retorno_centavos": retorno_centavos or 0}
            )
        ]
    payload: dict[str, Any] = {"estado": estado}
    # `None` quer dizer "não estou falando disso": mandar 0 apagava a comissão gravada na criação e
    # inflava o retorno (mesma lição do projeto antigo).
    if retorno_centavos is not None:
        payload["retorno_centavos"] = retorno_centavos
    if comissao_centavos is not None:
        payload["comissao_centavos"] = comissao_centavos
    return [EventoNovo("RESULTADO_REGISTRADO", FONTE_DA_PESSOA, payload)]


def eventos_da_exclusao() -> list[EventoNovo]:
    return [EventoNovo("APOSTA_CANCELADA", FONTE_DA_PESSOA, {"motivo": MOTIVO_APAGADA})]


def eventos_da_restauracao() -> list[EventoNovo]:
    # "Voltar a contar como minha" já É `SELECAO_ALTERADA`, o mesmo evento da coluna `Fiz?` da
    # planilha antiga: um evento novo seria um segundo idioma para a mesma frase.
    return [EventoNovo("SELECAO_ALTERADA", FONTE_DA_PESSOA, {"selecionada": True})]
