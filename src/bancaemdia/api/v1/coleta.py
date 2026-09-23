import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from kombu.exceptions import OperationalError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import COLETA_ERROR_RESPONSES, CollectionResponse
from bancaemdia.coleta.leitores import LEITORES
from bancaemdia.coleta.leitura import ColetaInvalidaError
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db
from bancaemdia.domain.coleta_casa import (
    ApostaInvalidaError,
    Resultado,
    casa_do_envio,
    chave_casa,
    conferir_guardavel,
    hash_do_conteudo,
    recado_de_teto,
    recado_sem_leitor,
    validar,
)
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.middleware.rate_limit import coleta_limiter, coleta_token_digest
from bancaemdia.observability.metrics import coleta_dedup, coleta_received
from bancaemdia.observability.tracing import custom_span
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.coleta_token_repo import ColetaTokenRepo
from bancaemdia.workers.celery_app import app as celery

TOKEN_HEADER = "X-Coleta-Token"
CONTRATO = 1
TAMANHO_MAXIMO = 5 * 1024 * 1024
APOSTAS_POR_ENVIO = 1000
TAREFA = "materialization.materializar_coleta"

# Compatibility names used by the existing coleta tests and by older integrations. Enforcement is
# now centralized in RateLimitMiddleware, but both names still reference the same implementation.
limiter = coleta_limiter
chave_do_limite = coleta_token_digest


def hash_do_token(token: str) -> str:
    segredo = get_settings().COLETA_TOKEN_SECRET.encode()
    return hmac.new(segredo, token.encode(), hashlib.sha256).hexdigest()


router = APIRouter(responses=COLETA_ERROR_RESPONSES)


def erro(status_code: int, mensagem: str) -> JSONResponse:
    # A extensão mostra o campo `erro` e trata tudo que não é 200 como falha, guardando o que
    # capturou para o próximo envio.
    return JSONResponse(status_code=status_code, content={"erro": mensagem})


async def _set_current_user(session: AsyncSession, usuario_id: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario_id)}
    )


async def _guardar_sem_leitor(
    session: AsyncSession, usuario_id: int, casa: str, casa_id: int, apostas: list[object]
) -> Resultado:
    resultado = Resultado()
    guardadas = 0
    for posicao, bruto in enumerate(apostas):
        if not isinstance(bruto, dict):
            resultado.recusadas.append({
                "posicao": posicao,
                "motivo": "isto não veio como objeto — não dá nem para guardar como prova da casa",
            })
            continue
        try:
            conferir_guardavel(bruto)
        except ColetaInvalidaError as sem_guarda:
            resultado.recusadas.append({"posicao": posicao, "motivo": str(sem_guarda)})
            continue
        # Sem leitor não se sabe qual campo é a identidade da casa, e chutar juntaria apostas
        # diferentes; o hash do próprio objeto guarda duas fotos da mesma aposta, nunca uma a menos.
        identidade = hash_do_conteudo(bruto)
        await ColetaCasaRepo().upsert_idempotent(
            session,
            {
                "usuario_id": usuario_id,
                "casa_id": casa_id,
                "identidade": identidade,
                "hash_conteudo": identidade,
                "bruto_json": bruto,
            },
        )
        coleta_received.labels(casa=casa, status="sem_leitor").inc()
        guardadas += 1
    if guardadas:
        resultado.sem_leitor.append({
            "casa": casa,
            "guardadas": guardadas,
            "recado": recado_sem_leitor(casa, guardadas, LEITORES),
        })
    return resultado


