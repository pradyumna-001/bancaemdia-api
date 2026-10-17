"""Mandatory real HTTP/JWT and restricted PostgreSQL confirmation acceptance."""

import asyncio
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.db.session import get_db_primary_snapshot
from bancaemdia.main import app
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo
from tests.integration.coleta.test_pairing import (  # ruff: ignore[unused-import]
    real_database_required,
    signing_key,
)
from tests.integration.coleta.test_pairing import system as system
from tests.integration.test_identity_db import identity_service_db as identity_service_db
from tests.integration.test_troca_titular import _setup, _tenant

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]
PATH = "/api/v1/titulares/trocas"


@pytest.fixture
async def switch(system, monkeypatch):

    async def sessions():
        async with AsyncSession(system.engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db_primary_snapshot, sessions)
    house, source, target, start = await _setup(system.admin, system.engine, system.user)
    system.body = {
        "casa_id": house,
        "conta_origem_id": source,
        "conta_destino_id": target,
        "efetiva_em": (start + timedelta(days=5)).isoformat(),
        "estado_origem": "LIMITADA",
    }
    system.key = "intention:" + str(uuid4())

    async def request(suffix="", *, key=None, body=None):
        return await system.http.post(
            PATH + suffix,
            json=system.body if body is None else body,
            headers={**system.headers(), "Idempotency-Key": key or system.key},
        )

    async def bet(offset=0):
        async with AsyncSession(system.engine) as session, session.begin():
            await _tenant(session, system.user)
            return await session.scalar(
                insert(models.Aposta)
                .values(
                    usuario_id=system.user,
                    chave="synthetic:" + str(uuid4()),
                    origem="manual",
                    stake_unidades=1,
                    stake_centavos=100,
                    conta_casa_id=source,
                    data_jogo=start + timedelta(days=5, seconds=offset),
                )
                .returning(models.Aposta.id)
            )

    system.request, system.bet = request, bet

    async def recover(key=None, user=None):
        return await system.http.get(
            PATH + "/intencao",
            headers={
                **system.headers(system.user if user is None else user),
                "Idempotency-Key": key or system.key,
            },
        )

    system.recover = recover
    yield system


async def test_insert_after_preview_conflicts_without_partial_switch(switch):
    first = await switch.bet()
    preview = await switch.request("/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["apostas_afetadas_ids"] == [first]
    await switch.bet(1)
    response = await switch.request()
    assert response.status_code == 409, response.text
    async with AsyncSession(switch.engine) as session:
        await _tenant(session, switch.user)
        assert (
            await session.scalar(
                select(models.ContaCasa.estado).where(
                    models.ContaCasa.id == switch.body["conta_origem_id"]
                )
            )
            == "EM_USO"
        )
        assert len(list(await session.scalars(select(models.UsoContaCasa)))) == 1
        assert list(await session.scalars(select(models.TrocaTitularRequisicao.tipo))) == [
            "PREVIEW"
        ]
    assert response.json() == {"detail": "swap_preview_stale"}
    assert (await switch.request("/preview")).json() == preview.json()
    fresh = "renewed:" + str(uuid4())
    renewed = await switch.request("/preview", key=fresh)
    assert len(renewed.json()["apostas_afetadas_ids"]) == 2
    assert (await switch.request()).status_code == 409
    assert (await switch.request(key=fresh)).status_code == 200


@pytest.mark.parametrize("state", ["DISPONIVEL", "LIMITADA", "ENCERRADA"])
async def test_stable_preview_concurrent_apply_exact_replay_and_restart(switch, state):
    switch.body["estado_origem"] = state
    await switch.bet()
    assert (await switch.request("/preview")).status_code == 200
    first, second = await asyncio.gather(switch.request(), switch.request())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        assert await session.scalar(select(func.count()).select_from(models.UsoContaCasa)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(models.TrocaTitularEvento)) == 2
        )
        # Later state changes cannot invalidate a committed receipt or renew its evidence.
        await session.execute(
            update(models.ContaCasa)
            .where(models.ContaCasa.id == switch.body["conta_origem_id"])
            .values(apelido="changed later")
        )
        await session.execute(
            update(models.TrocaTitularRequisicao).values(
                criado_em=func.now() - text("interval '30 days'")
            )
        )
    # Fresh HTTP requests/sessions read durable PostgreSQL records, no process-local state.
    recovered = await switch.recover()
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["estado"] == "APPLIED"
    assert recovered.json()["recibo"] == first.json()
    assert (await switch.request()).json() == first.json()
    changed = {**switch.body, "estado_origem": "ENCERRADA" if state != "ENCERRADA" else "LIMITADA"}
    assert (await switch.request(body=changed)).json() == {"detail": "swap_intention_conflict"}


