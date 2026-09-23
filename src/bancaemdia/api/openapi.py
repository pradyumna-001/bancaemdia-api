from collections.abc import Iterable
from typing import Any, cast

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from bancaemdia.domain.vocabulario import CASAS

HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
COLLECTION_PATHS = frozenset({"/coleta", "/api/v1/coleta"})

type OperationKey = tuple[str, str]
type JsonObject = dict[str, Any]

MANUAL_BET_HOUSES = sorted(
    {name for name, _ in CASAS}
    | {name.lower() for name, _ in CASAS}
    | {domain.split(".", maxsplit=1)[0] for _, domain in CASAS}
)


OPERATION_DOCUMENTATION: dict[OperationKey, tuple[str, str]] = {
    ("post", "/api/v1/telegram/link-codes"): (
        "Emitir código de vínculo Telegram",
        "Invalida códigos anteriores e retorna um código de oito caracteres válido por 30 minutos uma única vez.",
    ),
    ("get", "/api/v1/telegram/link"): (
        "Consultar vínculo Telegram",
        "Consulta o vínculo ativo da conta sem expor identificadores do Telegram.",
    ),
    ("delete", "/api/v1/telegram/link"): (
        "Revogar vínculo Telegram",
        "Revoga o vínculo ativo para impedir novo ingresso desta identidade.",
    ),
    ("post", "/coleta"): (
        "Receber coleta da extensão",
        "Recebe um lote bruto capturado pela extensão e agenda a materialização idempotente.",
    ),
    ("post", "/api/v1/coleta"): (
        "Receber coleta da extensão",
        "Alias versionado para receber um lote bruto e agendar sua materialização idempotente.",
    ),
    ("post", "/api/v1/upload"): (
        "Enviar exportação do Telegram",
        "Valida e guarda uma exportação do Telegram antes de agendar seu processamento assíncrono.",
    ),
    ("get", "/api/v1/upload/{job_id}"): (
        "Consultar processamento do upload",
        "Retorna estado, progresso, contagens e custo observado de um upload do usuário.",
    ),
    ("get", "/api/v1/apostas"): (
        "Listar apostas",
        "Lista apostas do usuário com filtros temporais, dimensionais e paginação.",
    ),
    ("post", "/api/v1/apostas"): (
        "Criar aposta manual",
        "Cria uma aposta manual e registra o primeiro evento em seu histórico auditável.",
    ),
    ("post", "/api/v1/apostas/importar-planilha"): (
        "Importar apostas do Excel",
        "Importa um XLSX com origem_id estável; identifica cada aposta pela aba e linha e aplica somente versões mais recentes.",
    ),
    ("get", "/api/v1/apostas/{chave}"): (
        "Consultar aposta",
        "Retorna a aposta materializada, suas seleções, eventos e eventual revisão pendente.",
    ),
    ("patch", "/api/v1/apostas/{chave}"): (
        "Corrigir aposta",
        "Acrescenta uma correção manual ao histórico e rematerializa a aposta.",
    ),
    ("delete", "/api/v1/apostas/{chave}"): (
        "Excluir aposta da apuração",
        "Marca a aposta para não participar da apuração sem apagar seu histórico.",
    ),
    ("post", "/api/v1/apostas/{chave}/resultado"): (
        "Registrar resultado da aposta",
        "Registra liquidação, cashout ou reabertura e rematerializa os valores financeiros.",
    ),
    ("post", "/api/v1/apostas/{chave}/restaurar"): (
        "Restaurar aposta na apuração",
        "Volta a incluir uma aposta anteriormente excluída, preservando todo o histórico.",
    ),
    ("post", "/api/v1/caixa"): (
        "Registrar movimento de caixa",
        "Registra depósito, saque, ajuste ou transferência como lançamentos auditáveis.",
    ),
    ("get", "/api/v1/caixa"): (
        "Listar movimentos de caixa",
        "Lista movimentos financeiros do usuário com filtros e paginação.",
    ),
    ("get", "/api/v1/caixa/saldo"): (
        "Consultar saldos",
        "Calcula saldos conhecidos das contas a partir de movimentos e apostas.",
    ),
    ("patch", "/api/v1/caixa/contas/{conta_casa_id}/banca"): (
        "Vincular conta à banca",
        "Associa ou desassocia uma conta da casa a uma banca do mesmo usuário.",
    ),
    ("get", "/api/v1/caixa/extrato"): (
        "Consultar extrato",
        "Combina movimentos de caixa e liquidações de apostas numa linha do tempo paginada.",
    ),
    ("get", "/api/v1/painel"): (
        "Consultar painel financeiro",
        "Lê agregados materializados e informa separadamente frescor dos dados e atraso da réplica.",
    ),
    ("get", "/api/v1/painel/metricas"): (
        "Consultar séries para gráficos",
        "Retorna séries temporais já agregadas para os gráficos do painel.",
    ),
    ("get", "/api/v1/painel/export"): (
        "Exportar painel em Excel",
        "Gera um XLSX write-only e o transmite sem manter o arquivo completo em memória.",
    ),
    ("get", "/api/v1/revisao/stats"): (
        "Consultar estatísticas de revisão",
        "Retorna volume, motivos e idade da fila de revisões pendentes do usuário.",
    ),
    ("get", "/api/v1/revisao"): (
        "Listar revisões pendentes",
        "Lista revisões do usuário por motivo, intervalo e paginação.",
    ),
    ("get", "/api/v1/revisao/{revisao_id}/foto"): (
        "Consultar foto da revisão",
        "Transmite a imagem privada ligada a uma revisão pendente.",
    ),
    ("get", "/api/v1/revisao/{revisao_id}"): (
        "Consultar revisão",
        "Retorna os dados auditáveis de uma revisão pertencente ao usuário.",
    ),
    ("post", "/api/v1/titulares/trocas/preview"): (
        "Prévia de troca de conta",
        "Mostra as apostas cuja atribuição difere após a troca, sem alterar as contas.",
    ),
    ("post", "/api/v1/titulares/trocas"): (
        "Trocar conta da casa",
        "Aplica uma prévia feita com a mesma chave; fecha a origem e abre o destino no instante escolhido.",
    ),
    ("post", "/api/v1/titulares"): (
        "Criar titular",
        "Cria um titular pertencente ao usuário autenticado.",
    ),
    ("get", "/api/v1/titulares"): (
        "Listar titulares",
        "Lista titulares do usuário com busca e paginação.",
    ),
    ("get", "/api/v1/titulares/casas/{casa_id}/matriz"): (
        "Consultar titulares por casa",
        "Lista contas, estados e histórico de uso nesta casa; inclui titulares sem conta.",
    ),
    ("get", "/api/v1/titulares/financeiro"): (
        "Consultar financeiro por titular e conta",
        "Concilia apostas por conta, titular e usuário em centavos; mantém caixa e não atribuídas separados.",
    ),
    ("get", "/api/v1/titulares/{titular_id}"): (
        "Consultar titular",
        "Retorna um titular pertencente ao usuário.",
    ),
    ("patch", "/api/v1/titulares/{titular_id}"): (
        "Editar titular",
        "Atualiza o nome de um titular ativo.",
    ),
    ("delete", "/api/v1/titulares/{titular_id}"): (
        "Arquivar titular",
        "Arquiva um titular que não tenha conta em uso.",
    ),
    ("get", "/api/v1/titulares/{titular_id}/matriz"): (
        "Consultar casas do titular",
        "Lista contas estáveis, intervalos de uso e ações disponíveis do titular.",
    ),
    ("post", "/api/v1/titulares/{titular_id}/contas"): (
        "Criar conta da casa",
        "Cria uma conta estável da casa para o titular.",
    ),
    ("patch", "/api/v1/titulares/{titular_id}/contas/{conta_id}"): (
        "Editar conta da casa",
        "Altera apelido ou estado de uma conta fora de uso.",
    ),
    ("post", "/api/v1/titulares/{titular_id}/contas/{conta_id}/ativar"): (
        "Ativar primeira conta da casa",
        "Abre o primeiro intervalo de uso no instante atual quando a casa está livre.",
    ),
    ("get", "/api/v1/usuario/me/export"): (
        "Exportar meus registros",
        "Baixa perfil e registros vinculados à conta em JSON ou Excel, sem arquivos binários.",
    ),
    ("delete", "/api/v1/usuario/me"): (
        "Desativar e anonimizar minha conta",
        "Remove registros vinculados e desativa a conta; dados brutos sem dono exclusivo exigem atendimento separado.",
    ),
    ("post", "/api/v1/revisao/{revisao_id}/resolver"): (
        "Resolver revisão",
        "Corrige ou descarta uma revisão sob trava transacional e registra os eventos resultantes.",
    ),
    ("get", "/health"): (
        "Consultar liveness",
        "Confirma que o processo HTTP está vivo sem consultar dependências externas.",
    ),
    ("get", "/ready"): (
        "Consultar readiness",
        "Verifica de forma limitada e concorrente as dependências necessárias para receber tráfego.",
    ),
    ("get", "/metrics"): (
        "Exportar métricas Prometheus",
        "Expõe métricas da API, filas, materialização e circuit breakers no formato Prometheus.",
    ),
}