async def registrar(
    session: AsyncSession, usuario_id: int, casa: str, casa_id: int, apostas: list[object]
) -> tuple[Resultado, list[int]]:
    leitor = LEITORES.get(casa)
    if leitor is None:
        return await _guardar_sem_leitor(session, usuario_id, casa, casa_id, apostas), []

    resultado = Resultado()
    fila: list[int] = []
    coletas = ColetaCasaRepo()
    for posicao, bruto in enumerate(apostas):
        try:
            conferir_guardavel(bruto)
            # A casa que vale é a do envio, nunca a que o leitor carrega: com duas marcas na mesma
            # plataforma, a aposta da marca nova entraria com o lucro na casa errada.
            coletada = replace(leitor(bruto), casa=casa)
        except ColetaInvalidaError as erro_da_leitura:
            resultado.recusadas.append({"posicao": posicao, "motivo": str(erro_da_leitura)})
            coleta_received.labels(casa=casa, status="recusada").inc()
            continue

        hash_novo = hash_do_conteudo(bruto)
        existente = await coletas.get_by_identidade(
            session, usuario_id, casa_id, coletada.identidade
        )
        # O cru fica guardado mesmo quando a aposta é recusada: é a prova de por que ela não entrou.
        gravada = await coletas.upsert_idempotent(
            session,
            {
                "usuario_id": usuario_id,
                "casa_id": casa_id,
                "identidade": coletada.identidade,
                "hash_conteudo": hash_novo,
                "bruto_json": bruto,
            },
        )
        # Another request may have inserted this identity after our first read. The unique
        # constraint serializes the writes; look it up again when the upsert was a no-op so the
        # response counts it as known and a failed publication can still be retried.
        if existente is None and gravada is None:
            existente = await coletas.get_by_identidade(
                session, usuario_id, casa_id, coletada.identidade
            )
        chave = chave_casa(casa, coletada.identidade)
        # Já é aposta nossa: o que pode faltar é o resultado, e ele não passa de novo pela régua da
        # criação, como no projeto antigo.
        if await ApostaRepo().get_by_chave(session, usuario_id, chave) is None:
            try:
                validar(coletada, VALOR_UNIDADE_PADRAO_CENTAVOS)
            except ApostaInvalidaError as recusa:
                resultado.recusadas.append({
                    "posicao": posicao,
                    "identidade": coletada.identidade,
                    "motivo": f"a {casa} mandou uma aposta que o planilhador não aceita: {recusa}",
                })
                coleta_received.labels(casa=casa, status="recusada").inc()
                continue

        if existente is None:
            resultado.novas_contando += 1
            situacao = "nova"
        elif existente.hash_conteudo != hash_novo:
            resultado.atualizadas += 1
            situacao = "atualizada"
        else:
            resultado.ja_conhecidas += 1
            coleta_dedup.inc()
            situacao = "igual"
        coleta_received.labels(casa=casa, status=situacao).inc()
        if gravada is not None:
            fila.append(gravada.id)
        elif existente is not None and existente.processado_em is None:
            # O reenvio igual do que ainda não foi processado volta à fila: se a publicação falhou
            # depois de gravar, é o próximo envio da extensão que o recupera.
            fila.append(existente.id)
    return resultado, fila


def _enfileirar(usuario_id: int, fila: list[int]) -> None:
    for coleta_id in fila:
        celery.send_task(TAREFA, kwargs={"usuario_id": usuario_id, "coleta_id": coleta_id})


@router.post("/api/v1/coleta", response_model=CollectionResponse)
@router.post("/coleta", response_model=CollectionResponse)
async def receber_coleta(request: Request, session: AsyncSession = Depends(get_db)) -> JSONResponse:
    token = request.headers.get(TOKEN_HEADER)
    tokens = ColetaTokenRepo()
    usuario_id = (
        await tokens.get_usuario_id_by_hash(session, hash_do_token(token)) if token else None
    )
    if usuario_id is None:
        return erro(
            status.HTTP_403_FORBIDDEN,
            "o token não confere — abra a tela de coleta do planilhador e cole o token de novo",
        )
    await _set_current_user(session, usuario_id)

    # O teto vem antes de ler o corpo: quem já passou dele não custa nem a memória do envio.
    hoje = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    quantas = await ColetaCasaRepo().count_received_since(session, usuario_id, hoje)
    recado = recado_de_teto(quantas, get_settings().COLETA_DAILY_LIMIT)
    if recado is not None:
        return erro(status.HTTP_429_TOO_MANY_REQUESTS, recado)

    corpo = await request.body()
    if len(corpo) > TAMANHO_MAXIMO:
        return erro(
            status.HTTP_400_BAD_REQUEST,
            "o envio veio grande demais — abra o seu histórico em partes menores",
        )
    try:
        envio = json.loads(corpo.decode("utf-8", "replace"))
    except ValueError:
        return erro(status.HTTP_400_BAD_REQUEST, "não entendi o que a extensão mandou")
    if not isinstance(envio, dict) or envio.get("contrato") != CONTRATO:
        return erro(
            status.HTTP_400_BAD_REQUEST,
            "esta versão do planilhador não conhece o formato que a extensão mandou — atualize"
            " um dos dois",
        )
    apostas = envio.get("apostas")
    if not isinstance(apostas, list):
        return erro(status.HTTP_400_BAD_REQUEST, "o envio veio sem a lista de apostas")
    if len(apostas) > APOSTAS_POR_ENVIO:
        return erro(
            status.HTTP_400_BAD_REQUEST,
            "o envio veio com apostas demais de uma vez — abra o seu histórico em partes menores",
        )
    try:
        casa = casa_do_envio(str(envio.get("casa") or ""))
    except ColetaInvalidaError as envio_quebrado:
        return erro(status.HTTP_400_BAD_REQUEST, str(envio_quebrado))
    nome = casa_canonica(casa)
    casa_id = None if nome is None else await CasaRepo().get_id_by_nome(session, nome)
    if casa_id is None:
        return erro(
            status.HTTP_400_BAD_REQUEST,
            f"não conheço a casa {casa!r} — confira o nome que a extensão mandou",
        )

    with custom_span("coleta.casa", casa=casa, quantidade=len(apostas), usuario_id=usuario_id):
        resultado, fila = await registrar(session, usuario_id, casa, casa_id, apostas)
        await session.commit()
    try:
        await run_in_threadpool(_enfileirar, usuario_id, fila)
    except OperationalError:
        return erro(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "guardei o que a extensão mandou, mas não consegui pôr na fila agora — o próximo"
            " envio termina o serviço",
        )
    return JSONResponse(resultado.para_json())
