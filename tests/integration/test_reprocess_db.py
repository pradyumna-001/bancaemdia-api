from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1 import coleta
from bancaemdia.cli import reprocess
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.workers import materialization

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
BRASIL = timezone(timedelta(hours=-3))
DENTRO = datetime(2026, 8, 10, 21, 0, tzinfo=BRASIL)
FORA = datetime(2026, 9, 2, 10, 0, tzinfo=BRASIL)
PLACED = 1785708161930


async def _apostas(
    engine: AsyncEngine, como: Como, usuario: int, *linhas: tuple[object, ...]
) -> None:
    async with como(engine, usuario) as session:
        for origem, chat_id, message_id, ordem, grave, quando in linhas:
            await session.execute(
                insert(models.Aposta).values(
                    usuario_id=usuario,
                    chave=f"{origem}:{uuid4().hex}",
                    origem=origem,
                    chat_id=chat_id,
                    message_id=message_id,
                    ordem_na_mensagem=ordem,
                    stake_unidades=1.0,
                    stake_centavos=1000,
                    revisao_grave=grave,
                    data_aposta=quando,
                )
            )
        await session.commit()


def _bilhete(id_: str, *, resultado: str | None = "Lose", ganho: float = 0.0) -> dict[str, object]:
    bruto: dict[str, object] = {
        "id": id_,
        "bonusType": 0,
        "totalAmount": 160.0,
        "totalAmountWithCurrency": {"amount": 160.0, "currencyCode": "BRL"},
        "totalOdds": 1.9,
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
                        "startTime": PLACED + 1_638_070,
                        "selections": [{"description": "Mais de 41.5", "odds": 1.9}],
                    }
                ]
            }
        ],
    }
    if resultado is None:
        del bruto["finalBetResult"], bruto["settledAt"]
    return bruto


async def _casa_betano(engine_admin: AsyncEngine) -> int:
    async with engine_admin.begin() as conn:
        await conn.execute(
            upsert(models.Casa)
            .values(nome="Betano", dominio="betano.bet.br")
            .on_conflict_do_nothing()
        )
        casa_id = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    assert casa_id is not None
    return int(casa_id)


async def _enviar(
    engine: AsyncEngine, como: Como, usuario: int, casa_id: int, *bilhetes: dict[str, object]
) -> None:
    async with como(engine, usuario) as session:
        await coleta.registrar(session, usuario, "betano", casa_id, list(bilhetes))
        await session.commit()


async def test_bets_are_counted_per_message_inside_the_days_and_only_for_their_owner(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    outro = await novo_usuario()
    await _apostas(
        engine_app,
        como,
        usuario,
        ("telegram", 100, 1, 0, True, DENTRO),
        ("telegram", 100, 1, 1, False, DENTRO),
        ("telegram", 100, 2, 0, False, DENTRO),
        ("telegram", 100, 3, 0, True, FORA),
        ("casa", None, None, 0, False, DENTRO),
    )
    await _apostas(engine_app, como, outro, ("telegram", 100, 1, 0, True, DENTRO))
    desde = datetime(2026, 8, 1, tzinfo=BRASIL)
    ate = datetime(2026, 9, 1, tzinfo=BRASIL)

    async with como(engine_app, usuario) as session:
        contagens = {
            c.origem: c for c in await ApostaRepo().count_by_origem(session, usuario, desde, ate)
        }
    plano = await reprocess.reprocessar_usuario(engine_app, usuario, desde, ate)
    tudo = await reprocess.reprocessar_usuario(engine_app, usuario, escopo=reprocess.ESCOPO_TUDO)
    async with AsyncSession(engine_admin) as session:
        ativos = await UsuarioRepo().list_active_ids(session, min(usuario, outro) - 1)

    telegram = contagens["telegram"]
    assert (telegram.apostas, telegram.apostas_em_revisao) == (3, 1)
    assert (telegram.mensagens, telegram.mensagens_em_revisao) == (2, 1)
    assert (contagens["casa"].apostas, contagens["casa"].apostas_em_revisao) == (1, 0)
    assert (plano.apostas, plano.bilhetes, plano.fora_do_escopo) == (1, 1, 2)
    assert plano.sem_releitura == {"casa": 1}
    assert (tudo.apostas, tudo.bilhetes, tudo.fora_do_escopo) == (4, 3, 0)
    assert {usuario, outro} <= set(ativos)


async def test_raw_stored_before_the_reader_becomes_bets_once_and_stays_put(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    monkeypatch,
) -> None:
    usuario = await novo_usuario()
    outro = await novo_usuario()
    casa_id = await _casa_betano(engine_admin)
    liquidada, perdida = str(uuid4().int)[:11], str(uuid4().int)[:11]
    monkeypatch.delitem(coleta.LEITORES, "betano")
    await _enviar(engine_app, como, usuario, casa_id, _bilhete(liquidada, resultado=None))
    await _enviar(
        engine_app,
        como,
        usuario,
        casa_id,
        _bilhete(liquidada, resultado="Win", ganho=304.0),
        _bilhete(perdida),
    )
    await _enviar(engine_app, como, outro, casa_id, _bilhete(str(uuid4().int)[:11]))
    monkeypatch.undo()
    enfileiradas: list[tuple[int, list[int]]] = []
    monkeypatch.setattr(
        reprocess, "_enfileirar", lambda dono, coleta_ids: enfileiradas.append((dono, coleta_ids))
    )

    mostrado = await reprocess.reprocessar_casa(engine_app, usuario, "betano")
    passadas = []
    for _ in range(2):
        await reprocess.reprocessar_casa(engine_app, usuario, "betano", sim=True)
        for dono, coleta_ids in enfileiradas:
            await materialization.gravar_coletas(engine_app, dono, coleta_ids)
        enfileiradas.clear()
        async with como(engine_app, usuario) as session:
            apostas = await ApostaRepo().list_by_usuario(session, usuario, origem="casa")
            passadas.append({
                a.chave: (
                    a.estado,
                    a.retorno_centavos,
                    len(await EventoRepo().list_by_aposta_chave(session, usuario, a.chave or "")),
                )
                for a in apostas
            })

    assert (mostrado.guardadas, len(mostrado.apostas), mostrado.enfileiradas) == (3, 2, 0)
    assert sorted(len(historico) for historico in mostrado.apostas) == [1, 2]
    assert passadas[0] == {
        f"c:betano:{liquidada}": ("GREEN", 30400, 2),
        f"c:betano:{perdida}": ("RED", 0, 2),
    }
    assert passadas[1] == passadas[0]
