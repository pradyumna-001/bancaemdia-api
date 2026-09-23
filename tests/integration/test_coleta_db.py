from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia import main, models
from bancaemdia.api.v1 import coleta
from bancaemdia.db.session import get_db
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.coleta_token_repo import ColetaTokenRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.workers import materialization

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
PLACED = 1785708161930
START = 1785709800000


def _bilhete(id_=None, *, resultado="Lose", ganho=0.0, odd=1.90, **extra):
    bruto = {
        "id": id_ or str(uuid4().int)[:11],
        "bonusType": 0,
        "totalAmount": 160.0,
        "totalAmountWithCurrency": {"amount": 160.0, "currencyCode": "BRL"},
        "totalOdds": odd,
        "finalWinnings": ganho,
        "placedAt": PLACED,
        "finalBetResult": resultado,
        "settledAt": PLACED + 3_600_000,
        "legs": [
            {
                "legItems": [
                    {
                        "eventId": "86389413",
                        "eventName": "Internacional - Corinthians",
                        "startTime": START,
                        "selections": [{"description": "Mais de 41.5", "odds": odd}],
                    }
                ]
            }
        ],
        **extra,
    }
    if resultado is None:
        del bruto["finalBetResult"], bruto["settledAt"]
    return bruto


async def _casa(engine_admin: AsyncEngine, nome: str = "Betano") -> int:
    async with engine_admin.begin() as conn:
        await conn.execute(
            insert(models.Casa)
            .values(nome=nome, dominio=f"{nome.lower()}.bet.br")
            .on_conflict_do_nothing()
        )
        casa_id = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == nome))
    assert casa_id is not None
    return int(casa_id)


async def _token(
    engine: AsyncEngine, como: Como, usuario: int, expira_em: datetime | None = None
) -> str:
    token = f"tok-{uuid4().hex}"
    async with como(engine, usuario) as session:
        await ColetaTokenRepo().create(session, usuario, coleta.hash_do_token(token), expira_em)
        await session.commit()
    return token


async def _enviar(
    engine: AsyncEngine, como: Como, usuario: int, casa_id: int, bilhetes: list[object]
) -> tuple[coleta.Resultado, list[int]]:
    async with como(engine, usuario) as session:
        resultado, fila = await coleta.registrar(session, usuario, "betano", casa_id, bilhetes)
        await session.commit()
    return resultado, fila


async def _aposta(engine: AsyncEngine, como: Como, usuario: int, chave: str):
    async with como(engine, usuario) as session:
        return await ApostaRepo().get_by_chave(session, usuario, chave)


