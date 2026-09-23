from dataclasses import dataclass
from typing import Any

from bancaemdia.domain.aposta_service import (
    ApostaInvalidaError,
    eventos_da_correcao,
    eventos_da_exclusao,
    mudancas,
    validar_correcao,
)
from bancaemdia.domain.materializar import EventoNovo

ACOES = frozenset({"CORRIGIR", "DESCARTAR"})
CAMPOS_DA_FILA = frozenset({"revisao_motivo", "revisao_grave"})


class ResolucaoInvalidaError(ValueError):
    pass


@dataclass(frozen=True)
class PlanoResolucao:
    acao: str
    eventos: tuple[EventoNovo, ...]
    campos_corrigidos: tuple[str, ...]


def chave_da_aposta(extracao_bruta: dict[str, Any] | None) -> str | None:
    if not isinstance(extracao_bruta, dict):
        return None
    chave = extracao_bruta.get("aposta_chave")
    return chave if isinstance(chave, str) and chave.strip() else None


def planejar_resolucao(
    acao: str,
    aposta_corrigida: dict[str, Any] | None,
    estado_atual: dict[str, Any],
) -> PlanoResolucao:
    acao = acao.strip().upper()
    if acao not in ACOES:
        raise ResolucaoInvalidaError(f"ação desconhecida: {acao!r}")

    correcoes = dict(aposta_corrigida or {})
    internos = sorted(CAMPOS_DA_FILA.intersection(correcoes))
    if internos:
        raise ResolucaoInvalidaError(
            f"campo controlado pela fila de revisão: {', '.join(internos)}"
        )

    if acao == "DESCARTAR":
        if correcoes:
            raise ResolucaoInvalidaError("descartar não aceita correções da aposta")
        # A correção tira a retenção; o cancelamento seguro da #27 tira a linha das contas sem
        # apagá-la. Nesta ordem, o estado final fica limpo e ainda pode ser restaurado pelo log.
        eventos = (
            EventoNovo(
                "CORRECAO_MANUAL",
                "manual",
                {"revisao_motivo": None, "revisao_grave": False},
            ),
            *eventos_da_exclusao(),
        )
        return PlanoResolucao(acao, eventos, ())

    # Cashout precisa do valor realmente pago pela casa. A correção genérica só mudaria o estado
    # e deixaria o retorno indefinido; depois de confirmar a revisão, use a rota de resultado.
    if correcoes.get("estado") == "CASHOUT":
        raise ResolucaoInvalidaError(
            "cashout precisa do valor pago — confirme a revisão e registre o resultado"
        )

    try:
        validar_correcao(correcoes, estado_atual)
    except ApostaInvalidaError as recusa:
        raise ResolucaoInvalidaError(str(recusa)) from recusa

    alterados = mudancas(estado_atual, correcoes)
    limpar_fila = mudancas(
        estado_atual,
        {"revisao_motivo": None, "revisao_grave": False},
    )
    eventos = tuple(eventos_da_correcao({**alterados, **limpar_fila}))
    if estado_atual.get("duvida_de_par"):
        # CORRIGIR confirms this is a distinct bet. Restore it to the totals and clear the
        # pending-pair marker in the same append-only transaction as the review resolution.
        eventos += (
            EventoNovo(
                "SELECAO_ALTERADA",
                "manual",
                {"selecionada": True, "duvida_de_par": False},
            ),
        )
    return PlanoResolucao(acao, eventos, tuple(sorted(alterados)))


def evento_da_resolucao(
    revisao_id: int,
    acao: str,
    motivo: str,
    campos_corrigidos: tuple[str, ...],
) -> EventoNovo:
    return EventoNovo(
        "REVISAO_RESOLVIDA",
        "manual",
        {
            "revisao_id": revisao_id,
            "acao": acao,
            "motivo": motivo,
            "campos_corrigidos": list(campos_corrigidos),
        },
    )
