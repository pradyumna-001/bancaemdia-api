import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bancaemdia.api.deps import get_current_user_snapshot
from bancaemdia.api.v1 import painel
from bancaemdia.db.session import get_db_snapshot
from bancaemdia.domain.painel import FrescorPainel
from bancaemdia.domain.registros import Usuario


async def _sessao() -> AsyncIterator[object]:
    await asyncio.sleep(0)
    yield object()


def _cliente(monkeypatch, usuario_id: int) -> TestClient:
    class RepoAnalises:
        async def consultar(self, _session, id_, filtros, fuso_horario):
            assert id_ == usuario_id
            assert filtros.casa_id == 3
            vazio = {
                "total_apostas": 0,
                "pendentes": 0,
                "greens": 0,
                "reds": 0,
                "giro_centavos": 0,
                "base_roi_centavos": 0,
                "lucro_centavos": 0,
                "roi": "0.000000",
                "hit_rate": None,
                "resultado_nao_aplicavel": 0,
            }
            bucket = {"chave": "unknown", **vazio}
            return {
                "total_filtrado": {**vazio, "lucro_centavos": usuario_id},
                "faixas_odds": [bucket],
                "heatmap": [bucket],
                "por_esporte": [bucket],
                "quartis_stake": [bucket],
                "por_banca_progressao": [bucket],
                "odd_media": None,
                "odds_desconhecidas": 0,
                "odds_nao_aplicaveis": 0,
                "profit_factor": None,
                "fuso_horario": fuso_horario,
            }

    class RepoPainel:
        async def frescor(self, _session, id_):
            assert id_ == usuario_id
            agora = datetime(2026, 9, 29, tzinfo=UTC)
            return FrescorPainel(agora, Decimal(0), agora, None)

    monkeypatch.setattr(painel, "AnalyticsRepo", RepoAnalises)
    monkeypatch.setattr(painel, "PainelRepo", RepoPainel)
    app = FastAPI()
    app.include_router(painel.router)
    app.dependency_overrides[get_current_user_snapshot] = lambda: Usuario(
        usuario_id,
        f"u{usuario_id}@teste.local",
        "Teste",
        datetime(2026, 9, 29, tzinfo=UTC),
        True,
        "America/Sao_Paulo",
    )
    app.dependency_overrides[get_db_snapshot] = _sessao
    return TestClient(app)


def test_analytics_response_is_private_and_tenant_scoped(monkeypatch) -> None:
    ana = _cliente(monkeypatch, 10).get("/api/v1/painel/analises?casa_id=3")
    bia = _cliente(monkeypatch, 20).get("/api/v1/painel/analises?casa_id=3")
    assert ana.status_code == bia.status_code == 200
    assert ana.headers["Cache-Control"] == bia.headers["Cache-Control"] == "private, no-store"
    assert ana.headers["Vary"] == bia.headers["Vary"] == "Authorization, Cookie"
    assert ana.json()["total_filtrado"]["lucro_centavos"] == 10
    assert bia.json()["total_filtrado"]["lucro_centavos"] == 20
