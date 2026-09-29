from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.cli.refresh_painel import refresh_painel
from bancaemdia.db.seed import seed_canonical
from bancaemdia.domain.painel import FiltrosPainel
from bancaemdia.repositories.analytics_repo import AnalyticsRepo
from bancaemdia.repositories.painel_repo import PainelRepo

pytestmark = pytest.mark.xdist_group("postgres")
Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]


async def _aposta(
    engine: AsyncEngine,
    usuario_id: int,
    *,
    estado: str,
    stake: int,
    retorno: int | None,
    face: int | None = None,
    odd: float | None = None,
    grave: bool = False,
    banca_id: int | None = None,
    conta_casa_id: int | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO apostas (
                    usuario_id, chave, origem, banca_id, conta_casa_id, data_aposta, stake_unidades,
                    stake_centavos, valor_aposta_centavos, retorno_centavos,
                    estado, odd, freebet, selecionada, revisao_grave
                ) VALUES (
                    :usuario_id, :chave, 'manual', :banca_id, :conta_casa_id, :data_aposta, 1,
                    :stake, :face, :retorno, :estado, :odd, :freebet, true, :grave
                )
                """
            ),
            {
                "usuario_id": usuario_id,
                "chave": f"a:{uuid4().hex}",
                "data_aposta": datetime(2026, 9, 28, 2, 30, tzinfo=UTC),
                "stake": stake,
                "face": stake if face is None else face,
                "retorno": retorno,
                "estado": estado,
                "odd": odd,
                "freebet": stake == 0,
                "grave": grave,
                "banca_id": banca_id,
                "conta_casa_id": conta_casa_id,
            },
        )


async def test_live_analytics_reconcile_and_isolate_tenants(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    await _aposta(engine_admin, ana, estado="GREEN", stake=1_000, retorno=2_000, odd=2.0)
    await _aposta(engine_admin, ana, estado="GREEN", stake=0, face=1_500, retorno=1_000)
    await _aposta(engine_admin, ana, estado="PENDENTE", stake=500, retorno=None)
    await _aposta(engine_admin, ana, estado="ANULADA", stake=500, retorno=500)
    await _aposta(engine_admin, ana, estado="CASHOUT", stake=1_000, retorno=700)
    await _aposta(engine_admin, ana, estado="RED", stake=3_000, retorno=0, grave=True)
    await _aposta(engine_admin, bia, estado="RED", stake=100_000, retorno=0)
    filtros = FiltrosPainel.criar("7d", agora=datetime(2026, 9, 28, 12, tzinfo=UTC))

    async with como(engine_app, ana) as session:
        analises = await AnalyticsRepo().consultar(session, ana, filtros, "America/Sao_Paulo")
        outra = await AnalyticsRepo().consultar(session, bia, filtros, "America/Sao_Paulo")
    assert analises["total_filtrado"]["total_apostas"] == 5
    assert analises["total_filtrado"]["lucro_centavos"] == 1_700
    assert analises["odds_desconhecidas"] == 2
    assert analises["odds_nao_aplicaveis"] == 2
    assert outra["total_filtrado"]["total_apostas"] == 0
    for eixo in ("faixas_odds", "heatmap", "por_esporte", "quartis_stake", "por_banca_progressao"):
        assert sum(bucket["total_apostas"] for bucket in analises[eixo]) == 5
        assert sum(bucket["lucro_centavos"] for bucket in analises[eixo]) == 1_700


async def test_goals_table_enforces_tenant_policy(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    async with como(engine_app, ana) as session:
        meta_id = await session.scalar(
            text(
                """
                INSERT INTO metas_desempenho
                    (usuario_id, titulo, metrica, inicio, fim, alvo, linha_base)
                VALUES (:usuario_id, 'Teste', 'lucro_centavos', '2026-09-01', '2026-09-30', 1000, 0)
                RETURNING id
                """
            ),
            {"usuario_id": ana},
        )
        await session.commit()
    async with como(engine_app, bia) as session:
        visivel = await session.scalar(
            text("SELECT count(*) FROM metas_desempenho WHERE id = :id"), {"id": meta_id}
        )
    assert visivel == 0


async def test_existing_evolution_separates_bets_deposits_and_withdrawals(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    async with engine_admin.begin() as conn:
        await seed_canonical(conn)
        casa_id = await conn.scalar(text("SELECT id FROM casas ORDER BY id LIMIT 1"))
        banca_id = await conn.scalar(
            text(
                "INSERT INTO bancas (usuario_id, nome, saldo_inicial_centavos, criado_em) "
                "VALUES (:usuario_id, :nome, 10000, '2026-01-01 00:00:00+00') RETURNING id"
            ),
            {"usuario_id": usuario_id, "nome": f"Analytics {uuid4().hex}"},
        )
        conta_id = await conn.scalar(
            text(
                "INSERT INTO contas_casa (usuario_id, casa_id, banca_id, apelido) "
                "VALUES (:usuario_id, :casa_id, :banca_id, :apelido) RETURNING id"
            ),
            {
                "usuario_id": usuario_id,
                "casa_id": casa_id,
                "banca_id": banca_id,
                "apelido": uuid4().hex,
            },
        )
        for tipo, valor in (("DEPOSITO", 2_000), ("SAQUE", -300)):
            await conn.execute(
                text(
                    "INSERT INTO movimentos (usuario_id, conta_casa_id, tipo, valor_centavos, ocorrido_em) "
                    "VALUES (:usuario_id, :conta_id, :tipo, :valor, :quando)"
                ),
                {
                    "usuario_id": usuario_id,
                    "conta_id": conta_id,
                    "tipo": tipo,
                    "valor": valor,
                    "quando": datetime(2026, 9, 28, 10, tzinfo=UTC),
                },
            )
    await _aposta(
        engine_admin,
        usuario_id,
        estado="GREEN",
        stake=1_000,
        retorno=1_500,
        banca_id=banca_id,
        conta_casa_id=conta_id,
    )
    await refresh_painel(engine_admin)
    filtros = FiltrosPainel.criar("7d", agora=datetime(2026, 9, 28, 12, tzinfo=UTC))
    async with como(engine_app, usuario_id) as session:
        painel = await PainelRepo().consultar(session, usuario_id, filtros)
    ponto = next(p for p in painel.evolucao if p.banca_id == banca_id)
    assert ponto.contribuicao_centavos == ponto.acumulado_centavos == 500
    assert ponto.depositos_acumulados_centavos == 2_000
    assert ponto.saques_acumulados_centavos == 300
    assert ponto.saldo_centavos == 12_200