@pytest.mark.parametrize(
    "change", ["game_time", "explicit", "target_alias", "target_state", "source_state", "usage"]
)
async def test_relevant_correction_or_context_change_conflicts_atomically(switch, change):
    ident = await switch.bet()
    assert (await switch.request("/preview")).status_code == 200
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        await CruzamentoCandidatoRepo().lock(session, switch.user)
        if change == "game_time":
            await session.execute(
                update(models.Aposta)
                .where(models.Aposta.id == ident)
                .values(
                    data_jogo=datetime.fromisoformat(switch.body["efetiva_em"])
                    + timedelta(seconds=1)
                )
            )
        elif change == "explicit":
            bet = await session.get(models.Aposta, ident)
            session.add(
                models.Evento(
                    usuario_id=switch.user,
                    tipo="APOSTA_CRIADA",
                    fonte="manual",
                    aposta_chave=bet.chave,
                    payload_json={"conta_referencia_explicita": True},
                )
            )
        elif change == "usage":
            await session.execute(
                update(models.UsoContaCasa).values(
                    vigente_de=models.UsoContaCasa.vigente_de - timedelta(seconds=1)
                )
            )
        else:
            account = (
                switch.body["conta_origem_id"]
                if change == "source_state"
                else switch.body["conta_destino_id"]
            )
            values = {"apelido": "Renamed"} if change == "target_alias" else {"estado": "LIMITADA"}
            await session.execute(
                update(models.ContaCasa).where(models.ContaCasa.id == account).values(**values)
            )
    result = await switch.request()
    assert result.status_code == 409, result.text
    async with AsyncSession(switch.engine) as session:
        await _tenant(session, switch.user)
        assert await session.scalar(select(func.count()).select_from(models.UsoContaCasa)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.TrocaTitularEvento)
                .where(models.TrocaTitularEvento.tipo == "APPLY")
            )
            == 0
        )


async def test_materialization_writer_holds_matching_lock_until_commit(switch):
    ident = await switch.bet()
    assert (await switch.request("/preview")).status_code == 200
    async with AsyncSession(switch.engine) as writer:
        await _tenant(writer, switch.user)
        row = await writer.get(models.Aposta, ident)
        # Actual materialization repository, same lock acquired by the worker before key/rows.
        await ApostaRepo().upsert_materializada(
            writer,
            {
                "usuario_id": switch.user,
                "chave": row.chave,
                "origem": "manual",
                "stake_unidades": 1,
                "stake_centavos": 100,
                "conta_casa_id": switch.body["conta_origem_id"],
                "data_jogo": datetime.fromisoformat(switch.body["efetiva_em"])
                + timedelta(seconds=1),
            },
        )
        pending = asyncio.create_task(switch.request())
        try:
            # Verify the server is waiting on a PostgreSQL advisory lock, not a sleep assumption.
            for _ in range(100):
                async with switch.admin.connect() as conn:
                    waiting = await conn.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                        )
                    )
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting and not pending.done()
            await writer.commit()
            result = await asyncio.wait_for(pending, 5)
            assert result.status_code == 409, result.text
            assert result.json() == {"detail": "swap_preview_stale"}
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_irrelevant_result_change_does_not_invalidate_confirmation(switch):
    ident = await switch.bet()
    await switch.request("/preview")
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        row = await session.get(models.Aposta, ident)
        await ApostaRepo().update_estado(session, switch.user, row.chave, "GREEN", 200)
    assert (await switch.request()).status_code == 200


async def test_preview_expiry_is_durable_and_old_key_never_renews(switch):
    original = await switch.request("/preview")
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        await session.execute(
            update(models.TrocaTitularRequisicao).values(
                criado_em=func.now() - text("interval '24 hours 1 second'")
            )
        )
    assert (await switch.request()).status_code == 410
    assert (await switch.request("/preview")).json() == original.json()
    assert (await switch.recover()).json()["estado"] == "EXPIRED"
    fresh = "new:" + str(uuid4())
    assert (await switch.request(key=fresh)).status_code == 409
    assert (await switch.request("/preview", key=fresh)).status_code == 200
    assert (await switch.request(key=fresh)).status_code == 200


