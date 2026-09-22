from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.cli.refresh_painel import (
    MATERIALIZED_VIEWS,
    PAINEL_REFRESH_LOCK,
    RefreshPainelEmAndamentoError,
    refresh_painel,
)
from bancaemdia.db.seed import seed_canonical
from bancaemdia.domain.painel import FiltrosPainel
from bancaemdia.repositories.painel_repo import PainelRepo

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
AGORA = datetime(2026, 9, 21, 15, tzinfo=UTC)


@dataclass(frozen=True)
class Dimensoes:
    casa_id: int
    conta_casa_id: int
    tipster_id: int
    mercado_id: int
    banca_id: int


async def _scalar_id(engine: AsyncEngine, sql: str, parametros: dict[str, object]) -> int:
    async with engine.begin() as connection:
        valor = await connection.scalar(text(sql), parametros)
    assert isinstance(valor, int)
    return valor


async def _criar_dimensoes(
    engine: AsyncEngine,
    usuario_id: int,
    *,
    casa_id: int | None = None,
    tipster_id: int | None = None,
    mercado_id: int | None = None,
    saldo_inicial_centavos: int | None = 50_000,
) -> Dimensoes:
    sufixo = uuid4().hex
    # Reuse the canonical shared vocabulary so this test neither changes its public counts nor
    # depends on another test having seeded it first.
    async with engine.begin() as connection:
        await seed_canonical(connection)
    if casa_id is None:
        casa_id = await _scalar_id(
            engine,
            "SELECT id FROM casas ORDER BY id LIMIT 1",
            {},
        )
    if tipster_id is None:
        tipster_id = await _scalar_id(
            engine,
            "INSERT INTO tipsters (nome) VALUES (:nome) RETURNING id",
            {"nome": f"Painel Tipster {sufixo}"},
        )
    if mercado_id is None:
        mercado_id = await _scalar_id(
            engine,
            "SELECT id FROM mercados ORDER BY id LIMIT 1",
            {},
        )
    conta_casa_id = await _scalar_id(
        engine,
        """
        INSERT INTO contas_casa (usuario_id, casa_id, apelido)
        VALUES (:usuario_id, :casa_id, :apelido)
        RETURNING id
        """,
        {"usuario_id": usuario_id, "casa_id": casa_id, "apelido": sufixo},
    )
    banca_id = await _scalar_id(
        engine,
        """
        INSERT INTO bancas (usuario_id, nome, saldo_inicial_centavos, criado_em)
        VALUES (:usuario_id, :nome, :saldo, :criado_em)
        RETURNING id
        """,
        {
            "usuario_id": usuario_id,
            "nome": f"Painel Banca {sufixo}",
            "saldo": saldo_inicial_centavos,
            "criado_em": datetime(2026, 1, 1, tzinfo=UTC),
        },
    )
    return Dimensoes(casa_id, conta_casa_id, tipster_id, mercado_id, banca_id)


async def _inserir_aposta(
    engine: AsyncEngine,
    usuario_id: int,
    *,
    estado: str,
    stake_centavos: int,
    retorno_centavos: int | None,
    data_aposta: datetime = AGORA,
    valor_aposta_centavos: int | None = None,
    freebet: bool = False,
    selecionada: bool = True,
    revisao_grave: bool = False,
    dimensoes: Dimensoes | None = None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO apostas (
                    usuario_id, chave, origem, banca_id, conta_casa_id, tipster_id,
                    mercado_id, data_aposta, stake_unidades, stake_centavos,
                    valor_aposta_centavos, retorno_centavos, estado, freebet,
                    selecionada, revisao_grave
                ) VALUES (
                    :usuario_id, :chave, 'manual', :banca_id, :conta_casa_id,
                    :tipster_id, :mercado_id, :data_aposta, 1, :stake_centavos,
                    :valor_aposta_centavos, :retorno_centavos, :estado, :freebet,
                    :selecionada, :revisao_grave
                )
                """
            ),
            {
                "usuario_id": usuario_id,
                "chave": f"m:{uuid4().hex}",
                "banca_id": None if dimensoes is None else dimensoes.banca_id,
                "conta_casa_id": None if dimensoes is None else dimensoes.conta_casa_id,
                "tipster_id": None if dimensoes is None else dimensoes.tipster_id,
                "mercado_id": None if dimensoes is None else dimensoes.mercado_id,
                "data_aposta": data_aposta,
                "stake_centavos": stake_centavos,
                "valor_aposta_centavos": (
                    stake_centavos if valor_aposta_centavos is None else valor_aposta_centavos
                ),
                "retorno_centavos": retorno_centavos,
                "estado": estado,
                "freebet": freebet,
                "selecionada": selecionada,
                "revisao_grave": revisao_grave,
            },
        )


async def _inserir_movimento(
    engine: AsyncEngine,
    usuario_id: int,
    conta_casa_id: int,
    *,
    tipo: str,
    valor_centavos: int,
    ocorrido_em: datetime,
    transferencia_id: UUID | None = None,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO movimentos (
                    usuario_id, conta_casa_id, tipo, valor_centavos, ocorrido_em,
                    transferencia_id
                ) VALUES (
                    :usuario_id, :conta_casa_id, :tipo, :valor_centavos, :ocorrido_em,
                    :transferencia_id
                )
                """
            ),
            {
                "usuario_id": usuario_id,
                "conta_casa_id": conta_casa_id,
                "tipo": tipo,
                "valor_centavos": valor_centavos,
                "ocorrido_em": ocorrido_em,
                "transferencia_id": transferencia_id,
            },
        )