PARAMETER_DESCRIPTIONS = {
    "Idempotency-Key": (
        "Chave opaca obrigatória do cliente; reutilizá-la com o mesmo corpo reproduz a resposta "
        "original sem repetir a operação."
    ),
    "ate": "Limite final exclusivo do intervalo, em ISO 8601.",
    "casa_id": "Identificador canônico da casa usada como filtro.",
    "chave": "Chave estável e opaca da aposta.",
    "competicao_id": "Identificador canônico da competição usada como filtro.",
    "conta_casa_id": "Identificador da conta da casa usada como filtro.",
    "conta_id": "Identificador da conta estável da casa pertencente ao titular.",
    "data_corte": "Data civil opcional para reconstruir o saldo histórico.",
    "desde": "Limite inicial inclusivo do intervalo, em ISO 8601.",
    "estado": "Estado de liquidação da aposta usado como filtro.",
    "fresh": "Lê no primário quando verdadeiro, sem forçar refresh das materialized views.",
    "formato": "Formato da exportação dos dados da conta: JSON ou Excel.",
    "incluir_apagadas": "Inclui apostas retiradas da apuração quando verdadeiro.",
    "include_archived": "Inclui titulares arquivados na listagem quando verdadeiro.",
    "job_id": "UUID público retornado quando o upload foi aceito.",
    "mercado_id": "Identificador canônico do mercado usado como filtro.",
    "motivo": "Texto do motivo usado para filtrar a fila de revisões.",
    "origem": "Canal de origem da aposta usado como filtro.",
    "page": "Número da página, começando em 1.",
    "page_size": "Quantidade máxima de itens retornados na página.",
    "periodo": "Janela civil de agregação no fuso America/Sao_Paulo.",
    "revisao_grave": "Filtra apostas pela marca de revisão grave.",
    "revisao_id": "Identificador numérico da revisão pertencente ao usuário.",
    "search": "Busca textual pelo nome do titular.",
    "titular_id": "Identificador do titular pertencente ao usuário.",
    "tipo": "Tipo de movimento de caixa usado como filtro.",
    "tipster_id": "Identificador canônico do tipster usado como filtro.",
}