async def test_old_preview_without_evidence_requires_renewal(switch):
    await switch.request("/preview")
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        row = await session.scalar(select(models.TrocaTitularRequisicao))
        await session.execute(
            update(models.TrocaTitularRequisicao).values(
                resposta={k: v for k, v in row.resposta.items() if not k.startswith("_")}
            )
        )
    assert (await switch.request()).json() == {"detail": "swap_preview_stale"}


async def test_anonymous_foreign_owner_and_unknown_identity_are_safe(switch):
    await switch.request("/preview")
    assert (await switch.http.post(PATH, json=switch.body)).status_code == 401
    assert (
        await switch.http.get(PATH + "/intencao", headers={"Idempotency-Key": switch.key})
    ).status_code == 401
    foreign = await switch.recover(user=switch.other)
    missing = await switch.recover(key="never-created")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "swap_intention_unknown"}
    async with AsyncSession(switch.engine) as session:
        await _tenant(session, switch.other)
        assert list(await session.scalars(select(models.TrocaTitularRequisicao))) == []
        assert list(await session.scalars(select(models.TrocaTitularEvento))) == []
        assert (
            await session.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one() == (False, False)


@pytest.mark.parametrize(
    "field,value",
    [("estado_origem", "PENDENTE"), ("efetiva_em", "2026-10-01T12:00:00"), ("casa_id", 0)],
)
async def test_validation_does_not_echo_input_or_internal_diagnostics(switch, field, value):
    result = await switch.request("/preview", body={**switch.body, field: value})
    assert result.status_code == 422
    assert result.json() == {"detail": "swap_invalid_request"}


@pytest.fixture
async def cookie_switch(switch, identity_service_db, monkeypatch):
    from bancaemdia.auth import identity_config, identity_service, transport
    from bancaemdia.auth.identity_crypto import digest

    service = identity_service_db
    await service.verify_database_role()
    settings = service.settings.model_copy(
        update={
            "AUTH_ENABLED": True,
            "AUTH_PUBLIC_URL": "https://api.test",
            "AUTH_FRONTEND_ORIGIN": "https://site.test",
        }
    )
    service.settings = settings
    monkeypatch.setattr(identity_config, "identity_settings", lambda: settings)
    monkeypatch.setattr(identity_service, "identity_service", lambda: service)
    monkeypatch.setattr(transport, "identity_settings", lambda: settings)
    monkeypatch.setattr(transport, "identity_service", lambda: service)
    identity, sid, cookie = str(uuid4()), str(uuid4()), secrets.token_urlsafe(32)
    async with switch.admin.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO auth_private.identities(id,issuer,subject,usuario_id) VALUES(:id,:issuer,:sub,:uid)"
            ),
            {
                "id": identity,
                "issuer": settings.OIDC_ISSUER,
                "sub": str(uuid4()),
                "uid": switch.user,
            },
        )
    async with AsyncSession(service.engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO auth_private.sessions(id,identity_id,cookie_hash,encrypted,expires_at,idle_until,access_until) VALUES(:sid,:identity,:cookie,:encrypted,now()+interval '1 hour',now()+interval '1 hour',now()+interval '1 hour')"
            ),
            {
                "sid": sid,
                "identity": identity,
                "cookie": digest(cookie),
                "encrypted": service._credentials(
                    sid,
                    switch.user,
                    0,
                    {
                        "refresh_token": "synthetic-refresh",
                        "id_token": "synthetic-id",
                    },
                ),
            },
        )
    switch.headers = lambda uid=None: {
        "Cookie": settings.session_cookie + "=" + cookie,
        "Origin": "https://site.test",
        "X-CSRF-Token": service.keys.csrf(cookie),
    }
    yield switch


@pytest.mark.parametrize("kind", ["missing_csrf", "wrong_origin"])
async def test_cookie_preview_and_apply_require_csrf(cookie_switch, kind):
    headers = {**cookie_switch.headers(), "Idempotency-Key": cookie_switch.key}
    if kind == "missing_csrf":
        headers.pop("X-CSRF-Token")
    else:
        headers["Origin"] = "https://attacker.invalid"
    for suffix in ("/preview", ""):
        response = await cookie_switch.http.post(
            PATH + suffix, headers=headers, json=cookie_switch.body
        )
        assert response.status_code == 403
    assert (await cookie_switch.recover()).status_code == 404


