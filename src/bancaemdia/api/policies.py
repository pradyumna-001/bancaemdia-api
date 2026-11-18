"""Uma política por operação, publicada como `x-bancaemdia-policy` (#213).

O cliente gera acesso, retry e identidade de idempotência só a partir deste documento; ele não
pode presumir pelo verbo HTTP. Operação nova sem entrada aqui quebra a geração do OpenAPI.
"""

from dataclasses import dataclass
from typing import Any, Literal

AccessMode = Literal["FULL_WRITE", "READ_ONLY"]
OperationKey = tuple[str, str]
EXTENSION = "x-bancaemdia-policy"
AMBOS: tuple[AccessMode, ...] = ("FULL_WRITE", "READ_ONLY")
SO_ESCRITA: tuple[AccessMode, ...] = ("FULL_WRITE",)
# Leitura segura: o cliente pode repetir sozinho uma falha de rede ou um 503.
TENTATIVAS_LEITURA = 3


@dataclass(frozen=True)
class Politica:
    acesso: tuple[AccessMode, ...]
    retry: bool
    identidade: str = "none"
    retencao: str | None = None
    recuperacao: str | None = None

    def documento(self) -> dict[str, Any]:
        return {
            "access_modes": list(self.acesso),
            "automatic_retry": {
                "allowed": self.retry,
                "max_attempts": TENTATIVAS_LEITURA if self.retry else None,
            },
            "idempotency": {
                "identity": self.identidade,
                "retention": self.retencao,
                "recovery": self.recuperacao,
            },
        }


LEITURA = Politica(AMBOS, retry=True)
ESCRITA = Politica(SO_ESCRITA, retry=False)
# Controles de identidade, privacidade e cobrança: funcionam em READ_ONLY e não se repetem sozinhos.
CONTROLE = Politica(AMBOS, retry=False)
CALCULO = Politica(AMBOS, retry=True)
CHAVE = "header:Idempotency-Key"
RETENCAO_CONTA = "until_account_deletion"


def _por_caminho(parametro: str, recuperacao: str) -> Politica:
    # Repetir o mesmo DELETE/restaurar não empilha evento: a própria aposta é a identidade.
    return Politica(
        SO_ESCRITA,
        retry=True,
        identidade=f"path:{parametro}",
        retencao="while_resource_exists",
        recuperacao=recuperacao,
    )


def _intencao(parametro: str, recuperacao: str) -> Politica:
    # UUID da intenção escolhido antes do POST: após resposta perdida, consultar o recibo;
    # os contratos de manual, resultados, revisão e planilha proíbem repetir a escrita sozinho.
    return Politica(
        SO_ESCRITA,
        retry=False,
        identidade=f"path:{parametro}",
        retencao=RETENCAO_CONTA,
        recuperacao=recuperacao,
    )


def _por_chave(recuperacao: str, retencao: str = RETENCAO_CONTA) -> Politica:
    return Politica(
        SO_ESCRITA, retry=True, identidade=CHAVE, retencao=retencao, recuperacao=recuperacao
    )


PRINT_ITEM = "GET /api/v1/prints/intencoes/{intent_id}/itens/{item_id}"


