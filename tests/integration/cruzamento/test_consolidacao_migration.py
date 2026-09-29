"""Actual upgrade/rollback, legacy adoption and immutable evidence on an isolated database."""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.xdist_group("postgres")


def test_empty_roundtrip_and_populated_legacy_adoption_refuses_destructive_rollback(
    isolated_database, monkeypatch
):
    config = Config(str(Path(__file__).parents[3] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[3] / "alembic"))
    monkeypatch.setenv("DATABASE_URL", isolated_database)
    command.upgrade(config, "head")
    command.downgrade(config, "c109match2026")
    command.upgrade(config, "head")
    command.downgrade(config, "c109match2026")

    async def legacy():
        engine = create_async_engine(isolated_database)
        try:
            async with engine.begin() as conn:
                user = await conn.scalar(
                    text(
                        "INSERT INTO usuarios(email,nome) VALUES('legacy@synthetic.invalid','Synthetic') RETURNING id"
                    )
                )
                await conn.execute(
                    text("""INSERT INTO apostas(usuario_id,chave,origem,chat_id,message_id,stake_unidades,stake_centavos,valor_aposta_centavos,odd,estado,selecionada,parceira_chave)
                VALUES(:u,'c:legacy','casa',NULL,NULL,1,10000,10000,2,'PENDENTE',true,'t:legacy'),
                (:u,'t:legacy','telegram',1,1,1,10000,10000,2,'PENDENTE',false,'c:legacy')"""),
                    {"u": user},
                )
                return user
        finally:
            await engine.dispose()

    user = asyncio.run(legacy())
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    async def inspect():
        engine = create_async_engine(isolated_database)
        try:
            async with engine.begin() as conn:
                relation = (
                    (
                        await conn.execute(
                            text("SELECT * FROM aposta_consolidacoes WHERE usuario_id=:u"),
                            {"u": user},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert relation["decisao"] == "legacy" and relation["versao"] == "legacy/manual"
                assert relation["evidencia"]["telegram_selecionada"] is False
                assert (
                    await conn.scalar(
                        text(
                            "SELECT count(*) FROM eventos WHERE usuario_id=:u AND tipo='APOSTAS_CONSOLIDADAS'"
                        ),
                        {"u": user},
                    )
                    == 2
                )
                assert (
                    await conn.scalar(
                        text(
                            "SELECT sum(stake_centavos) FROM apostas_financeiras WHERE usuario_id=:u"
                        ),
                        {"u": user},
                    )
                    == 10000
                )
            with pytest.raises(DBAPIError, match="immutable consolidation"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE aposta_consolidacoes SET evidencia='{}' WHERE usuario_id=:u"),
                        {"u": user},
                    )
            with pytest.raises(DBAPIError, match="preserve consolidation audit"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("DELETE FROM aposta_consolidacoes WHERE usuario_id=:u"), {"u": user}
                    )
        finally:
            await engine.dispose()

    asyncio.run(inspect())
    with pytest.raises(DBAPIError, match="preserve consolidation evidence"):
        command.downgrade(config, "c109match2026")
