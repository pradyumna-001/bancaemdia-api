from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.db.seed import seed_canonical
from bancaemdia.domain import vocabulario
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo

ORIGENS = ("telegram", "print", "manual", "planilha", "casa")

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]


async def test_resending_the_same_bet_keeps_one_row_per_origin(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    usuario = await novo_usuario()
    lote = uuid4().hex[:6]
    for numero, origem in enumerate(ORIGENS, start=1):
        dados: dict[str, object] = {
            "usuario_id": usuario,
            "chave": f"{lote}:{origem}",
            "origem": origem,
            "stake_unidades": 1.0,
            "stake_centavos": 10_000,
            "odd": 1.9,
        }
        if origem == "telegram":
            dados |= {"chat_id": 100, "message_id": numero}
        async with como(engine_app, usuario) as session:
            primeira = await ApostaRepo().upsert_idempotent(session, dados)
            await session.commit()
        async with como(engine_app, usuario) as session:
            segunda = await ApostaRepo().upsert_idempotent(session, {**dados, "odd": 2.0})
            await session.commit()
        assert segunda.id == primeira.id
        assert segunda.odd == pytest.approx(2.0)
        assert segunda.criada_em == primeira.criada_em
        assert segunda.atualizada_em >= primeira.atualizada_em
    async with como(engine_app, usuario) as session:
        apostas = await ApostaRepo().list_by_usuario(session, usuario)
    assert sorted(a.origem for a in apostas) == sorted(ORIGENS)


async def test_the_same_telegram_message_cannot_become_two_bets(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    usuario = await novo_usuario()
    lote = uuid4().hex[:6]
    base: dict[str, object] = {
        "usuario_id": usuario,
        "origem": "telegram",
        "chat_id": 200,
        "message_id": 7,
        "ordem_na_mensagem": 0,
        "stake_unidades": 1.0,
        "stake_centavos": 10_000,
    }
    async with como(engine_app, usuario) as session:
        await ApostaRepo().upsert_idempotent(session, {**base, "chave": f"{lote}:a"})
        await session.commit()
    async with como(engine_app, usuario) as session:
        with pytest.raises(IntegrityError):
            await ApostaRepo().upsert_idempotent(session, {**base, "chave": f"{lote}:b"})
        await session.rollback()


async def test_telegram_bets_need_their_message(
    engine_app: AsyncEngine, como: Como, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    usuario = await novo_usuario()
    async with como(engine_app, usuario) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(models.Aposta).values(
                    usuario_id=usuario,
                    chave=f"t:{uuid4().hex[:8]}",
                    origem="telegram",
                    stake_unidades=1.0,
                    stake_centavos=10_000,
                )
            )
        await session.rollback()


async def test_coleta_casa_is_kept_once_and_rewritten_only_when_it_changes(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario = await novo_usuario()
    lote = uuid4().hex[:6]
    async with engine_admin.begin() as conn:
        casa_id = await conn.scalar(
            insert(models.Casa)
            .values(nome=f"Casa {lote}", dominio=f"{lote}.bet.br")
            .returning(models.Casa.id)
        )
    dados: dict[str, object] = {
        "usuario_id": usuario,
        "casa_id": casa_id,
        "identidade": "B-1",
        "hash_conteudo": "aberta",
        "bruto_json": {"estado": "open"},
    }
    async with como(engine_app, usuario) as session:
        primeira = await ColetaCasaRepo().upsert_idempotent(session, dados)
        await session.commit()
    async with como(engine_app, usuario) as session:
        repetida = await ColetaCasaRepo().upsert_idempotent(session, dados)
        await session.commit()
    async with como(engine_app, usuario) as session:
        liquidada = await ColetaCasaRepo().upsert_idempotent(
            session, {**dados, "hash_conteudo": "liquidada", "bruto_json": {"estado": "settled"}}
        )
        await session.commit()
    assert primeira is not None
    assert repetida is None
    assert liquidada is not None
    assert liquidada.id == primeira.id
    assert liquidada.hash_conteudo == "liquidada"
    assert liquidada.bruto_json == {"estado": "settled"}
    async with como(engine_app, usuario) as session:
        quantas = await session.execute(
            select(func.count())
            .select_from(models.ColetaCasa)
            .where(models.ColetaCasa.usuario_id == usuario)
        )
    assert quantas.scalar_one() == 1


async def test_seed_canonical_runs_twice_without_duplicates(engine_admin: AsyncEngine) -> None:
    async with engine_admin.begin() as conn:
        await seed_canonical(conn)
    async with engine_admin.begin() as conn:
        segunda = await seed_canonical(conn)
    assert set(segunda.values()) == {0}
    async with engine_admin.connect() as conn:
        esportes = await conn.scalar(select(func.count()).select_from(models.Esporte))
        mercados = await conn.scalar(select(func.count()).select_from(models.Mercado))
        times = await conn.scalar(select(func.count()).select_from(models.Time))
        competicoes = await conn.scalar(select(func.count()).select_from(models.Competicao))
    assert esportes == len(vocabulario.ESPORTES)
    assert mercados == len(vocabulario.MERCADOS)
    assert times == len(vocabulario.TIMES)
    assert competicoes == len(vocabulario.COMPETICOES)