POLITICAS: dict[OperationKey, Politica] = {
    ("get", "/health"): LEITURA,
    ("get", "/ready"): LEITURA,
    ("get", "/metrics"): LEITURA,
    ("get", "/auth/start"): CONTROLE,
    ("get", "/auth/callback"): CONTROLE,
    ("get", "/auth/session"): LEITURA,
    ("post", "/auth/refresh"): CONTROLE,
    ("post", "/auth/logout"): CONTROLE,
    ("get", "/auth/jwks"): LEITURA,
    ("get", "/api/v1/admin/casas"): LEITURA,
    ("get", "/api/v1/admin/casas/export"): LEITURA,
    ("post", "/api/v1/catalogo/candidatos"): CONTROLE,
    ("get", "/api/v1/apostas"): LEITURA,
    ("post", "/api/v1/apostas"): ESCRITA,
    ("post", "/api/v1/apostas/importar-planilha"): ESCRITA,
    ("get", "/api/v1/apostas/{chave}"): LEITURA,
    ("patch", "/api/v1/apostas/{chave}"): ESCRITA,
    ("delete", "/api/v1/apostas/{chave}"): _por_caminho("chave", "GET /api/v1/apostas/{chave}"),
    ("post", "/api/v1/apostas/{chave}/restaurar"): _por_caminho(
        "chave", "GET /api/v1/apostas/{chave}"
    ),
    ("post", "/api/v1/apostas/{chave}/resultado"): ESCRITA,
    ("post", "/api/v1/consolidacoes"): ESCRITA,
    ("post", "/api/v1/consolidacoes/{relacao_id}/desvincular"): ESCRITA,
    ("get", "/api/v1/billing/status"): LEITURA,
    ("post", "/api/v1/billing/subscribe"): Politica(
        AMBOS, retry=True, identidade=CHAVE, retencao=RETENCAO_CONTA, recuperacao="replay"
    ),
    ("post", "/api/v1/billing/portal"): CONTROLE,
    ("post", "/api/v1/billing/cancel"): CONTROLE,
    ("post", "/api/v1/billing/webhook"): CONTROLE,
    ("post", "/api/v1/integrations/telegram/webhook"): CONTROLE,
    ("get", "/api/v1/caixa"): LEITURA,
    ("post", "/api/v1/caixa"): Politica(
        SO_ESCRITA, retry=True, identidade=CHAVE, retencao=RETENCAO_CONTA, recuperacao="replay"
    ),
    ("get", "/api/v1/caixa/extrato"): LEITURA,
    ("get", "/api/v1/caixa/saldo"): LEITURA,
    ("patch", "/api/v1/caixa/contas/{conta_casa_id}/banca"): ESCRITA,
    ("post", "/api/v1/calculadoras/cobertura-ao-vivo"): CALCULO,
    ("post", "/api/v1/calculadoras/distribuir-entre-resultados"): CALCULO,
    ("post", "/api/v1/calculadoras/mercado-justo"): CALCULO,
    ("post", "/api/v1/calculadoras/percentual-banca"): CALCULO,
    ("post", "/coleta"): ESCRITA,
    ("post", "/api/v1/coleta"): ESCRITA,
    ("post", "/api/v1/coleta/batches"): Politica(
        SO_ESCRITA,
        retry=True,
        identidade="body:batch_id",
        retencao=RETENCAO_CONTA,
        recuperacao="GET /api/v1/coleta/jobs/{job_id}",
    ),
    ("get", "/api/v1/coleta/catalogo"): LEITURA,
    ("get", "/api/v1/coleta/contract"): LEITURA,
    ("get", "/api/v1/coleta/contract/schema"): LEITURA,
    ("get", "/api/v1/coleta/status"): LEITURA,
    ("get", "/api/v1/coleta/jobs/{job_id}"): LEITURA,
    ("post", "/api/v1/coleta/pairing-codes"): CONTROLE,
    ("post", "/api/v1/coleta/pairing-exchange"): CONTROLE,
    ("get", "/api/v1/coleta/installations"): LEITURA,
    ("delete", "/api/v1/coleta/installations/{instalacao_id}"): CONTROLE,
    ("post", "/api/v1/coleta/installations/{instalacao_id}/rotate"): CONTROLE,
    ("post", "/api/v1/coleta/sessions"): CONTROLE,
    ("get", "/api/v1/coleta/sessions/{sessao_id}"): LEITURA,
    ("delete", "/api/v1/coleta/sessions/{sessao_id}"): CONTROLE,
    ("get", "/api/v1/painel"): LEITURA,
    ("get", "/api/v1/painel/analises"): LEITURA,
    ("get", "/api/v1/painel/export"): LEITURA,
    ("get", "/api/v1/painel/metricas"): LEITURA,
    ("get", "/api/v1/painel/preferencias"): LEITURA,
    ("patch", "/api/v1/painel/preferencias"): ESCRITA,
    ("get", "/api/v1/painel/metas"): LEITURA,
    ("post", "/api/v1/painel/metas"): ESCRITA,
    ("get", "/api/v1/painel/metas/{meta_id}"): LEITURA,
    ("patch", "/api/v1/painel/metas/{meta_id}"): ESCRITA,
    ("delete", "/api/v1/painel/metas/{meta_id}"): ESCRITA,
    ("get", "/api/v1/revisao"): LEITURA,
    ("get", "/api/v1/revisao/stats"): LEITURA,
    ("get", "/api/v1/revisao/{revisao_id}"): LEITURA,
    ("get", "/api/v1/revisao/{revisao_id}/foto"): LEITURA,
    ("post", "/api/v1/revisao/{revisao_id}/resolver"): ESCRITA,
    ("get", "/api/v1/telegram/link"): LEITURA,
    ("delete", "/api/v1/telegram/link"): CONTROLE,
    ("post", "/api/v1/telegram/link-codes"): CONTROLE,
    ("get", "/api/v1/titulares"): LEITURA,
    ("post", "/api/v1/titulares"): ESCRITA,
    ("get", "/api/v1/titulares/financeiro"): LEITURA,
    ("get", "/api/v1/titulares/casas/{casa_id}/matriz"): LEITURA,
    ("post", "/api/v1/titulares/trocas/preview"): Politica(
        SO_ESCRITA, retry=True, identidade=CHAVE, retencao=RETENCAO_CONTA, recuperacao="replay"
    ),
    ("post", "/api/v1/titulares/trocas"): Politica(
        SO_ESCRITA, retry=True, identidade=CHAVE, retencao=RETENCAO_CONTA, recuperacao="replay"
    ),
    ("get", "/api/v1/titulares/{titular_id}"): LEITURA,
    ("patch", "/api/v1/titulares/{titular_id}"): ESCRITA,
    ("delete", "/api/v1/titulares/{titular_id}"): ESCRITA,
    ("get", "/api/v1/titulares/{titular_id}/matriz"): LEITURA,
    ("post", "/api/v1/titulares/{titular_id}/contas"): ESCRITA,
    ("patch", "/api/v1/titulares/{titular_id}/contas/{conta_id}"): ESCRITA,
    ("post", "/api/v1/titulares/{titular_id}/contas/{conta_id}/ativar"): ESCRITA,
    ("post", "/api/v1/upload"): ESCRITA,
    ("get", "/api/v1/upload/{job_id}"): LEITURA,
    ("get", "/api/v1/usuario/me/export"): LEITURA,
    ("delete", "/api/v1/usuario/me"): CONTROLE,
    ("get", "/api/v1/apostas/lotes/previa-apagar"): LEITURA,
    ("post", "/api/v1/apostas/lotes"): _por_chave("GET /api/v1/apostas/lotes/intencao"),
    ("get", "/api/v1/apostas/lotes/intencao"): LEITURA,
    ("get", "/api/v1/apostas/lotes/{lote_id}"): LEITURA,
    ("post", "/api/v1/apostas/lotes/{lote_id}/restaurar"): _por_chave(
        "GET /api/v1/apostas/lotes/intencao"
    ),
    ("get", "/api/v1/bancas"): LEITURA,
    ("post", "/api/v1/bancas"): _por_chave("GET /api/v1/bancas/intencao"),
    ("patch", "/api/v1/bancas/{banca_id}"): _por_chave("GET /api/v1/bancas/intencao"),
    ("post", "/api/v1/bancas/{banca_id}/arquivar"): _por_chave("GET /api/v1/bancas/intencao"),
    ("get", "/api/v1/bancas/intencao"): LEITURA,
    ("get", "/api/v1/unidade"): LEITURA,
    # O contrato da unidade guarda a chave enquanto a vigência existir.
    ("post", "/api/v1/unidade/vigencias"): _por_chave(
        "GET /api/v1/unidade/vigencias/intencao", retencao="while_resource_exists"
    ),
    ("get", "/api/v1/unidade/vigencias/intencao"): LEITURA,
    ("get", "/api/v1/titulares/trocas/intencao"): LEITURA,
    ("post", "/api/v1/apostas-manuais/operacoes/{intencao_id}"): _intencao(
        "intencao_id", "GET /api/v1/apostas-manuais/operacoes/{intencao_id}"
    ),
    ("get", "/api/v1/apostas-manuais/operacoes/{intencao_id}"): LEITURA,
    ("get", "/api/v1/apostas/{chave}/foto"): LEITURA,
    ("get", "/api/v1/prints/intencoes/{intent_id}"): LEITURA,
    ("get", "/api/v1/prints/intencoes/{intent_id}/itens/{item_id}"): LEITURA,
    # Mesmos bytes na mesma intenção+item não criam fatos; nunca reenviar sozinho.
    ("post", "/api/v1/prints/intencoes/{intent_id}/itens/{item_id}"): Politica(
        SO_ESCRITA,
        retry=False,
        identidade="path:item_id",
        retencao="while_resource_exists",
        recuperacao=PRINT_ITEM,
    ),
    ("post", "/api/v1/prints/intencoes/{intent_id}/itens/{item_id}/retry"): Politica(
        SO_ESCRITA,
        retry=False,
        identidade="body:retry_id",
        retencao="while_resource_exists",
        recuperacao=PRINT_ITEM,
    ),
    ("post", "/api/v1/prints/revisoes/{revisao_id}/resolver"): ESCRITA,
    ("get", "/api/v1/resultados/fila"): LEITURA,
    ("get", "/api/v1/resultados/apostas/{chave}"): LEITURA,
    ("post", "/api/v1/resultados/operacoes/{intention_id}"): _intencao(
        "intention_id", "GET /api/v1/resultados/operacoes/{intention_id}"
    ),
    ("post", "/api/v1/resultados/operacoes/{intention_id}/desfazer"): _intencao(
        "intention_id", "GET /api/v1/resultados/operacoes/{intention_id}"
    ),
    ("get", "/api/v1/resultados/operacoes/{intention_id}"): LEITURA,
    ("get", "/api/v1/revisao-operacional/fila"): LEITURA,
    ("get", "/api/v1/revisao-operacional/revisoes/{revisao_id}"): LEITURA,
    ("get", "/api/v1/revisao-operacional/revisoes/{revisao_id}/foto"): LEITURA,
    ("post", "/api/v1/revisao-operacional/operacoes/{operacao_id}"): _intencao(
        "operacao_id", "GET /api/v1/revisao-operacional/operacoes/{operacao_id}"
    ),
    ("get", "/api/v1/revisao-operacional/operacoes/{operacao_id}"): LEITURA,
    ("get", "/api/v1/painel/filtrado"): LEITURA,
    ("get", "/api/v1/painel/filtrado/metricas"): LEITURA,
    ("get", "/api/v1/painel/filtrado/export"): LEITURA,
    ("get", "/api/v1/painel/filtrado/diario"): LEITURA,
    ("get", "/api/v1/painel/filtrado/grupos"): LEITURA,
    ("get", "/api/v1/painel/filtrado/detalhado/export"): LEITURA,
    ("get", "/api/v1/painel/hipotetica"): LEITURA,
    ("get", "/api/v1/filtros/{dimensao}"): LEITURA,
    ("post", "/api/v1/grupos"): ESCRITA,
    ("patch", "/api/v1/grupos/{grupo_id}"): ESCRITA,
    ("put", "/api/v1/apostas/{chave}/grupos"): ESCRITA,
    ("get", "/api/v1/planilhas/campos"): LEITURA,
    ("get", "/api/v1/planilhas/fontes"): LEITURA,
    ("put", "/api/v1/planilhas/fontes/{source_id}"): _intencao(
        "source_id", "GET /api/v1/planilhas/fontes/{source_id}"
    ),
    ("get", "/api/v1/planilhas/fontes/{source_id}"): LEITURA,
    ("post", "/api/v1/planilhas/fontes/{source_id}/previas/{preview_id}"): _intencao(
        "preview_id", "GET /api/v1/planilhas/previas/{preview_id}"
    ),
    ("get", "/api/v1/planilhas/previas/{preview_id}"): LEITURA,
    ("get", "/api/v1/planilhas/previas/{preview_id}/linhas"): LEITURA,
    ("post", "/api/v1/planilhas/previas/{preview_id}/remapear/{new_preview_id}"): _intencao(
        "new_preview_id", "GET /api/v1/planilhas/previas/{preview_id}"
    ),
    ("post", "/api/v1/planilhas/confirmacoes/{confirmation_id}"): _intencao(
        "confirmation_id", "GET /api/v1/planilhas/confirmacoes/{confirmation_id}"
    ),
    ("get", "/api/v1/planilhas/confirmacoes/{confirmation_id}"): LEITURA,
    ("post", "/api/v1/coleta/reader-captures"): ESCRITA,
    ("get", "/api/v1/financeiro/nativo/contas"): LEITURA,
    ("post", "/api/v1/financeiro/nativo/contas"): ESCRITA,
    ("get", "/api/v1/financeiro/nativo/resumo"): LEITURA,
    ("get", "/api/v1/financeiro/nativo/apostas"): LEITURA,
}

