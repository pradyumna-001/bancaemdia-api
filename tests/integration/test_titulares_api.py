"""Holder API state transitions, matrix directions and tenant boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1 import titulares as api
from bancaemdia.domain.titulares import TrocaPedido, trocar_conta
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]
Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]


async def _user(session: AsyncSession, user_id: int):
    value = await UsuarioRepo().get_by_id(session, user_id)
    assert value is not None
    return value


async def test_crud_and_both_matrix_directions_preserve_usage_history(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    user_id, other_id = await novo_usuario(), await novo_usuario()
    async with engine_admin.begin() as conn:
        house_id = await conn.scalar(
            insert(models.Casa).values(nome=f"Matrix {uuid4().hex[:12]}").returning(models.Casa.id)
        )
    assert house_id is not None
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        ana = await api.criar_titular(api.TitularEntrada(nome="Ana"), user, session)
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        bia = await api.criar_titular(api.TitularEntrada(nome="Bia"), user, session)
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        page = await api.listar_titulares(user, session, search="an", page_size=1)
        assert page.total == 1 and page.data[0].id == ana.id
        before = await api.matriz_casa(house_id, user, session)
        assert {holder.id for holder in before.titulares_sem_conta} == {ana.id, bia.id}
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        account_ana = await api.criar_conta_titular(
            ana.id, api.ContaEntrada(casa_id=house_id, apelido="Ana 1"), user, session
        )
        assert account_ana.estado == "DISPONIVEL"
        assert account_ana.acoes_validas == ["ATIVAR", "EDITAR"]
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        account_bia = await api.criar_conta_titular(
            bia.id, api.ContaEntrada(casa_id=house_id, apelido="Bia 1"), user, session
        )
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        current = await api.ativar_conta_titular(ana.id, account_ana.conta_casa_id, user, session)
        assert current.estado == "EM_USO" and current.intervalo_ativo is not None
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        house = await api.matriz_casa(house_id, user, session)
        assert {row.estado for row in house.contas} == {"EM_USO", "DISPONIVEL"}
        assert next(row for row in house.contas if row.titular_id == bia.id).acoes_validas == [
            "TROCAR_PARA",
            "EDITAR",
        ]
        with pytest.raises(HTTPException) as exc:
            await api.arquivar_titular(ana.id, user, session)
        assert exc.value.status_code == 409
    key = f"matrix:{uuid4()}"
    effective = datetime.now(UTC)
    async with como(engine_app, user_id) as session:
        await trocar_conta(
            session,
            user_id,
            TrocaPedido(
                house_id,
                account_ana.conta_casa_id,
                account_bia.conta_casa_id,
                effective,
                "LIMITADA",
            ),
            key,
            aplicar=False,
        )
        await session.commit()
    async with como(engine_app, user_id) as session:
        await trocar_conta(
            session,
            user_id,
            TrocaPedido(
                house_id,
                account_ana.conta_casa_id,
                account_bia.conta_casa_id,
                effective,
                "LIMITADA",
            ),
            key,
            aplicar=True,
        )
        await session.commit()
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        matrix = await api.matriz_titular(ana.id, user, session)
        assert matrix.contas[0].estado == "LIMITADA"
        assert matrix.contas[0].intervalo_ativo is None
        assert len(matrix.contas[0].historico_uso) == 1
        house = await api.matriz_casa(house_id, user, session)
        assert next(row for row in house.contas if row.titular_id == bia.id).intervalo_ativo
    async with como(engine_app, user_id) as session:
        user = await _user(session, user_id)
        archived = await api.arquivar_titular(ana.id, user, session)
        assert archived.arquivado
    async with como(engine_app, other_id) as session:
        other = await _user(session, other_id)
        page = await api.listar_titulares(other, session)
        assert page.total == 0
        with pytest.raises(HTTPException) as exc:
            await api.matriz_titular(ana.id, other, session)
        assert exc.value.status_code == 404
        assert (await api.matriz_casa(house_id, other, session)).contas == []
