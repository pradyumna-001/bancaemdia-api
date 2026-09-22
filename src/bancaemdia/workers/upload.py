import asyncio
import base64
import hashlib
import io
from datetime import datetime
from functools import lru_cache

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.domain.upload import (
    ExportInvalidoError,
    ExportTelegram,
    bilhetes_do_export,
    casas_por_url,
    odds_citadas,
)
from bancaemdia.extracao.cliente import tipo_da_imagem
from bancaemdia.observability.metrics import batch_bets_processed, observe_stage
from bancaemdia.repositories.mensagem_repo import MensagemRepo, MidiaArquivoRepo, MidiaRepo
from bancaemdia.repositories.upload_repo import UploadArquivoRepo, UploadBilheteRepo, UploadRepo
from bancaemdia.workers.celery_app import app
from bancaemdia.workers.extraction import cadeia_do_bilhete
from bancaemdia.workers.materialization import (
    FUSO_DO_BRASIL,
    RETRY_ON,
    _set_current_user,
    get_engine,
)

ETAPA = "upload"
WEBHOOK_HEADER = "X-Webhook-Token"
WEBHOOK_PATH = "/webhook/upload-complete"
TEMPO_DO_WEBHOOK = 10.0


class WebhookSemDestinoError(RuntimeError):
    pass


@lru_cache
def get_webhook_client() -> httpx.Client:
    return httpx.Client(base_url=get_settings().API_INTERNAL_URL or "", timeout=TEMPO_DO_WEBHOOK)


def _inicio_do_dia() -> datetime:
    # O teto é por dia do Brasil: contado em UTC ele zerava às 21h (defeito medido no antigo).
    agora = datetime.now(FUSO_DO_BRASIL)
    return agora.replace(hour=0, minute=0, second=0, microsecond=0)


async def _guardar_export(
    engine: AsyncEngine, usuario_id: int, upload_id: int
) -> tuple[int, int] | None:
    async with (
        engine.connect() as conexao,
        AsyncSession(bind=conexao, expire_on_commit=False) as session,
        session.begin(),
    ):
        await _set_current_user(session, usuario_id)
        uploads = UploadRepo()
        upload = await uploads.get_by_id_for_update(session, usuario_id, upload_id)
        conteudo = await UploadArquivoRepo().get_by_upload_id(session, usuario_id, upload_id)
        # A tarefa pode ser reentregue depois de o export já ter sido guardado: sem o arquivo em
        # espera não há nada para reler, e o que falta é só enfileirar o que ficou pendente.
        if upload is None or conteudo is None:
            return None
        await uploads.set_status(session, usuario_id, upload_id, "processing")

        por_mensagem: dict[int, str] = {}
        with ExportTelegram(io.BytesIO(conteudo), upload.filename) as export:
            mensagens = list(export.mensagens())
            bilhetes = bilhetes_do_export(export)
            com_foto = {bilhete.mensagem.message_id for bilhete in bilhetes}
            for mensagem in mensagens:
                # Uma foto por vez: guardar as de um export inteiro de uma vez poria centenas de
                # megabytes na memória do trabalhador.
                dados = export.bytes_da_foto(mensagem) if mensagem.message_id in com_foto else None
                midia_hash = None if dados is None else hashlib.sha256(dados).hexdigest()
                # A data do export vem sem fuso, na hora local de quem postou, como a gravação da
                # aposta já supõe.
                data = (
                    mensagem.data
                    if mensagem.data.tzinfo
                    else mensagem.data.replace(tzinfo=FUSO_DO_BRASIL)
                )
                mensagem_id, _ = await MensagemRepo().save(
                    session,
                    chat_id=mensagem.chat_id,
                    message_id=mensagem.message_id,
                    data=data,
                    autor=mensagem.autor,
                    texto=mensagem.texto,
                    editada_em=mensagem.editada_em,
                    midia_hash=midia_hash,
                )
                if midia_hash is not None and dados is not None:
                    await MidiaRepo().upsert_idempotent(
                        session,
                        midia_hash,
                        tipo_da_imagem(mensagem.caminho_foto or ""),
                        len(dados),
                        mensagem_id,
                    )
                    await MidiaArquivoRepo().upsert_idempotent(session, midia_hash, dados)
                    por_mensagem[mensagem.message_id] = midia_hash

        restante = await _restante_do_dia(session, usuario_id)
        linhas: list[dict[str, object]] = []
        for bilhete in bilhetes:
            mensagem = bilhete.mensagem
            if bilhete.ignorado:
                estado = "IGNORADO"
            elif por_mensagem.get(mensagem.message_id) is None:
                # A foto não abriu (passa do teto por foto, ou o zip mente o tamanho): sem ela não
                # há o que ler. Fica IGNORADO, e não FALHOU, porque não gastou leitura nenhuma —
                # deixá-la pendente prenderia o envio, e contá-la como falha queimaria uma vaga do
                # teto do dia por uma foto que a IA nunca viu.
                estado = "IGNORADO"
            elif restante is None or restante > 0:
                estado = "PENDENTE"
                restante = None if restante is None else restante - 1
            else:
                # Acima do teto do dia: guardado, não lido. Reenviar amanhã lê só estes.
                estado = "TETO"
            linhas.append({
                "upload_id": upload_id,
                "usuario_id": usuario_id,
                "chat_id": mensagem.chat_id,
                "message_id": mensagem.message_id,
                "midia_hash": por_mensagem.get(mensagem.message_id),
                "estado": estado,
            })
        await UploadBilheteRepo().insert_many_idempotent(session, linhas)
        await uploads.set_contagem_do_export(
            session,
            usuario_id,
            upload_id,
            len(mensagens),
            mensagens[0].chat_id if mensagens else upload.chat_id,
        )
        await UploadArquivoRepo().delete_by_upload_id(session, usuario_id, upload_id)
        return len(mensagens), sum(linha["estado"] == "PENDENTE" for linha in linhas)


