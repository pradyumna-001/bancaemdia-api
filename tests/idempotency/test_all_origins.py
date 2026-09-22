from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import main, models
from bancaemdia.api.v1 import coleta
from bancaemdia.db.session import get_db
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.coleta_token_repo import ColetaTokenRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.workers import materialization

pytestmark = [pytest.mark.idempotency, pytest.mark.xdist_group("postgres")]
Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
T0 = datetime(2030, 1, 1, tzinfo=UTC)


async def _gravar(
    engine: AsyncEngine, como: Como, usuario: int, dados: dict[str, object]
) -> object:
    async with como(engine, usuario) as session:
        aposta = await ApostaRepo().upsert_materializada(session, {"usuario_id": usuario, **dados})
        await session.commit()
    return aposta


async def _contar(
    engine: AsyncEngine, como: Como, usuario: int, chave: str
) -> tuple[int, models.Aposta]:
    async with como(engine, usuario) as session:
        linhas = (
            (
                await session.execute(
                    select(models.Aposta).where(
                        models.Aposta.usuario_id == usuario, models.Aposta.chave == chave
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(linhas) == 1
    return len(linhas), linhas[0]


async def test_exact_duplicate_keeps_one_row_and_refreshes_timestamp(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    aposta_payload: dict[str, object],
) -> None:
    usuario = await novo_usuario()
    primeira = await _gravar(engine_app, como, usuario, aposta_payload)
    repetida = await _gravar(engine_app, como, usuario, aposta_payload)
    assert primeira is not None and repetida is not None
    _, linha = await _contar(engine_app, como, usuario, str(aposta_payload["chave"]))
    assert (primeira.id, repetida.id, linha.id) == (linha.id,) * 3
    assert linha.criada_em == primeira.criada_em
    assert linha.atualizada_em >= primeira.atualizada_em


async def test_newer_timestamp_updates_and_older_timestamp_is_ignored(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    aposta_payload: dict[str, object],
) -> None:
    usuario = await novo_usuario()
    primeira = await _gravar(engine_app, como, usuario, {**aposta_payload, "atualizada_em": T0})
    nova = await _gravar(
        engine_app,
        como,
        usuario,
        {**aposta_payload, "odd": 2.1, "atualizada_em": T0 + timedelta(minutes=1)},
    )
    velha = await _gravar(
        engine_app, como, usuario, {**aposta_payload, "odd": 9.0, "atualizada_em": T0}
    )
    assert primeira is not None and nova is not None and velha is None
    _, linha = await _contar(engine_app, como, usuario, str(aposta_payload["chave"]))
    assert linha.id == primeira.id == nova.id
    assert linha.odd == pytest.approx(2.1)
    assert linha.atualizada_em == T0 + timedelta(minutes=1)


async def test_partial_update_preserves_fields_not_in_payload(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    aposta_payload: dict[str, object],
) -> None:
    usuario = await novo_usuario()
    primeira = await _gravar(
        engine_app,
        como,
        usuario,
        {**aposta_payload, "estado": "GREEN", "retorno_centavos": 18000},
    )
    assert primeira is not None
    minimo = {
        "chave": aposta_payload["chave"],
        "origem": aposta_payload["origem"],
        "stake_unidades": 1.0,
        "stake_centavos": 10000,
        "odd": 2.2,
    }
    if aposta_payload["origem"] == "telegram":
        minimo |= {
            "chat_id": aposta_payload["chat_id"],
            "message_id": aposta_payload["message_id"],
        }
    segunda = await _gravar(
        engine_app,
        como,
        usuario,
        minimo,
    )
    assert segunda is not None
    _, linha = await _contar(engine_app, como, usuario, str(aposta_payload["chave"]))
    assert linha.id == primeira.id
    assert (linha.origem, linha.stake_centavos, linha.stake_unidades) == (
        aposta_payload["origem"],
        10000,
        1.0,
    )
    assert linha.odd == pytest.approx(2.2)
    assert (linha.estado, linha.retorno_centavos) == ("GREEN", 18000)


async def test_house_open_settled_and_repeat_keep_one_raw_row(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as conn:
        casa_id = await conn.scalar(
            insert(models.Casa)
            .values(nome=f"Casa {uuid4().hex}", dominio=f"{uuid4().hex}.test")
            .returning(models.Casa.id)
        )
    assert casa_id is not None
    identidade = uuid4().hex
    base = {"usuario_id": usuario, "casa_id": casa_id, "identidade": identidade}
    async with como(engine_app, usuario) as session:
        aberta = await ColetaCasaRepo().upsert_idempotent(
            session, {**base, "hash_conteudo": "open", "bruto_json": {"status": "open"}}
        )
        await session.commit()
    async with como(engine_app, usuario) as session:
        liquidada = await ColetaCasaRepo().upsert_idempotent(
            session, {**base, "hash_conteudo": "settled", "bruto_json": {"status": "settled"}}
        )
        await session.commit()
    async with como(engine_app, usuario) as session:
        repetida = await ColetaCasaRepo().upsert_idempotent(
            session, {**base, "hash_conteudo": "settled", "bruto_json": {"status": "settled"}}
        )
        await session.commit()
    async with como(engine_app, usuario) as session:
        quantas = await session.scalar(
            select(func.count())
            .select_from(models.ColetaCasa)
            .where(
                models.ColetaCasa.usuario_id == usuario,
                models.ColetaCasa.casa_id == casa_id,
                models.ColetaCasa.identidade == identidade,
            )
        )
    assert aberta is not None and liquidada is not None and repetida is None
    assert aberta.id == liquidada.id and liquidada.hash_conteudo == "settled"
    assert quantas == 1


async def test_house_open_settled_replay_materializes_one_bet(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as conn:
        await conn.execute(
            insert(models.Casa)
            .values(nome="Betano", dominio="betano.bet.br")
            .on_conflict_do_nothing()
        )
        casa_id = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    assert casa_id is not None
    envio = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "coleta" / "betano.json").read_text()
    )["apostas"][0]
    identidade = uuid4().hex
    aberta = {**envio, "id": identidade, "finalWinnings": 0.0}
    aberta.pop("finalBetResult")
    aberta.pop("settledAt")
    async with como(engine_app, usuario) as session:
        primeira, (coleta_id,) = await coleta.registrar(
            session, usuario, "betano", casa_id, [aberta]
        )
        await session.commit()
    gravada = await materialization.gravar_coleta(engine_app, usuario, coleta_id)
    chave = f"c:betano:{identidade}"
    async with como(engine_app, usuario) as session:
        aposta_aberta = await ApostaRepo().get_by_chave(session, usuario, chave)
    liquidada = {**envio, "id": identidade}
    async with como(engine_app, usuario) as session:
        segunda, fila = await coleta.registrar(session, usuario, "betano", casa_id, [liquidada])
        await session.commit()
    assert fila == [coleta_id]
    await materialization.gravar_coleta(engine_app, usuario, coleta_id)
    async with como(engine_app, usuario) as session:
        terceira, fila_repetida = await coleta.registrar(
            session, usuario, "betano", casa_id, [liquidada]
        )
        await session.commit()
    async with como(engine_app, usuario) as session:
        aposta_final = await ApostaRepo().get_by_chave(session, usuario, chave)
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario, chave)
        quantas = await session.scalar(
            select(func.count())
            .select_from(models.ColetaCasa)
            .where(
                models.ColetaCasa.usuario_id == usuario,
                models.ColetaCasa.casa_id == casa_id,
                models.ColetaCasa.identidade == identidade,
            )
        )
    assert gravada is not None and gravada.criada
    assert (primeira.novas_contando, segunda.atualizadas, terceira.ja_conhecidas) == (1, 1, 1)
    assert fila_repetida == [] and quantas == 1
    assert aposta_aberta is not None and aposta_final is not None
    assert aposta_aberta.id == aposta_final.id and aposta_final.estado == "GREEN"
    assert [evento.tipo for evento in eventos] == ["APOSTA_CRIADA", "RESULTADO_REGISTRADO"]


async def test_ten_concurrent_identical_upserts_create_one_row(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    dados: dict[str, object] = {
        "usuario_id": usuario,
        "chave": f"m:{uuid4().hex}",
        "origem": "manual",
        "stake_unidades": 1.0,
        "stake_centavos": 10000,
        "odd": 1.9,
    }

    async def enviar() -> int:
        async with como(engine_app, usuario) as session:
            aposta = await ApostaRepo().upsert_idempotent(session, dados)
            await session.commit()
            return aposta.id

    ids = await asyncio.gather(*(enviar() for _ in range(10)))
    _, linha = await _contar(engine_app, como, usuario, str(dados["chave"]))
    assert set(ids) == {linha.id}


async def test_ten_concurrent_house_captures_create_one_raw_row(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as conn:
        casa_id = await conn.scalar(
            insert(models.Casa)
            .values(nome=f"Casa {uuid4().hex}", dominio=f"{uuid4().hex}.test")
            .returning(models.Casa.id)
        )
    assert casa_id is not None
    identidade = uuid4().hex
    dados: dict[str, object] = {
        "usuario_id": usuario,
        "casa_id": casa_id,
        "identidade": identidade,
        "hash_conteudo": "open",
        "bruto_json": {"status": "open"},
    }

    async def enviar() -> object:
        async with como(engine_app, usuario) as session:
            gravada = await ColetaCasaRepo().upsert_idempotent(session, dados)
            await session.commit()
            return gravada

    respostas = await asyncio.gather(*(enviar() for _ in range(10)))
    assert sum(resposta is not None for resposta in respostas) == 1
    async with como(engine_app, usuario) as session:
        linhas = (
            (
                await session.execute(
                    select(models.ColetaCasa).where(
                        models.ColetaCasa.usuario_id == usuario,
                        models.ColetaCasa.casa_id == casa_id,
                        models.ColetaCasa.identidade == identidade,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(linhas) == 1


async def test_ten_concurrent_http_collections_return_200_and_store_one_row(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as conn:
        await conn.execute(
            insert(models.Casa)
            .values(nome="Betano", dominio="betano.bet.br")
            .on_conflict_do_nothing()
        )
    token = f"token-{uuid4().hex}"
    async with como(engine_app, usuario) as session:
        await ColetaTokenRepo().create(session, usuario, coleta.hash_do_token(token))
        await session.commit()
    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "coleta" / "betano.json").read_text()
    )
    payload["apostas"] = payload["apostas"][:1]
    payload["apostas"][0]["id"] = uuid4().hex

    async def banco_da_rota():
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            yield session

    monkeypatch.setitem(main.app.dependency_overrides, get_db, banco_da_rota)
    monkeypatch.setattr(coleta, "_enfileirar", lambda _usuario, _fila: None)
    monkeypatch.setattr(coleta.limiter, "enabled", False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        respostas = await asyncio.gather(
            *(
                client.post("/coleta", json=payload, headers={coleta.TOKEN_HEADER: token})
                for _ in range(10)
            )
        )
    assert all(resposta.status_code == 200 for resposta in respostas)
    assert sum(resposta.json()["novas_contando"] for resposta in respostas) == 1
    async with como(engine_app, usuario) as session:
        quantas = await session.scalar(
            select(func.count())
            .select_from(models.ColetaCasa)
            .where(
                models.ColetaCasa.usuario_id == usuario,
                models.ColetaCasa.identidade == payload["apostas"][0]["id"],
            )
        )
    assert quantas == 1
