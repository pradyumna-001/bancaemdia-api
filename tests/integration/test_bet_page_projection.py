"""Operational bet pages through cookie auth, real API writes and migrated PostgreSQL/RLS."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia import main, models
from bancaemdia.api.contracts import BetResponse, BetsPageResponse
from bancaemdia.auth import identity_config, identity_service, transport
from bancaemdia.auth.identity_service import IdentityService
from bancaemdia.db import session as db_session
from bancaemdia.domain.billing import AccessMode
from bancaemdia.domain.titulares import TrocaPedido, trocar_conta
from bancaemdia.repositories.aposta_contexto_repo import ApostaContextoRepo
from bancaemdia.repositories.assinatura_repo import AssinaturaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from tests.identity.support import key_settings

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


@pytest.fixture
async def page_env(isolated_banco: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Only the external issuer is simulated. Cookie, CSRF, access and RLS are real."""
    admin = create_async_engine(isolated_banco.url_admin, poolclass=NullPool)
    app_engine = create_async_engine(isolated_banco.url_app, poolclass=NullPool)
    role, password = "page_auth_" + uuid4().hex, uuid4().hex
    async with admin.begin() as conn:
        await conn.execute(insert(models.Casa).values(nome="Betano", dominio="betano.com"))
        await conn.execute(insert(models.Casa).values(nome="KTO", dominio="kto.com"))
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"))
        await conn.execute(text(f"GRANT bancaemdia_auth TO {role}"))
    auth_url = make_url(isolated_banco.url_admin).set(username=role, password=password)
    auth_engine = create_async_engine(auth_url, poolclass=NullPool)
    settings = key_settings(
        tmp_path,
        AUTH_ENABLED=True,
        AUTH_DATABASE_URL=SecretStr(auth_url.render_as_string(hide_password=False)),
        AUTH_PUBLIC_URL="https://page.example.org",
        AUTH_FRONTEND_ORIGIN="https://page.example.org",
        OIDC_ISSUER="https://issuer.example.org",
        OIDC_CLIENT_ID="page",
    )
    service = IdentityService(settings, auth_engine)
    await service.verify_database_role()

    class Issuer:
        async def authorization(self, state: Any, nonce: Any, verifier: Any) -> Any:
            return "https://issuer.example.org/authorize?" + urlencode({"state": state})

        async def tokens(self, data: Any) -> Any:
            return {"refresh_token": "synthetic-refresh", "id_token": "synthetic-identity"}

        async def identity(self, token: Any, nonce: Any) -> Any:
            return {
                "iss": settings.OIDC_ISSUER,
                "sub": uuid4().hex,
                "email_verified": True,
                "email": uuid4().hex + "@example.org",
                "name": "Disposable page user",
            }

    monkeypatch.setattr(service, "oidc", Issuer())
    monkeypatch.setattr(identity_config, "identity_settings", lambda: settings)
    monkeypatch.setattr(identity_service, "identity_service", lambda: service)
    monkeypatch.setattr(transport, "identity_settings", lambda: settings)
    monkeypatch.setattr(transport, "identity_service", lambda: service)
    ordinary = async_sessionmaker(app_engine, expire_on_commit=False)
    snapshot = async_sessionmaker(
        app_engine.execution_options(isolation_level="REPEATABLE READ"), expire_on_commit=False
    )
    for name in ("SessionLocal", "ReplicaSession"):
        monkeypatch.setattr(db_session, name, ordinary)
    for name in ("SnapshotSessionLocal", "SnapshotReplicaSession"):
        monkeypatch.setattr(db_session, name, snapshot)

    @asynccontextmanager
    async def login() -> Any:
        url, browser = await service.begin("/apostas")
        cookie, _ = await service.callback(
            parse_qs(urlparse(url).query)["state"][0], browser, "code"
        )
        uid = await service.authenticate(cookie)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url=settings.AUTH_PUBLIC_URL,
            cookies={settings.session_cookie: cookie},
            headers={
                "Origin": settings.AUTH_FRONTEND_ORIGIN,
                "X-CSRF-Token": service.keys.csrf(cookie),
            },
        ) as client:
            yield uid, client

    @asynccontextmanager
    async def tenant(uid: Any) -> Any:
        async with ordinary() as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(uid)}
            )
            yield session

    try:
        async with login() as (uid, client):
            yield uid, client, tenant, login, admin, app_engine
    finally:
        await app_engine.dispose()
        await auth_engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))
        await admin.dispose()