async def _restante_do_dia(session: AsyncSession, usuario_id: int) -> int | None:
    limite = get_settings().UPLOAD_DAILY_PHOTO_LIMIT
    if not limite:
        return None
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"upload:{usuario_id}"},
    )
    ja_hoje = await UploadBilheteRepo().count_enviados_desde(session, usuario_id, _inicio_do_dia())
    return max(limite - ja_hoje, 0)


async def _enfileirar_bilhetes(engine: AsyncEngine, usuario_id: int, upload_id: int) -> int:
    async with (
        engine.connect() as conexao,
        AsyncSession(bind=conexao, expire_on_commit=False) as session,
    ):
        async with session.begin():
            await _set_current_user(session, usuario_id)
            pendentes = await UploadBilheteRepo().list_pendentes(session, usuario_id, upload_id)

        enfileirados = 0
        for bilhete in pendentes:
            mensagem = None
            dados = None
            midia_hash = bilhete.midia_hash
            if midia_hash is not None:
                # Uma foto por vez: um export do teto do dia traz 200, e carregar todas de uma vez
                # colocaria centenas de megabytes na memória do trabalhador.
                async with session.begin():
                    await _set_current_user(session, usuario_id)
                    mensagem = await MensagemRepo().get_by_chat_message(
                        session, bilhete.chat_id, bilhete.message_id
                    )
                    dados = await MidiaArquivoRepo().get_by_hash(session, midia_hash)
            if midia_hash is None or dados is None:
                # Bilhete sem foto para mandar fecha aqui, sem gastar leitura: pendente para sempre
                # é envio preso.
                async with session.begin():
                    await _set_current_user(session, usuario_id)
                    await UploadBilheteRepo().finish(
                        session,
                        usuario_id,
                        upload_id,
                        bilhete.chat_id,
                        bilhete.message_id,
                        "IGNORADO",
                    )
                continue
            texto = "" if mensagem is None else mensagem.texto
            cadeia_do_bilhete(
                usuario_id,
                base64.b64encode(dados).decode("ascii"),
                nome_do_arquivo=f"{midia_hash}.jpg",
                legenda=texto,
                # O banco devolve timestamptz em UTC e a gravação da aposta lê hora sem fuso como
                # hora do Brasil: sem converter, toda aposta andava 3 horas para a frente (medido).
                postada_em=(
                    None
                    if mensagem is None
                    else mensagem.data.astimezone(FUSO_DO_BRASIL).replace(tzinfo=None).isoformat()
                ),
                casas_do_link=casas_por_url(texto),
                odds_do_texto=odds_citadas(texto),
                chat_id=bilhete.chat_id,
                message_id=bilhete.message_id,
                midia_hash=midia_hash,
                upload_id=upload_id,
            ).apply_async()
            # A marca vem depois de publicar: uma reentrega manda só o que ficou faltando, e a
            # gravação já é idempotente pela chave da aposta.
            async with session.begin():
                await _set_current_user(session, usuario_id)
                await UploadBilheteRepo().mark_enfileirado(session, usuario_id, bilhete.id)
            enfileirados += 1
    return enfileirados


