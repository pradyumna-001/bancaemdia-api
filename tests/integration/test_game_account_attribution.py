"""Game time selects the default; actual multicontas identity survives a holder switch."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.account_attribution_service import attribute_account, game_instant
from bancaemdia.domain.titulares import TrocaPedido, trocar_conta
from tests.integration.test_troca_titular import _setup, _tenant

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


async def test_game_default_and_explicit_actual_account_are_distinct(
    engine_admin, engine_app, novo_usuario
):
    uid = await novo_usuario()
    house, source, target, start = await _setup(engine_admin, engine_app, uid)
    switch = start + timedelta(days=5)
    async with engine_admin.connect() as conn:
        name = await conn.scalar(
            models.Casa.__table__
            .select()
            .with_only_columns(models.Casa.nome)
            .where(models.Casa.id == house)
        )
    request = TrocaPedido(house, source, target, switch, "LIMITADA")
    key = f"game-switch:{uuid4()}"
    async with AsyncSession(engine_app) as session:
        await _tenant(session, uid)
        await trocar_conta(session, uid, request, key, aplicar=False)
        await trocar_conta(session, uid, request, key, aplicar=True)
        await session.commit()
    async with AsyncSession(engine_app) as session:
        await _tenant(session, uid)
        clock = game_instant({"data_aposta": start.isoformat(), "data_jogo": switch.isoformat()})
        assert (await attribute_account(session, uid, name, clock)).conta_casa_id == target
        assert (await attribute_account(session, uid, name, clock, source)).conta_casa_id == source
        assert (await attribute_account(session, uid, name, None)).conta_casa_id is None


async def test_switch_preview_excludes_actual_multicontas_and_missing_game_clock(
    engine_admin, engine_app, novo_usuario
):
    uid = await novo_usuario()
    house, source, target, start = await _setup(engine_admin, engine_app, uid)
    switch = start + timedelta(days=5)
    ids = {}
    async with AsyncSession(engine_app) as session:
        await _tenant(session, uid)
        for kind, game in (("default", switch), ("actual", switch), ("unknown", None)):
            key = f"game-preview:{kind}:{uuid4()}"
            ids[kind] = await session.scalar(
                insert(models.Aposta)
                .values(
                    usuario_id=uid,
                    chave=key,
                    origem="manual",
                    stake_unidades=1,
                    stake_centavos=10000,
                    conta_casa_id=source,
                    data_aposta=start,
                    data_jogo=game,
                )
                .returning(models.Aposta.id)
            )
            session.add(
                models.Evento(
                    usuario_id=uid,
                    tipo="APOSTA_CRIADA",
                    fonte="manual",
                    aposta_chave=key,
                    payload_json={
                        "conta_casa_id": source,
                        "conta_referencia_explicita": kind == "actual",
                    },
                )
            )
        await session.commit()
    async with AsyncSession(engine_app) as session:
        await _tenant(session, uid)
        result = await trocar_conta(
            session,
            uid,
            TrocaPedido(house, source, target, switch, "LIMITADA"),
            f"preview:{uuid4()}",
            aplicar=False,
        )
        assert result["apostas_afetadas_ids"] == [ids["default"]]
        await session.rollback()
