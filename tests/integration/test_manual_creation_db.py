"""Mandatory actual HTTP/PostgreSQL/RLS proof of manual creation by client intent."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.auth import middleware as authentication
from bancaemdia.auth.identity_config import identity_settings
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db
from bancaemdia.models.manual_creation import ManualCreationOperation
from tests.integration.cruzamento.test_consolidacao import accounts

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]
P = "/api/v1/apostas-manuais/operacoes/"


@dataclass
class Api:
    client: httpx.AsyncClient
    key: object

    def headers(self, uid):
        settings = get_settings()
        token = jwt.encode(
            {
                "sub": str(uid),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "exp": int(time.time()) + 600,
            },
            self.key,
            algorithm="RS256",
            headers={"kid": "manual-test"},
        )
        return {"Authorization": "Bearer " + token}


@pytest.fixture
async def api(engine_app, monkeypatch):
    from bancaemdia.main import app

    monkeypatch.setenv("AUTH_ENABLED", "false")
    identity_settings.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="manual-test", alg="RS256", use="sig")
    cache = JWKSCache(None, "RS256")
    cache.keys = {"manual-test": public}
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(authentication, "get_jwks_cache", lambda: cache)

    async def database():
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id',:u,true)"),
                {"u": current_user_id.get() or ""},
            )
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://api.test",
        ) as client:
            yield Api(client, key)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        identity_settings.cache_clear()
        get_settings.cache_clear()


@pytest.fixture
async def owner(engine_admin, engine_app, novo_usuario):
    uid = await novo_usuario()
    house, ids = await accounts(engine_admin, engine_app, uid)
    return uid, house, ids[0]


def body(**changes):
    return {
        "casa": "Betano",
        "odd": 2.0,
        "stake_unidades": 1.0,
        "data_aposta": "2026-09-20T12:00:00-03:00",
        "data_jogo": "2026-09-22T19:00:00-03:00",
        "evento": "Casa x Fora",
        "descricao": "Vitória",
        **changes,
    }


async def post(api, uid, identity, payload=None):
    return await api.client.post(
        P + str(identity), headers=api.headers(uid), json=body() if payload is None else payload
    )


async def get(api, uid, identity):
    return await api.client.get(P + str(identity), headers=api.headers(uid))


async def evidence(engine_app, como, uid):
    async with como(engine_app, uid) as session:
        return {
            name: (
                await session.execute(
                    text(f"SELECT to_jsonb(t) FROM {name} t WHERE usuario_id=:u ORDER BY id"),
                    {"u": uid},
                )
            )
            .scalars()
            .all()
            for name in ("apostas", "eventos", "revisao_pendente", "manual_creation_operations")
        }


async def test_real_lost_201_after_commit_recovers_without_replay(api, owner, engine_app, como):
    from bancaemdia.main import app

    uid, _, _ = owner
    identity = uuid4()
    messages = []

    async def disconnect(scope, receive, send):
        async def lost(message):
            messages.append(message)
            await asyncio.sleep(0)

        await app(scope, receive, lost)
        raise ConnectionError("response lost after commit")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disconnect), base_url="https://api.test"
    ) as client:
        with pytest.raises(ConnectionError):
            await client.post(P + str(identity), headers=api.headers(uid), json=body())
    assert next(x for x in messages if x["type"] == "http.response.start")["status"] == 201
    recovery = await get(api, uid, identity)
    assert recovery.status_code == 200 and recovery.json()["estado"] == "confirmada"
    detail = await api.client.get(recovery.json()["detalhe_url"], headers=api.headers(uid))
    assert detail.status_code == 200
    e = await evidence(engine_app, como, uid)
    assert len(e["apostas"]) == len(e["eventos"]) == len(e["manual_creation_operations"]) == 1


async def test_sequential_replay_normalized_body_freezes_original_confirmation(
    api, owner, engine_app, como
):
    uid, _, _ = owner
    identity = uuid4()
    first = await post(api, uid, identity)
    assert first.status_code == 201, first.text
    before = await evidence(engine_app, como, uid)
    retry = await post(
        api,
        uid,
        identity,
        body(
            casa="betano",
            odd=2,
            data_jogo="2026-09-22T22:00:00Z",
            comissao_centavos="0",
            freebet=False,
        ),
    )
    assert retry.status_code == 200 and retry.json() == first.json()
    assert (await get(api, uid, identity)).json() == first.json()
    assert await evidence(engine_app, como, uid) == before


async def test_multiple_concurrent_posts_converge_one_fact_event_receipt(
    api, owner, engine_app, como
):
    uid, _, _ = owner
    identity = uuid4()
    responses = await asyncio.gather(*(post(api, uid, identity) for _ in range(8)))
    assert sorted(r.status_code for r in responses) == [200] * 7 + [201], [
        r.text for r in responses
    ]
    assert all(r.json() == responses[0].json() for r in responses)
    e = await evidence(engine_app, como, uid)
    assert len(e["apostas"]) == len(e["eventos"]) == len(e["manual_creation_operations"]) == 1
    assert sum(r["stake_centavos"] for r in e["apostas"]) == 10000


async def test_same_identity_other_body_conflicts_no_effect(api, owner, engine_app, como):
    uid, _, _ = owner
    identity = uuid4()
    assert (await post(api, uid, identity)).status_code == 201
    before = await evidence(engine_app, como, uid)
    r = await post(api, uid, identity, body(descricao="Outra aposta"))
    assert r.status_code == 409 and r.json()["detail"] == "manual_intention_conflict"
    assert await evidence(engine_app, como, uid) == before


async def test_concurrent_different_bodies_one_wins_other_conflicts(api, owner, engine_app, como):
    uid, _, _ = owner
    identity = uuid4()
    a, b = await asyncio.gather(post(api, uid, identity), post(api, uid, identity, body(odd=3)))
    assert sorted([a.status_code, b.status_code]) == [201, 409]
    e = await evidence(engine_app, como, uid)
    assert len(e["apostas"]) == len(e["eventos"]) == len(e["manual_creation_operations"]) == 1


async def test_different_identities_equal_contents_are_distinct_legitimate_bets(
    api, owner, engine_app, como
):
    uid, _, _ = owner
    a, b = await asyncio.gather(post(api, uid, uuid4()), post(api, uid, uuid4()))
    assert a.status_code == b.status_code == 201
    assert a.json()["aposta_chave"] != b.json()["aposta_chave"]
    e = await evidence(engine_app, como, uid)
    assert len(e["apostas"]) == len(e["eventos"]) == len(e["manual_creation_operations"]) == 2
    assert sum(r["stake_centavos"] for r in e["apostas"]) == 20000


async def test_unknown_identity_404_has_no_state_or_rollback_claim(api, owner, engine_app, como):
    uid, _, _ = owner
    before = await evidence(engine_app, como, uid)
    r = await get(api, uid, uuid4())
    assert r.status_code == 404 and r.json()["detail"] == "manual_intention_not_found"
    assert "rollback" not in r.text and "processando" not in r.text
    assert await evidence(engine_app, como, uid) == before


async def test_uncommitted_intent_404_can_later_confirm_without_duplicate(
    api, owner, engine_admin, engine_app, como
):
    uid, _, _ = owner
    identity = uuid4()
    trigger = "manual_wait_" + uuid4().hex
    pending = None
    try:
        async with engine_admin.begin() as c:
            await c.execute(
                text(
                    f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    f"BEGIN IF NEW.usuario_id={uid} THEN PERFORM pg_sleep(2); END IF; RETURN NEW; END $$"
                )
            )
            await c.execute(
                text(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT ON manual_creation_operations "
                    f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
                )
            )
        pending = asyncio.create_task(post(api, uid, identity))
        deadline = time.monotonic() + 10
        while True:
            async with engine_admin.connect() as c:
                waiting = await c.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event='PgSleep' AND query LIKE '%manual_creation_operations%'"
                    )
                )
            if waiting:
                break
            assert not pending.done() and time.monotonic() < deadline
            await asyncio.sleep(0.02)
        unknown = await get(api, uid, identity)
        assert unknown.status_code == 404
        first = await pending
        assert first.status_code == 201, first.text
        assert (await get(api, uid, identity)).json() == first.json()
        assert (await post(api, uid, identity)).json() == first.json()
        e = await evidence(engine_app, como, uid)
        assert len(e["apostas"]) == len(e["eventos"]) == len(e["manual_creation_operations"]) == 1
    finally:
        if pending is not None and not pending.done():
            await pending
        async with engine_admin.begin() as c:
            await c.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON manual_creation_operations"))
            await c.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


@pytest.mark.parametrize("freebet", [False, True])
async def test_financial_face_cost_commission_dates_and_current_unit(
    api, owner, engine_app, como, freebet
):
    uid, _, _ = owner
    async with como(engine_app, uid) as s:
        s.add(
            models.Unidade(
                usuario_id=uid, valor_centavos=12345, vigente_de=datetime(2026, 1, 1, tzinfo=UTC)
            )
        )
        await s.commit()
    r = await post(
        api,
        uid,
        uuid4(),
        body(
            freebet=freebet,
            stake_unidades=2,
            comissao_centavos="105",
            data_aposta="2026-09-20T12:00:00",
        ),
    )
    assert r.status_code == 201, r.text
    original = r.json()["confirmacao_original"]
    assert original["valor_unidade_centavos"] == "12345"
    assert original["valor_aposta_centavos"] == "24690"
    assert original["stake_centavos"] == ("0" if freebet else "24690")
    assert original["comissao_centavos"] == "105"
    assert datetime.fromisoformat(original["data_aposta"]) == datetime(2026, 9, 20, 15, tzinfo=UTC)
    async with como(engine_app, uid) as s:
        events = (
            (await s.execute(select(models.Evento).where(models.Evento.usuario_id == uid)))
            .scalars()
            .all()
        )
        assert events[0].payload_json["stake_unidades"] == 2
        assert events[0].payload_json["comissao_centavos"] == 105
    settled = await api.client.post(
        "/api/v1/apostas/" + r.json()["aposta_chave"] + "/resultado",
        headers=api.headers(uid),
        json={"estado": "GREEN"},
    )
    assert settled.status_code == 200, settled.text
    e = await evidence(engine_app, como, uid)
    bet = e["apostas"][0]
    assert bet["retorno_centavos"] == (24690 if freebet else 49380) - 105


async def test_default_date_unit_and_assignment_not_recomputed_on_replay(
    api, owner, engine_app, como
):
    uid, _house, _account = owner
    identity = uuid4()
    payload = body(data_aposta=None)
    first = await post(api, uid, identity, payload)
    assert first.status_code == 201, first.text
    async with como(engine_app, uid) as s:
        s.add(
            models.Unidade(
                usuario_id=uid, valor_centavos=777, vigente_de=datetime(2020, 1, 1, tzinfo=UTC)
            )
        )
        await s.execute(
            text("UPDATE usos_conta_casa SET vigente_ate=:t WHERE usuario_id=:u"),
            {"u": uid, "t": datetime(2026, 9, 21, tzinfo=UTC)},
        )
        await s.execute(
            text("UPDATE contas_casa SET ativa=false,ate=now() WHERE usuario_id=:u"), {"u": uid}
        )
        await s.commit()
    before = await evidence(engine_app, como, uid)
    assert (await post(api, uid, identity, payload)).json() == first.json()
    assert (await get(api, uid, identity)).json() == first.json()
    assert await evidence(engine_app, como, uid) == before


@pytest.mark.parametrize("mutation", ["correct", "delete", "physical_delete"])
async def test_original_receipt_survives_later_bet_changes_never_resurrects(
    api, owner, engine_app, engine_admin, como, mutation
):
    uid, _, _ = owner
    identity = uuid4()
    first = await post(api, uid, identity)
    assert first.status_code == 201, first.text
    url = first.json()["detalhe_url"]
    if mutation == "correct":
        r = await api.client.patch(
            url, headers=api.headers(uid), json={"odd": 3, "descricao": "Corrigida"}
        )
        assert r.status_code == 200, r.text
    elif mutation == "delete":
        assert (await api.client.delete(url, headers=api.headers(uid))).status_code == 200
    else:
        async with como(engine_app, uid) as s:
            await s.execute(text("DELETE FROM apostas WHERE usuario_id=:u"), {"u": uid})
            await s.commit()
    before = await evidence(engine_app, como, uid)
    assert (await get(api, uid, identity)).json() == first.json()
    assert (await post(api, uid, identity)).json() == first.json()
    assert await evidence(engine_app, como, uid) == before


@pytest.mark.parametrize("invalid", ["wrong_owner", "wrong_house"])
async def test_explicit_account_wrong_house_or_owner_safe_422(
    api, owner, engine_app, engine_admin, como, novo_usuario, invalid
):
    uid, _, _ = owner
    other = await novo_usuario()
    _, ids = await accounts(engine_admin, engine_app, other)
    account = ids[0]
    if invalid == "wrong_house":
        async with engine_admin.begin() as c:
            house = await c.scalar(
                text("INSERT INTO casas(nome,dominio) VALUES(:n,:d) RETURNING id"),
                {"n": uuid4().hex, "d": uuid4().hex + ".test"},
            )
        async with como(engine_app, uid) as s:
            account = await s.scalar(
                text(
                    "INSERT INTO contas_casa(usuario_id,casa_id,apelido) VALUES(:u,:h,'Wrong') RETURNING id"
                ),
                {"u": uid, "h": house},
            )
            await s.commit()
    before = await evidence(engine_app, como, uid)
    identity = uuid4()
    r = await post(api, uid, identity, body(conta_casa_id=str(account)))
    assert r.status_code == 422 and r.json() == {
        "detail": "manual_account_invalid",
        "campo": "conta_casa_id",
        "efeito": "nenhuma_gravacao_neste_pedido",
    }, r.text
    assert await evidence(engine_app, como, uid) == before
    assert (await get(api, uid, identity)).status_code == 404


async def test_game_time_historical_account_and_explicit_multicontas_exception(
    api, owner, engine_app, como
):
    uid, house, account = owner
    switch = datetime(2026, 9, 21, tzinfo=UTC)
    async with como(engine_app, uid) as s:
        await s.execute(
            text("UPDATE usos_conta_casa SET vigente_ate=:t WHERE usuario_id=:u"),
            {"t": switch, "u": uid},
        )
        await s.execute(
            text("UPDATE contas_casa SET ativa=false,ate=:t WHERE id=:i AND usuario_id=:u"),
            {"t": switch, "i": account, "u": uid},
        )
        new = models.ContaCasa(usuario_id=uid, casa_id=house, apelido="New", desde=switch)
        s.add(new)
        await s.flush()
        s.add(
            models.UsoContaCasa(
                usuario_id=uid, casa_id=house, conta_casa_id=new.id, vigente_de=switch
            )
        )
        await s.commit()
        new_id = new.id
    game = await post(api, uid, uuid4())
    assert game.status_code == 201, game.text
    assert game.json()["confirmacao_original"]["conta_casa_id"] == str(new_id)
    explicit = await post(api, uid, uuid4(), body(conta_casa_id=str(account)))
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["confirmacao_original"]["conta_casa_id"] == str(account)
    assert explicit.json()["confirmacao_original"]["criterio_conta"] == "multicontas_explicita"
    historical = await post(api, uid, uuid4(), body(data_jogo="2026-09-20T18:00:00-03:00"))
    assert historical.json()["confirmacao_original"]["conta_casa_id"] == str(account)


async def test_missing_account_or_game_opens_one_typed_review_no_guess(
    api, engine_app, engine_admin, como, novo_usuario
):
    uid = await novo_usuario()
    await accounts(engine_admin, engine_app, uid, count=2)
    identity = uuid4()
    first = await post(api, uid, identity)
    assert first.status_code == 201, first.text
    assert first.json()["confirmacao_original"]["conta_casa_id"] is None
    notice = first.json()["avisos"][0]
    assert notice["codigo"] == "conta_ausente" and notice["encaminhamento"] == "revisar_conta"
    e = await evidence(engine_app, como, uid)
    assert str(e["revisao_pendente"][0]["id"]) == notice["revisao_id"]
    assert (await post(api, uid, identity)).json() == first.json()
    assert await evidence(engine_app, como, uid) == e


@pytest.mark.parametrize(
    "fields",
    [
        {"odd": 0},
        {"stake_unidades": 0},
        {"casa": "secret-provider-dump"},
        {"data_aposta": "wrong-secret"},
        {"data_jogo": "wrong-secret"},
        {"comissao_centavos": "-1"},
        {"conta_casa_id": True},
        {"conta_casa_id": 9007199254740995},
        {"conta_casa_id": "9223372036854775808"},
        {"freebet": "false"},
        {"provider_secret": "private-secret"},
        {"comissao_centavos": "9223372036854775808"},
    ],
)
async def test_safe_field_validation_422_writes_nothing(api, owner, engine_app, como, fields):
    uid, _, _ = owner
    before = await evidence(engine_app, como, uid)
    r = await post(api, uid, uuid4(), body(**fields))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "manual_validation_invalid"
    assert r.json()["efeito"] == "nenhuma_gravacao_neste_pedido"
    assert set(r.json()) == {"detail", "campo", "efeito"}
    assert all(
        s not in r.text
        for s in ("private-secret", "wrong-secret", "secret-provider-dump", "input", "ctx", "msg")
    )
    assert await evidence(engine_app, como, uid) == before


async def test_invalid_uuid_and_malformed_json_are_safe_422(api, owner, engine_app, como):
    uid, _, _ = owner
    before = await evidence(engine_app, como, uid)
    r = await post(api, uid, "wrong-secret")
    assert (
        r.status_code == 422 and r.json()["campo"] == "intencao_id" and "wrong-secret" not in r.text
    )
    r = await api.client.post(
        P + str(uuid4()),
        headers={**api.headers(uid), "Content-Type": "application/json"},
        content="{bad-private-secret",
    )
    assert (
        r.status_code == 422
        and r.json()["detail"] == "manual_validation_invalid"
        and "private-secret" not in r.text
    )
    assert await evidence(engine_app, como, uid) == before


async def test_401_requires_reauthentication_and_has_no_effect(api, owner, engine_app, como):
    before = await evidence(engine_app, como, owner[0])
    path = P + str(uuid4())
    assert (await api.client.post(path, json=body())).status_code == 401
    r = await api.client.get(path)
    assert r.status_code == 401 and r.headers["cache-control"] == "private, no-store"
    assert await evidence(engine_app, como, owner[0]) == before


async def test_owner_isolation_reference_not_authorization_and_forced_rls(
    api, owner, engine_app, como, novo_usuario
):
    uid, _, _ = owner
    other = await novo_usuario()
    identity = uuid4()
    first = await post(api, uid, identity)
    assert first.status_code == 201, first.text
    foreign = await get(api, other, identity)
    unknown = await get(api, other, uuid4())
    assert foreign.status_code == unknown.status_code == 404 and foreign.json() == unknown.json()
    async with como(engine_app, other) as s:
        assert await s.get(ManualCreationOperation, (uid, identity)) is None
        assert (await s.execute(text("SELECT * FROM manual_creation_operations"))).all() == []
        nested = await s.begin_nested()
        with pytest.raises(DBAPIError):
            await s.execute(
                text(
                    "INSERT INTO manual_creation_operations(usuario_id,id,fingerprint,receipt) VALUES(:u,:i,:f,CAST(:r AS jsonb))"
                ),
                {"u": uid, "i": uuid4(), "f": "a" * 64, "r": json.dumps(first.json())},
            )
        await nested.rollback()
    second = await post(api, other, identity)
    assert (
        second.status_code == 201 and second.json()["aposta_chave"] != first.json()["aposta_chave"]
    )


async def test_atomic_receipt_failure_rolls_back_bet_events_and_account_review(
    api, engine_app, engine_admin, como, novo_usuario
):
    from bancaemdia.main import app

    uid = await novo_usuario()
    await accounts(engine_admin, engine_app, uid, count=0)
    identity = uuid4()
    before = await evidence(engine_app, como, uid)
    async with engine_admin.connect() as conn:
        outer = await conn.begin()
        previous = dict(app.dependency_overrides)
        try:
            await conn.execute(
                text(
                    "CREATE FUNCTION fail_manual_receipt_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private-database-diagnostic'; END $$"
                )
            )
            await conn.execute(
                text(
                    "CREATE TRIGGER fail_manual_receipt_test BEFORE INSERT ON manual_creation_operations FOR EACH ROW EXECUTE FUNCTION fail_manual_receipt_test()"
                )
            )
            await conn.execute(text("SET LOCAL ROLE bancaemdia_app"))

            async def database():
                async with AsyncSession(
                    bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
                ) as s:
                    await s.execute(
                        text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(uid)}
                    )
                    yield s

            app.dependency_overrides[get_db] = database
            r = await post(api, uid, identity)
            assert r.status_code == 503 and r.json()["detail"] == "manual_creation_unavailable", (
                r.text
            )
            assert "private-database" not in r.text and r.headers["Retry-After"] == "5"
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await outer.rollback()
    assert await evidence(engine_app, como, uid) == before
    assert (await get(api, uid, identity)).status_code == 404
    assert (await post(api, uid, identity)).status_code == 201


async def test_legacy_post_remains_compatible_with_no_implicit_idempotency(
    api, owner, engine_app, como
):
    uid, _, _ = owner
    a = await api.client.post("/api/v1/apostas", headers=api.headers(uid), json=body())
    b = await api.client.post("/api/v1/apostas", headers=api.headers(uid), json=body())
    assert a.status_code == b.status_code == 201
    assert a.json()["aposta"]["chave"] != b.json()["aposta"]["chave"]
    assert not (await evidence(engine_app, como, uid))["manual_creation_operations"]


async def test_expiry_keeps_identity_occupied_and_immutable_no_recreation(
    api, owner, engine_app, engine_admin, como
):
    uid, _, _ = owner
    identity = uuid4()
    first = await post(api, uid, identity)
    assert first.status_code == 201, first.text
    # Privileged fixture moves both immutable timestamps together; application
    # role can neither edit nor erase the receipt, including after expiration.
    async with engine_admin.begin() as c:
        await c.execute(
            text("ALTER TABLE manual_creation_operations DISABLE TRIGGER manual_creation_immutable")
        )
        await c.execute(
            text(
                "UPDATE manual_creation_operations SET created_at=now()-interval '91 days' WHERE usuario_id=:u AND id=:i"
            ),
            {"u": uid, "i": identity},
        )
        await c.execute(
            text("ALTER TABLE manual_creation_operations ENABLE TRIGGER manual_creation_immutable")
        )
    before = await evidence(engine_app, como, uid)
    for r in (await get(api, uid, identity), await post(api, uid, identity)):
        assert r.status_code == 410 and r.json()["detail"] == "manual_receipt_expired"
        assert r.json()["efeito"] == "confirmacao_anterior_retida"
    conflict = await post(api, uid, identity, body(odd=3))
    assert conflict.status_code == 409
    assert await evidence(engine_app, como, uid) == before
    async with como(engine_app, uid) as s:
        for sql in (
            "UPDATE manual_creation_operations SET fingerprint='new' WHERE usuario_id=:u",
            "DELETE FROM manual_creation_operations WHERE usuario_id=:u",
        ):
            nested = await s.begin_nested()
            with pytest.raises(DBAPIError):
                await s.execute(text(sql), {"u": uid})
            await nested.rollback()


async def test_402_read_only_recovers_saved_receipt_without_payment_or_replay(
    api, owner, engine_app, engine_admin, como
):
    from bancaemdia.main import app

    uid, _, _ = owner
    identity = uuid4()
    first = await post(api, uid, identity)
    assert first.status_code == 201, first.text
    before = await evidence(engine_app, como, uid)
    async with engine_admin.connect() as c:
        outer = await c.begin()
        previous = dict(app.dependency_overrides)
        try:
            await c.execute(text("SELECT billing_activate_rollout()"))
            await c.execute(text("SET LOCAL ROLE bancaemdia_app"))

            async def database():
                async with AsyncSession(bind=c, join_transaction_mode="create_savepoint") as s:
                    await s.execute(
                        text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(uid)}
                    )
                    yield s

            app.dependency_overrides[get_db] = database
            assert (await get(api, uid, identity)).json() == first.json()
            assert (await post(api, uid, identity)).status_code == 402
            assert (await post(api, uid, uuid4())).status_code == 402
            assert (await get(api, uid, identity)).json() == first.json()
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await outer.rollback()
    assert await evidence(engine_app, como, uid) == before


async def test_explicit_bigint_and_receipt_money_are_exact_strings(api, owner, engine_app, como):
    uid, house, _ = owner
    large = 9007199254740995
    async with como(engine_app, uid) as s:
        s.add(models.ContaCasa(id=large, usuario_id=uid, casa_id=house, apelido="BIGINT"))
        await s.commit()
    r = await post(api, uid, uuid4(), body(conta_casa_id=str(large)))
    assert r.status_code == 201, r.text
    original = r.json()["confirmacao_original"]
    assert original["conta_casa_id"] == str(large)
    assert all(
        isinstance(original[f], str)
        for f in (
            "stake_centavos",
            "valor_aposta_centavos",
            "comissao_centavos",
            "valor_unidade_centavos",
        )
    )


async def test_legacy_ambiguous_usage_is_real_http_review_without_arbitrary_account(
    api, engine_app, engine_admin, novo_usuario
):
    from bancaemdia.main import app

    uid = await novo_usuario()
    house, ids = await accounts(engine_admin, engine_app, uid, count=2)
    async with engine_admin.connect() as c:
        outer = await c.begin()
        previous = dict(app.dependency_overrides)
        try:
            # Reproduce historical overlapping evidence on an isolated rolled-
            # back fixture. Production constraints remain enforced and restored.
            await c.execute(
                text(
                    "ALTER TABLE usos_conta_casa DROP CONSTRAINT ex_usos_conta_casa_sem_sobreposicao_v1"
                )
            )
            await c.execute(text("DROP INDEX uq_usos_conta_casa_um_aberto_v1"))
            await c.execute(
                text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(uid)}
            )
            for account in ids:
                await c.execute(
                    text(
                        "INSERT INTO usos_conta_casa(usuario_id,casa_id,conta_casa_id,origem) VALUES(:u,:h,:a,'LEGADO')"
                    ),
                    {"u": uid, "h": house, "a": account},
                )
            await c.execute(text("SET LOCAL ROLE bancaemdia_app"))

            async def database():
                async with AsyncSession(
                    bind=c, join_transaction_mode="create_savepoint", expire_on_commit=False
                ) as s:
                    await s.execute(
                        text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(uid)}
                    )
                    yield s

            app.dependency_overrides[get_db] = database
            identity = uuid4()
            first = await post(api, uid, identity)
            assert first.status_code == 201, first.text
            assert first.json()["confirmacao_original"]["conta_casa_id"] is None
            assert first.json()["avisos"][0]["codigo"] == "conta_ambigua"
            assert (await post(api, uid, identity)).json() == first.json()
            count = await c.scalar(
                text("SELECT count(*) FROM revisao_pendente WHERE usuario_id=:u"), {"u": uid}
            )
            assert count == 1
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await outer.rollback()


async def test_no_game_time_does_not_use_placement_day_for_account(api, owner):
    uid, _, _ = owner
    r = await post(api, uid, uuid4(), body(data_jogo=None))
    assert r.status_code == 201, r.text
    assert r.json()["confirmacao_original"]["conta_casa_id"] is None
    assert r.json()["avisos"][0]["codigo"] == "conta_ausente"


async def test_cookie_csrf_origin_and_revocation_actual_identity(
    api, engine_admin, engine_app, banco, monkeypatch, tmp_path
):
    from pydantic import SecretStr
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from bancaemdia.auth import middleware, transport
    from bancaemdia.auth.identity_crypto import digest
    from bancaemdia.auth.identity_service import IdentityService
    from tests.identity.support import key_settings

    role, password = "manual_auth_" + uuid4().hex, uuid4().hex
    async with engine_admin.begin() as c:
        await c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"))
        await c.execute(text(f"GRANT bancaemdia_auth TO {role}"))
    url = (
        make_url(banco.url_admin)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    settings = key_settings(
        tmp_path,
        AUTH_ENABLED=True,
        AUTH_DATABASE_URL=SecretStr(url),
        AUTH_PUBLIC_URL="https://api.test",
        AUTH_FRONTEND_ORIGIN="https://api.test:4443",
        OIDC_ISSUER="https://issuer.test",
        OIDC_CLIENT_ID="fixture",
    )
    service = IdentityService(settings, engine)
    monkeypatch.setattr(middleware, "identity_settings", lambda: settings, raising=False)
    monkeypatch.setattr("bancaemdia.auth.identity_config.identity_settings", lambda: settings)
    monkeypatch.setattr("bancaemdia.auth.identity_service.identity_service", lambda: service)
    monkeypatch.setattr(transport, "identity_settings", lambda: settings)
    monkeypatch.setattr(transport, "identity_service", lambda: service)
    sid, cookie = str(uuid4()), uuid4().hex
    try:
        await service.verify_database_role()
        async with AsyncSession(engine) as s, s.begin():
            uid, identity = await service.provision(
                s,
                {
                    "iss": settings.OIDC_ISSUER,
                    "sub": uuid4().hex,
                    "email_verified": True,
                    "email": uuid4().hex + "@test.invalid",
                    "name": "Manual fixture",
                },
            )
            sealed = service._credentials(
                sid, uid, 0, {"refresh_token": "synthetic", "id_token": "synthetic"}
            )
            await s.execute(
                text(
                    "INSERT INTO auth_private.sessions(id,identity_id,cookie_hash,encrypted,expires_at,idle_until,access_until) VALUES(:id,:identity,:cookie,:encrypted,now()+interval '1 hour',now()+interval '1 hour',now()+interval '5 minutes')"
                ),
                {"id": sid, "identity": identity, "cookie": digest(cookie), "encrypted": sealed},
            )
        await accounts(engine_admin, engine_app, uid)
        address = P + str(uuid4())
        headers = {
            "Cookie": settings.session_cookie + "=" + cookie,
            "Origin": settings.AUTH_FRONTEND_ORIGIN,
        }
        assert (await api.client.post(address, headers=headers, json=body())).status_code == 403
        headers["X-CSRF-Token"] = service.keys.csrf(cookie)
        wrong = {**headers, "Origin": "https://foreign.test"}
        assert (await api.client.post(address, headers=wrong, json=body())).status_code == 403
        saved = await api.client.post(address, headers=headers, json=body())
        assert saved.status_code == 201, saved.text
        assert (
            await api.client.get(address, headers={"Cookie": headers["Cookie"]})
        ).json() == saved.json()
        async with engine.begin() as c:
            await c.execute(
                text("UPDATE auth_private.sessions SET revoked_at=now() WHERE id=:id"), {"id": sid}
            )
        assert (
            await api.client.get(address, headers={"Cookie": headers["Cookie"]})
        ).status_code == 401
    finally:
        await engine.dispose()
        async with engine_admin.begin() as c:
            await c.execute(text(f"DROP ROLE {role}"))


async def test_busy_timeout_has_safe_code_retry_after_no_partial_write(
    api, owner, engine_app, como
):
    from bancaemdia.main import app

    uid, _, _ = owner
    before = await evidence(engine_app, como, uid)
    previous = dict(app.dependency_overrides)
    try:

        async def database():
            async with AsyncSession(engine_app, expire_on_commit=False) as s:
                await s.execute(
                    text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(uid)}
                )
                await s.execute(text("SET LOCAL statement_timeout='150ms'"))
                yield s

        app.dependency_overrides[get_db] = database
        async with como(engine_app, uid) as blocker:
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"),
                {"k": f"pareador:{uid}"},
            )
            r = await post(api, uid, uuid4())
            assert r.status_code == 409 and r.json()["detail"] == "manual_creation_busy", r.text
            assert r.headers["Retry-After"] == "1"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
    assert await evidence(engine_app, como, uid) == before


async def test_rate_limit_publishes_429_retry_after_without_creating(
    api, owner, engine_app, como, monkeypatch
):
    from bancaemdia.middleware.rate_limit import reset_rate_limiters

    uid, _, _ = owner
    before = await evidence(engine_app, como, uid)
    monkeypatch.setenv("API_RATE_LIMIT", "1/minute")
    get_settings.cache_clear()
    reset_rate_limiters()
    try:
        assert (await get(api, uid, uuid4())).status_code == 404
        r = await post(api, uid, uuid4())
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert int(r.headers["Retry-After"]) > 0
        assert r.headers["Cache-Control"] == "private, no-store"
        assert await evidence(engine_app, como, uid) == before
    finally:
        get_settings.cache_clear()
        reset_rate_limiters()


async def test_actual_api_process_restart_recovers_database_receipt(api, owner, banco, tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    uid, _, _ = owner
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(api.key.public_key()))
    public.update(kid="manual-test", alg="RS256", use="sig")
    jwks = json.dumps({"keys": [public]}).encode()

    class Keys(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(jwks)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Keys)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = {
        **os.environ,
        "DATABASE_URL": banco.url_app,
        "DATABASE_URL_REPLICA": banco.url_app,
        "AUTH_ENABLED": "false",
        "APP_ENV": "testing",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "JWT_JWKS_URL": f"http://127.0.0.1:{server.server_port}/keys",
        "PYTHONIOENCODING": "utf-8",
    }
    base = f"http://127.0.0.1:{port}"
    identity = uuid4()
    process = None
    try:
        with (tmp_path / "owned-api-process.log").open("w", encoding="utf8") as log:
            saved = None
            for generation in range(2):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "bancaemdia.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--lifespan",
                        "off",
                    ],
                    env=env,
                    stdout=log,
                    stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False) as client:
                    deadline = time.monotonic() + 90
                    while True:
                        assert process.poll() is None, (
                            tmp_path / "owned-api-process.log"
                        ).read_text(encoding="utf8")
                        try:
                            if (await client.get("/health")).status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        assert time.monotonic() < deadline, (
                            tmp_path / "owned-api-process.log"
                        ).read_text(encoding="utf8")
                        await asyncio.sleep(0.1)
                    if generation == 0:
                        r = await client.post(
                            P + str(identity), headers=api.headers(uid), json=body()
                        )
                        assert r.status_code == 201, r.text
                        saved = r.json()
                    else:
                        r = await client.get(P + str(identity), headers=api.headers(uid))
                        assert r.status_code == 200 and r.json() == saved, r.text
                        replay = await client.post(
                            P + str(identity), headers=api.headers(uid), json=body()
                        )
                        assert replay.status_code == 200 and replay.json() == saved
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=10)
                process = None
    finally:
        if process is not None:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=10)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
