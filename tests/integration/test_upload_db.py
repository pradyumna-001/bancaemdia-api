from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bancaemdia import models
from bancaemdia.db.session import connect_args
from bancaemdia.repositories.mensagem_repo import (
    MensagemRepo,
    MidiaArquivoRepo,
    MidiaRepo,
    Salvamento,
)
from bancaemdia.repositories.upload_repo import UploadArquivoRepo, UploadBilheteRepo, UploadRepo

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
CHAT = 900_000
FUSO = datetime(2026, 7, 24, 16, 17, 52, tzinfo=UTC)


def _mensagem() -> int:
    return int(uuid4().int % 1_000_000_000)


async def _criar_upload(session: AsyncSession, usuario_id: int, **extra: object) -> object:
    return await UploadRepo().create(
        session,
        {
            "usuario_id": usuario_id,
            "filename": "ChatExport.zip",
            "chat_id": CHAT,
            "total_messages": 3,
            "estimated_bets": 2,
            "estimated_cost_usd": Decimal("0.010800"),
            **extra,
        },
    )


async def test_an_upload_is_stored_with_a_job_id_and_read_back_by_it(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        upload = await _criar_upload(session, usuario)
        await session.commit()

    async with como(engine_app, usuario) as session:
        lido = await UploadRepo().get_by_job_id(session, usuario, upload.job_id)

    assert lido is not None
    assert (lido.status, lido.bets_processed, lido.cost_usd) == ("pending", 0, Decimal("0.000000"))
    assert lido.estimated_cost_usd == Decimal("0.010800")
    assert lido.criado_em is not None and lido.concluido_em is None


async def test_row_level_security_hides_the_uploads_of_another_person(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()
    outro = await novo_usuario()

    async with como(engine_app, dono) as session:
        upload = await _criar_upload(session, dono)
        await UploadArquivoRepo().create(session, dono, upload.id, b"PK-arquivo")
        await UploadBilheteRepo().insert_many_idempotent(
            session,
            [
                {
                    "upload_id": upload.id,
                    "usuario_id": dono,
                    "chat_id": CHAT,
                    "message_id": 1,
                    "midia_hash": None,
                    "estado": "PENDENTE",
                }
            ],
        )
        await session.commit()

    async with como(engine_app, outro) as session:
        assert await UploadRepo().get_by_job_id(session, outro, upload.job_id) is None
        assert await UploadArquivoRepo().get_by_upload_id(session, outro, upload.id) is None
        assert await UploadBilheteRepo().count_by_estado(session, outro, upload.id) == {}

    async with como(engine_app, dono) as session:
        assert await UploadArquivoRepo().get_by_upload_id(session, dono, upload.id) == b"PK-arquivo"
        assert await UploadBilheteRepo().count_by_estado(session, dono, upload.id) == {
            "PENDENTE": 1
        }


async def test_whoever_offers_the_job_id_closes_only_that_upload(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()
    async with como(engine_app, dono) as session:
        upload = await _criar_upload(session, dono)
        outro_envio = await _criar_upload(session, dono)
        await UploadRepo().set_status(session, dono, upload.id, "processing")
        await UploadRepo().set_status(session, dono, outro_envio.id, "processing")
        await session.commit()

    # O aviso do trabalhador chega sem usuário: a política do job_id é a única porta.
    async with como(engine_app, None) as session:
        await session.execute(
            text("SELECT set_config('app.upload_job_id', :job, true)"), {"job": str(upload.job_id)}
        )
        fechado = await UploadRepo().finish_by_job_id(
            session, upload.job_id, "completed", 4, 1, Decimal("0.021600")
        )
        invisivel = await UploadRepo().finish_by_job_id(
            session, outro_envio.job_id, "completed", 9, 9, Decimal("9")
        )
        await session.commit()

    assert (fechado, invisivel) == (upload.id, None)
    async with como(engine_app, dono) as session:
        lido = await UploadRepo().get_by_job_id(session, dono, upload.job_id)
        intacto = await UploadRepo().get_by_job_id(session, dono, outro_envio.job_id)
    assert lido is not None and intacto is not None
    assert (lido.status, lido.bets_processed, lido.bets_failed) == ("completed", 4, 1)
    assert lido.concluido_em is not None
    assert (intacto.status, intacto.bets_processed) == ("processing", 0)


async def test_an_upload_that_never_started_is_not_closed_by_the_webhook(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()
    async with como(engine_app, dono) as session:
        upload = await _criar_upload(session, dono)
        await session.commit()

    async with como(engine_app, None) as session:
        await session.execute(
            text("SELECT set_config('app.upload_job_id', :job, true)"), {"job": str(upload.job_id)}
        )
        assert (
            await UploadRepo().finish_by_job_id(
                session, upload.job_id, "completed", 1, 0, Decimal("0.001")
            )
            is None
        )


async def test_the_interval_between_uploads_ignores_the_ones_that_failed(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()

    async with como(engine_app, dono) as session:
        aceito = await _criar_upload(session, dono)
        recusado = await _criar_upload(session, dono)
        await UploadRepo().set_status(session, dono, recusado.id, "failed", "a fila caiu")
        await session.commit()

    async with como(engine_app, dono) as session:
        ultimo = await UploadRepo().get_ultimo_aceito_em(session, dono)

    assert ultimo == aceito.criado_em


async def test_a_bill_leaves_pending_only_once(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()
    message_id = _mensagem()

    async with como(engine_app, dono) as session:
        upload = await _criar_upload(session, dono)
        await UploadBilheteRepo().insert_many_idempotent(
            session,
            [
                {
                    "upload_id": upload.id,
                    "usuario_id": dono,
                    "chat_id": CHAT,
                    "message_id": message_id,
                    "midia_hash": None,
                    "estado": "PENDENTE",
                }
            ],
        )
        await session.commit()

    async with como(engine_app, dono) as session:
        primeira = await UploadBilheteRepo().finish(
            session, dono, upload.id, CHAT, message_id, "LIDO", 2, Decimal("0.004")
        )
        # Entrega repetida da mesma leitura não soma a mesma aposta de novo.
        repetida = await UploadBilheteRepo().finish(
            session, dono, upload.id, CHAT, message_id, "LIDO", 2, Decimal("0.004")
        )
        await session.commit()

    # Depois do commit a transação seguinte perde o usuário atual, e a RLS esconde as linhas.
    async with como(engine_app, dono) as session:
        apostas, falhas, custo = await UploadBilheteRepo().somar_resultado(session, dono, upload.id)

    assert primeira is not None and repetida is None
    assert (apostas, falhas, custo) == (2, 0, Decimal("0.004000"))


async def test_the_same_export_twice_does_not_duplicate_bills(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()
    message_id = _mensagem()

    async with como(engine_app, dono) as session:
        upload = await _criar_upload(session, dono)
        linhas = [
            {
                "upload_id": upload.id,
                "usuario_id": dono,
                "chat_id": CHAT,
                "message_id": message_id,
                "midia_hash": "h1",
                "estado": "PENDENTE",
            }
        ]
        await UploadBilheteRepo().insert_many_idempotent(session, linhas)
        await UploadBilheteRepo().finish(
            session, dono, upload.id, CHAT, message_id, "LIDO", 1, Decimal("0.004")
        )
        await UploadBilheteRepo().insert_many_idempotent(session, linhas)
        await session.commit()

    async with como(engine_app, dono) as session:
        por_estado = await UploadBilheteRepo().count_by_estado(session, dono, upload.id)

    assert por_estado == {"LIDO": 1}


async def test_the_daily_count_only_sees_what_was_sent_to_the_ai(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()

    async with como(engine_app, dono) as session:
        upload = await _criar_upload(session, dono)
        await UploadBilheteRepo().insert_many_idempotent(
            session,
            [
                {
                    "upload_id": upload.id,
                    "usuario_id": dono,
                    "chat_id": CHAT,
                    "message_id": _mensagem(),
                    "midia_hash": None,
                    "estado": estado,
                }
                for estado in ("PENDENTE", "LIDO", "FALHOU", "IGNORADO", "TETO")
            ],
        )
        await session.commit()

    async with como(engine_app, dono) as session:
        desde = datetime.now(UTC) - timedelta(hours=1)
        quantos = await UploadBilheteRepo().count_enviados_desde(session, dono, desde)
        ontem = await UploadBilheteRepo().count_enviados_desde(
            session, dono, datetime.now(UTC) + timedelta(hours=1)
        )

    # IGNORADO e TETO não foram lidos, então não gastaram nada do teto do dia.
    assert (quantos, ontem) == (3, 0)


async def test_a_message_is_saved_edited_and_never_undone_by_an_older_export(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    message_id = _mensagem()
    repo = MensagemRepo()

    async with como(engine_app, None) as session:
        _, nova = await repo.save(session, CHAT, message_id, FUSO, "girafalles", "1u")
        _, igual = await repo.save(session, CHAT, message_id, FUSO, "girafalles", "1u")
        _, alterada = await repo.save(
            session,
            CHAT,
            message_id,
            FUSO,
            "girafalles",
            "1u ✅",
            editada_em=datetime(2026, 7, 25, 9, 41, tzinfo=UTC),
        )
        _, antiga = await repo.save(session, CHAT, message_id, FUSO, "girafalles", "1u")
        await session.commit()

    async with como(engine_app, None) as session:
        guardada = await repo.get_by_chat_message(session, CHAT, message_id)
        versoes = (
            await session.execute(
                select(models.MensagemVersao.texto)
                .where(models.MensagemVersao.mensagem_id == guardada.id)
                .order_by(models.MensagemVersao.criado_em)
            )
        ).scalars()

    assert (nova, igual, alterada, antiga) == (
        Salvamento.NOVA,
        Salvamento.IGUAL,
        Salvamento.ALTERADA,
        Salvamento.ANTIGA,
    )
    assert guardada.texto == "1u ✅"
    assert guardada.versao_atual == 2
    assert list(versoes) == ["1u", "1u ✅"]


async def test_a_photo_that_arrives_later_fills_the_message_and_is_never_overwritten(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    message_id = _mensagem()
    repo = MensagemRepo()

    async with como(engine_app, None) as session:
        await repo.save(session, CHAT, message_id, FUSO, "girafalles", "1u")
        _, com_foto = await repo.save(
            session, CHAT, message_id, FUSO, "girafalles", "1u", midia_hash="hash-1"
        )
        _, sem_foto = await repo.save(session, CHAT, message_id, FUSO, "girafalles", "1u")
        _, outra_foto = await repo.save(
            session, CHAT, message_id, FUSO, "girafalles", "1u", midia_hash="hash-2"
        )
        await session.commit()

    async with como(engine_app, None) as session:
        guardada = await repo.get_by_chat_message(session, CHAT, message_id)

    # O conserto de 19/08 do projeto antigo: a foto que faltava entra mesmo com o texto igual, e
    # export sem foto nunca apaga a que já entrou.
    assert (com_foto, sem_foto, outra_foto) == (
        Salvamento.ALTERADA,
        Salvamento.IGUAL,
        Salvamento.IGUAL,
    )
    assert guardada.midia_hash == "hash-1"


async def test_the_bytes_of_a_photo_are_stored_once_and_shared(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dados = b"JPEG-compartilhado"
    hash_ = hashlib.sha256(dados + uuid4().bytes).hexdigest()
    dono = await novo_usuario()
    outro = await novo_usuario()

    async with como(engine_app, dono) as session:
        await MidiaRepo().upsert_idempotent(session, hash_, "image/jpeg", len(dados))
        await MidiaArquivoRepo().upsert_idempotent(session, hash_, dados)
        await MidiaArquivoRepo().upsert_idempotent(session, hash_, b"outro conteudo")
        await session.commit()

    # midias e midia_arquivos são compartilhadas de propósito, como no projeto antigo.
    async with como(engine_app, outro) as session:
        assert await MidiaArquivoRepo().get_by_hash(session, hash_) == dados
        quantas = (
            await session.execute(
                select(models.MidiaArquivo).where(models.MidiaArquivo.hash == hash_)
            )
        ).scalars()
    assert len(list(quantas)) == 1


async def test_an_export_of_fifty_megabytes_fits_the_write_timeout_of_the_api(
    banco, como: Como, novo_usuario: NovoUsuario
) -> None:
    # O motor da API corta consultas em 5 s: guardar o arquivo do tamanho máximo tem de caber nisso.
    engine = create_async_engine(banco.url_app, connect_args=connect_args(replica=False))
    dono = await novo_usuario()
    try:
        async with como(engine, dono) as session:
            upload = await _criar_upload(session, dono)
            await UploadArquivoRepo().create(session, dono, upload.id, b"x" * (50 * 1024 * 1024))
            await session.commit()
        # Depois do commit a transação seguinte perde o usuário atual, e a RLS esconde a linha.
        async with como(engine, dono) as session:
            guardado = await UploadArquivoRepo().get_by_upload_id(session, dono, upload.id)
            await UploadArquivoRepo().delete_by_upload_id(session, dono, upload.id)
            await session.commit()
    finally:
        await engine.dispose()

    assert guardado is not None and len(guardado) == 50 * 1024 * 1024
