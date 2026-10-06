from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1.apostas import ApostaManual, criar_aposta
from bancaemdia.domain.financeiro import Aposta, Estado, resolver_retorno
from bancaemdia.domain.temporal import Movimento, TipoMovimento, saldo
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = [pytest.mark.edge_cases, pytest.mark.xdist_group("postgres")]
Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
BRASIL = ZoneInfo("America/Sao_Paulo")


def _quando(mes: int, dia: int) -> datetime:
    return datetime(2026, mes, dia, 12, tzinfo=BRASIL)


async def _criar_manual(
    engine: AsyncEngine, como: Como, usuario_id: int, mes: int
) -> tuple[str, int]:
    async with como(engine, usuario_id) as session:
        usuario = await UsuarioRepo().get_by_id(session, usuario_id)
        assert usuario is not None
        resposta = await criar_aposta(
            ApostaManual(
                casa="Betano",
                data_aposta=_quando(mes, 10).isoformat(),
                stake_unidades=1.0,
                odd=2.0,
            ),
            usuario,
            session,
        )
    assert resposta.status_code == 201
    aposta = json.loads(resposta.body)["aposta"]
    return str(aposta["chave"]), int(aposta["stake_centavos"])


async def test_unit_history_is_used_by_manual_bets_and_march_change_is_not_retroactive(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as conn:
        await conn.execute(
            insert(models.Casa)
            .values(nome="Betano", dominio="betano.bet.br")
            .on_conflict_do_nothing()
        )
    async with como(engine_app, usuario) as session:
        await UnidadeRepo().create(
            session,
            {
                "usuario_id": usuario,
                "valor_centavos": 10_000,
                "vigente_de": _quando(1, 1),
                "vigente_ate": _quando(2, 1),
            },
        )
        await UnidadeRepo().create(
            session,
            {
                "usuario_id": usuario,
                "valor_centavos": 20_000,
                "vigente_de": _quando(2, 1),
                "vigente_ate": _quando(3, 1),
            },
        )
        await session.commit()

    jan_chave, jan_stake = await _criar_manual(engine_app, como, usuario, 1)
    fev_chave, fev_stake = await _criar_manual(engine_app, como, usuario, 2)
    async with como(engine_app, usuario) as session:
        await UnidadeRepo().create(
            session,
            {
                "usuario_id": usuario,
                "valor_centavos": 30_000,
                "vigente_de": _quando(3, 1),
            },
        )
        await session.commit()
    mar_chave, mar_stake = await _criar_manual(engine_app, como, usuario, 3)

    async with como(engine_app, usuario) as session:
        apostas = [
            await ApostaRepo().get_by_chave(session, usuario, chave)
            for chave in (jan_chave, fev_chave, mar_chave)
        ]
        unidades = [
            await UnidadeRepo().get_vigente(session, usuario, _quando(mes, 10)) for mes in (1, 2, 3)
        ]
    assert (jan_stake, fev_stake, mar_stake) == (10_000, 20_000, 30_000)
    assert [aposta.stake_centavos for aposta in apostas if aposta is not None] == [
        10_000,
        20_000,
        30_000,
    ]
    assert [unidade.valor_centavos for unidade in unidades if unidade is not None] == [
        10_000,
        20_000,
        30_000,
    ]


async def test_house_account_starts_on_january_fifteenth_and_balance_starts_at_first_movement(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as conn:
        casa_id = await conn.scalar(
            insert(models.Casa).values(nome=f"Temporal {uuid4().hex}").returning(models.Casa.id)
        )
        nome = await conn.scalar(select(models.Casa.nome).where(models.Casa.id == casa_id))
    assert casa_id is not None and nome is not None
    async with como(engine_app, usuario) as session:
        conta = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": usuario,
                "casa_id": casa_id,
                "apelido": "principal",
                "desde": _quando(1, 15),
            },
        )
        await MovimentoRepo().append(
            session,
            {
                "usuario_id": usuario,
                "conta_casa_id": conta.id,
                "tipo": "DEPOSITO",
                "valor_centavos": 30_000,
                "ocorrido_em": _quando(1, 15),
            },
        )
        for dia in (10, 20):
            await ApostaRepo().upsert_idempotent(
                session,
                {
                    "usuario_id": usuario,
                    "chave": f"m:{uuid4().hex}",
                    "origem": "manual",
                    "conta_casa_id": conta.id,
                    "stake_unidades": 1.0,
                    "stake_centavos": 10_000,
                    "valor_aposta_centavos": 10_000,
                    "estado": "RED",
                    "retorno_centavos": 0,
                    "data_aposta": _quando(1, dia),
                },
            )
        await session.commit()

    async with como(engine_app, usuario) as session:
        antes = await ContaCasaRepo().get_vigente_by_nome_da_casa(
            session, usuario, nome, _quando(1, 10)
        )
        depois = await ContaCasaRepo().get_vigente_by_nome_da_casa(
            session, usuario, nome, _quando(1, 20)
        )
        agregado = (await MovimentoRepo().aggregate_saldos_by_usuario(session, usuario))[conta.id]
    assert antes is None and depois is not None and depois.id == conta.id
    assert agregado.desde == date(2026, 1, 15)
    assert agregado.apostas_antes_do_caixa == 1
    assert agregado.apostado_no_periodo_centavos == 10_000
    assert agregado.saldo_centavos == 20_000
    assert agregado.lucro_centavos == -20_000

    # The domain and the SQL aggregate must agree about the historical bet: it affects profit,
    # while the balance starts at the first cash movement.
    apostas = [
        resolver_retorno(
            Aposta(
                1.0,
                10_000,
                odd=2.0,
                estado=Estado.RED,
                conta_casa_id=conta.id,
                data_aposta=date(2026, 1, dia),
            )
        )
        for dia in (10, 20)
    ]
    movimentos = [Movimento(TipoMovimento.DEPOSITO, 30_000, conta.id, date(2026, 1, 15))]
    dominio = saldo(conta.id, apostas, movimentos)
    assert dominio.saldo_centavos == agregado.saldo_centavos
    assert dominio.lucro_centavos == agregado.lucro_centavos