async def test_lost_response_then_subscription_expiry_allows_read_recovery(cookie_switch):
    switch = cookie_switch
    assert (await switch.request("/preview")).status_code == 200
    committed = await switch.request()
    assert committed.status_code == 200, committed.text
    # Discarded network response; recover through read contract, never auto replay.
    async with switch.admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("SELECT billing_activate_rollout()"))
    try:
        assert (await switch.request()).status_code == 402
        assert (await switch.request("/preview", key="other-intention")).status_code == 402
        recovered = await switch.recover()
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["estado"] == "APPLIED"
        assert recovered.json()["recibo"] == committed.json()
        assert "no-store" in recovered.headers["Cache-Control"]
    finally:
        async with switch.admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:previous WHERE id=1"),
                {"previous": previous},
            )


async def test_cookie_cors_allows_idempotency_identity(cookie_switch):
    response = await cookie_switch.http.options(
        PATH + "/preview",
        headers={
            "Origin": "https://site.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token,idempotency-key",
        },
    )
    assert response.status_code == 204
    assert "idempotency-key" in response.headers["Access-Control-Allow-Headers"]


async def test_exact_boundary_old_open_explicit_and_unknown_game_keep_account(switch):
    before = await switch.bet(-0.000001)
    at = await switch.bet()
    explicit, unknown = await switch.bet(), await switch.bet()
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        row = await session.get(models.Aposta, explicit)
        session.add(
            models.Evento(
                usuario_id=switch.user,
                tipo="APOSTA_CRIADA",
                fonte="manual",
                aposta_chave=row.chave,
                payload_json={"conta_casa_ref": switch.body["conta_origem_id"]},
            )
        )
        await session.execute(
            update(models.Aposta).where(models.Aposta.id == unknown).values(data_jogo=None)
        )
    preview = await switch.request("/preview")
    assert preview.status_code == 200
    assert preview.json()["apostas_afetadas_ids"] == [at]
    assert (await switch.request()).status_code == 200
    async with AsyncSession(switch.engine) as session:
        await _tenant(session, switch.user)
        rows = list(
            await session.scalars(
                select(models.Aposta).where(models.Aposta.id.in_([before, at, explicit, unknown]))
            )
        )
        assert all(
            r.conta_casa_id == switch.body["conta_origem_id"] and r.estado == "PENDENTE"
            for r in rows
        )


@pytest.mark.parametrize(
    "kind", ["foreign_owner", "foreign_house", "future", "same_account", "before_usage"]
)
async def test_invalid_switch_never_writes_preview_or_apply(switch, kind):
    body = dict(switch.body)
    if kind in {"foreign_owner", "foreign_house"}:
        uid = switch.other if kind == "foreign_owner" else switch.user
        _, _, target, _ = await _setup(switch.admin, switch.engine, uid)
        body["conta_destino_id"] = target
    elif kind == "future":
        body["efetiva_em"] = "2099-01-01T00:00:00Z"
    elif kind == "same_account":
        body["conta_destino_id"] = body["conta_origem_id"]
    else:
        body["efetiva_em"] = "2000-01-01T00:00:00Z"
    assert (await switch.request("/preview", body=body)).status_code == 409
    assert (await switch.request(body=body)).status_code == 409
    assert (await switch.recover()).status_code == 404


async def test_unknown_and_preview_recovery_do_not_apply_or_claim_rollback(switch):
    assert (await switch.recover()).status_code == 404
    assert (await switch.request()).status_code == 409
    preview = await switch.request("/preview")
    status = await switch.recover()
    assert status.json()["estado"] == "PREVIEW"
    assert status.json()["previa"] == preview.json()
    assert status.json()["recibo"] is None
    # Same durable key/body can be explicitly submitted; no recovery GET caused it.
    assert (await switch.request()).status_code == 200


