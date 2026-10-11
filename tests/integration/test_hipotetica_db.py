"""Mandatory hypothetical acceptance: real HTTP, PostgreSQL 16, forced RLS.

Fixtures use declared cents/results independent of the implementation, retain
event history, and clean up shared catalog references through the site fixture.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from jsonschema_rs import Draft202012Validator
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.main import app
from bancaemdia.repositories.assinatura_repo import AssinaturaRepo
from tests.integration.test_site_filters_db import api as api
from tests.integration.test_site_filters_db import dataset as dataset

pytestmark = pytest.mark.xdist_group("postgres")
PATH = "/api/v1/painel/hipotetica"


@pytest.fixture
async def population(dataset: Any, engine_app: Any, como: Any) -> AsyncIterator[Any]:
    async with como(engine_app, dataset.user) as session:
        await session.execute(
            delete(models.ApostaGrupo).where(models.ApostaGrupo.usuario_id == dataset.user)
        )
        await session.execute(delete(models.Aposta).where(models.Aposta.usuario_id == dataset.user))
        await session.commit()
    yield dataset


async def create_pair(
    d: Any,
    engine: Any,
    como: Any,
    *,
    estado: str = "GREEN",
    stake: int = 16000,
    retorno: int | None = 32000,
    indicada: int = 2000,
    odd: float | None = 2,
    tip_odd: float = 9,
    tip_estado: str = "RED",
    linked: str | None = "active",
    freebet: bool = False,
    review: bool = False,
    selected: bool = True,
    tip_review: bool = False,
    duvida: bool = False,
    historico: bool = True,
    commission: int = 0,
    fixed: bool = False,
    bonus: bool = False,
    date: datetime | None = datetime(2026, 9, 10, 3, tzinfo=UTC),
) -> tuple[int, int, str]:
    async with como(engine, d.user) as session:
        house = models.Aposta(
            usuario_id=d.user,
            chave="h:" + uuid4().hex,
            origem="casa",
            estado=estado,
            stake_centavos=0 if freebet else stake,
            valor_aposta_centavos=stake,
            stake_unidades=1,
            retorno_centavos=retorno,
            odd=odd,
            freebet=freebet,
            revisao_grave=review,
            selecionada=selected,
            duvida_de_par=duvida,
            data_aposta=date,
            data_jogo=datetime(2026, 11, 1, tzinfo=UTC),
            conta_casa_id=d.contas[0],
            banca_id=d.bancas[0],
            tipster_id=d.tipsters[0],
            mercado_id=d.mercados[0],
            competicao_id=d.competicoes[0],
        )
        tip = models.Aposta(
            usuario_id=d.user,
            chave="t:" + uuid4().hex,
            origem="telegram",
            estado=tip_estado,
            chat_id=123,
            message_id=int(uuid4().int % 10**15),
            odd=tip_odd,
            stake_centavos=indicada,
            valor_aposta_centavos=indicada,
            stake_unidades=1,
            retorno_centavos=0,
            tipster_id=d.tipsters[0],
            revisao_grave=tip_review,
            selecionada=False,
            data_aposta=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add_all([house, tip])
        await session.flush()
        if linked:
            session.add(
                models.ApostaConsolidacao(
                    usuario_id=d.user,
                    casa_aposta_id=house.id,
                    telegram_aposta_id=tip.id,
                    conta_casa_id=d.contas[0],
                    estado=linked,
                    decisao="reviewed",
                    versao="hypothetical-test-v1",
                    evidencia={},
                    contexto={},
                    ator="test",
                    desvinculada_em=None if linked == "active" else datetime.now(UTC),
                )
            )
        session.add(models.ApostaGrupo(usuario_id=d.user, aposta_id=house.id, grupo_id=d.grupos[0]))
        if historico:
            payload: dict[str, object] = {
                "origem": "casa",
                "comissao_centavos": commission,
                "stake_centavos": stake,
            }
            if bonus:
                payload["bonus_centavos"] = 100
            session.add(
                models.Evento(
                    usuario_id=d.user,
                    aposta_chave=house.chave,
                    tipo="APOSTA_CRIADA",
                    fonte="casa",
                    payload_json=payload,
                )
            )
        if fixed:
            session.add(
                models.Evento(
                    usuario_id=d.user,
                    aposta_chave=house.chave,
                    tipo="CORRECAO_MANUAL",
                    fonte="manual",
                    payload_json={"retorno_centavos": retorno},
                )
            )
        await session.commit()
        assert house.chave is not None
        return house.id, tip.id, house.chave


async def read(api: Any, d: Any, **params: object) -> dict[str, Any]:
    client, headers = api
    response = await client.get(PATH, params=params, headers=headers(d.user))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "Authorization" in response.headers["vary"]
    return cast(dict[str, Any], response.json())


async def test_historical_example_keeps_real_odd_result_and_does_not_count_tip(
    api: Any, population: Any, engine_app: Any, como: Any
) -> None:
    await create_pair(population, engine_app, como)
    result = await read(api, population, origem="casa")
    assert (
        result["apostas_consideradas"]
        == result["apostas_alteradas"]
        == result["vinculos_confirmados"]
        == 1
    )
    assert result["estado"] == "comparado"
    assert [result["real"][k] for k in ("giro_centavos", "retorno_centavos", "lucro_centavos")] == [
        "16000",
        "32000",
        "16000",
    ]
    assert [
        result["hipotetico"][k] for k in ("giro_centavos", "retorno_centavos", "lucro_centavos")
    ] == ["2000", "4000", "2000"]
    assert result["diferencas"]["lucro_centavos"] == "-14000"
    assert result["hipotetico"]["roi"] == "1.000000"
    assert result["filtros"]["origem"] == "casa"


@pytest.mark.parametrize(
    "estado,retorno,expected_giro,expected_return,expected_profit",
    [
        ("GREEN", 200, "20", "40", "20"),
        ("RED", 0, "20", "0", "-20"),
        ("MEIO_GREEN", 150, "20", "30", "10"),
        ("MEIO_RED", 50, "20", "10", "-10"),
        ("ANULADA", 100, "0", "0", "0"),
        ("PENDENTE", None, "0", None, None),
        ("CASHOUT", 125, None, None, None),
    ],
)
async def test_all_states_have_independent_financial_expectations(
    api: Any,
    population: Any,
    engine_app: Any,
    como: Any,
    estado: str,
    retorno: int | None,
    expected_giro: str | None,
    expected_return: str | None,
    expected_profit: str | None,
) -> None:
    await create_pair(
        population, engine_app, como, estado=estado, stake=100, indicada=20, retorno=retorno
    )
    result = await read(api, population)
    assert result["hipotetico"]["giro_centavos"] == expected_giro
    assert result["hipotetico"]["retorno_centavos"] == expected_return
    assert result["hipotetico"]["lucro_centavos"] == expected_profit
    if estado == "PENDENTE":
        assert result["hipotetico"]["exposicao_pendente_centavos"] == "20"
        assert result["real"]["exposicao_pendente_centavos"] == "100"
        assert result["limitacoes"] == [{"motivo": "pendente", "quantidade": 1}]
    if estado == "CASHOUT":
        assert result["estado"] == "indisponivel"
        assert result["real"]["lucro_centavos"] == "25"


@pytest.mark.parametrize(
    "estado,retorno,expected", [("GREEN", 2, "2"), ("MEIO_GREEN", 2, "2"), ("MEIO_RED", 0, "2")]
)
async def test_rounding_half_even_at_each_step(
    api: Any, population: Any, engine_app: Any, como: Any, estado: str, retorno: int, expected: str
) -> None:
    await create_pair(
        population, engine_app, como, estado=estado, stake=1, indicada=3, odd=2.5, retorno=retorno
    )
    result = await read(api, population)
    # GREEN: 3*2.5=7.5 -> 8. MEIO_GREEN: (8+3)/2=5.5 -> 6.
    # MEIO_RED: 3/2=1.5 -> 2. Original 1/2=0.5 -> 0.
    returns = {"GREEN": "8", "MEIO_GREEN": "6", "MEIO_RED": expected}
    assert result["hipotetico"]["retorno_centavos"] == returns[estado]


@pytest.mark.parametrize(
    "linked,indicada,duvida,state",
    [
        (None, 2000, False, "sem_vinculos_elegiveis"),
        ("unlinked", 2000, False, "sem_vinculos_elegiveis"),
        ("rejected", 2000, False, "sem_vinculos_elegiveis"),
        ("active", 16000, False, "sem_stakes_diferentes"),
        (None, 2000, True, "sem_vinculos_elegiveis"),
    ],
)
async def test_unlinked_rejected_ambiguous_equal_stakes_remain_real(
    api: Any,
    population: Any,
    engine_app: Any,
    como: Any,
    linked: str | None,
    indicada: int,
    duvida: bool,
    state: str,
) -> None:
    await create_pair(population, engine_app, como, linked=linked, indicada=indicada, duvida=duvida)
    result = await read(api, population)
    assert result["apostas_alteradas"] == 0
    assert result["real"] == result["hipotetico"]
    assert result["estado"] == state


@pytest.mark.parametrize(
    "options,motivo",
    [
        ({"stake": 0, "retorno": 0}, "stake_real_zero"),
        ({"indicada": 0}, "stake_indicada_invalida"),
        ({"odd": None}, "odd_real_invalida"),
        ({"retorno": None}, "retorno_real_desconhecido"),
        ({"retorno": 32001}, "retorno_fora_da_regra_padrao"),
        ({"commission": 100}, "taxa_bonus_ou_retorno_fixo"),
        ({"fixed": True}, "taxa_bonus_ou_retorno_fixo"),
        ({"bonus": True}, "taxa_bonus_ou_retorno_fixo"),
        ({"historico": False}, "historico_financeiro_ausente"),
        ({"tip_review": True}, "dica_em_revisao"),
    ],
)
async def test_unsafe_data_is_explicit_and_never_a_complete_comparison(
    api: Any, population: Any, engine_app: Any, como: Any, options: dict[str, Any], motivo: str
) -> None:
    await create_pair(population, engine_app, como, **options)
    result = await read(api, population)
    assert result["estado"] == "indisponivel"
    assert result["apostas_consideradas"] == result["apostas_indisponiveis"] == 1
    assert result["apostas_alteradas"] == 0
    assert result["hipotetico"]["giro_centavos"] is None
    assert result["hipotetico"]["lucro_centavos"] is None
    assert {"motivo": motivo, "quantidade": 1} in result["limitacoes"]


@pytest.mark.parametrize(
    "estado,retorno,expected_return",
    [("GREEN", 100, "20"), ("RED", 0, "0"), ("MEIO_GREEN", 50, "10"), ("MEIO_RED", 0, "0")],
)
async def test_freebet_uses_face_and_zero_cost_not_proportional_bonus(
    api: Any,
    population: Any,
    engine_app: Any,
    como: Any,
    estado: str,
    retorno: int,
    expected_return: str,
) -> None:
    await create_pair(
        population,
        engine_app,
        como,
        estado=estado,
        stake=100,
        indicada=20,
        retorno=retorno,
        freebet=True,
    )
    result = await read(api, population)
    assert result["hipotetico"]["giro_centavos"] == "0"
    assert result["hipotetico"]["base_roi_centavos"] == "20"
    assert (
        result["hipotetico"]["lucro_centavos"]
        == result["hipotetico"]["retorno_centavos"]
        == expected_return
    )
    assert result["hipotetico"]["freebets"] == 1


async def test_full_population_larger_than_page_and_no_n_plus_one(
    api: Any, population: Any, engine_app: Any, como: Any
) -> None:
    for _ in range(151):
        await create_pair(population, engine_app, como)
    statements = []

    def capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    event.listen(engine_app.sync_engine, "before_cursor_execute", capture)
    try:
        result = await read(api, population, origem="casa")
    finally:
        event.remove(engine_app.sync_engine, "before_cursor_execute", capture)
    assert result["apostas_consideradas"] == result["apostas_alteradas"] == 151
    assert result["real"]["giro_centavos"] == "2416000"
    assert result["hipotetico"]["lucro_centavos"] == "302000"
    assert len(statements) <= 9


@pytest.mark.parametrize(
    "campo,attribute",
    [
        ("conta_casa_id", "contas"),
        ("titular_id", "titulares"),
        ("casa_id", "casas"),
        ("tipster_id", "tipsters"),
        ("banca_id", "bancas"),
        ("grupo_id", "grupos"),
        ("mercado_id", "mercados"),
        ("competicao_id", "competicoes"),
    ],
)
async def test_all_selectors_use_owned_canonical_population(
    api: Any, population: Any, engine_app: Any, como: Any, campo: str, attribute: str
) -> None:
    await create_pair(population, engine_app, como)
    result = await read(api, population, **{campo: str(getattr(population, attribute)[0])})
    assert result["apostas_alteradas"] == 1
    assert result["filtros"][campo] == str(getattr(population, attribute)[0])
    excluded = await read(api, population, **{campo: str(getattr(population, attribute)[1])})
    assert excluded["estado"] == "sem_apostas"


async def test_dates_timezone_missing_dates_deleted_review_and_sources(
    api: Any, population: Any, engine_app: Any, como: Any
) -> None:
    await create_pair(
        population, engine_app, como, date=datetime(2026, 9, 10, 2, 59, 59, tzinfo=UTC)
    )
    await create_pair(population, engine_app, como)
    await create_pair(population, engine_app, como, date=None)
    await create_pair(population, engine_app, como, selected=False)
    await create_pair(population, engine_app, como, review=True)
    result = await read(
        api,
        population,
        origem="casa",
        desde="2026-09-10T00:00:00-03:00",
        ate="2026-09-11T00:00:00-03:00",
    )
    assert result["apostas_consideradas"] == 2
    assert result["apostas_alteradas"] == 1
    assert result["real"]["em_revisao"] == 1
    assert result["real"]["giro_centavos"] == "16000"
    assert result["criterio_temporal"] == "data_aposta"
    assert result["fuso_horario"] == "America/Sao_Paulo"
    all_rows = await read(api, population, visibilidade="todas")
    assert all_rows["apostas_consideradas"] == 10
    assert all_rows["real"]["fora_do_financeiro"] == 5
    assert all_rows["real"]["giro_centavos"] == "64000"
    deleted = await read(api, population, visibilidade="apagadas", origem="casa")
    assert deleted["apostas_consideradas"] == deleted["apostas_alteradas"] == 1
    summary = await api[0].get(
        "/api/v1/painel/filtrado", params={"visibilidade": "todas"}, headers=api[1](population.user)
    )
    assert str(summary.json()["resumo"]["giro_centavos"]) == all_rows["real"]["giro_centavos"]
    assert str(summary.json()["resumo"]["lucro_centavos"]) == all_rows["real"]["lucro_centavos"]


@pytest.mark.parametrize("campo", ["conta_casa_id", "titular_id", "banca_id", "grupo_id"])
async def test_foreign_and_nonexistent_private_selectors_are_indistinguishable(
    api: Any, population: Any, campo: str
) -> None:
    client, headers = api
    foreign = await client.get(
        PATH, params={campo: str(population.other_ids[campo])}, headers=headers(population.user)
    )
    missing = await client.get(
        PATH, params={campo: "9223372036854775807"}, headers=headers(population.user)
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "filtro_nao_encontrado"}


async def test_rls_foreign_tip_fk_and_unauthenticated_private_response(
    api: Any, population: Any, engine_app: Any, como: Any
) -> None:
    house, tip, _ = await create_pair(population, engine_app, como, linked=None)
    async with como(engine_app, population.other) as session:
        assert await session.scalar(select(models.Aposta.id).where(models.Aposta.id == tip)) is None
        session.add(
            models.ApostaConsolidacao(
                usuario_id=population.other,
                casa_aposta_id=house,
                telegram_aposta_id=tip,
                conta_casa_id=population.other_ids["conta_casa_id"],
                estado="active",
                decisao="reviewed",
                versao="test",
                evidencia={},
                contexto={},
                ator="test",
            )
        )
        with pytest.raises(DBAPIError) as error:
            await session.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "P0001"
        await session.rollback()
    result = await read(api, population, origem="casa")
    assert result["apostas_alteradas"] == result["vinculos_confirmados"] == 0
    response = await api[0].get(PATH)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"


async def test_read_only_permission_and_real_facts_events_caixa_exports_unchanged(
    api: Any, population: Any, engine_admin: Any, engine_app: Any, como: Any
) -> None:
    await create_pair(population, engine_app, como)

    async def snapshot() -> list[object]:
        async with como(engine_app, population.user) as session:
            return [
                list(
                    (
                        await session.execute(
                            select(model.__table__).where(model.usuario_id == population.user)
                        )
                    ).mappings()
                )
                for model in (
                    models.Aposta,
                    models.Evento,
                    models.ApostaConsolidacao,
                    models.Movimento,
                )
            ]

    before = await snapshot()
    async with engine_admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("UPDATE billing_rollout SET activated_at=now() WHERE id=1"))
    try:
        async with como(engine_app, population.user) as session:
            status = await AssinaturaRepo().read_status(session, population.user)
            assert status.access.value == "READ_ONLY"
        before_panel = await api[0].get("/api/v1/painel/filtrado", headers=api[1](population.user))
        before_export = await api[0].get(
            "/api/v1/painel/filtrado/export", headers=api[1](population.user)
        )
        for _ in range(2):
            result = await read(api, population)
            assert result["hipotetico"]["lucro_centavos"] == "2000"
        after_panel = await api[0].get("/api/v1/painel/filtrado", headers=api[1](population.user))
        assert before_panel.json()["resumo"] == after_panel.json()["resumo"]
        after_export = await api[0].get(
            "/api/v1/painel/filtrado/export", headers=api[1](population.user)
        )
        import io

        from openpyxl import load_workbook

        books = [
            load_workbook(io.BytesIO(x.content), read_only=True)
            for x in (before_export, after_export)
        ]
        try:
            assert [list(s.values) for s in books[0]] == [list(s.values) for s in books[1]]
        finally:
            for book in books:
                book.close()
        assert before == await snapshot()
    finally:
        async with engine_admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:p WHERE id=1"), {"p": previous}
            )


@pytest.mark.parametrize(
    "params",
    [
        {"stake": "123"},
        {"page": "2"},
        {"estado": "other"},
        {"origem": "other"},
        {"periodo": "30d", "desde": "2026-01-01T00:00:00Z"},
        {"desde": "2026-09-11T00:00:00Z", "ate": "2026-09-10T00:00:00Z"},
        {"visibilidade": "apagadas", "incluir_apagadas": "true"},
        {"grupo_id": "9223372036854775808"},
    ],
)
async def test_invalid_inputs_are_rejected_privately(
    api: Any, population: Any, params: dict[str, str]
) -> None:
    response = await api[0].get(PATH, params=params, headers=api[1](population.user))
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"


async def test_real_postgres_timeout_is_503_not_empty_success(
    api: Any, population: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = AsyncSession.execute

    async def timed_out(session: Any, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "FROM apostas" in str(statement):
            await original(session, text("SET LOCAL statement_timeout = '1ms'"))
            return await original(session, text("SELECT pg_sleep(0.05)"))
        return await original(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", timed_out)
    response = await api[0].get(PATH, headers=api[1](population.user))
    assert response.status_code == 503
    assert response.json() == {"detail": "hipotetica_temporariamente_indisponivel"}


async def test_bigint_money_empty_and_published_examples_match_full_openapi(
    api: Any, population: Any, engine_app: Any, como: Any
) -> None:
    empty = await read(api, population)
    assert empty["estado"] == "sem_apostas"
    assert empty["real"]["giro_centavos"] == empty["hipotetico"]["lucro_centavos"] == "0"
    await create_pair(
        population,
        engine_app,
        como,
        stake=9007199254740993,
        retorno=9007199254740993,
        odd=1,
        indicada=9007199254740995,
    )
    result = await read(api, population)
    assert result["real"]["retorno_centavos"] == "9007199254740993"
    assert result["hipotetico"]["retorno_centavos"] == "9007199254740995"
    assert result["diferencas"]["giro_centavos"] == "2"
    schema = app.openapi()
    response_schema = {
        **schema["components"]["schemas"]["HipoteticaSaida"],
        "components": schema["components"],
    }
    validator = Draft202012Validator(response_schema)
    validator.validate(empty)
    validator.validate(result)
    examples = json.loads(
        (Path(__file__).parents[2] / "docs/contracts/examples/hipotetica.json").read_text()
    )
    for name in ("comparado", "vazio", "indisponivel"):
        validator.validate(examples[name])


async def test_over_limit_population_is_rejected_without_truncation(
    api: Any, population: Any, engine_app: Any, como: Any
) -> None:
    async with como(engine_app, population.user) as session:
        await session.execute(
            text(
                "INSERT INTO apostas(usuario_id,chave,origem,estado,stake_unidades,stake_centavos,valor_aposta_centavos,retorno_centavos) SELECT :uid, 'limit:'||:suffix||':'||n, 'manual','RED',1,100,100,0 FROM generate_series(1,10001) n"
            ),
            {"uid": population.user, "suffix": uuid4().hex},
        )
        await session.commit()
    response = await api[0].get(PATH, headers=api[1](population.user))
    assert response.status_code == 422
    assert response.json() == {"detail": "populacao_excede_10000_apostas_reduza_os_filtros"}
