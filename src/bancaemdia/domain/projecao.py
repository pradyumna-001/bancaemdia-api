"""Canonical, pure fold of an individual bet's append-only event history."""

from collections.abc import Iterable
from math import isfinite
from typing import Any

from bancaemdia.domain.financeiro import Aposta, Estado

MOTIVO_APAGADA = "você apagou esta aposta"
FONTES_DA_PESSOA = frozenset({"manual", "planilha"})
CAMPOS_DA_CRIACAO = (
    "chat_id",
    "message_id",
    "ordem_na_mensagem",
    "origem",
    "casa",
    "tipster",
    "evento",
    "descricao",
    "tipo_aposta",
    "odd",
    "odd_original",
    "comeca_em",
    "stake_unidades",
    "valor_unidade_centavos",
    "freebet",
    "selecionada",
    "revisao_motivo",
    "revisao_grave",
    "ao_vivo",
    "data_aposta",
    "midia_hash",
    "comissao_centavos",
    "mercado_bruto",
    "conta_casa_id",
    "tipster_id",
    "time_casa_id",
    "time_fora_id",
    "mercado_id",
    "competicao_id",
    "data_jogo",
    "versao_prompt",
    "fonte_atualizada_em",
    "linha_hash",
)
TIPOS_DE_APOSTA = frozenset({
    "APOSTA_CRIADA",
    "ODD_ALTERADA",
    "STAKE_ALTERADA",
    "RESULTADO_REGISTRADO",
    "CASHOUT_REGISTRADO",
    "APOSTA_ANULADA",
    "APOSTA_CANCELADA",
    "SELECAO_ALTERADA",
    "CORRECAO_MANUAL",
    "CLV_REGISTRADO",
    "REVISAO_RESOLVIDA",
})


class EventoIrrecuperavelError(ValueError):
    """History cannot be safely materialized."""


def _retorno_calculado(estado: dict[str, Any]) -> int | None:
    if estado.get("estado") not in set(Estado):
        return None
    return Aposta(
        stake_unidades=float(estado.get("stake_unidades") or 0.0),
        valor_unidade_centavos=int(estado.get("valor_unidade_centavos") or 0),
        odd=estado.get("odd"),
        estado=Estado(estado["estado"]),
        freebet=bool(estado.get("freebet")),
        comissao_centavos=int(estado.get("comissao_centavos") or 0),
    ).retorno_calculado()


def projetar(eventos: Iterable[tuple[str, str, dict[str, Any]]]) -> tuple[dict[str, Any], set[str]]:
    estado: dict[str, Any] = {
        "odd": None,
        "stake_unidades": 0.0,
        "revisao_grave": False,
        "selecionada": True,
    }
    protegidos: set[str] = set()
    for tipo, fonte, payload in eventos:
        if tipo == "APOSTA_CRIADA":
            estado.update({c: payload[c] for c in CAMPOS_DA_CRIACAO if payload.get(c) is not None})
        elif tipo == "ODD_ALTERADA":
            estado["odd"] = payload.get("para")
        elif tipo == "STAKE_ALTERADA":
            estado["stake_unidades"] = payload.get("para", estado["stake_unidades"])
        elif tipo in {"RESULTADO_REGISTRADO", "APOSTA_ANULADA"}:
            estado["estado"] = payload.get(
                "estado",
                "ANULADA" if tipo == "APOSTA_ANULADA" else estado.get("estado", "PENDENTE"),
            )
            estado["comissao_centavos"] = payload.get(
                "comissao_centavos", estado.get("comissao_centavos", 0)
            )
            estado["retorno_informado"] = payload.get("retorno_centavos") is not None
            estado["retorno_centavos"] = (
                payload["retorno_centavos"]
                if estado["retorno_informado"]
                else _retorno_calculado(estado)
            )
        elif tipo == "CASHOUT_REGISTRADO":
            estado["estado"] = "CASHOUT"
            estado["retorno_centavos"] = payload.get("retorno_centavos", 0)
            estado["retorno_informado"] = True
        elif tipo == "APOSTA_CANCELADA" and payload.get("motivo") not in (None, "", MOTIVO_APAGADA):
            estado["revisao_motivo"] = payload["motivo"]
            estado["revisao_grave"] = False
        elif tipo == "APOSTA_CANCELADA":
            estado["selecionada"] = False
        elif tipo == "SELECAO_ALTERADA":
            estado["selecionada"] = bool(payload.get("selecionada", True))
            for campo in ("parceira_chave", "duvida_de_par"):
                if campo in payload:
                    estado[campo] = payload[campo]
        elif tipo == "CORRECAO_MANUAL":
            estado.update(payload)
            if payload.get("retorno_centavos") is not None:
                estado["retorno_informado"] = True
            if fonte in FONTES_DA_PESSOA:
                protegidos.update(payload)
        if not estado.get("retorno_informado") and estado.get("estado", "PENDENTE") != "PENDENTE":
            estado["retorno_centavos"] = _retorno_calculado(estado)
    return estado, protegidos


def projetar_validado(
    eventos: Iterable[tuple[str, str, dict[str, Any]]],
) -> tuple[dict[str, Any], set[str]]:
    """Reject gaps and malformed events before any replay mutation."""
    historico = list(eventos)
    if not historico or historico[0][0] != "APOSTA_CRIADA":
        raise EventoIrrecuperavelError("aposta sem evento de criação")
    if sum(tipo == "APOSTA_CRIADA" for tipo, _, _ in historico) != 1:
        raise EventoIrrecuperavelError("criação duplicada")
    for tipo, _, payload in historico:
        if tipo not in TIPOS_DE_APOSTA or not isinstance(payload, dict):
            raise EventoIrrecuperavelError(f"evento desconhecido ou inválido: {tipo}")
        if tipo == "APOSTA_CRIADA" and (
            payload.get("origem") is None or payload.get("stake_unidades") is None
        ):
            raise EventoIrrecuperavelError("criação sem origem ou stake")
        if tipo == "CASHOUT_REGISTRADO" and payload.get("retorno_centavos") is None:
            raise EventoIrrecuperavelError("cashout sem retorno")
        if tipo == "ODD_ALTERADA" and payload.get("para") is None:
            raise EventoIrrecuperavelError("alteração de odd sem destino")
        if tipo == "STAKE_ALTERADA" and payload.get("para") is None:
            raise EventoIrrecuperavelError("alteração de stake sem destino")
    estado, protegidos = projetar(historico)
    if estado.get("origem") not in {"telegram", "print", "manual", "planilha", "casa"}:
        raise EventoIrrecuperavelError("origem da aposta desconhecida")
    if estado.get("estado", "PENDENTE") not in set(Estado):
        raise EventoIrrecuperavelError("estado da aposta desconhecido")
    stake = estado.get("stake_unidades")
    if not isinstance(stake, (int, float)) or not isfinite(stake) or stake < 0:
        raise EventoIrrecuperavelError("stake inválida")
    unidade = estado.get("valor_unidade_centavos")
    if unidade is not None and (not isinstance(unidade, int) or unidade <= 0):
        raise EventoIrrecuperavelError("unidade inválida")
    retorno = estado.get("retorno_centavos")
    if retorno is not None and (not isinstance(retorno, int) or retorno < 0):
        raise EventoIrrecuperavelError("retorno inválido")
    return estado, protegidos