async def test_database_dependency_failure_has_safe_503_and_retry_after(switch):
    await switch.request("/preview")
    async with switch.admin.begin() as conn:
        await conn.execute(text("REVOKE SELECT ON trocas_titular_requisicoes FROM bancaemdia_app"))
    try:
        for response in (await switch.request(), await switch.recover()):
            assert response.status_code == 503
            assert response.json() == {"detail": "swap_temporarily_unavailable"}
            assert response.headers["Retry-After"] == "5"
    finally:
        async with switch.admin.begin() as conn:
            await conn.execute(text("GRANT SELECT ON trocas_titular_requisicoes TO bancaemdia_app"))


async def test_rate_limit_preserves_intention_and_retry_after(switch, monkeypatch):
    from bancaemdia.config import get_settings
    from bancaemdia.middleware.rate_limit import reset_rate_limiters

    monkeypatch.setenv("API_RATE_LIMIT", "1/minute")
    get_settings.cache_clear()
    reset_rate_limiters()
    try:
        assert (await switch.request("/preview")).status_code == 200
        response = await switch.request()
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1
    finally:
        monkeypatch.delenv("API_RATE_LIMIT")
        get_settings.cache_clear()
        reset_rate_limiters()
    assert (await switch.recover()).json()["estado"] == "PREVIEW"


async def test_real_worker_materialization_after_preview_requires_new_confirmation(switch):
    from bancaemdia.domain.materializar import NovaAposta
    from bancaemdia.workers.materialization import _gravar

    await switch.bet()
    assert (await switch.request("/preview")).status_code == 200
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        house = await session.scalar(
            select(models.Casa.nome).where(models.Casa.id == switch.body["casa_id"])
        )
        await _gravar(
            session,
            switch.user,
            NovaAposta(
                chave="t:synthetic:" + uuid4().hex,
                ordem=0,
                origem="telegram",
                chat_id=switch.user,
                message_id=999,
                bilhete=None,
                casa=house,
                data_aposta=switch.body["efetiva_em"],
                revisao_motivo=None,
                revisao_grave=False,
                confianca=1.0,
                payload={
                    "casa": house,
                    "data_jogo": switch.body["efetiva_em"],
                    "stake_unidades": 1,
                    "valor_unidade_centavos": 100,
                    "odd": 2.0,
                    "evento": "Synthetic event",
                    "selecionada": True,
                },
            ),
            {},
            None,
        )
    assert (await switch.request()).json() == {"detail": "swap_preview_stale"}
    fresh = await switch.request("/preview", key="worker-renewed:" + uuid4().hex)
    assert len(fresh.json()["apostas_afetadas_ids"]) == 2


async def test_real_http_game_correction_changes_confirmed_evidence(switch):
    ident = await switch.bet()
    async with AsyncSession(switch.engine) as session, session.begin():
        await _tenant(session, switch.user)
        row = await session.get(models.Aposta, ident)
        key = row.chave
        house = await session.scalar(
            select(models.Casa.nome).where(models.Casa.id == switch.body["casa_id"])
        )
        session.add(
            models.Evento(
                usuario_id=switch.user,
                tipo="APOSTA_CRIADA",
                fonte="manual",
                aposta_chave=key,
                payload_json={
                    "casa": house,
                    "data_jogo": switch.body["efetiva_em"],
                    "conta_casa_id": switch.body["conta_origem_id"],
                    "stake_unidades": 1,
                    "valor_unidade_centavos": 100,
                    "odd": 2.0,
                },
            )
        )
    assert (await switch.request("/preview")).status_code == 200
    changed = (datetime.fromisoformat(switch.body["efetiva_em"]) + timedelta(seconds=1)).isoformat()
    result = await switch.http.patch(
        "/api/v1/apostas/" + key, json={"data_jogo": changed}, headers=switch.headers()
    )
    assert result.status_code == 200, result.text
    assert (await switch.request()).json() == {"detail": "swap_preview_stale"}


async def test_another_key_is_another_intention_and_other_body_conflicts(switch):
    assert (await switch.request("/preview")).status_code == 200
    changed = {**switch.body, "estado_origem": "DISPONIVEL"}
    assert (await switch.request("/preview", body=changed)).json() == {
        "detail": "swap_intention_conflict"
    }
    second = "independent:" + uuid4().hex
    assert (await switch.request("/preview", key=second)).status_code == 200
    assert (await switch.request()).status_code == 200
    assert (await switch.request(key=second)).status_code == 409
    assert (await switch.recover(key=second)).json()["estado"] == "PREVIEW"
