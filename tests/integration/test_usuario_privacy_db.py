from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bancaemdia.api.v1.usuario import anonimizar_minha_conta, collect_user_data
from bancaemdia.core.context import current_user_id
from bancaemdia.domain.registros import Usuario


async def _set_tenant(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)}
    )


@pytest.mark.integration
async def test_export_then_anonymize_account_and_reject_late_writes(banco) -> None:
    user_id = uuid4().int % (2**60)
    email = f"privacy-{user_id}@example.test"
    engine = create_async_engine(banco.url_app)
    context_token = current_user_id.set(str(user_id))
    try:
        async with AsyncSession(engine) as session:
            await _set_tenant(session, user_id)
            await session.execute(
                text("INSERT INTO usuarios (id, email, nome) VALUES (:id, :email, 'Pessoa')"),
                {"id": user_id, "email": email},
            )
            await session.execute(
                text("INSERT INTO bancas (id, usuario_id, nome) VALUES (:id, :id, 'Principal')"),
                {"id": user_id},
            )
            await session.commit()
            await _set_tenant(session, user_id)

            usuario = Usuario(
                id=user_id,
                email=email,
                nome="Pessoa",
                criado_em=datetime.now(UTC),
                ativo=True,
            )
            exported = await collect_user_data(session, usuario)
            assert exported["usuario"]["email"] == email
            assert exported["bancas"][0]["nome"] == "Principal"
            await session.rollback()

            await _set_tenant(session, user_id)
            response = await anonimizar_minha_conta(usuario, session)
            assert response.status_code == 200
            await _set_tenant(session, user_id)
            row = (
                await session.execute(
                    text("SELECT email, nome, ativo FROM usuarios WHERE id = :id"),
                    {"id": user_id},
                )
            ).one()
            assert row == (f"deleted-{user_id}@invalid.local", "Conta excluída", False)
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM bancas WHERE usuario_id = :id"), {"id": user_id}
                )
            ) == 0
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM audit_log WHERE usuario_id = :id"),
                    {"id": user_id},
                )
            ) >= 4
            await session.rollback()

            await _set_tenant(session, user_id)
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("INSERT INTO bancas (id, usuario_id, nome) VALUES (:id, :id, 'Late')"),
                    {"id": user_id},
                )
            await session.rollback()
    finally:
        current_user_id.reset(context_token)
        await engine.dispose()


@pytest.mark.integration
async def test_raw_telegram_data_blocks_automatic_anonymization(banco) -> None:
    user_id = uuid4().int % (2**60)
    engine = create_async_engine(banco.url_app)
    context_token = current_user_id.set(str(user_id))
    try:
        async with AsyncSession(engine) as session:
            await _set_tenant(session, user_id)
            await session.execute(
                text("INSERT INTO usuarios (id, email, nome) VALUES (:id, :email, 'Pessoa')"),
                {"id": user_id, "email": f"raw-{user_id}@example.test"},
            )
            await session.execute(
                text(
                    "INSERT INTO uploads (id, usuario_id, filename, chat_id) "
                    "VALUES (:id, :id, 'export.zip', 123)"
                ),
                {"id": user_id},
            )
            await session.commit()
            await _set_tenant(session, user_id)
            usuario = Usuario(
                id=user_id,
                email=f"raw-{user_id}@example.test",
                nome="Pessoa",
                criado_em=datetime.now(UTC),
                ativo=True,
            )
            with pytest.raises(HTTPException) as error:
                await anonimizar_minha_conta(usuario, session)
            assert error.value.status_code == 409
            await session.rollback()
            await _set_tenant(session, user_id)
            assert (
                await session.scalar(
                    text("SELECT ativo FROM usuarios WHERE id = :id"), {"id": user_id}
                )
                is True
            )
    finally:
        current_user_id.reset(context_token)
        await engine.dispose()