REQUEST_EXAMPLES: dict[OperationKey, tuple[str, JsonObject]] = {
    ("post", "/api/v1/titulares"): ("Criar titular", {"nome": "Ana"}),
    ("patch", "/api/v1/titulares/{titular_id}"): (
        "Renomear titular",
        {"nome": "Ana Silva"},
    ),
    ("post", "/api/v1/titulares/{titular_id}/contas"): (
        "Criar conta estável",
        {"casa_id": 7, "apelido": "Ana Betano"},
    ),
    ("patch", "/api/v1/titulares/{titular_id}/contas/{conta_id}"): (
        "Limitar conta",
        {"estado": "LIMITADA"},
    ),
    ("patch", "/api/v1/caixa/contas/{conta_casa_id}/banca"): (
        "Vincular conta à banca",
        {"banca_id": 12},
    ),
    ("post", "/api/v1/apostas"): (
        "Aposta manual",
        {
            "casa": "betano",
            "data_aposta": "2026-09-22T14:30:00-03:00",
            "odd": 1.95,
            "stake_unidades": 1.0,
            "evento": "Time A x Time B",
            "descricao": "Time A vence",
            "mercado_bruto": "resultado final",
            "freebet": False,
        },
    ),
    ("patch", "/api/v1/apostas/{chave}"): (
        "Correção de odd e descrição",
        {"odd": 2.05, "descricao": "Time A vence", "tipster_id": 17},
    ),
    ("post", "/api/v1/apostas/{chave}/resultado"): (
        "Liquidação green",
        {"estado": "GREEN", "retorno_centavos": 19_500},
    ),
    ("post", "/api/v1/caixa"): (
        "Depósito",
        {
            "tipo": "DEPOSITO",
            "valor_centavos": 100_000,
            "conta_casa_id": 42,
            "ocorrido_em": "2026-09-22T10:00:00-03:00",
            "descricao": "Depósito inicial",
        },
    ),
    ("post", "/api/v1/revisao/{revisao_id}/resolver"): (
        "Corrigir e resolver",
        {"acao": "CORRIGIR", "aposta_corrigida": {"odd": 2.05}},
    ),
    ("post", "/api/v1/titulares/trocas/preview"): (
        "Prévia de troca de conta",
        {
            "casa_id": 3,
            "conta_origem_id": 42,
            "conta_destino_id": 43,
            "efetiva_em": "2026-09-22T14:30:00-03:00",
            "estado_origem": "LIMITADA",
        },
    ),
    ("post", "/api/v1/titulares/trocas"): (
        "Aplicar troca após prévia",
        {
            "casa_id": 3,
            "conta_origem_id": 42,
            "conta_destino_id": 43,
            "efetiva_em": "2026-09-22T14:30:00-03:00",
            "estado_origem": "LIMITADA",
        },
    ),
}


