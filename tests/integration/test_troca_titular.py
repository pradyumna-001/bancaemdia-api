"""PostgreSQL checks for temporal holder switching, retries, and tenant isolation."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.domain.account_attribution import ResolutionStatus
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
)
from bancaemdia.domain.titulares import (
    ChaveIdempotenciaEmConflitoError,
    TrocaPedido,
    trocar_conta,
)
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.titular_repo import TitularRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


async def _tenant(session: AsyncSession, usuario_id: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(usuario_id)},
    )


async def _setup(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, usuario_id: int
) -> tuple[int, int, int, datetime]:
    async with engine_admin.begin() as conn:
        casa_id = await conn.scalar(
            insert(models.Casa).values(nome=f"Holder {uuid4().hex[:12]}").returning(models.Casa.id)
        )
    assert casa_id is not None
    start = datetime.now(UTC) - timedelta(days=10)
    async with AsyncSession(engine_app) as session:
        await _tenant(session, usuario_id)
        titular = await TitularRepo().create(session, usuario_id, "Ana")
        destino_titular = await TitularRepo().create(session, usuario_id, "Bia")
        source = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": usuario_id,
                "casa_id": casa_id,
                "titular_id": titular.id,
                "apelido": "primeira",
                "estado": "EM_USO",
                "desde": start,
            },
        )
        target = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": usuario_id,
                "casa_id": casa_id,
                "titular_id": destino_titular.id,
                "apelido": "segunda",
                "estado": "DISPONIVEL",
                "ativa": False,
            },
        )
        await UsoContaCasaRepo().open(session, usuario_id, casa_id, source.id, start)
        await session.commit()
    return casa_id, source.id, target.id, start


async def test_attribution_uses_bet_time_and_validates_explicit_account(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario_id = await novo_usuario()
    other_user = await novo_usuario()
    casa, source, target, start = await _setup(engine_admin, engine_app, usuario_id)
    house_name = None
    async with engine_admin.connect() as conn:
        house_name = await conn.scalar(select(models.Casa.nome).where(models.Casa.id == casa))
    assert house_name is not None
    effective = start + timedelta(days=5)
    async with AsyncSession(engine_app) as session:
        await _tenant(session, usuario_id)
        await trocar_conta(
            session,
            usuario_id,
            TrocaPedido(casa, source, target, effective, "LIMITADA"),
            f"switch:{uuid4()}",
            aplicar=True,
        )
        await session.commit()
    async with AsyncSession(engine_app) as session:
        await _tenant(session, usuario_id)
        before = await attribute_account(
            session, usuario_id, house_name, effective - timedelta(microseconds=1)
        )
        after = await attribute_account(session, usuario_id, house_name, effective)
        assert (before.conta_casa_id, after.conta_casa_id) == (source, target)
        assert (
            await attribute_account(session, usuario_id, house_name, start - timedelta(days=1))
        ).status == ResolutionStatus.NONE
        assert (
            await attribute_account(session, usuario_id, house_name, effective, source)
        ).conta_casa_id == source
        with pytest.raises(InvalidAccountReferenceError):
            await attribute_account(session, usuario_id, "Betano", effective, source)
    async with AsyncSession(engine_app) as session:
        await _tenant(session, other_user)
        with pytest.raises(InvalidAccountReferenceError):
            await attribute_account(session, other_user, house_name, effective, source)


async def test_preview_apply_retry_and_event_time_resolution(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario_id = await novo_usuario()
    casa, source, target, start = await _setup(engine_admin, engine_app, usuario_id)
    effective = start + timedelta(days=5)
    async with AsyncSession(engine_app) as session:
        await _tenant(session, usuario_id)
        old_bet = (
            await session.execute(
                insert(models.Aposta)
                .values(
                    usuario_id=usuario_id,
                    origem="manual",
                    stake_unidades=1.0,
                    stake_centavos=10000,
                    conta_casa_id=source,
                    data_aposta=effective - timedelta(seconds=1),
                )
                .returning(models.Aposta.id)
            )
        ).scalar_one()
        later_bet = (
            await session.execute(
                insert(models.Aposta)
                .values(
                    usuario_id=usuario_id,
                    origem="manual",
                    stake_unidades=1.0,
                    stake_centavos=10000,
                    conta_casa_id=source,
                    data_aposta=effective,
                )
                .returning(models.Aposta.id)
            )
        ).scalar_one()
        await session.commit()
    pedido = TrocaPedido(casa, source, target, effective, "LIMITADA")
    key = f"switch:{uuid4()}"
    async with AsyncSession(engine_app) as session:
        await _tenant(session, usuario_id)
        preview = await trocar_conta(session, usuario_id, pedido, key, aplicar=False)
        await session.commit()
    assert preview["apostas_afetadas_ids"] == [later_bet]
    assert preview["aplicada"] is False

    async def apply_once() -> dict[str, object]:
        async with AsyncSession(engine_app) as session:
            await _tenant(session, usuario_id)
            response = await trocar_conta(session, usuario_id, pedido, key, aplicar=True)
            await session.commit()
            return response

    applied, repeated = await asyncio.gather(apply_once(), apply_once())
    assert applied == repeated
    assert applied["aplicada"] is True
    async with AsyncSession(engine_app) as session:
        await _tenant(session, usuario_id)
        intervals = await UsoContaCasaRepo().list_by_house(session, usuario_id, casa)
        assert [(u.conta_casa_id, u.vigente_de, u.vigente_ate) for u in intervals] == [
            (source, start, effective),
            (target, effective, None),
        ]
        assert (await ContaCasaRepo().get_by_id(session, usuario_id, source)).estado == "LIMITADA"
        assert (await ContaCasaRepo().get_by_id(session, usuario_id, target)).estado == "EM_USO"
        original = await session.scalar(
            select(models.Aposta.conta_casa_id).where(models.Aposta.id == old_bet)
        )
        assert original == source
        assert (
            await session.scalar(
                select(models.Aposta.conta_casa_id).where(models.Aposta.id == later_bet)
            )
            == source
        )
        house_name = await session.scalar(select(models.Casa.nome).where(models.Casa.id == casa))
        assert house_name is not None
        current = await ContaCasaRepo().get_vigente_by_nome_da_casa(
            session, usuario_id, house_name, effective
        )
        assert current is not None and current.id == target
        assert (
            await session.scalar(
                select(models.TrocaTitularEvento.id).where(
                    models.TrocaTitularEvento.usuario_id == usuario_id,
                    models.TrocaTitularEvento.tipo == "APPLY",
                )
            )
            is not None
        )
        event_id = await session.scalar(
            select(models.TrocaTitularEvento.id).where(
                models.TrocaTitularEvento.usuario_id == usuario_id,
                models.TrocaTitularEvento.tipo == "APPLY",
            )
        )
        with pytest.raises(DBAPIError):
            await session.execute(
                update(models.TrocaTitularEvento)
                .where(models.TrocaTitularEvento.id == event_id)
                .values(tipo="PREVIEW")
            )
        await session.rollback()
        await _tenant(session, usuario_id)
        with pytest.raises(ChaveIdempotenciaEmConflitoError):
            await trocar_conta(
                session,
                usuario_id,
                TrocaPedido(casa, source, target, effective, "ENCERRADA"),
                key,
                aplicar=True,
            )
        await session.rollback()


async def test_usage_constraint_and_new_table_rls(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    casa, source, target, start = await _setup(engine_admin, engine_app, ana)
    async with AsyncSession(engine_app) as session:
        await _tenant(session, ana)
        with pytest.raises(DBAPIError):
            await UsoContaCasaRepo().open(session, ana, casa, target, start + timedelta(days=1))
        await session.rollback()
    async with AsyncSession(engine_app) as session:
        assert (await session.execute(select(models.Titular))).scalars().all() == []
        assert (await session.execute(select(models.UsoContaCasa))).scalars().all() == []
        assert (await session.execute(select(models.TrocaTitularEvento))).scalars().all() == []
        await _tenant(session, bia)
        assert (await session.execute(select(models.Titular))).scalars().all() == []
        assert (await session.execute(select(models.UsoContaCasa))).scalars().all() == []
        with pytest.raises(DBAPIError):
            await session.execute(insert(models.Titular).values(usuario_id=ana, nome="Intruso"))
        await session.rollback()
    async with AsyncSession(engine_app) as session:
        await _tenant(session, ana)
        assert (
            await session.scalar(
                select(models.UsoContaCasa.conta_casa_id).where(models.UsoContaCasa.casa_id == casa)
            )
            == source
        )
        await session.execute(
            update(models.ContaCasa).where(models.ContaCasa.id == target).values(ativa=False)
        )
        await session.rollback()
