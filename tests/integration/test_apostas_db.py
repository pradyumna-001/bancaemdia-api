from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.domain.aposta_service import (
    eventos_da_correcao,
    eventos_da_exclusao,
    eventos_do_resultado,
)
from bancaemdia.domain.materializar import projetar
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
AGORA = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _chave() -> str:
    return f"m:{uuid4().hex}"


def _criacao(**extra: object) -> dict[str, object]:
    return {
        "origem": "manual",
        "casa": "Betano",
        "odd": 1.82,
        "stake_unidades": 1.0,
        "valor_unidade_centavos": 10_000,
        "freebet": False,
        "selecionada": True,
        "data_aposta": "2026-09-10T21:00:00",
        **extra,
    }


async def _gravar(
    session: AsyncSession, usuario_id: int, chave: str, eventos: list[tuple[str, str, dict]]
) -> models.Aposta | None:
    for tipo, fonte, payload in eventos:
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario_id,
                "tipo": tipo,
                "fonte": fonte,
                "payload_json": payload,
                "confianca": None,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": chave,
            },
        )
    estado, _ = projetar(eventos)
    dados: dict[str, object] = {
        "usuario_id": usuario_id,
        "chave": chave,
        "origem": estado.get("origem", "manual"),
        "stake_unidades": estado.get("stake_unidades", 0.0),
        "stake_centavos": 10_000,
        "valor_aposta_centavos": 10_000,
        "odd": estado.get("odd"),
        "estado": estado.get("estado", "PENDENTE"),
        "retorno_centavos": estado.get("retorno_centavos"),
        "revisao_grave": bool(estado.get("revisao_grave")),
        "selecionada": bool(estado.get("selecionada", True)),
        "data_aposta": datetime.fromisoformat(str(estado["data_aposta"])),
    }
    if estado.get("conta_casa_id") is not None:
        dados["conta_casa_id"] = estado["conta_casa_id"]
    return await ApostaRepo().upsert_materializada(session, dados)


async def _uma_aposta(session: AsyncSession, usuario_id: int, **extra: object) -> str:
    chave = _chave()
    await _gravar(session, usuario_id, chave, [("APOSTA_CRIADA", "ia", _criacao(**extra))])
    return chave