def _object(value: object, *, context: str) -> JsonObject:
    if not isinstance(value, dict):
        raise RuntimeError(f"OpenAPI {context} must be an object")
    return cast(JsonObject, value)


def _operations(document: JsonObject) -> Iterable[tuple[str, str, JsonObject]]:
    paths = _object(document.get("paths"), context="paths")
    for path, path_item_value in paths.items():
        path_item = _object(path_item_value, context=f"path {path}")
        for method, operation_value in path_item.items():
            if method in HTTP_METHODS:
                yield (
                    method,
                    path,
                    _object(
                        operation_value,
                        context=f"operation {method.upper()} {path}",
                    ),
                )


def _json_value_schema() -> JsonObject:
    reference = {"$ref": "#/components/schemas/JsonValue"}
    return {
        "title": "JsonValue",
        "description": "A recursively typed JSON value; never an unconstrained Any schema.",
        "oneOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "array", "items": reference},
            {"type": "object", "additionalProperties": reference},
            {"type": "null"},
        ],
    }


def _nullable(schema: JsonObject) -> JsonObject:
    return {"anyOf": [schema, {"type": "null"}]}


def _correction_schema(*, review: bool) -> JsonObject:
    identifier = _nullable({"type": "integer", "minimum": 1})
    text = _nullable({"type": "string"})
    properties: JsonObject = {
        "conta_casa_id": identifier,
        "tipster_id": identifier,
        "time_casa_id": identifier,
        "time_fora_id": identifier,
        "mercado_id": identifier,
        "competicao_id": identifier,
        "casa": text,
        "evento": text,
        "descricao": text,
        "odd": {"type": "number", "minimum": 1.01, "maximum": 1000},
        "stake_unidades": {"type": "number", "exclusiveMinimum": 0},
        "data_aposta": _nullable({"type": "string", "format": "date-time"}),
        "data_jogo": _nullable({"type": "string", "format": "date-time"}),
        "freebet": {"type": "boolean"},
        "comissao_centavos": {"type": "integer", "minimum": 0},
        "estado": {
            "type": "string",
            "enum": [
                "PENDENTE",
                "GREEN",
                "RED",
                "ANULADA",
                "MEIO_GREEN",
                "MEIO_RED",
                "CASHOUT",
            ],
        },
    }
    if not review:
        properties.update({
            "revisao_motivo": text,
            "revisao_grave": {"type": "boolean"},
        })
    return {
        "type": "object",
        "title": "CorrecaoDaRevisao" if review else "Correcao",
        "description": "Campos de domínio que podem ser corrigidos; campos desconhecidos são recusados.",
        "properties": properties,
        "additionalProperties": False,
    }