async def test_repo_applies_all_filters_together_and_the_wrapper_is_tenant_safe(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    exatas = await _criar_dimensoes(engine_admin, ana)
    da_bia = await _criar_dimensoes(
        engine_admin,
        bia,
        casa_id=exatas.casa_id,
        tipster_id=exatas.tipster_id,
        mercado_id=exatas.mercado_id,
    )
    outro_mercado = await _scalar_id(
        engine_admin,
        "SELECT id FROM mercados WHERE id <> :id ORDER BY id LIMIT 1",
        {"id": exatas.mercado_id},
    )

    await _inserir_aposta(
        engine_admin,
        ana,
        estado="GREEN",
        stake_centavos=10_000,
        retorno_centavos=19_000,
        dimensoes=exatas,
    )
    await _inserir_aposta(
        engine_admin,
        ana,
        estado="RED",
        stake_centavos=20_000,
        retorno_centavos=0,
        dimensoes=Dimensoes(
            exatas.casa_id,
            exatas.conta_casa_id,
            exatas.tipster_id,
            outro_mercado,
            exatas.banca_id,
        ),
    )
    await _inserir_aposta(
        engine_admin,
        ana,
        estado="RED",
        stake_centavos=30_000,
        retorno_centavos=0,
        data_aposta=datetime(2026, 1, 10, tzinfo=UTC),
        dimensoes=exatas,
    )
    await _inserir_aposta(
        engine_admin,
        bia,
        estado="GREEN",
        stake_centavos=99_000,
        retorno_centavos=198_000,
        dimensoes=da_bia,
    )
    await refresh_painel(engine_admin)

    filtros = FiltrosPainel.criar(
        "7d",
        casa_id=exatas.casa_id,
        tipster_id=exatas.tipster_id,
        mercado_id=exatas.mercado_id,
        agora=AGORA,
    )
    repo = PainelRepo()
    async with como(engine_app, ana) as session:
        painel = await repo.consultar(session, ana, filtros, respondido_em=AGORA)
        tentativa_de_vazar = await repo.consultar(session, bia, filtros, respondido_em=AGORA)
    async with como(engine_app, bia) as session:
        painel_da_bia = await repo.consultar(session, bia, filtros, respondido_em=AGORA)

    assert painel.resumo.total_apostas == 1
    assert painel.resumo.lucro_centavos == 9_000
    assert [(grupo.id, grupo.metricas.total_apostas) for grupo in painel.por_casa] == [
        (exatas.casa_id, 1)
    ]
    assert [grupo.id for grupo in painel.por_tipster] == [exatas.tipster_id]
    assert [grupo.id for grupo in painel.por_mercado] == [exatas.mercado_id]
    assert [(ponto.banca_id, ponto.contribuicao_centavos) for ponto in painel.evolucao] == [
        (exatas.banca_id, 9_000)
    ]
    assert tentativa_de_vazar.resumo.total_apostas == 0
    assert tentativa_de_vazar.por_casa == ()
    assert tentativa_de_vazar.frescor.atualizado_em is None
    assert painel_da_bia.resumo.total_apostas == 1
    assert painel_da_bia.resumo.giro_centavos == 99_000


async def test_repo_matches_every_canonical_financial_state(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    dimensoes = await _criar_dimensoes(engine_admin, usuario_id)
    casos = (
        ("PENDENTE", 1_000, None, 1_000, False, True, False),
        ("ANULADA", 2_000, 2_000, 2_000, False, True, False),
        ("MEIO_GREEN", 10_000, 15_000, 10_000, False, True, False),
        ("MEIO_RED", 8_000, 4_000, 8_000, False, True, False),
        ("CASHOUT", 5_000, 4_500, 5_000, False, True, False),
        ("GREEN", 0, 8_000, 10_000, True, True, False),
        ("GREEN", 10_000, 18_000, 10_000, False, True, False),
        ("RED", 7_000, 0, 7_000, False, True, False),
        ("GREEN", 100_000, 200_000, 100_000, False, False, False),
        ("GREEN", 100_000, 200_000, 100_000, False, True, True),
    )
    for estado, stake, retorno, face, freebet, selecionada, grave in casos:
        await _inserir_aposta(
            engine_admin,
            usuario_id,
            estado=estado,
            stake_centavos=stake,
            retorno_centavos=retorno,
            valor_aposta_centavos=face,
            freebet=freebet,
            selecionada=selecionada,
            revisao_grave=grave,
            dimensoes=dimensoes,
        )
    await refresh_painel(engine_admin)

    async with como(engine_app, usuario_id) as session:
        painel = await PainelRepo().consultar(
            session,
            usuario_id,
            FiltrosPainel.criar("all", agora=AGORA),
            respondido_em=AGORA,
        )

    resumo = painel.resumo
    assert (resumo.total_apostas, resumo.pendentes, resumo.greens, resumo.reds) == (8, 1, 2, 1)
    assert resumo.giro_centavos == 40_000
    assert resumo.base_roi_centavos == 50_000
    assert resumo.retorno_centavos == 49_500
    assert resumo.lucro_centavos == 9_500
    assert resumo.freebets == 1
    assert resumo.roi == Decimal("0.19")
    assert resumo.win_rate == Decimal(2) / Decimal(3)


async def test_balance_combines_cash_movements_and_marks_an_unseeded_account_unknown(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    primeira = await _criar_dimensoes(engine_admin, usuario_id)
    async with engine_admin.connect() as connection:
        outras_casas = (
            (
                await connection.execute(
                    text("SELECT id FROM casas WHERE id <> :id ORDER BY id LIMIT 2"),
                    {"id": primeira.casa_id},
                )
            )
            .scalars()
            .all()
        )
    assert len(outras_casas) == 2
    segunda = await _criar_dimensoes(engine_admin, usuario_id, casa_id=outras_casas[0])
    await _criar_dimensoes(
        engine_admin,
        usuario_id,
        casa_id=outras_casas[1],
    )  # Sem movimento: saldo desconhecido.
    inicio = datetime(2026, 9, 1, 12, tzinfo=UTC)
    for conta_id, tipo, valor, deslocamento in (
        (primeira.conta_casa_id, "DEPOSITO", 100_000, 0),
        (primeira.conta_casa_id, "SAQUE", -20_000, 1),
        (primeira.conta_casa_id, "BONUS", 5_000, 2),
        (segunda.conta_casa_id, "DEPOSITO", 40_000, 3),
    ):
        await _inserir_movimento(
            engine_admin,
            usuario_id,
            conta_id,
            tipo=tipo,
            valor_centavos=valor,
            ocorrido_em=inicio + timedelta(minutes=deslocamento),
        )
    transferencia_id = uuid4()
    await _inserir_movimento(
        engine_admin,
        usuario_id,
        primeira.conta_casa_id,
        tipo="TRANSFERENCIA",
        valor_centavos=-10_000,
        ocorrido_em=inicio + timedelta(minutes=4),
        transferencia_id=transferencia_id,
    )
    await _inserir_movimento(
        engine_admin,
        usuario_id,
        segunda.conta_casa_id,
        tipo="TRANSFERENCIA",
        valor_centavos=10_000,
        ocorrido_em=inicio + timedelta(minutes=4),
        transferencia_id=transferencia_id,
    )
    await _inserir_aposta(
        engine_admin,
        usuario_id,
        estado="GREEN",
        stake_centavos=10_000,
        retorno_centavos=18_000,
        data_aposta=datetime(2026, 9, 20, tzinfo=UTC),
        dimensoes=primeira,
    )
    await _inserir_aposta(
        engine_admin,
        usuario_id,
        estado="RED",
        stake_centavos=5_000,
        retorno_centavos=0,
        data_aposta=datetime(2026, 9, 20, tzinfo=UTC),
        dimensoes=segunda,
    )
    await refresh_painel(engine_admin)

    # O filtro reduz as apostas, mas o caixa publicado permanece all-time e inclui todas as contas.
    filtros = FiltrosPainel.criar("7d", casa_id=primeira.casa_id, agora=AGORA)
    async with como(engine_app, usuario_id) as session:
        painel = await PainelRepo().consultar(session, usuario_id, filtros, respondido_em=AGORA)

    assert painel.resumo.total_apostas == 1
    assert painel.saldo.saldo_total_centavos is None
    assert painel.saldo.saldo_conhecido_centavos == 128_000
    assert painel.saldo.contas_saldo_desconhecido == 1
    assert painel.saldo.escopo.value == "contas_casa_all_time"


async def test_refresh_timestamp_is_stable_until_a_complete_new_refresh(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    dimensoes = await _criar_dimensoes(engine_admin, usuario_id)
    await _inserir_aposta(
        engine_admin,
        usuario_id,
        estado="GREEN",
        stake_centavos=10_000,
        retorno_centavos=18_000,
        dimensoes=dimensoes,
    )
    primeiro_refresh = await refresh_painel(engine_admin)
    resposta = primeiro_refresh + timedelta(seconds=7)
    filtros = FiltrosPainel.criar("all", agora=AGORA)

    async with como(engine_app, usuario_id) as session:
        primeiro = await PainelRepo().consultar(
            session, usuario_id, filtros, respondido_em=resposta
        )
    await _inserir_aposta(
        engine_admin,
        usuario_id,
        estado="RED",
        stake_centavos=3_000,
        retorno_centavos=0,
        dimensoes=dimensoes,
    )
    async with como(engine_app, usuario_id) as session:
        ainda_antigo = await PainelRepo().consultar(
            session, usuario_id, filtros, respondido_em=resposta
        )

    assert primeiro.resumo.total_apostas == ainda_antigo.resumo.total_apostas == 1
    assert primeiro.frescor.atualizado_em == primeiro_refresh
    assert ainda_antigo.frescor.atualizado_em == primeiro_refresh
    assert primeiro.frescor.idade_mv_segundos == Decimal(7)
    assert primeiro.frescor.replica_atraso_segundos is None

    await asyncio.sleep(0.002)
    segundo_refresh = await refresh_painel(engine_admin)
    async with como(engine_app, usuario_id) as session:
        atualizado = await PainelRepo().consultar(
            session,
            usuario_id,
            filtros,
            respondido_em=segundo_refresh,
        )

    assert atualizado.resumo.total_apostas == 2
    assert atualizado.frescor.atualizado_em == segundo_refresh
    assert segundo_refresh > primeiro_refresh


async def test_every_materialized_view_has_a_full_valid_unique_index(
    engine_admin: AsyncEngine,
) -> None:
    async with engine_admin.connect() as connection:
        linhas = (
            await connection.execute(
                text(
                    """
                    SELECT mv.relname,
                           BOOL_OR(
                               i.indisunique
                               AND i.indisvalid
                               AND i.indpred IS NULL
                               AND i.indexprs IS NULL
                           ) AS indice_compativel
                    FROM pg_class AS mv
                    JOIN pg_namespace AS n ON n.oid = mv.relnamespace
                    LEFT JOIN pg_index AS i ON i.indrelid = mv.oid
                    WHERE n.nspname = 'painel'
                      AND mv.relkind = 'm'
                    GROUP BY mv.relname
                    """
                )
            )
        ).all()

    assert dict(linhas) == dict.fromkeys(MATERIALIZED_VIEWS, True)


async def _esperar_refresh_bloqueado(engine: AsyncEngine) -> bool:
    for _ in range(200):
        async with engine.connect() as connection:
            bloqueado = await connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND query LIKE
                              'REFRESH MATERIALIZED VIEW CONCURRENTLY painel.%'
                          AND wait_event_type = 'Lock'
                    )
                    """
                )
            )
        if bloqueado:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_refresh_lock_rejects_a_second_runner(
    engine_admin: AsyncEngine,
) -> None:
    async with engine_admin.connect() as detentor:
        await detentor.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": PAINEL_REFRESH_LOCK},
        )
        await detentor.commit()
        try:
            with pytest.raises(RefreshPainelEmAndamentoError):
                await refresh_painel(engine_admin)
        finally:
            await detentor.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": PAINEL_REFRESH_LOCK},
            )
            await detentor.commit()


async def test_public_view_remains_readable_while_concurrent_refresh_waits_on_source(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    await refresh_painel(engine_admin)

    async with engine_admin.connect() as bloqueador:
        await bloqueador.execute(text("LOCK TABLE public.apostas IN ACCESS EXCLUSIVE MODE"))
        tarefa = asyncio.create_task(refresh_painel(engine_admin))
        bloqueado = False
        total: int | None = None
        try:
            bloqueado = await _esperar_refresh_bloqueado(engine_admin)
            async with como(engine_app, usuario_id) as session:
                await session.execute(text("SET LOCAL statement_timeout = '1s'"))
                total = await session.scalar(
                    text(
                        "SELECT total_apostas FROM public.painel_resumo "
                        "WHERE usuario_id = :usuario_id"
                    ),
                    {"usuario_id": usuario_id},
                )
        finally:
            await bloqueador.rollback()
            await asyncio.wait_for(tarefa, timeout=30)

    assert bloqueado is True
    assert total == 0