async def test_token_is_found_by_its_hash_before_there_is_a_current_user(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    outro = await novo_usuario()
    token = await _token(engine_app, como, usuario)
    await _token(engine_app, como, outro)

    async with como(engine_app, None) as session:
        sem_hash = (await session.execute(select(models.ColetaToken.id))).all()
    async with como(engine_app, None) as session:
        dono = await ColetaTokenRepo().get_usuario_id_by_hash(session, coleta.hash_do_token(token))
        visiveis = (await session.execute(select(models.ColetaToken.usuario_id))).scalars().all()
    async with como(engine_app, None) as session:
        errado = await ColetaTokenRepo().get_usuario_id_by_hash(session, coleta.hash_do_token("x"))

    assert sem_hash == []
    assert dono == usuario
    assert visiveis == [usuario]
    assert errado is None


async def test_deactivated_or_expired_token_is_not_accepted(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    vencido = await novo_usuario()
    token = await _token(engine_app, como, usuario)
    token_vencido = await _token(
        engine_app, como, vencido, datetime.now(UTC) - timedelta(minutes=1)
    )
    async with como(engine_app, usuario) as session:
        desativados = await ColetaTokenRepo().deactivate_by_usuario(session, usuario)
        await session.commit()

    async with como(engine_app, None) as session:
        tokens = ColetaTokenRepo()
        assert await tokens.get_usuario_id_by_hash(session, coleta.hash_do_token(token)) is None
        assert (
            await tokens.get_usuario_id_by_hash(session, coleta.hash_do_token(token_vencido))
            is None
        )
    assert desativados == 1


async def test_house_send_becomes_a_bet_that_follows_the_house_result(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    casa_id = await _casa(engine_admin)
    identidade = str(uuid4().int)[:11]
    chave = f"c:betano:{identidade}"

    aberta, fila = await _enviar(
        engine_app, como, usuario, casa_id, [_bilhete(identidade, resultado=None)]
    )
    (coleta_id,) = fila
    await materialization.gravar_coleta(engine_app, usuario, coleta_id)
    pendente = await _aposta(engine_app, como, usuario, chave)
    liquidada, fila_liquidada = await _enviar(
        engine_app, como, usuario, casa_id, [_bilhete(identidade, resultado="Win", ganho=304.0)]
    )
    gravada = await materialization.gravar_coleta(engine_app, usuario, coleta_id)
    ganha = await _aposta(engine_app, como, usuario, chave)
    reenvio, fila_do_reenvio = await _enviar(
        engine_app, como, usuario, casa_id, [_bilhete(identidade, resultado="Win", ganho=304.0)]
    )

    assert (aberta.novas_contando, liquidada.atualizadas, reenvio.ja_conhecidas) == (1, 1, 1)
    assert (fila_liquidada, fila_do_reenvio) == ([coleta_id], [])
    assert pendente is not None
    assert (pendente.origem, pendente.estado, pendente.retorno_centavos) == (
        "casa",
        "PENDENTE",
        None,
    )
    assert pendente.stake_centavos == 16000
    assert pendente.data_aposta == datetime(2026, 8, 2, 22, 30, tzinfo=UTC)
    assert gravada is not None and (gravada.criada, gravada.eventos) == (False, 1)
    assert ganha is not None and ganha.id == pendente.id
    assert (ganha.estado, ganha.retorno_centavos) == ("GREEN", 30400)
    async with como(engine_app, usuario) as session:
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario, chave)
        guardada = await ColetaCasaRepo().get_by_id_for_update(session, usuario, coleta_id)
    assert [(e.tipo, e.fonte) for e in eventos] == [
        ("APOSTA_CRIADA", "casa"),
        ("RESULTADO_REGISTRADO", "casa"),
    ]
    assert guardada is not None and guardada.processado_em is not None


async def test_another_user_sees_neither_the_raw_bet_nor_the_bet(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    outro = await novo_usuario()
    casa_id = await _casa(engine_admin)
    _, (coleta_id,) = await _enviar(engine_app, como, usuario, casa_id, [_bilhete()])
    await materialization.gravar_coleta(engine_app, usuario, coleta_id)

    async with como(engine_app, outro) as session:
        cruas = (await session.execute(select(models.ColetaCasa.id))).all()
        apostas = await ApostaRepo().list_by_usuario(session, usuario)
    assert await materialization.gravar_coleta(engine_app, outro, coleta_id) is None
    assert (cruas, apostas) == ([], [])


async def test_concurrent_processing_of_one_row_creates_the_bet_once(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    casa_id = await _casa(engine_admin)
    identidade = str(uuid4().int)[:11]
    _, (coleta_id,) = await _enviar(engine_app, como, usuario, casa_id, [_bilhete(identidade)])

    gravadas = await asyncio.gather(
        *(materialization.gravar_coleta(engine_app, usuario, coleta_id) for _ in range(5))
    )

    assert sum(g is not None and g.criada for g in gravadas) == 1
    async with como(engine_app, usuario) as session:
        eventos = await EventoRepo().list_by_aposta_chave(
            session, usuario, f"c:betano:{identidade}"
        )
    assert [e.tipo for e in eventos] == ["APOSTA_CRIADA", "RESULTADO_REGISTRADO"]


async def test_late_task_waits_for_the_newer_capture_and_records_its_result(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    casa_id = await _casa(engine_admin)
    identidade = str(uuid4().int)[:11]
    _, (coleta_id,) = await _enviar(
        engine_app, como, usuario, casa_id, [_bilhete(identidade, resultado=None)]
    )
    await materialization.gravar_coleta(engine_app, usuario, coleta_id)

    async with como(engine_app, usuario) as rota:
        await coleta.registrar(
            rota, usuario, "betano", casa_id, [_bilhete(identidade, resultado="Win", ganho=304.0)]
        )
        atrasada = asyncio.create_task(
            materialization.gravar_coleta(engine_app, usuario, coleta_id)
        )
        await asyncio.sleep(0.5)
        esperou = not atrasada.done()
        await rota.commit()
    gravada = await atrasada

    ganha = await _aposta(engine_app, como, usuario, f"c:betano:{identidade}")
    assert esperou
    assert gravada is not None and gravada.eventos == 1
    assert ganha is not None and (ganha.estado, ganha.retorno_centavos) == ("GREEN", 30400)


async def test_held_house_bet_opens_a_review_linked_to_its_raw_row(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    casa_id = await _casa(engine_admin)
    _, (coleta_id,) = await _enviar(engine_app, como, usuario, casa_id, [_bilhete(bonusType=3)])

    await materialization.gravar_coleta(engine_app, usuario, coleta_id)
    await materialization.gravar_coleta(engine_app, usuario, coleta_id)

    async with como(engine_app, usuario) as session:
        revisoes = await RevisaoPendenteRepo().list_by_usuario(session, usuario)
    (revisao,) = [r for r in revisoes if r.extracao_bruta.get("tipo_revisao") != "conta"]
    assert len(revisoes) == 2
    assert "bonusType=3" in revisao.motivo
    assert revisao.extracao_bruta is not None
    assert revisao.extracao_bruta["coleta_id"] == coleta_id


async def test_rows_received_today_are_counted_for_the_daily_limit(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    casa_id = await _casa(engine_admin)
    await _enviar(engine_app, como, usuario, casa_id, [_bilhete(), _bilhete(), _bilhete(odd=1.0)])
    hoje = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    async with como(engine_app, usuario) as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        quantas = await ColetaCasaRepo().count_received_since(session, usuario, hoje)
        amanha = await ColetaCasaRepo().count_received_since(
            session, usuario, hoje + timedelta(days=1)
        )

    assert (quantas, amanha) == (3, 0)


async def test_bet_jsonb_cannot_hold_is_refused_and_the_rest_of_the_send_is_stored(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    casa_id = await _casa(engine_admin)

    resultado, fila = await _enviar(
        engine_app, como, usuario, casa_id, [_bilhete(header="nulo\x00"), _bilhete()]
    )

    assert [r["posicao"] for r in resultado.recusadas] == [0]
    assert (resultado.novas_contando, len(fila)) == (1, 1)


def test_route_stores_the_send_through_the_row_policies(banco, monkeypatch) -> None:
    async def preparar() -> tuple[int, str, int]:
        engine = create_async_engine(banco.url_admin, poolclass=NullPool)
        app = create_async_engine(banco.url_app, poolclass=NullPool)
        try:
            casa_id = await _casa(engine)
            async with AsyncSession(app) as session:
                usuario = (
                    await UsuarioRepo().create(session, f"{uuid4().hex[:10]}@t.local", "T")
                ).id
                await session.commit()
            token = f"tok-{uuid4().hex}"
            async with AsyncSession(app) as session:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(usuario)},
                )
                await ColetaTokenRepo().create(session, usuario, coleta.hash_do_token(token))
                await session.commit()
            return usuario, token, casa_id
        finally:
            await engine.dispose()
            await app.dispose()

    usuario, token, casa_id = asyncio.run(preparar())
    enfileiradas = []

    class Sessoes:
        async def abrir(self):
            engine = create_async_engine(banco.url_app, poolclass=NullPool)
            try:
                async with AsyncSession(engine) as session:
                    yield session
            finally:
                await engine.dispose()

    monkeypatch.setitem(main.app.dependency_overrides, get_db, Sessoes().abrir)
    monkeypatch.setattr(coleta, "_enfileirar", lambda uid, fila: enfileiradas.append((uid, fila)))
    monkeypatch.setattr(coleta.limiter, "enabled", False)
    corpo = {"contrato": 1, "casa": "betano", "capturado_em": "x", "apostas": [_bilhete()]}

    resposta = TestClient(main.app).post(
        "/coleta", json=corpo, headers={coleta.TOKEN_HEADER: token}
    )

    assert (resposta.status_code, resposta.json()["novas_contando"]) == (200, 1)
    ((dono, (coleta_id,)),) = enfileiradas
    assert dono == usuario

    async def ler() -> tuple[int, int] | None:
        engine = create_async_engine(banco.url_app, poolclass=NullPool)
        try:
            async with AsyncSession(engine) as session:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(usuario)},
                )
                linha = await ColetaCasaRepo().get_by_id_for_update(session, usuario, coleta_id)
                return None if linha is None else (linha.casa_id, linha.usuario_id)
        finally:
            await engine.dispose()

    assert asyncio.run(ler()) == (casa_id, usuario)
