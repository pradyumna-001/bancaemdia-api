from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.domain.financeiro import Aposta, Estado, resolver_retorno, resumir
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo

pytestmark = pytest.mark.edge_cases
Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]


@pytest.mark.parametrize(
    ("estado", "freebet", "retorno_informado", "retorno", "lucro"),
    [
        (Estado.GREEN, True, None, 10_000, 10_000),
        (Estado.RED, True, None, 0, 0),
        (Estado.MEIO_GREEN, False, None, 15_000, 5_000),
        (Estado.MEIO_RED, False, None, 5_000, -5_000),
        (Estado.CASHOUT, False, 7_350, 7_350, -2_650),
        (Estado.ANULADA, False, None, 10_000, 0),
    ],
)
def test_settlement_is_exact_to_the_cent(
    estado: Estado,
    freebet: bool,
    retorno_informado: int | None,
    retorno: int,
    lucro: int,
) -> None:
    aposta = resolver_retorno(
        Aposta(
            stake_unidades=1.0,
            valor_unidade_centavos=10_000,
            odd=2.0,
            estado=estado,
            freebet=freebet,
            retorno_centavos=retorno_informado,
            retorno_informado=retorno_informado is not None,
        )
    )
    assert aposta.stake_centavos == (0 if freebet else 10_000)
    assert aposta.valor_aposta_centavos == 10_000
    assert (aposta.retorno_centavos, aposta.lucro_centavos) == (retorno, lucro)


def test_roi_without_bonus_excludes_only_freebet_face_from_denominator() -> None:
    freebet = resolver_retorno(Aposta(1.0, 10_000, odd=2.0, estado=Estado.GREEN, freebet=True))
    perda = resolver_retorno(Aposta(1.0, 10_000, odd=2.0, estado=Estado.RED))
    resumo = resumir([freebet, freebet, perda])

    assert resumo.lucro_centavos == 10_000
    assert resumo.base_roi_centavos == 30_000
    assert resumo.giro_proprio_centavos == 10_000
    assert resumo.roi == pytest.approx(1 / 3)
    assert resumo.roi_sem_bonus == pytest.approx(1.0)


@pytest.mark.xdist_group("postgres")
async def test_postgres_negative_balance_is_unknown_with_exact_missing_deposit(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario = await novo_usuario()
    outro = await novo_usuario()
    async with engine_admin.begin() as conn:
        casa_id = await conn.scalar(
            insert(models.Casa).values(nome=f"Financeiro {uuid4().hex}").returning(models.Casa.id)
        )
    assert casa_id is not None
    async with como(engine_app, usuario) as session:
        conta = await ContaCasaRepo().create(
            session,
            {"usuario_id": usuario, "casa_id": casa_id, "apelido": "principal"},
        )
        await MovimentoRepo().append(
            session,
            {
                "usuario_id": usuario,
                "conta_casa_id": conta.id,
                "tipo": "DEPOSITO",
                "valor_centavos": 5_000,
                "ocorrido_em": datetime(2026, 1, 15, 15, tzinfo=UTC),
            },
        )
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
                "data_aposta": datetime(2026, 1, 20, 15, tzinfo=UTC),
            },
        )
        await session.commit()

    async with como(engine_app, usuario) as session:
        saldo = (await MovimentoRepo().aggregate_saldos_by_usuario(session, usuario))[conta.id]
        aposta = await session.scalar(
            select(models.Aposta).where(models.Aposta.usuario_id == usuario)
        )
    async with como(engine_app, outro) as session:
        alheio = await MovimentoRepo().aggregate_saldos_by_usuario(session, outro)
    assert aposta is not None and aposta.stake_centavos == 10_000
    assert saldo.saldo_centavos is None
    assert saldo.deposito_faltante_centavos == 5_000
    assert saldo.lucro_centavos == -10_000
    assert conta.id not in alheio


@pytest.mark.xdist_group("postgres")
async def test_opposite_account_pairs_acquire_row_locks_in_stable_order(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as conn:
        casas = [
            await conn.scalar(
                insert(models.Casa).values(nome=f"Lock {uuid4().hex}").returning(models.Casa.id)
            )
            for _ in range(2)
        ]
    assert all(casa_id is not None for casa_id in casas)
    async with como(engine_app, usuario) as session:
        contas = [
            await ContaCasaRepo().create(
                session,
                {"usuario_id": usuario, "casa_id": casa_id, "apelido": "principal"},
            )
            for casa_id in casas
        ]
        await session.commit()
    menor, maior = sorted(conta.id for conta in contas)
    primeira_travada = asyncio.Event()

    async def travar(ids: list[int], *, segurar: bool) -> list[int]:
        if not segurar:
            await primeira_travada.wait()
        async with como(engine_app, usuario) as session:
            linhas = await ContaCasaRepo().get_many_for_update(session, usuario, ids)
            if segurar:
                primeira_travada.set()
                await asyncio.sleep(0.05)
            await session.commit()
        return [linha.id for linha in linhas]

    resultados = await asyncio.wait_for(
        asyncio.gather(
            travar([maior, menor], segurar=True),
            travar([menor, maior], segurar=False),
        ),
        timeout=5,
    )
    assert resultados == [[menor, maior], [menor, maior]]