def _configure_domain_request_schemas(schemas: JsonObject) -> None:
    manual = _object(schemas["ApostaManual"], context="ApostaManual schema")
    manual["required"] = ["casa", "odd", "stake_unidades"]
    manual_properties = _object(manual["properties"], context="ApostaManual properties")
    manual_properties["casa"] = {
        "type": "string",
        "enum": MANUAL_BET_HOUSES,
        "description": "Nome canônico, grafia minúscula ou domínio oficial da casa.",
    }
    manual_properties["odd"] = {"type": "number", "minimum": 1.01, "maximum": 1000}
    manual_properties["stake_unidades"] = {"type": "number", "exclusiveMinimum": 0}
    manual_properties["data_aposta"] = _nullable({"type": "string", "format": "date-time"})
    manual_properties["comissao_centavos"] = _nullable({"type": "integer", "minimum": 0})

    result = _object(schemas["Resultado"], context="Resultado schema")
    result_properties = _object(result["properties"], context="Resultado properties")
    result_properties["estado"] = {
        "type": "string",
        "enum": [
            "PENDENTE",
            "GREEN",
            "RED",
            "ANULADA",
            "MEIO_GREEN",
            "MEIO_RED",
            "CASHOUT",
        ],
    }
    for field in ("retorno_centavos", "cashout_valor_centavos", "comissao_centavos"):
        result_properties[field] = _nullable({"type": "integer", "minimum": 0})
    result["allOf"] = [
        {
            "if": {
                "properties": {"estado": {"const": "CASHOUT"}},
                "required": ["estado"],
            },
            "then": {
                "anyOf": [
                    {
                        "properties": {"retorno_centavos": {"type": "integer", "minimum": 0}},
                        "required": ["retorno_centavos"],
                    },
                    {
                        "properties": {"cashout_valor_centavos": {"type": "integer", "minimum": 0}},
                        "required": ["cashout_valor_centavos"],
                    },
                ],
                "properties": {"comissao_centavos": {"type": "null"}},
            },
            "else": {"properties": {"cashout_valor_centavos": {"type": "null"}}},
        }
    ]