def _avisar(upload_id: int, usuario_id: int) -> None:
    # O aviso vai pela fila, não por chamada direta: a API pode estar fora do ar no instante em que
    # o envio termina, e a tarefa tem as próprias tentativas — um envio bom não vira falho por isso.
    app.send_task(
        "materialization.notificar_upload",
        kwargs={"upload_id": upload_id, "usuario_id": usuario_id},
    )


def processar_upload(upload_id: int, usuario_id: int) -> dict[str, object]:
    with observe_stage(ETAPA):
        try:
            guardado = asyncio.run(_guardar_export(get_engine(), usuario_id, upload_id))
        except ExportInvalidoError:
            # O export já passou pela mesma leitura na rota: se recusa agora, o arquivo guardado
            # está corrompido, e insistir não conserta.
            asyncio.run(_falhar(get_engine(), usuario_id, upload_id, "o export guardado não abriu"))
            _avisar(upload_id, usuario_id)
            return {"upload_id": upload_id, "status": "failed"}
        enfileirados = asyncio.run(_enfileirar_bilhetes(get_engine(), usuario_id, upload_id))
        batch_bets_processed.labels(stage=ETAPA).inc(enfileirados)
    if not enfileirados:
        _avisar(upload_id, usuario_id)
    mensagens = 0 if guardado is None else guardado[0]
    return {"upload_id": upload_id, "mensagens": mensagens, "bilhetes": enfileirados}


async def _falhar(engine: AsyncEngine, usuario_id: int, upload_id: int, motivo: str) -> None:
    async with (
        engine.connect() as conexao,
        AsyncSession(bind=conexao, expire_on_commit=False) as session,
        session.begin(),
    ):
        await _set_current_user(session, usuario_id)
        await UploadRepo().set_status(session, usuario_id, upload_id, "failed", motivo)
        await UploadArquivoRepo().delete_by_upload_id(session, usuario_id, upload_id)


def falhar_upload(upload_id: int, usuario_id: int) -> dict[str, object]:
    asyncio.run(_falhar(get_engine(), usuario_id, upload_id, "o processamento do export falhou"))
    _avisar(upload_id, usuario_id)
    return {"upload_id": upload_id, "status": "failed"}


async def _resultado(engine: AsyncEngine, usuario_id: int, upload_id: int) -> dict[str, object]:
    async with (
        engine.connect() as conexao,
        AsyncSession(bind=conexao, expire_on_commit=False) as session,
        session.begin(),
    ):
        await _set_current_user(session, usuario_id)
        upload = await UploadRepo().get_by_id_for_update(session, usuario_id, upload_id)
        apostas, falhas, custo = await UploadBilheteRepo().somar_resultado(
            session, usuario_id, upload_id
        )
    if upload is None:
        raise WebhookSemDestinoError(f"envio {upload_id} não existe mais")
    return {
        "job_id": str(upload.job_id),
        "status": "failed" if upload.status == "failed" else "completed",
        "bets_processed": apostas,
        "bets_failed": falhas,
        "cost_usd": float(custo),
    }


def notificar_upload(upload_id: int, usuario_id: int) -> dict[str, object]:
    segredo = get_settings().UPLOAD_WEBHOOK_SECRET
    if not get_settings().API_INTERNAL_URL or not segredo:
        structlog.get_logger().error("upload_webhook_sem_configuracao", upload_id=upload_id)
        raise WebhookSemDestinoError("sem API_INTERNAL_URL ou UPLOAD_WEBHOOK_SECRET")
    corpo = asyncio.run(_resultado(get_engine(), usuario_id, upload_id))
    resposta = get_webhook_client().post(
        WEBHOOK_PATH, json=corpo, headers={WEBHOOK_HEADER: segredo}
    )
    resposta.raise_for_status()
    return corpo


processar_upload_task = app.task(
    name="materialization.processar_upload",
    autoretry_for=RETRY_ON,
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=False,
    max_retries=3,
)(processar_upload)

falhar_upload_task = app.task(name="materialization.falhar_upload")(falhar_upload)

notificar_upload_task = app.task(
    name="materialization.notificar_upload",
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=False,
    max_retries=5,
)(notificar_upload)