# Fora do gate de usuário (_current_user): sem conta comercial (saúde, identidade, provedores,
# operador, cálculo puro) ou com credencial de instalação, que tem o próprio gate.
FORA_DO_USUARIO = frozenset({
    "/health",
    "/ready",
    "/metrics",
    "/auth/start",
    "/auth/callback",
    "/auth/session",
    "/auth/refresh",
    "/auth/logout",
    "/auth/jwks",
    "/coleta",
    "/api/v1/coleta",
    "/api/v1/coleta/batches",
    "/api/v1/coleta/reader-captures",
    "/api/v1/coleta/catalogo",
    "/api/v1/coleta/contract",
    "/api/v1/coleta/contract/schema",
    "/api/v1/coleta/status",
    "/api/v1/coleta/jobs/{job_id}",
    "/api/v1/coleta/sessions",
    "/api/v1/coleta/sessions/{sessao_id}",
    "/api/v1/coleta/pairing-exchange",
    "/api/v1/billing/webhook",
    "/api/v1/integrations/telegram/webhook",
    "/api/v1/admin/casas",
    "/api/v1/admin/casas/export",
    "/api/v1/catalogo/candidatos",
    "/api/v1/calculadoras/cobertura-ao-vivo",
    "/api/v1/calculadoras/distribuir-entre-resultados",
    "/api/v1/calculadoras/mercado-justo",
    "/api/v1/calculadoras/percentual-banca",
})

