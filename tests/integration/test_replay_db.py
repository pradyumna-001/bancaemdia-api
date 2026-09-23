from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.cli.replay import ReplayInseguroError, reconstruir_usuario
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo

pytestmark = pytest.mark.xdist_group("postgres")


async def _aposta(engine: AsyncEngine, usuario_id: int, prefixo: str = "m:") -> str:
    chave = f"{prefixo}{uuid4().hex}"
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario_id)}
        )
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario_id,
                "tipo": "APOSTA_CRIADA",
                "fonte": "manual",
                "payload_json": {
                    "origem": "manual",
                    "stake_unidades": 1.0,
                    "valor_unidade_centavos": 10_000,
                    "odd": 2.0,
                    "data_aposta": "2026-09-20T12:00:00-03:00",
                },
                "confianca": None,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": chave,
            },
        )
        await ApostaRepo().upsert_materializada(
            session,
            {
                "usuario_id": usuario_id,
                "chave": chave,
                "origem": "manual",
                "stake_unidades": 1.0,
                "stake_centavos": 10_000,
                "valor_aposta_centavos": 10_000,
                "odd": 2.0,
                "freebet": False,
                "estado": "PENDENTE",
                "revisao_grave": False,
                "selecionada": True,
            },
        )
    return chave


async def test_replay_repairs_centavos_atomically_and_is_idempotent(
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario = await novo_usuario()
    outro = await novo_usuario()
    chave = await _aposta(engine_app, usuario)
    outra_chave = await _aposta(engine_app, outro)
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        antes = (
            await session.execute(
                select(models.Aposta).where(
                    models.Aposta.usuario_id == usuario,
                    models.Aposta.chave == chave,
                )
            )
        ).scalar_one()
        identidade = (antes.id, antes.criada_em, antes.atualizada_em)
        await session.execute(
            update(models.Aposta)
            .where(
                models.Aposta.usuario_id == usuario,
                models.Aposta.chave == chave,
            )
            .values(stake_centavos=1, atualizada_em=antes.atualizada_em)
        )

    seco = await reconstruir_usuario(usuario, engine=engine_app, dry_run=True)
    assert seco.alteradas == 1
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        ainda_errada = (
            await session.execute(
                select(models.Aposta.stake_centavos).where(
                    models.Aposta.usuario_id == usuario,
                    models.Aposta.chave == chave,
                )
            )
        ).scalar_one()
        assert ainda_errada == 1
    reparado = await reconstruir_usuario(usuario, engine=engine_app)
    repetido = await reconstruir_usuario(usuario, engine=engine_app)
    assert (reparado.alteradas, repetido.alteradas) == (1, 0)
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        aposta = (
            await session.execute(
                select(models.Aposta).where(
                    models.Aposta.usuario_id == usuario,
                    models.Aposta.chave == chave,
                )
            )
        ).scalar_one()
        assert aposta.stake_centavos == 10_000
        assert (aposta.id, aposta.criada_em, aposta.atualizada_em) == identidade
        assert (
            await session.execute(
                select(models.Aposta).where(
                    models.Aposta.chave == outra_chave,
                )
            )
        ).scalar_one_or_none() is None


async def test_replay_refuses_missing_projection_without_deleting_history(
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario = await novo_usuario()
    chave = f"m:{uuid4().hex}"
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario,
                "tipo": "APOSTA_CRIADA",
                "fonte": "manual",
                "payload_json": {"origem": "manual", "stake_unidades": 1.0},
                "confianca": None,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": chave,
            },
        )
    with pytest.raises(ReplayInseguroError, match="ID histórico"):
        await reconstruir_usuario(usuario, engine=engine_app)
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        assert (
            await session.execute(
                select(models.Evento).where(
                    models.Evento.usuario_id == usuario,
                    models.Evento.aposta_chave == chave,
                )
            )
        ).scalar_one_or_none() is not None


async def test_replay_recreates_bet_from_complete_creation_snapshot(
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario = await novo_usuario()
    chave = f"m:{uuid4().hex}"
    snapshot = {
        "snapshot_completo": True,
        "origem": "manual",
        "stake_unidades": 1.0,
        "valor_unidade_centavos": 10_000,
        "conta_casa_id": None,
        "chat_id": None,
        "message_id": None,
        "ordem_na_mensagem": 0,
        "data_aposta": "2026-09-20T12:00:00-03:00",
        "freebet": False,
    }
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario,
                "tipo": "APOSTA_CRIADA",
                "fonte": "manual",
                "payload_json": {**snapshot, "snapshot_replay": snapshot, "odd": 2.0},
                "confianca": None,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": chave,
            },
        )
    seco = await reconstruir_usuario(usuario, engine=engine_app, dry_run=True)
    assert seco.recriadas == 1
    feito = await reconstruir_usuario(usuario, engine=engine_app)
    assert feito.recriadas == 1
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        aposta = (
            await session.execute(
                select(models.Aposta).where(
                    models.Aposta.usuario_id == usuario, models.Aposta.chave == chave
                )
            )
        ).scalar_one()
        assert (aposta.stake_centavos, aposta.odd) == (10_000, 2.0)


async def test_replay_rolls_back_earlier_updates_if_later_history_is_invalid(
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario = await novo_usuario()
    primeira = await _aposta(engine_app, usuario, "m:a")
    segunda = await _aposta(engine_app, usuario, "m:z")
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        await session.execute(
            update(models.Aposta)
            .where(
                models.Aposta.usuario_id == usuario,
                models.Aposta.chave == primeira,
            )
            .values(stake_centavos=1)
        )
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario,
                "tipo": "CASHOUT_REGISTRADO",
                "fonte": "manual",
                "payload_json": {},
                "confianca": None,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": segunda,
            },
        )
    with pytest.raises(ReplayInseguroError, match="cashout"):
        await reconstruir_usuario(usuario, engine=engine_app)
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        aposta = (
            await session.execute(
                select(models.Aposta).where(
                    models.Aposta.usuario_id == usuario,
                    models.Aposta.chave == primeira,
                )
            )
        ).scalar_one()
        assert aposta.stake_centavos == 1
