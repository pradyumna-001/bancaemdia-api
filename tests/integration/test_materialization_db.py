from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bancaemdia import models
from bancaemdia.config import get_settings
from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.workers import materialization

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
CHAVE = "t:100:200:0"


def _extracao(**campos: object) -> dict[str, object]:
    bilhete = ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="Velez x Instituto",
        selecoes=[Selecao(mercado="Handicap", escolha="Instituto", odd=1.82)],
        odd_total=1.82,
        confianca=0.95,
    )
    return {
        "usuario_id": 7,
        "chat_id": 100,
        "message_id": 200,
        "postada_em": "2026-07-24T16:00:00",
        "versao_prompt": "extrair_bilhete_v3",
        "bilhete": bilhete.model_dump(mode="json"),
        "motivo": None,
        "grave": False,
        "cupons": [],
        "degrau": "BARATO",
        "custo_usd": 0.012,
        "nao_e_aposta": False,
        **campos,
    }


async def _materializar(
    engine: AsyncEngine, usuario: int, extracao: dict[str, object]
) -> list[materialization.Gravada]:
    return await materialization.gravar_leitura(
        engine,
        usuario,
        materialization.ExtracaoDoJson.model_validate(extracao),
        extracao,
        "hash-da-foto",
    )


async def _aposta(engine: AsyncEngine, como: Como, usuario: int):
    async with como(engine, usuario) as session:
        return await ApostaRepo().get_by_chave(session, usuario, CHAVE)


async def _eventos(engine: AsyncEngine, como: Como, usuario: int):
    async with como(engine, usuario) as session:
        return await EventoRepo().list_by_aposta_chave(session, usuario, CHAVE)