def _install_manual_request_bodies(document: JsonObject) -> None:
    paths = _object(document["paths"], context="paths")
    collection_schema: JsonObject = {
        "type": "object",
        "required": ["contrato", "casa", "apostas"],
        "properties": {
            "contrato": {"type": "integer", "const": 1},
            "casa": {"type": "string", "minLength": 2, "maxLength": 40},
            "capturado_em": {
                "$ref": "#/components/schemas/JsonValue",
                "description": (
                    "Metadado informativo enviado pela extensão; o servidor o ignora para "
                    "ordenação e aceita clientes legados."
                ),
            },
            "apostas": {
                "type": "array",
                "maxItems": 1000,
                "items": {"$ref": "#/components/schemas/JsonValue"},
            },
        },
        "additionalProperties": {"$ref": "#/components/schemas/JsonValue"},
    }
    collection_example = {
        "contrato": 1,
        "casa": "betano",
        "capturado_em": "2026-09-22T14:30:00-03:00",
        "apostas": [{"id": "BET-123", "odd": 1.95, "status": "open"}],
    }
    for path in COLLECTION_PATHS:
        operation = _object(
            _object(paths[path], context=f"path {path}")["post"],
            context=f"POST {path}",
        )
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": collection_schema,
                    "examples": {
                        "default": {"summary": "Lote da extensão", "value": collection_example}
                    },
                }
            },
        }

    upload_operation = _object(
        _object(paths["/api/v1/upload"], context="path /api/v1/upload")["post"],
        context="POST /api/v1/upload",
    )
    upload_operation["requestBody"] = {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "chat_filter": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "IDs de chats a aceitar dentro da exportação.",
                        },
                    },
                    "additionalProperties": {"type": "string"},
                },
                "encoding": {"chat_filter": {"style": "form", "explode": True}},
                "examples": {
                    "default": {
                        "summary": "Exportação ZIP com filtro opcional",
                        "value": {"file": "telegram-export.zip", "chat_filter": [123456789]},
                    }
                },
            }
        },
    }

    planilha_operation = _object(
        _object(paths["/api/v1/apostas/importar-planilha"], context="path importar-planilha")[
            "post"
        ],
        context="POST importar-planilha",
    )
    planilha_operation["requestBody"] = {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["arquivo", "origem_id"],
                    "properties": {
                        "arquivo": {"type": "string", "format": "binary"},
                        "origem_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                },
                "examples": {
                    "default": {
                        "summary": "Planilha de apostas",
                        "value": {"arquivo": "apostas.xlsx", "origem_id": "planilha-principal"},
                    }
                },
            }
        },
    }


def _install_body_limit_responses(document: JsonObject) -> None:
    """Document the endpoint streaming body limit on every operation that accepts a body."""
    for method, path, operation in _operations(document):
        if not isinstance(operation.get("requestBody"), dict):
            continue
        responses = _object(
            operation.get("responses"), context=f"responses for {method.upper()} {path}"
        )
        response_413 = responses.setdefault(
            "413",
            {"description": "The request body exceeds this endpoint's streaming limit."},
        )
        response = _object(response_413, context=f"413 response for {method.upper()} {path}")
        content = response.setdefault("content", {})
        media_types = _object(content, context=f"413 content for {method.upper()} {path}")
        media_types["text/plain"] = {
            "schema": {"type": "string"},
            "example": "Content Too Large",
        }
        if path == "/api/v1/upload":
            response["description"] = (
                "The upload exceeds either the declared application limit (JSON) or the "
                "endpoint streaming limit (plain text)."
            )


def _document_operations(document: JsonObject) -> None:
    seen: set[OperationKey] = set()
    for method, path, operation in _operations(document):
        key = (method, path)
        seen.add(key)
        try:
            summary, description = OPERATION_DOCUMENTATION[key]
        except KeyError as error:
            raise RuntimeError(
                f"missing OpenAPI documentation for {method.upper()} {path}"
            ) from error
        operation["summary"] = summary
        operation["description"] = description

        parameters = operation.get("parameters", [])
        if not isinstance(parameters, list):
            raise RuntimeError(f"parameters for {method.upper()} {path} must be a list")
        for parameter_value in parameters:
            parameter = _object(parameter_value, context=f"parameter in {method.upper()} {path}")
            name = parameter.get("name")
            if not isinstance(name, str):
                raise RuntimeError(f"unnamed parameter in {method.upper()} {path}")
            try:
                parameter["description"] = PARAMETER_DESCRIPTIONS[name]
            except KeyError as error:
                raise RuntimeError(
                    f"missing description for parameter {name!r} in {method.upper()} {path}"
                ) from error

        example = REQUEST_EXAMPLES.get(key)
        if example is not None:
            title, value = example
            request_body = _object(
                operation.get("requestBody"),
                context=f"requestBody for {method.upper()} {path}",
            )
            content = _object(request_body.get("content"), context="requestBody content")
            for media_value in content.values():
                media = _object(media_value, context="request media type")
                media["examples"] = {"default": {"summary": title, "value": value}}

    missing = set(OPERATION_DOCUMENTATION).difference(seen)
    if missing:
        rendered = ", ".join(f"{method.upper()} {path}" for method, path in sorted(missing))
        raise RuntimeError(f"documented operations no longer exist: {rendered}")