POLICY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["access_modes", "automatic_retry", "idempotency"],
    "description": (
        "Política da operação: modos de acesso comercial em que ela pode ter efeito, retry "
        "automático permitido ao cliente e identidade de idempotência com retenção e recuperação."
    ),
    "properties": {
        "access_modes": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "enum": ["FULL_WRITE", "READ_ONLY"]},
        },
        "automatic_retry": {
            "type": "object",
            "additionalProperties": False,
            "required": ["allowed", "max_attempts"],
            "properties": {
                "allowed": {"type": "boolean"},
                "max_attempts": {"type": ["integer", "null"], "minimum": 2},
            },
        },
        "idempotency": {
            "type": "object",
            "additionalProperties": False,
            "required": ["identity", "retention", "recovery"],
            "properties": {
                "identity": {
                    "type": "string",
                    "pattern": r"^(none|header:[A-Za-z][A-Za-z0-9-]*|path:[a-z_]+|body:[a-z_]+)$",
                },
                "retention": {
                    "type": ["string", "null"],
                    "enum": ["until_account_deletion", "while_resource_exists", None],
                },
                "recovery": {
                    "type": ["string", "null"],
                    "pattern": r"^(replay|GET /\S+)$",
                },
            },
        },
    },
}


def instalar(document: dict[str, Any], operations: list[tuple[str, str, dict[str, Any]]]) -> None:
    from bancaemdia.api.collection_contract import CONTRACT_PATHS
    from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse

    seen: set[OperationKey] = set()
    for method, path, operation in operations:
        key = (method, path)
        seen.add(key)
        try:
            politica = POLITICAS[key]
        except KeyError as error:
            raise RuntimeError(f"missing {EXTENSION} for {method.upper()} {path}") from error
        operation[EXTENSION] = politica.documento()
        responses = operation["responses"]
        if path in CONTRACT_PATHS:
            # Artefato congelado da extensão (v2): as respostas só mudam num release do contrato.
            continue
        if politica.acesso == SO_ESCRITA:
            # Só aqui o gate comercial pode recusar: 402 sem efeito.
            responses.setdefault(
                "402",
                {
                    "description": AUTHENTICATED_ERROR_RESPONSES[402]["description"],
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{ErrorResponse.__name__}"}
                        }
                    },
                },
            )
        else:
            # Leitura e controles nunca devolvem account_read_only.
            responses.pop("402", None)
    missing = set(POLITICAS).difference(seen)
    if missing:
        rendered = ", ".join(f"{method.upper()} {path}" for method, path in sorted(missing))
        raise RuntimeError(f"{EXTENSION} declared for operations that no longer exist: {rendered}")
    document.setdefault("components", {}).setdefault("schemas", {})["BancaemdiaPolicy"] = (
        POLICY_SCHEMA
    )