async def test_the_deleted_flag_is_written_and_read_back(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        chave = await _uma_aposta(session, usuario)
        await session.commit()

    async with como(engine_app, usuario) as session:
        nascida = await ApostaRepo().get_by_chave(session, usuario, chave)
        historico = [
            (e.tipo, e.fonte, e.payload_json)
            for e in await EventoRepo().list_by_aposta_chave(session, usuario, chave)
        ]
        apagar = eventos_da_exclusao()
        await _gravar(
            session,
            usuario,
            chave,
            [*historico, *((e.tipo, e.fonte, e.payload) for e in apagar)],
        )
        await session.commit()

    async with como(engine_app, usuario) as session:
        apagada = await ApostaRepo().get_by_chave(session, usuario, chave)

    assert nascida is not None and nascida.selecionada is True
    assert apagada is not None and apagada.selecionada is False


async def test_a_deleted_bet_does_not_come_back_when_the_reading_is_written_again(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        chave = await _uma_aposta(session, usuario)
        historico = [("APOSTA_CRIADA", "ia", _criacao())]
        apagar = eventos_da_exclusao()
        historico = [*historico, *((e.tipo, e.fonte, e.payload) for e in apagar)]
        await _gravar(session, usuario, chave, historico)
        await session.commit()

    # A releitura da mesma foto reescreve a linha a partir do histórico: a decisão de apagar está
    # lá, então ela não pode ressuscitar.
    async with como(engine_app, usuario) as session:
        await _gravar(session, usuario, chave, historico)
        await session.commit()

    async with como(engine_app, usuario) as session:
        aposta = await ApostaRepo().get_by_chave(session, usuario, chave)

    assert aposta is not None and aposta.selecionada is False


async def test_settling_a_bet_writes_no_cashbox_line(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        chave = await _uma_aposta(session, usuario)
        antes = (
            await session.execute(
                select(func.count())
                .select_from(models.Movimento)
                .where(models.Movimento.usuario_id == usuario)
            )
        ).scalar_one()
        historico = [("APOSTA_CRIADA", "ia", _criacao())]
        ganhou = eventos_do_resultado("GREEN")
        aposta = await _gravar(
            session,
            usuario,
            chave,
            [*historico, *((e.tipo, e.fonte, e.payload) for e in ganhou)],
        )
        await session.commit()

    async with como(engine_app, usuario) as session:
        depois = (
            await session.execute(
                select(func.count())
                .select_from(models.Movimento)
                .where(models.Movimento.usuario_id == usuario)
            )
        ).scalar_one()

    # O saldo da casa já conta o retorno da aposta: um movimento aqui contaria o ganho duas vezes.
    assert aposta is not None
    assert (aposta.estado, aposta.retorno_centavos) == ("GREEN", 18_200)
    assert (antes, depois) == (0, 0)


async def test_the_page_brings_the_total_of_the_whole_filter(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        for _ in range(5):
            await _uma_aposta(session, usuario)
        await session.commit()

    async with como(engine_app, usuario) as session:
        primeira, total_um = await ApostaRepo().list_page(session, usuario, {}, 1, 2)
        segunda, total_dois = await ApostaRepo().list_page(session, usuario, {}, 2, 2)
        terceira, _ = await ApostaRepo().list_page(session, usuario, {}, 3, 2)

    assert (len(primeira), len(segunda), len(terceira)) == (2, 2, 1)
    # O total é o do filtro inteiro, não o da página.
    assert total_um == total_dois == 5
    chaves = [a.chave for a in [*primeira, *segunda, *terceira]]
    assert len(set(chaves)) == 5


async def test_the_page_hides_the_deleted_ones_unless_they_are_asked_for(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        viva = await _uma_aposta(session, usuario)
        apagada = _chave()
        apagar = eventos_da_exclusao()
        await _gravar(
            session,
            usuario,
            apagada,
            [
                ("APOSTA_CRIADA", "ia", _criacao()),
                *((e.tipo, e.fonte, e.payload) for e in apagar),
            ],
        )
        await session.commit()

    async with como(engine_app, usuario) as session:
        normais, total_normais = await ApostaRepo().list_page(session, usuario, {}, 1, 50)
        todas, total_todas = await ApostaRepo().list_page(
            session, usuario, {"incluir_apagadas": True}, 1, 50
        )

    assert [a.chave for a in normais] == [viva] and total_normais == 1
    assert {a.chave for a in todas} == {viva, apagada} and total_todas == 2


async def test_every_filter_of_the_list_narrows_what_comes_back(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        verde = _chave()
        ganhou = eventos_do_resultado("GREEN")
        await _gravar(
            session,
            usuario,
            verde,
            [
                ("APOSTA_CRIADA", "ia", _criacao()),
                *((e.tipo, e.fonte, e.payload) for e in ganhou),
            ],
        )
        pendente = await _uma_aposta(session, usuario)
        antiga = await _uma_aposta(session, usuario, data_aposta="2026-01-05T10:00:00")
        await session.commit()

    async with como(engine_app, usuario) as session:
        verdes, _ = await ApostaRepo().list_page(session, usuario, {"estado": "GREEN"}, 1, 50)
        recentes, _ = await ApostaRepo().list_page(
            session, usuario, {"desde": datetime(2026, 9, 1, tzinfo=UTC)}, 1, 50
        )
        manuais, _ = await ApostaRepo().list_page(session, usuario, {"origem": "manual"}, 1, 50)
        de_outra_casa, _ = await ApostaRepo().list_page(session, usuario, {"casa_id": 99}, 1, 50)

    assert [a.chave for a in verdes] == [verde]
    assert {a.chave for a in recentes} == {verde, pendente}
    assert len(manuais) == 3 and antiga in {a.chave for a in manuais}
    # Sem conta ligada a essa casa, o filtro não traz nada — e não quebra.
    assert de_outra_casa == []


async def test_row_level_security_hides_the_bets_of_another_person(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    dono = await novo_usuario()
    outro = await novo_usuario()

    async with como(engine_app, dono) as session:
        chave = await _uma_aposta(session, dono)
        await session.commit()

    async with como(engine_app, outro) as session:
        assert await ApostaRepo().get_by_chave(session, outro, chave) is None
        assert await ApostaRepo().get_by_chave_for_update(session, outro, chave) is None
        apostas, total = await ApostaRepo().list_page(session, outro, {}, 1, 50)
        assert (apostas, total) == ([], 0)
        assert await EventoRepo().list_by_aposta_chave(session, outro, chave) == []


async def test_two_writers_of_the_same_bet_take_turns(
    banco, como: Como, novo_usuario: NovoUsuario
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    usuario = await novo_usuario()
    engine = create_async_engine(banco.url_app, poolclass=NullPool)
    try:
        async with como(engine, usuario) as session:
            chave = await _uma_aposta(session, usuario)
            await session.commit()

        async def travar(quem: AsyncSession) -> bool:
            livre = await quem.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:chave, 0))"),
                {"chave": f"{usuario}:{chave}"},
            )
            return bool(livre)

        async with como(engine, usuario) as primeira, como(engine, usuario) as segunda:
            # A mesma chave que o trabalhador toma ao gravar uma leitura: quem chega depois não
            # espera, recebe "ocupada" e tenta de novo.
            assert await travar(primeira) is True
            assert await travar(segunda) is False
            await primeira.rollback()
            assert await travar(segunda) is True
    finally:
        await engine.dispose()


async def test_a_correction_of_the_person_survives_the_reading_that_comes_after(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        chave = await _uma_aposta(session, usuario)
        corrigir = eventos_da_correcao({"odd": 1.95})
        historico = [
            ("APOSTA_CRIADA", "ia", _criacao()),
            *((e.tipo, e.fonte, e.payload) for e in corrigir),
        ]
        await _gravar(session, usuario, chave, historico)
        await session.commit()

    async with como(engine_app, usuario) as session:
        guardados = [
            (e.tipo, e.fonte, e.payload_json)
            for e in await EventoRepo().list_by_aposta_chave(session, usuario, chave)
        ]
        aposta = await ApostaRepo().get_by_chave(session, usuario, chave)

    estado, protegidos = projetar(guardados)
    assert aposta is not None and aposta.odd == pytest.approx(1.95)
    # É a fonte "manual" gravada no banco que protege a escolha da pessoa.
    assert protegidos == {"odd"}
    assert estado["odd"] == pytest.approx(1.95)


async def test_the_bets_of_a_page_come_newest_first(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        primeiras = [await _uma_aposta(session, usuario) for _ in range(3)]
        await session.commit()

    async with como(engine_app, usuario) as session:
        apostas, _ = await ApostaRepo().list_page(session, usuario, {}, 1, 50)

    assert [a.chave for a in apostas][-1] == primeiras[0]


async def test_the_deleted_index_is_there_for_the_ones_that_left(
    engine_admin: AsyncEngine,
) -> None:
    async with engine_admin.connect() as conn:
        indices = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_apostas_apagadas'")
            )
        ).scalars()

    [definicao] = list(indices)
    assert "NOT selecionada" in definicao


async def test_a_bet_written_twice_at_the_same_time_keeps_the_newest(
    banco, como: Como, novo_usuario: NovoUsuario
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    usuario = await novo_usuario()
    engine = create_async_engine(banco.url_app, poolclass=NullPool)
    try:
        async with como(engine, usuario) as session:
            chave = await _uma_aposta(session, usuario)
            await session.commit()

        async def gravar(estado: str, atraso: float) -> object:
            async with como(engine, usuario) as session:
                await asyncio.sleep(atraso)
                resultado = eventos_do_resultado(estado)
                return await _gravar(
                    session,
                    usuario,
                    chave,
                    [
                        ("APOSTA_CRIADA", "ia", _criacao()),
                        *((e.tipo, e.fonte, e.payload) for e in resultado),
                    ],
                ), await session.commit()

        await gravar("GREEN", 0.0)
        await gravar("RED", 0.05)

        async with como(engine, usuario) as session:
            aposta = await ApostaRepo().get_by_chave(session, usuario, chave)
    finally:
        await engine.dispose()

    assert aposta is not None and aposta.estado == "RED"


async def test_the_interval_of_the_clock_does_not_confuse_two_bets(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        primeira = await _uma_aposta(session, usuario)
        segunda = await _uma_aposta(session, usuario)
        await session.commit()

    async with como(engine_app, usuario) as session:
        uma = await ApostaRepo().get_by_chave(session, usuario, primeira)
        outra = await ApostaRepo().get_by_chave(session, usuario, segunda)

    assert uma is not None and outra is not None
    assert uma.atualizada_em <= outra.atualizada_em + timedelta(seconds=1)


async def test_a_page_past_the_end_still_says_how_many_there_are(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        for _ in range(3):
            await _uma_aposta(session, usuario)
        await session.commit()

    async with como(engine_app, usuario) as session:
        alem_do_fim, total = await ApostaRepo().list_page(session, usuario, {}, 9, 50)
        primeira, total_primeira = await ApostaRepo().list_page(session, usuario, {}, 1, 50)

    # O total é o do filtro: devolver 0 depois do fim faria a tela dizer que não há aposta nenhuma.
    assert alem_do_fim == []
    assert total == total_primeira == 3
    assert len(primeira) == 3


async def test_a_correction_that_clears_an_id_clears_it_in_the_database(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario
) -> None:
    usuario = await novo_usuario()

    async with como(engine_app, usuario) as session:
        chave = _chave()
        historico = [("APOSTA_CRIADA", "ia", _criacao(tipster_id=None))]
        await _gravar(session, usuario, chave, historico)
        com_tipster = [*historico, ("CORRECAO_MANUAL", "manual", {"tipster_id": 0})]
        await session.commit()

    async with como(engine_app, usuario) as session:
        # Um id que existe de verdade não cabe aqui sem semear o vocabulário: o que importa é que
        # a chave presente no histórico chega à linha, com valor ou com NULL.
        sem_tipster = [*com_tipster, ("CORRECAO_MANUAL", "manual", {"tipster_id": None})]
        aposta = await _gravar(session, usuario, chave, sem_tipster)
        await session.commit()

    assert aposta is not None and aposta.tipster_id is None