def _install_collection_security(document: JsonObject) -> None:
    components = _object(document.setdefault("components", {}), context="components")
    schemes = _object(components.setdefault("securitySchemes", {}), context="securitySchemes")
    schemes["CollectionToken"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Coleta-Token",
        "description": "Token opaco da extensão; armazenado no servidor somente como HMAC.",
    }
    paths = _object(document["paths"], context="paths")
    for path in COLLECTION_PATHS:
        operation = _object(
            _object(paths[path], context=f"path {path}")["post"],
            context=f"POST {path}",
        )
        operation["security"] = [{"CollectionToken": []}]


def _strictify_schema(schema: JsonObject) -> None:
    if not schema:
        schema["$ref"] = "#/components/schemas/JsonValue"
        return
    if schema.get("type") == "object":
        additional = schema.get("additionalProperties", None)
        if "additionalProperties" not in schema:
            schema["additionalProperties"] = False
        elif additional is True or additional == {}:
            schema["additionalProperties"] = {"$ref": "#/components/schemas/JsonValue"}

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for child in properties.values():
            _strictify_schema(_object(child, context="property schema"))

    for key in ("additionalProperties", "contains", "else", "if", "items", "not", "then"):
        child = schema.get(key)
        if isinstance(child, dict):
            _strictify_schema(cast(JsonObject, child))

    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = schema.get(key)
        if isinstance(children, list):
            for child in children:
                _strictify_schema(_object(child, context=f"{key} schema"))


def _strictify_document(document: JsonObject) -> None:
    components = _object(document.setdefault("components", {}), context="components")
    schemas = _object(components.setdefault("schemas", {}), context="component schemas")
    schemas["JsonValue"] = _json_value_schema()
    schemas["Correcao"] = _correction_schema(review=False)
    schemas["CorrecaoDaRevisao"] = _correction_schema(review=True)
    _configure_domain_request_schemas(schemas)
    for schema_value in schemas.values():
        _strictify_schema(_object(schema_value, context="component schema"))

    for method, path, operation in _operations(document):
        parameters = operation.get("parameters", [])
        if isinstance(parameters, list):
            for parameter_value in parameters:
                parameter = _object(parameter_value, context="parameter")
                schema_value = parameter.get("schema")
                if isinstance(schema_value, dict):
                    _strictify_schema(cast(JsonObject, schema_value))

        request_body_value = operation.get("requestBody")
        if isinstance(request_body_value, dict):
            request_body = cast(JsonObject, request_body_value)
            content = _object(request_body.get("content"), context="requestBody content")
            for media_value in content.values():
                media = _object(media_value, context="request media type")
                schema_value = _object(media.get("schema"), context="request schema")
                _strictify_schema(schema_value)

        responses = _object(operation.get("responses"), context=f"responses {method} {path}")
        for response_value in responses.values():
            response = _object(response_value, context="response")
            content_value = response.get("content")
            if not isinstance(content_value, dict):
                continue
            for media_value in content_value.values():
                media = _object(media_value, context="response media type")
                schema_value = _object(media.get("schema"), context="response schema")
                _strictify_schema(schema_value)


def build_openapi(app: FastAPI) -> JsonObject:
    if app.openapi_schema is not None:
        return app.openapi_schema

    document = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        terms_of_service=app.terms_of_service,
        contact=app.contact,
        license_info=app.license_info,
        separate_input_output_schemas=app.separate_input_output_schemas,
        external_docs=app.openapi_external_docs,
    )
    _install_manual_request_bodies(document)
    _install_body_limit_responses(document)
    _document_operations(document)
    _install_collection_security(document)
    _strictify_document(document)
    app.openapi_schema = document
    return document