async def test_resending_the_same_reading_keeps_one_bet_and_updates_atualizada_em(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    (primeira,) = await _materializar(engine_app, usuario, _extracao())
    antes = await _aposta(engine_app, como, usuario)
    (segunda,) = await _materializar(engine_app, usuario, _extracao())
    depois = await _aposta(engine_app, como, usuario)

    assert (primeira.criada, segunda.criada, segunda.eventos) == (True, False, 0)
    assert antes is not None and depois is not None
    assert depois.id == antes.id
    assert depois.criada_em == antes.criada_em
    assert depois.atualizada_em > antes.atualizada_em
    assert depois.odd == pytest.approx(1.82)
    assert depois.data_aposta == datetime(2026, 7, 24, 19, tzinfo=UTC)
    async with como(engine_app, usuario) as session:
        apostas = await ApostaRepo().list_by_usuario(session, usuario)
    assert [a.chave for a in apostas] == [CHAVE]
    assert [e.tipo for e in await _eventos(engine_app, como, usuario)] == ["APOSTA_CRIADA"]


async def test_grave_reading_opens_one_review_even_when_resent(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    grave = _extracao(motivo="coerência das odds: diverge", grave=True)

    await _materializar(engine_app, usuario, grave)
    await _materializar(engine_app, usuario, grave)

    async with como(engine_app, usuario) as session:
        revisoes = await RevisaoPendenteRepo().list_by_usuario(session, usuario)
    extraction_reviews = [r for r in revisoes if r.extracao_bruta.get("tipo_revisao") != "conta"]
    assert [(r.motivo, r.midia_hash) for r in extraction_reviews] == [
        ("coerência das odds: diverge", "hash-da-foto")
    ]
    assert len(revisoes) == 2
    assert extraction_reviews[0].extracao_bruta["aposta_chave"] == CHAVE
    aposta = await _aposta(engine_app, como, usuario)
    assert aposta is not None and aposta.revisao_grave is True


async def test_better_reading_updates_the_bet_but_not_what_the_person_fixed(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()
    falhou = _extracao(bilhete=None, motivo="a leitura falhou (APIError)", grave=True)
    await _materializar(engine_app, usuario, falhou)
    async with como(engine_app, usuario) as session:
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario,
                "tipo": "CORRECAO_MANUAL",
                "fonte": "manual",
                "payload_json": {"evento": "Meu evento"},
                "aposta_chave": CHAVE,
            },
        )
        await session.commit()

    await _materializar(engine_app, usuario, _extracao())

    eventos = await _eventos(engine_app, como, usuario)
    assert [(e.tipo, e.fonte) for e in eventos] == [
        ("APOSTA_CRIADA", "ia"),
        ("CORRECAO_MANUAL", "manual"),
        ("CORRECAO_MANUAL", "ia"),
    ]
    assert "evento" not in eventos[-1].payload_json
    assert eventos[-1].payload_json["odd"] == pytest.approx(1.82)
    aposta = await _aposta(engine_app, como, usuario)
    assert aposta is not None
    assert aposta.odd == pytest.approx(1.82)
    assert aposta.revisao_grave is False


async def test_reviews_follow_the_latest_reading(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    await _materializar(engine_app, usuario, _extracao(motivo="motivo A", grave=True))
    await _materializar(engine_app, usuario, _extracao(motivo="motivo B", grave=True))
    async with como(engine_app, usuario) as session:
        abertas = await RevisaoPendenteRepo().list_by_usuario(session, usuario)
    await _materializar(engine_app, usuario, _extracao())
    async with como(engine_app, usuario) as session:
        abertas_depois = await RevisaoPendenteRepo().list_by_usuario(session, usuario)
        todas = await RevisaoPendenteRepo().list_by_usuario(session, usuario, apenas_abertas=False)

    assert [r.motivo for r in abertas if r.extracao_bruta.get("tipo_revisao") != "conta"] == [
        "motivo B"
    ]
    assert [
        r.motivo for r in abertas_depois if r.extracao_bruta.get("tipo_revisao") != "conta"
    ] == []
    assert sorted(r.motivo for r in todas if r.extracao_bruta.get("tipo_revisao") != "conta") == [
        "motivo A",
        "motivo B",
    ]
    assert len([r for r in abertas_depois if r.extracao_bruta.get("tipo_revisao") == "conta"]) == 1


async def test_bet_goes_to_the_users_account_at_the_house_it_was_read_from(
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
    async with como(engine_app, usuario) as session:
        conta = await ContaCasaRepo().create(session, {"usuario_id": usuario, "casa_id": casa_id})
        await UsoContaCasaRepo().open(
            session, usuario, casa_id, conta.id, datetime(2026, 7, 1, tzinfo=UTC)
        )
        await session.commit()

    await _materializar(engine_app, usuario, _extracao())
    aposta = await _aposta(engine_app, como, usuario)
    async with engine_admin.begin() as conn:
        await conn.execute(
            update(models.ContaCasa).where(models.ContaCasa.id == conta.id).values(ativa=False)
        )
    await _materializar(engine_app, usuario, _extracao())
    reenviada = await _aposta(engine_app, como, usuario)

    assert aposta is not None and aposta.conta_casa_id == conta.id
    assert reenviada is not None and reenviada.conta_casa_id == conta.id


async def test_another_user_never_sees_the_bet(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono, outro = await novo_usuario(), await novo_usuario()

    await _materializar(engine_app, dono, _extracao())

    async with como(engine_app, outro) as session:
        apostas = await session.scalar(
            select(func.count()).select_from(models.Aposta).where(models.Aposta.chave == CHAVE)
        )
        eventos = await session.scalar(
            select(func.count())
            .select_from(models.Evento)
            .where(models.Evento.aposta_chave == CHAVE)
        )
    assert (apostas, eventos) == (0, 0)


async def test_concurrent_deliveries_create_the_bet_once(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    resultados = await asyncio.gather(
        *(_materializar(engine_app, usuario, _extracao()) for _ in range(5))
    )

    assert sum(gravada.criada for (gravada,) in resultados) == 1
    assert [e.tipo for e in await _eventos(engine_app, como, usuario)] == ["APOSTA_CRIADA"]


async def _criar_usuario(url: str) -> int:
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session:
            usuario = await UsuarioRepo().create(session, f"{uuid4().hex[:10]}@teste.local", "T")
            await session.commit()
            return usuario.id
    finally:
        await engine.dispose()


def test_task_materializes_through_celery_against_the_database(banco, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", banco.url_app)
    get_settings.cache_clear()
    materialization.get_engine.cache_clear()
    try:
        usuario = asyncio.run(_criar_usuario(banco.url_app))
        argumentos = {"usuario_id": usuario, "extracao_json": _extracao(), "midia_hash": "h"}
        primeira = materialization.materializar_aposta_task.apply(kwargs=argumentos).get()
        segunda = materialization.materializar_aposta_task.apply(kwargs=argumentos).get()
    finally:
        get_settings.cache_clear()
        materialization.get_engine.cache_clear()

    assert (primeira["criadas"], segunda["criadas"]) == (1, 0)
    assert primeira["apostas"] == [CHAVE]
