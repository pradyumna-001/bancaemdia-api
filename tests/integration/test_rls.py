from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.db.seed import seed_canonical
from bancaemdia.domain.registros import Aposta
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]


async def _aposta(como: Como, engine: AsyncEngine, usuario_id: int) -> Aposta:
    dados = {
        "usuario_id": usuario_id,
        "chave": f"m:{uuid4().hex[:8]}",
        "origem": "manual",
        "stake_unidades": 1.0,
        "stake_centavos": 10_000,
    }
    async with como(engine, usuario_id) as session:
        aposta = await ApostaRepo().upsert_idempotent(session, dados)
        await session.commit()
    return aposta


async def _chaves_visiveis(como: Como, engine: AsyncEngine, usuario_id: int | None) -> list[str]:
    async with como(engine, usuario_id) as session:
        chaves = (await session.execute(select(models.Aposta.chave))).scalars().all()
    return [chave for chave in chaves if chave is not None]


async def test_a_user_only_sees_their_own_bets(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    de_ana = await _aposta(como, engine_app, ana)
    de_bia = await _aposta(como, engine_app, bia)
    assert await _chaves_visiveis(como, engine_app, ana) == [de_ana.chave]
    assert await _chaves_visiveis(como, engine_app, bia) == [de_bia.chave]


async def test_without_a_current_user_nothing_is_visible(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    ana = await novo_usuario()
    await _aposta(como, engine_app, ana)
    assert await _chaves_visiveis(como, engine_app, None) == []


async def test_cross_tenant_update_and_delete_touch_nothing(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    de_bia = await _aposta(como, engine_app, bia)
    assert de_bia.chave is not None
    async with como(engine_app, ana) as session:
        assert await ApostaRepo().update_estado(session, bia, de_bia.chave, "GREEN") is None
        apagados = await session.execute(
            delete(models.Aposta).where(models.Aposta.chave == de_bia.chave)
        )
        assert apagados.rowcount == 0
        await session.rollback()
    async with como(engine_app, bia) as session:
        intacta = await ApostaRepo().get_by_chave(session, bia, de_bia.chave)
    assert intacta is not None
    assert intacta.estado == "PENDENTE"


async def test_inserting_on_behalf_of_another_user_is_rejected(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    async with como(engine_app, ana) as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                insert(models.Aposta).values(
                    usuario_id=bia,
                    chave=f"m:{uuid4().hex[:8]}",
                    origem="manual",
                    stake_unidades=1.0,
                    stake_centavos=10_000,
                )
            )
        await session.rollback()
    assert await _chaves_visiveis(como, engine_app, bia) == []


async def test_usuarios_are_private_but_can_be_created_before_login(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    async with como(engine_app, None) as session:
        assert await UsuarioRepo().get_by_id(session, bia) is None
    async with como(engine_app, ana) as session:
        assert await UsuarioRepo().get_by_id(session, bia) is None
        assert await UsuarioRepo().get_by_id(session, ana) is not None
        de_outro = await session.execute(
            update(models.Usuario).where(models.Usuario.id == bia).values(nome="Invasor")
        )
        proprio = await session.execute(
            update(models.Usuario).where(models.Usuario.id == ana).values(nome="Ana")
        )
        assert (de_outro.rowcount, proprio.rowcount) == (0, 1)
        await session.rollback()


async def test_shared_tables_are_readable_by_everyone(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como
) -> None:
    async with engine_admin.begin() as conn:
        await seed_canonical(conn)
    async with como(engine_app, None) as session:
        casas = await session.execute(select(func.count()).select_from(models.Casa))
        mercados = await session.execute(select(func.count()).select_from(models.Mercado))
    assert casas.scalar_one() >= 54
    assert mercados.scalar_one() == 78


async def test_the_superuser_bypasses_rls_so_the_app_must_not_connect_as_it(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    ana = await novo_usuario()
    de_ana = await _aposta(como, engine_app, ana)
    assert de_ana.chave in await _chaves_visiveis(como, engine_admin, None)