async def create(client: Any, **extra: Any) -> Any:
    response = await client.post(
        "/api/v1/apostas",
        json={
            "casa": "Betano",
            "odd": 2.0,
            "stake_unidades": 1.0,
            "data_aposta": "2026-09-20T12:00:00Z",
            "data_jogo": "2026-09-22T12:00:00Z",
            "evento": "Time A x Time B",
            "descricao": "Mais de 2.5",
            "mercado_bruto": "Total de gols",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["aposta"]


async def page(client: Any, **params: Any) -> Any:
    response = await client.get("/api/v1/apostas", params=params)
    assert response.status_code == 200, response.text
    BetsPageResponse.model_validate(response.json())
    return response.json()


async def detail(client: Any, key: Any) -> Any:
    response = await client.get(f"/api/v1/apostas/{key}")
    assert response.status_code == 200, response.text
    row = response.json()["aposta"]
    BetResponse.model_validate(row)
    return row


async def test_text_corrections_use_real_allowlist_and_list_agrees_with_detail(
    page_env: Any,
) -> None:
    _, client, *_ = page_env
    bet = await create(client)
    assert (await page(client))["data"] == [await detail(client, bet["chave"])]
    correction = {
        "casa": "KTO",
        "evento": "Time C x Time D",
        "descricao": "Menos de 1.5",
        "mercado_bruto": "Gols no primeiro tempo",
    }
    response = await client.patch(f"/api/v1/apostas/{bet['chave']}", json=correction)
    assert response.status_code == 200, response.text
    row = (await page(client))["data"][0]
    assert row == await detail(client, bet["chave"])
    assert {key: row[key] for key in ("casa", "evento", "descricao")} == {
        key: correction[key] for key in ("casa", "evento", "descricao")
    }
    assert row["mercado"] == correction["mercado_bruto"]
    for invalid in (
        {"mercado_bruto": 3},
        {"lucro_centavos": 99},
        {"conta_contexto": {"apelido": "fake"}},
    ):
        rejected = await client.patch(f"/api/v1/apostas/{bet['chave']}", json=invalid)
        assert rejected.status_code == 422
    cleared = await client.patch(
        f"/api/v1/apostas/{bet['chave']}", json={"descricao": None, "mercado_bruto": None}
    )
    assert cleared.status_code == 200
    row = (await page(client))["data"][0]
    assert row["descricao"] is None and row["mercado"] is None


async def test_legacy_freebet_review_deleted_restore_empty_pages_and_totals(page_env: Any) -> None:
    uid, client, tenant, *_ = page_env
    normal = await create(client)
    freebet = await create(client, freebet=True, evento="Freebet")
    won = await client.post(
        f"/api/v1/apostas/{freebet['chave']}/resultado", json={"estado": "GREEN"}
    )
    assert won.status_code == 200
    reviewed = await create(client, evento="Em revisão")
    marked = await client.patch(
        f"/api/v1/apostas/{reviewed['chave']}", json={"revisao_grave": True}
    )
    assert marked.status_code == 200
    legacy_key = "m:legacy:" + uuid4().hex
    async with tenant(uid) as session:
        # An actual old row with no history: unknown text cannot be guessed from a house/account.
        session.add(
            models.Aposta(
                usuario_id=uid,
                chave=legacy_key,
                origem="manual",
                stake_unidades=1.0,
                stake_centavos=10000,
            )
        )
        await session.commit()
    rows = (await page(client))["data"]
    assert len(rows) == 4
    for row in rows:
        assert row == await detail(client, row["chave"])
    legacy = next(row for row in rows if row["chave"] == legacy_key)
    assert all(
        legacy[field] is None
        for field in (
            "casa",
            "evento",
            "descricao",
            "mercado",
            "retorno_centavos",
            "lucro_centavos",
            "conta_contexto",
            "banca_contexto",
        )
    )
    free = next(row for row in rows if row["chave"] == freebet["chave"])
    assert (
        free["freebet"],
        free["stake_centavos"],
        free["valor_aposta_centavos"],
        free["retorno_centavos"],
        free["lucro_centavos"],
    ) == (True, 0, 10000, 10000, 10000)
    assert (await page(client, revisao_grave=True))["pagination"]["total"] == 1
    first = await page(client, page_size=2)
    second = await page(client, page_size=2, page=2)
    empty = await page(client, page_size=2, page=3)
    assert first["data"] + second["data"] == rows
    assert empty["data"] == [] and empty["pagination"]["total"] == 4
    assert (await page(client, estado="CASHOUT"))["pagination"]["total"] == 0
    deleted = await client.delete(f"/api/v1/apostas/{normal['chave']}")
    assert deleted.status_code == 200
    assert (await page(client))["pagination"]["total"] == 3
    all_rows = await page(client, incluir_apagadas="true")
    assert all_rows["pagination"]["total"] == 4
    assert next(row for row in all_rows["data"] if row["chave"] == normal["chave"])["apagada"]
    assert (await client.post(f"/api/v1/apostas/{normal['chave']}/restaurar")).status_code == 200
    assert (await page(client))["pagination"]["total"] == 4


async def test_batched_queries_are_constant_and_only_load_page_histories(
    page_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, _, _, _, engine = page_env
    # Measure the page queries independently of the periodic replica health probe.
    client.headers["X-Read-Replica"] = "false"
    for index in range(12):
        await create(client, evento=f"Página {index}")
    queries, requested = [], []
    original = EventoRepo.list_by_aposta_chaves

    async def histories(self: EventoRepo, session: Any, uid: Any, keys: Any) -> Any:
        requested.append(keys)
        return await original(self, session, uid, keys)

    monkeypatch.setattr(EventoRepo, "list_by_aposta_chaves", histories)

    def record(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any
    ) -> Any:
        queries.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        small = await page(client, page_size=1)
        small_count = len(queries)
        queries.clear()
        large = await page(client, page_size=10)
        # Two existing RLS configs (after_begin + _open), user and three page data queries.
        assert len(queries) == small_count == 6, queries
        assert len([sql for sql in queries if "set_config" in sql]) == 2
        assert len([sql for sql in queries if "FROM eventos" in sql]) == 1
        assert requested == [
            [row["chave"] for row in small["data"]],
            [row["chave"] for row in large["data"]],
        ]
        assert small["pagination"]["total"] == large["pagination"]["total"] == 12
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_recorded_game_account_and_prior_holder_survive_switch_and_renaming(
    page_env: Any,
) -> None:
    uid, client, tenant, _, admin, _ = page_env
    start = datetime.now(UTC) - timedelta(days=10)
    effective = start + timedelta(days=5)
    async with admin.connect() as conn:
        house = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    async with tenant(uid) as session:
        ana = (
            await session.execute(
                insert(models.Titular)
                .values(usuario_id=uid, nome="Ana")
                .returning(models.Titular.id)
            )
        ).scalar_one()
        bia = (
            await session.execute(
                insert(models.Titular)
                .values(usuario_id=uid, nome="Bia")
                .returning(models.Titular.id)
            )
        ).scalar_one()
        old_bank = (
            await session.execute(
                insert(models.Banca)
                .values(usuario_id=uid, nome="Banca histórica")
                .returning(models.Banca.id)
            )
        ).scalar_one()
        new_bank = (
            await session.execute(
                insert(models.Banca)
                .values(usuario_id=uid, nome="Banca atual da conta")
                .returning(models.Banca.id)
            )
        ).scalar_one()
        source = (
            await session.execute(
                insert(models.ContaCasa)
                .values(
                    usuario_id=uid,
                    casa_id=house,
                    titular_id=ana,
                    apelido="Primeira",
                    estado="EM_USO",
                    desde=start,
                    banca_id=old_bank,
                )
                .returning(models.ContaCasa.id)
            )
        ).scalar_one()
        target = (
            await session.execute(
                insert(models.ContaCasa)
                .values(
                    usuario_id=uid,
                    casa_id=house,
                    titular_id=bia,
                    apelido="Segunda",
                    estado="DISPONIVEL",
                    ativa=False,
                )
                .returning(models.ContaCasa.id)
            )
        ).scalar_one()
        session.add(
            models.UsoContaCasa(
                usuario_id=uid, casa_id=house, conta_casa_id=source, vigente_de=start
            )
        )
        await session.commit()
    before = await create(
        client,
        data_aposta=(effective + timedelta(days=1)).isoformat(),
        data_jogo=(effective - timedelta(days=1)).isoformat(),
    )
    assert before["conta_casa_id"] == source
    async with tenant(uid) as session:
        pedido = TrocaPedido(house, source, target, effective, "LIMITADA")
        key = "page-switch:" + uuid4().hex
        await trocar_conta(session, uid, pedido, key, aplicar=False)
        await trocar_conta(session, uid, pedido, key, aplicar=True)
        await session.execute(
            update(models.ContaCasa)
            .where(models.ContaCasa.id == source)
            .values(apelido="Primeira renomeada", banca_id=new_bank)
        )
        await session.execute(
            update(models.Titular)
            .where(models.Titular.id == ana)
            .values(nome="Ana renomeada", arquivado=True)
        )
        # Bet bank reference is independent of the account's current bank.
        await session.execute(
            update(models.Aposta)
            .where(models.Aposta.chave == before["chave"])
            .values(banca_id=old_bank)
        )
        await session.commit()
    after = await create(client, data_aposta=start.isoformat(), data_jogo=effective.isoformat())
    actual = await create(client, data_jogo=effective.isoformat(), conta_casa_ref=source)
    unassigned = await create(client, data_jogo=None)
    rows = {row["chave"]: row for row in (await page(client))["data"]}
    old = rows[before["chave"]]
    assert old == await detail(client, before["chave"])
    assert old["conta_casa_id"] == source
    assert old["conta_contexto"] == {
        "id": str(source),
        "casa_id": str(house),
        "apelido": "Primeira renomeada",
        "ativa": False,
        "estado": "LIMITADA",
        "titular": {"id": str(ana), "nome": "Ana renomeada", "arquivado": True},
    }
    assert old["banca_contexto"] == {"id": str(old_bank), "nome": "Banca histórica"}
    assert rows[after["chave"]]["conta_contexto"]["titular"]["id"] == str(bia)
    assert rows[actual["chave"]]["conta_contexto"]["id"] == str(source)
    assert rows[unassigned["chave"]]["conta_contexto"] is None
    assert (await page(client, conta_casa_id=source))["pagination"]["total"] == 2
    assert (await page(client, titular_id=ana))["pagination"]["total"] == 2
    corrected = await client.patch(
        f"/api/v1/apostas/{before['chave']}", json={"data_jogo": effective.isoformat()}
    )
    assert corrected.status_code == 200
    assert (await detail(client, before["chave"]))["conta_contexto"]["id"] == str(target)


async def test_private_foreign_references_and_exact_bigints_never_leak(page_env: Any) -> None:
    uid, client, tenant, login, admin, _ = page_env
    bet = await create(client)
    exact_id = 9007199254740993
    async with admin.connect() as conn:
        house = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    async with login() as (other, other_client):
        foreign = await create(other_client, descricao="SEGREDO DO OUTRO")
        async with tenant(other) as session:
            foreign_bank = (
                await session.execute(
                    insert(models.Banca)
                    .values(usuario_id=other, nome="Banca privada")
                    .returning(models.Banca.id)
                )
            ).scalar_one()
            holder = (
                await session.execute(
                    insert(models.Titular)
                    .values(usuario_id=other, nome="Titular privado")
                    .returning(models.Titular.id)
                )
            ).scalar_one()
            session.add(
                models.ContaCasa(
                    id=exact_id,
                    usuario_id=other,
                    casa_id=house,
                    titular_id=holder,
                    apelido="Conta privada",
                )
            )
            await session.commit()
        assert (await client.get(f"/api/v1/apostas/{foreign['chave']}")).status_code == 404
        for path, payload in (
            (
                "/api/v1/apostas",
                {"casa": "Betano", "odd": 2, "stake_unidades": 1, "conta_casa_ref": exact_id},
            ),
            (f"/api/v1/apostas/{bet['chave']}", {"conta_casa_id": exact_id}),
        ):
            response = await (
                client.post(path, json=payload)
                if path.endswith("apostas")
                else client.patch(path, json=payload)
            )
            assert response.status_code == 422
        # Defense in depth: corrupt legacy private reference created by a privileged repair.
        async with admin.begin() as conn:
            foreign_id = await conn.scalar(
                select(models.Aposta.id).where(models.Aposta.chave == foreign["chave"])
            )
            await conn.execute(
                update(models.Aposta)
                .where(models.Aposta.chave == bet["chave"])
                .values(conta_casa_id=exact_id, banca_id=foreign_bank)
            )
        rows = (await page(client))["data"]
        assert len(rows) == 1 and rows[0]["conta_contexto"] is None
        assert rows[0]["banca_contexto"] is None and "Banca privada" not in json.dumps(rows)
        assert "Titular privado" not in json.dumps(rows) and "SEGREDO" not in json.dumps(rows)
        async with tenant(uid) as session:
            foreign_context = await ApostaContextoRepo().list_by_ids(session, other, [foreign_id])
            foreign_history = await EventoRepo().list_by_aposta_chaves(
                session, other, [foreign["chave"]]
            )
            assert foreign_context == {}
            assert foreign_history == {}
        authorized = (await page(other_client))["data"]
        assert len(authorized) == 1
        assigned = await create(other_client, conta_casa_ref=exact_id)
        row = await detail(other_client, assigned["chave"])
        assert row["conta_contexto"]["id"] == str(exact_id)


async def test_legacy_account_without_holder_or_nickname_is_not_fabricated(page_env: Any) -> None:
    uid, client, tenant, _, admin, _ = page_env
    async with admin.connect() as conn:
        house = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    async with tenant(uid) as session:
        account = (
            await session.execute(
                insert(models.ContaCasa)
                .values(usuario_id=uid, casa_id=house, apelido="", ativa=False)
                .returning(models.ContaCasa.id)
            )
        ).scalar_one()
        await session.commit()
    bet = await create(client, conta_casa_ref=account)
    row = (await page(client))["data"][0]
    assert row == await detail(client, bet["chave"])
    assert row["conta_contexto"]["id"] == str(account)
    assert row["conta_contexto"]["apelido"] is None
    assert row["conta_contexto"]["titular"] is None
    assert row["banca_contexto"] is None


async def test_read_only_cookie_reads_are_allowed_and_mutations_require_csrf(page_env: Any) -> None:
    uid, client, tenant, _, admin, _ = page_env
    bet = await create(client)
    before = (await page(client))["data"]
    async with admin.begin() as conn:
        await conn.execute(
            update(models.BillingRollout)
            .where(models.BillingRollout.id == 1)
            .values(activated_at=datetime.now(UTC))
        )
    async with tenant(uid) as session:
        assert (await AssinaturaRepo().read_status(session, uid)).access == AccessMode.READ_ONLY
    assert (await page(client))["data"] == before
    assert await detail(client, bet["chave"]) == before[0]
    response = await client.patch(
        f"/api/v1/apostas/{bet['chave']}", json={"descricao": "Bloqueada"}
    )
    assert response.status_code == 402
    client.headers.pop("X-CSRF-Token")
    assert (await client.get("/api/v1/apostas")).status_code == 200
    assert (
        await client.patch(f"/api/v1/apostas/{bet['chave']}", json={"descricao": "Sem CSRF"})
    ).status_code == 403
    client.cookies.clear()
    assert (await client.get("/api/v1/apostas")).status_code == 401


async def test_page_snapshot_keeps_rows_events_and_labels_coherent_under_concurrent_commit(
    page_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid, client, tenant, *_ = page_env
    bet = await create(client)
    original = EventoRepo.list_by_aposta_chaves
    called = False

    async def commit_between_page_and_history(
        self: EventoRepo, session: Any, owner: Any, keys: Any
    ) -> Any:
        nonlocal called
        if not called:
            called = True
            async with tenant(uid) as writer:
                await EventoRepo().append(
                    writer,
                    {
                        "usuario_id": uid,
                        "tipo": "CORRECAO_MANUAL",
                        "fonte": "manual",
                        "aposta_chave": bet["chave"],
                        "payload_json": {"descricao": "Versão seguinte"},
                    },
                )
                await writer.execute(
                    update(models.Aposta)
                    .where(models.Aposta.chave == bet["chave"])
                    .values(revisao_grave=True)
                )
                await writer.commit()
        return await original(self, session, owner, keys)

    monkeypatch.setattr(EventoRepo, "list_by_aposta_chaves", commit_between_page_and_history)
    row = (await page(client))["data"][0]
    assert row["descricao"] == bet["descricao"] and not row["revisao_grave"]
    newer = (await page(client))["data"][0]
    assert newer["descricao"] == "Versão seguinte" and newer["revisao_grave"]
