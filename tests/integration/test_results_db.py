"""Mandatory real HTTP/PostgreSQL proof of ordered, conditional financial writes."""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.v1.apostas import _aplicar, _historico
from bancaemdia.auth import middleware as authentication
from bancaemdia.auth.identity_config import identity_settings
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db, get_db_primary_snapshot
from bancaemdia.domain.materializar import EventoNovo, projetar
from bancaemdia.domain.projecao import projetar_validado
from bancaemdia.models.result_operation import ResultOperation
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.services import results as service
from tests.integration.test_telegram_confirmation import _account

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]
P = "/api/v1/resultados"


@dataclass
class Api:
    client: httpx.AsyncClient
    key: Any

    def headers(self, uid: int) -> dict[str, str]:
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
            headers={"kid": "result-test"},
        )
        return {"Authorization": "Bearer " + token}

    async def get(self, uid: int, path: str, **kwargs):
        return await self.client.get(P + path, headers=self.headers(uid), **kwargs)

    async def post(self, uid: int, path: str, body: dict):
        return await self.client.post(P + path, headers=self.headers(uid), json=body)


@pytest.fixture
async def api(engine_app, monkeypatch) -> AsyncIterator[Api]:
    from bancaemdia.main import app

    monkeypatch.setenv("AUTH_ENABLED", "false")
    identity_settings.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="result-test", alg="RS256", use="sig")
    cache = JWKSCache(None, "RS256")
    cache.keys = {"result-test": public}
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(authentication, "get_jwks_cache", lambda: cache)

    async def database():
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id',:uid,true)"),
                {"uid": current_user_id.get() or ""},
            )
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_db_primary_snapshot] = database
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
async def owner(engine_admin, engine_app, como, novo_usuario):
    uid = await novo_usuario()
    account = await _account(engine_admin, engine_app, como, uid)
    return uid, account


async def seed(engine, como, uid, account, **changes) -> str:
    key = "m:" + uuid4().hex
    payload = {
        "origem": "manual",
        "casa": "Betano",
        "evento": "Casa x Fora",
        "descricao": "Vitória",
        "data_aposta": "2026-01-01T09:00:00-03:00",
        "data_jogo": "2026-09-20T12:00:00-03:00",
        "odd": 2.0,
        "stake_unidades": 1.0,
        "valor_unidade_centavos": 10000,
        "freebet": False,
        "conta_casa_id": account,
        **changes,
    }
    events = [EventoNovo("APOSTA_CRIADA", "manual", payload)]
    async with como(engine, uid) as session:
        user = await UsuarioRepo().get_by_id(session, uid)
        state, _ = projetar([(e.tipo, e.fonte, e.payload) for e in events])
        assert await _aplicar(session, user, key, events, state) is not None
        await session.commit()
    return key


async def intent(api, uid, key, state="GREEN", **fields):
    current = await api.get(uid, "/apostas/" + key)
    assert current.status_code == 200, current.text
    return {"chave": key, "revisao_esperada": current.json()["revisao"], "estado": state, **fields}


async def counts(engine, como, uid, key):
    async with como(engine, uid) as session:
        history = await _historico(session, uid, key)
        row = (
            await session.execute(
                text(
                    "SELECT estado,retorno_centavos,stake_centavos,result_revision FROM apostas WHERE usuario_id=:uid AND chave=:key"
                ),
                {"uid": uid, "key": key},
            )
        ).one()
        movements = await session.scalar(
            text("SELECT count(*) FROM movimentos WHERE usuario_id=:uid"), {"uid": uid}
        )
        return len(history), row, movements, history


async def test_global_keyset_order_ties_nulls_and_shrinking_population(
    api, owner, engine_app, como
):
    uid, account = owner
    keys = []
    for date in (
        "2026-09-23T15:00:00Z",
        "2026-09-20T12:00:00-03:00",
        "2026-09-20T15:00:00Z",
        None,
        "2026-09-21T15:00:00Z",
    ):
        keys.append(await seed(engine_app, como, uid, account, data_jogo=date))
    page = (await api.get(uid, "/fila", params={"page_size": 2})).json()
    assert page["total_elegivel"] == 5
    assert [a["chave"] for a in page["data"]] == [keys[1], keys[2]]
    assert page["data"][0]["data_aposta"] != page["data"][0]["data_jogo"]
    seen = [a["chave"] for a in page["data"]]
    while page["data"]:
        for bet in page["data"]:
            body = {"chave": bet["chave"], "revisao_esperada": bet["revisao"], "estado": "GREEN"}
            response = await api.post(uid, "/operacoes/" + str(uuid4()), body)
            assert response.status_code == 200, response.text
        if page["next_cursor"] is None:
            break
        page = (
            await api.get(uid, "/fila", params={"page_size": 2, "cursor": page["next_cursor"]})
        ).json()
        seen.extend(a["chave"] for a in page["data"])
    assert seen == [keys[1], keys[2], keys[4], keys[0], keys[3]]
    assert (await api.get(uid, "/fila")).json()["total_elegivel"] == 0


async def test_queue_exceeds_ten_thousand_without_truncation(api, owner, engine_app, como):
    uid, account = owner
    async with como(engine_app, uid) as session:
        await session.execute(
            text("""INSERT INTO apostas(usuario_id,chave,origem,conta_casa_id,stake_unidades,stake_centavos,valor_aposta_centavos,data_jogo)
          SELECT CAST(:uid AS bigint),'large:'||CAST(:uid AS bigint)::text||':'||n,'manual',:account,1,10000,10000,'2026-09-20T15:00:00Z'::timestamptz FROM generate_series(1,10003) n"""),
            {"uid": uid, "account": account},
        )
        await session.commit()
    seen = set()
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["cursor"] = cursor
        response = await api.get(uid, "/fila", params=params)
        if response.status_code == 429:
            assert response.json()["error"] == "rate_limited"
            await asyncio.sleep(int(response.headers["retry-after"]) + 1)
            response = await api.get(uid, "/fila", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total_elegivel"] == 10003
        new = {bet["chave"] for bet in page["data"]}
        assert not seen & new
        seen |= new
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 10003


async def test_twenty_gestures_twenty_settlements_duplicate_delivery(api, owner, engine_app, como):
    uid, account = owner
    for index in range(20):
        key = await seed(engine_app, como, uid, account)
        body = await intent(api, uid, key, "GREEN" if index % 2 else "RED")
        identity = str(uuid4())
        response = await api.post(uid, "/operacoes/" + identity, body)
        assert response.status_code == 200, response.text
        assert (await api.post(uid, "/operacoes/" + identity, body)).json() == response.json()
        count, row, movements, _ = await counts(engine_app, como, uid, key)
        assert count == 2 and movements == 0
        assert row.retorno_centavos == (20000 if index % 2 else 0)
    async with como(engine_app, uid) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM result_operations WHERE usuario_id=:uid"), {"uid": uid}
            )
            == 20
        )


@pytest.mark.parametrize(
    "state,freebet,paid,expected",
    [
        ("GREEN", False, None, 20000),
        ("RED", False, None, 0),
        ("ANULADA", False, None, 10000),
        ("MEIO_GREEN", False, None, 15000),
        ("MEIO_RED", False, None, 5000),
        ("CASHOUT", False, 7531, 7531),
        ("CASHOUT", False, 0, 0),
        ("GREEN", True, None, 10000),
        ("RED", True, None, 0),
        ("ANULADA", True, None, 0),
        ("MEIO_GREEN", True, None, 5000),
        ("MEIO_RED", True, None, 0),
        ("CASHOUT", True, 7531, 7531),
        ("PENDENTE", False, None, None),
    ],
)
async def test_financial_states_and_exact_undo(
    api, owner, engine_app, como, state, freebet, paid, expected
):
    uid, account = owner
    key = await seed(engine_app, como, uid, account, freebet=freebet)
    fields = {"cashout_valor_centavos": paid} if state == "CASHOUT" else {}
    body = await intent(api, uid, key, state, **fields)
    identity = str(uuid4())
    before = await counts(engine_app, como, uid, key)
    result = await api.post(uid, "/operacoes/" + identity, body)
    assert result.status_code == 200, result.text
    bet = result.json()["aposta"]
    assert bet["retorno_centavos"] == expected
    assert bet["retorno_informado"] is (paid is not None)
    assert bet["stake_centavos"] == (0 if freebet else 10000)
    assert bet["valor_aposta_centavos"] == 10000
    assert bet["lucro_centavos"] == (
        None if expected is None else expected - (0 if freebet else 10000)
    )
    undone = await api.post(
        uid,
        "/operacoes/" + str(uuid4()) + "/desfazer",
        {"operacao_id": identity, "revisao_esperada": bet["revisao"]},
    )
    assert undone.status_code == 200, undone.text
    after = await counts(engine_app, como, uid, key)
    assert after[1].estado == "PENDENTE" and after[1].retorno_centavos is None
    assert after[0] == 3 and after[3][-1][0] == "RESULTADO_DESFEITO"
    assert projetar_validado(after[3])[0] == projetar(before[3])[0]


@pytest.mark.parametrize(
    "original", ["calculated", "explicit", "cashout", "unknown", "null_commission"]
)
async def test_restore_previous_settled_state_exactly(api, owner, engine_app, como, original):
    uid, account = owner
    key = await seed(
        engine_app,
        como,
        uid,
        account,
        odd=None if original == "unknown" else 2.0,
        comissao_centavos=None if original == "null_commission" else 311,
    )
    first = await intent(
        api,
        uid,
        key,
        "CASHOUT" if original == "cashout" else "GREEN",
        **(
            {"cashout_valor_centavos": 7111}
            if original == "cashout"
            else {"retorno_centavos": 12345}
            if original == "explicit"
            else {}
        ),
    )
    assert (await api.post(uid, "/operacoes/" + str(uuid4()), first)).status_code == 200
    if original == "null_commission":
        # Creation omits null fields; explicitly persist the distinct null fact as
        # an external writer would, rather than accidentally testing commission zero.
        async with como(engine_app, uid) as session:
            user = await UsuarioRepo().get_by_id(session, uid)
            history = await _historico(session, uid, key)
            event = EventoNovo("CORRECAO_MANUAL", "casa", {"comissao_centavos": None})
            state, _ = projetar([*history, (event.tipo, event.fonte, event.payload)])
            assert await _aplicar(session, user, key, [event], state) is not None
            await session.commit()
    before = await counts(engine_app, como, uid, key)
    if original == "null_commission":
        assert "comissao_centavos" in projetar(before[3])[0]
        assert projetar(before[3])[0]["comissao_centavos"] is None
    identity = str(uuid4())
    second = await api.post(
        uid, "/operacoes/" + identity, await intent(api, uid, key, "RED", comissao_centavos=19)
    )
    assert second.status_code == 200, second.text
    result = await api.post(
        uid,
        "/operacoes/" + str(uuid4()) + "/desfazer",
        {"operacao_id": identity, "revisao_esperada": second.json()["aposta"]["revisao"]},
    )
    assert result.status_code == 200, result.text
    after = await counts(engine_app, como, uid, key)
    assert tuple(before[1])[:3] == tuple(after[1])[:3]
    assert projetar_validado(after[3])[0] == projetar(before[3])[0]
    expected = {
        "calculated": 19689,
        "explicit": 12345,
        "cashout": 7111,
        "unknown": None,
        "null_commission": 20000,
    }[original]
    assert after[1].retorno_centavos == expected


async def test_two_intentions_same_revision_exactly_one_wins(api, owner, engine_app, como):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    body = await intent(api, uid, key)
    responses = await asyncio.gather(
        api.post(uid, "/operacoes/" + str(uuid4()), body),
        api.post(uid, "/operacoes/" + str(uuid4()), {**body, "retorno_centavos": 21345}),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["detail"] == "result_revision_conflict"
    assert (await counts(engine_app, como, uid, key))[0] == 2


@pytest.mark.parametrize(
    "writer",
    [
        "legacy_result",
        "correction",
        "external_same_state",
        "event_only",
        "review_resolution",
        "deleted_restored",
    ],
)
@pytest.mark.parametrize("phase", ["before_result", "before_undo"])
async def test_intermediate_writer_invalidates_intention(
    api, owner, engine_app, como, writer, phase
):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    body = await intent(api, uid, key)
    identity = str(uuid4())
    if phase == "before_undo":
        settled = await api.post(uid, "/operacoes/" + identity, body)
        body = {"operacao_id": identity, "revisao_esperada": settled.json()["aposta"]["revisao"]}
    if writer == "legacy_result":
        response = await api.client.post(
            "/api/v1/apostas/" + key + "/resultado",
            headers=api.headers(uid),
            json={"estado": "GREEN", "retorno_centavos": 22345},
        )
        assert response.status_code == 200, response.text
    elif writer == "correction":
        response = await api.client.patch(
            "/api/v1/apostas/" + key, headers=api.headers(uid), json={"odd": 2.31}
        )
        assert response.status_code == 200, response.text
    elif writer == "deleted_restored":
        for path in ("", "/restaurar"):
            response = await (
                api.client.delete("/api/v1/apostas/" + key, headers=api.headers(uid))
                if not path
                else api.client.post("/api/v1/apostas/" + key + path, headers=api.headers(uid))
            )
            assert response.status_code == 200, response.text
    else:
        async with como(engine_app, uid) as session:
            if writer == "external_same_state":
                await session.execute(
                    text(
                        "UPDATE apostas SET retorno_centavos=22345 WHERE usuario_id=:uid AND chave=:key"
                    ),
                    {"uid": uid, "key": key},
                )
            elif writer == "event_only":
                await session.execute(
                    text(
                        "INSERT INTO eventos(usuario_id,tipo,fonte,aposta_chave,payload_json) VALUES(:uid,'CORRECAO_MANUAL','casa',:key,jsonb_build_object('comissao_centavos',231))"
                    ),
                    {"uid": uid, "key": key},
                )
            else:
                await session.execute(
                    text(
                        "INSERT INTO revisao_pendente(usuario_id,motivo,resolvido_em) VALUES(:uid,'Fixture',now())"
                    ),
                    {"uid": uid},
                )
            await session.commit()
    before = await counts(engine_app, como, uid, key)
    path = "/operacoes/" + str(uuid4()) + ("/desfazer" if phase == "before_undo" else "")
    rejected = await api.post(uid, path, body)
    assert (
        rejected.status_code == 409 and rejected.json()["detail"] == "result_revision_conflict"
    ), rejected.text
    assert await counts(engine_app, como, uid, key) == before


async def test_isolation_queue_write_recovery_and_undo(api, owner, engine_app, como, novo_usuario):
    uid, account = owner
    other = await novo_usuario()
    key = await seed(engine_app, como, uid, account)
    body = await intent(api, uid, key)
    assert (await api.get(other, "/fila")).json()["total_elegivel"] == 0
    assert (await api.get(other, "/apostas/" + key)).status_code == 404
    assert (await api.post(other, "/operacoes/" + str(uuid4()), body)).status_code == 404
    identity = str(uuid4())
    result = (await api.post(uid, "/operacoes/" + identity, body)).json()
    assert (await api.get(other, "/operacoes/" + identity)).status_code == 404
    assert (
        await api.post(
            other,
            "/operacoes/" + str(uuid4()) + "/desfazer",
            {"operacao_id": identity, "revisao_esperada": result["aposta"]["revisao"]},
        )
    ).status_code == 404
    async with como(engine_app, other) as session:
        assert await session.get(ResultOperation, (uid, identity)) is None


async def test_lost_response_recovery_payload_conflict_and_repeated_undo(
    api, owner, engine_app, como
):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    body = await intent(api, uid, key)
    identity = str(uuid4())
    from bancaemdia.main import app

    # Let the real application finish (including the PostgreSQL commit) and then
    # fail transport delivery. The caller never receives a success response.
    captured = bytearray()

    async def disconnect_after_commit(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.body":
                captured.extend(message.get("body", b""))
            await send(message)

        await app(scope, receive, capture)
        raise httpx.ReadError("synthetic transport disconnect after commit")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disconnect_after_commit), base_url="https://api.test"
    ) as interrupted:
        with pytest.raises(httpx.ReadError, match="after commit"):
            await interrupted.post(
                P + "/operacoes/" + identity, headers=api.headers(uid), json=body
            )
    assert json.loads(captured)["id"] == identity
    recovered = await api.get(uid, "/operacoes/" + identity)
    assert (
        recovered.json() == json.loads(captured)
        and recovered.headers["cache-control"] == "private, no-store"
    )
    assert (await api.post(uid, "/operacoes/" + identity, {**body, "estado": "RED"})).json()[
        "detail"
    ] == "result_intention_conflict"
    undo_id = str(uuid4())
    undo_body = {"operacao_id": identity, "revisao_esperada": recovered.json()["aposta"]["revisao"]}
    undone = await api.post(uid, "/operacoes/" + undo_id + "/desfazer", undo_body)
    assert undone.status_code == 200
    assert (
        await api.post(uid, "/operacoes/" + undo_id + "/desfazer", undo_body)
    ).json() == undone.json()
    assert (await api.get(uid, "/operacoes/" + undo_id)).json() == undone.json()
    assert (
        await api.post(uid, "/operacoes/" + str(uuid4()) + "/desfazer", undo_body)
    ).status_code == 409
    assert (await counts(engine_app, como, uid, key))[0] == 3
    assert (await api.get(uid, "/operacoes/" + str(uuid4()))).status_code == 404


@pytest.mark.parametrize(
    "blocked,reason",
    [("deleted", "deleted"), ("severe", "severe_review"), ("unassigned", "account_unassigned")],
)
async def test_ineligible_items_are_decided_by_server(
    api, owner, engine_app, como, blocked, reason
):
    uid, account = owner
    key = await seed(
        engine_app,
        como,
        uid,
        None if blocked == "unassigned" else account,
        selecionada=blocked != "deleted",
        revisao_grave=blocked == "severe",
    )
    current = (await api.get(uid, "/apostas/" + key)).json()
    assert not current["elegivel"] and current["motivo_inelegivel"] == reason
    assert (await api.get(uid, "/fila")).json()["total_elegivel"] == 0
    before = await counts(engine_app, como, uid, key)
    result = await api.post(uid, "/operacoes/" + str(uuid4()), await intent(api, uid, key))
    assert result.status_code == 422 and result.json()["detail"] == "result_not_eligible"
    assert before == await counts(engine_app, como, uid, key)


@pytest.mark.parametrize(
    "params",
    [
        {"page_size": 0},
        {"page_size": 101},
        {"page": 2},
        {"incluir_apagadas": True},
        {"estado": "RED"},
        {"somente_apagadas": True},
        {"revisao_grave": True},
        {"cursor": "invalid"},
        {"desde": "2026-09-20"},
    ],
)
async def test_unsupported_or_invalid_queue_parameters_are_refused(api, owner, params):
    assert (await api.get(owner[0], "/fila", params=params)).status_code == 422


@pytest.mark.parametrize(
    "fields",
    [
        {"estado": "CASHOUT"},
        {"estado": "CASHOUT", "cashout_valor_centavos": 100, "comissao_centavos": 1},
        {"estado": "GREEN", "retorno_centavos": True},
        {"estado": "GREEN", "retorno_centavos": 2**63},
        {"estado": "GREEN", "comissao_centavos": -1},
        {"estado": "GREEN", "retorno_centavos": 1.5},
        {"estado": "PENDENTE", "retorno_centavos": 100},
        {"estado": "GREEN", "cashout_valor_centavos": 1},
        {"estado": "GREEN", "comissao_centavos": 20001},
        {"pre_imagem": {"estado": "PENDENTE"}},
    ],
)
async def test_invalid_result_no_partial_facts(api, owner, engine_app, como, fields):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    before = await counts(engine_app, como, uid, key)
    result = await api.post(
        uid, "/operacoes/" + str(uuid4()), {**await intent(api, uid, key), **fields}
    )
    assert result.status_code == 422, result.text
    assert before == await counts(engine_app, como, uid, key)


async def test_cursor_binding_and_new_insert_fence(api, owner, engine_app, como, novo_usuario):
    uid, account = owner
    for _ in range(3):
        await seed(engine_app, como, uid, account)
    page = (await api.get(uid, "/fila", params={"page_size": 1})).json()
    cursor = page["next_cursor"]
    new_key = await seed(engine_app, como, uid, account, data_jogo="2026-09-19T15:00:00Z")
    second = (await api.get(uid, "/fila", params={"page_size": 100, "cursor": cursor})).json()
    assert second["total_elegivel"] == 3 and new_key not in [a["chave"] for a in second["data"]]
    assert (
        await api.get(await novo_usuario(), "/fila", params={"cursor": cursor})
    ).status_code == 422
    assert (
        await api.get(uid, "/fila", params={"cursor": cursor, "freebet": True})
    ).status_code == 422
    assert (await api.get(uid, "/fila")).json()["total_elegivel"] == 4


async def test_filters_isolated_combined_and_authorized_labels(
    api, owner, engine_app, como, engine_admin
):
    uid, account = owner
    key = await seed(engine_app, como, uid, account, freebet=True)
    await seed(engine_app, como, uid, account, data_jogo="2026-09-21T15:00:00Z")
    for filters, expected in [
        ({"freebet": True}, 1),
        ({"conta_casa_id": account}, 2),
        ({"origem": "print"}, 0),
        ({"desde": "2026-09-20T15:00:00Z", "ate": "2026-09-21T15:00:00Z"}, 1),
        ({"freebet": True, "conta_casa_id": account, "origem": "manual"}, 1),
        ({"conta_casa_id": 2**62}, 0),
    ]:
        page = (await api.get(uid, "/fila", params=filters)).json()
        assert page["total_elegivel"] == expected
    item = (await api.get(uid, "/apostas/" + key)).json()
    assert (
        item["conta_contexto"]["id"] == str(account)
        and item["conta_contexto"]["apelido"] == "principal"
    )
    assert item["casa"] == "Betano" and item["evento"] == "Casa x Fora"
    assert "midia_hash" not in item and "usuario_id" not in item


async def test_expiry_preserves_financial_facts(api, owner, engine_app, como, monkeypatch):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    identity = str(uuid4())
    body = await intent(api, uid, key)
    result = (await api.post(uid, "/operacoes/" + identity, body)).json()
    frozen = datetime.now(UTC)
    before = await counts(engine_app, como, uid, key)
    monkeypatch.setattr(service, "now", lambda: frozen + timedelta(days=2))
    undo = await api.post(
        uid,
        "/operacoes/" + str(uuid4()) + "/desfazer",
        {"operacao_id": identity, "revisao_esperada": result["aposta"]["revisao"]},
    )
    assert undo.status_code == 410
    assert (await api.get(uid, "/operacoes/" + identity)).status_code == 200
    monkeypatch.setattr(service, "now", lambda: frozen + timedelta(days=91))
    assert (await api.get(uid, "/operacoes/" + identity)).status_code == 410
    assert (await api.post(uid, "/operacoes/" + identity, body)).status_code == 410
    assert before == await counts(engine_app, como, uid, key)


async def test_unauthenticated_private_get_and_write(api):
    for path in ("/fila", "/operacoes/" + str(uuid4())):
        response = await api.client.get(P + path)
        assert (
            response.status_code == 401 and response.headers["cache-control"] == "private, no-store"
        )
    assert (await api.client.post(P + "/operacoes/" + str(uuid4()), json={})).status_code == 401


async def test_read_only_preserves_session_reads_recovery_and_export(
    api, owner, engine_app, engine_admin, como
):
    from bancaemdia.main import app

    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    identity = str(uuid4())
    body = await intent(api, uid, key)
    committed = (await api.post(uid, "/operacoes/" + identity, body)).json()
    async with engine_admin.connect() as conn:
        outer = await conn.begin()
        previous = dict(app.dependency_overrides)
        try:
            await conn.execute(text("SELECT billing_activate_rollout()"))
            await conn.execute(text("SET LOCAL ROLE bancaemdia_app"))

            async def database():
                async with AsyncSession(
                    bind=conn, join_transaction_mode="create_savepoint"
                ) as session:
                    await session.execute(
                        text("SELECT set_config('app.current_user_id',:uid,true)"),
                        {"uid": str(uid)},
                    )
                    yield session

            app.dependency_overrides[get_db] = database
            app.dependency_overrides[get_db_primary_snapshot] = database
            assert (await api.get(uid, "/fila")).status_code == 200
            assert (await api.get(uid, "/apostas/" + key)).status_code == 200
            assert (await api.get(uid, "/operacoes/" + identity)).json() == committed
            assert (await api.post(uid, "/operacoes/" + identity, body)).status_code == 402
            assert (
                await api.post(
                    uid,
                    "/operacoes/" + str(uuid4()) + "/desfazer",
                    {"operacao_id": identity, "revisao_esperada": committed["aposta"]["revisao"]},
                )
            ).status_code == 402
            assert (
                await api.client.get("/api/v1/usuario/me/export", headers=api.headers(uid))
            ).status_code == 200
            assert (await api.get(uid, "/operacoes/" + identity)).status_code == 200
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await outer.rollback()


async def test_consolidation_invalidates_revision_and_excludes_contextual_source(
    api, owner, engine_app, como
):
    uid, account = owner
    house = await seed(engine_app, como, uid, account, origem="casa")
    contextual = await seed(engine_app, como, uid, account, origem="print")
    old_house = await intent(api, uid, house)
    old_context = await intent(api, uid, contextual)
    async with como(engine_app, uid) as session:
        ids = dict(
            (
                await session.execute(
                    text("SELECT chave,id FROM apostas WHERE usuario_id=:uid"), {"uid": uid}
                )
            ).all()
        )
        await session.execute(
            text("""INSERT INTO aposta_consolidacoes(usuario_id,casa_aposta_id,telegram_aposta_id,conta_casa_id,estado,decisao,versao,evidencia,contexto,ator)
          VALUES(:uid,:house,:context,:account,'active','reviewed','fixture','{}','{}','fixture')"""),
            {"uid": uid, "house": ids[house], "context": ids[contextual], "account": account},
        )
        await session.commit()
    assert (await api.get(uid, "/fila")).json()["total_elegivel"] == 1
    item = (await api.get(uid, "/apostas/" + contextual)).json()
    assert item["motivo_inelegivel"] == "contextual_source"
    for body in (old_house, old_context):
        assert (await api.post(uid, "/operacoes/" + str(uuid4()), body)).json()[
            "detail"
        ] == "result_revision_conflict"
    current_house = await intent(api, uid, house)
    result_id = str(uuid4())
    result = await api.post(uid, "/operacoes/" + result_id, current_house)
    assert result.status_code == 200, result.text
    current_context = await intent(api, uid, contextual)
    assert (await api.post(uid, "/operacoes/" + str(uuid4()), current_context)).status_code == 422
    async with como(engine_app, uid) as session:
        await session.execute(
            text("""UPDATE aposta_consolidacoes SET estado='unlinked',
          desvinculada_em=now(),motivo_desvinculacao='Fixture'
          WHERE usuario_id=:uid AND estado='active'"""),
            {"uid": uid},
        )
        await session.commit()
    before = await counts(engine_app, como, uid, house)
    obsolete = await api.post(
        uid,
        "/operacoes/" + str(uuid4()) + "/desfazer",
        {"operacao_id": result_id, "revisao_esperada": result.json()["aposta"]["revisao"]},
    )
    assert obsolete.status_code == 409 and obsolete.json()["detail"] == "result_revision_conflict"
    assert before == await counts(engine_app, como, uid, house)


async def test_same_identity_concurrent_delivery_commits_one_fact(api, owner, engine_app, como):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    identity = str(uuid4())
    body = await intent(api, uid, key)
    responses = await asyncio.gather(
        *(api.post(uid, "/operacoes/" + identity, body) for _ in range(2))
    )
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()
    assert (await counts(engine_app, como, uid, key))[0] == 2


async def test_busy_lock_and_unavailable_are_distinct_from_stale_revision(
    api, owner, engine_app, como, monkeypatch
):

    from bancaemdia.api.v1.results import run

    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    body = await intent(api, uid, key)
    async with como(engine_app, uid) as blocker:
        await blocker.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": f"{uid}:{key}"}
        )
        rejected = await api.post(uid, "/operacoes/" + str(uuid4()), body)
        assert rejected.status_code == 409 and rejected.json()["detail"] == "result_busy"
        assert rejected.headers["retry-after"] == "1"
    # The real closed connection produces a database exception, not a forged business conflict.
    async with como(engine_app, uid) as session:

        async def disconnected():
            await session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))

        response = await run(session, disconnected(), write=True)
        assert response.status_code == 503 and response.headers["retry-after"] == "5"
        assert json.loads(response.body)["detail"] == "result_unavailable"


async def test_migration_empty_roundtrip_and_populated_refusal(banco_migracao, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    monkeypatch.setenv("DATABASE_URL", banco_migracao.url_admin)
    engine = create_async_engine(banco_migracao.url_admin)
    uid = uuid4().int % 2**60
    try:
        await asyncio.to_thread(command.downgrade, config, "j6main2026")
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_user_id',:uid,true)"), {"uid": str(uid)}
            )
            await conn.execute(
                text("INSERT INTO usuarios(id,email,nome) VALUES(:uid,:email,'Fixture')"),
                {"uid": uid, "email": f"{uid}@test.invalid"},
            )
            bet_id = await conn.scalar(
                text(
                    "INSERT INTO apostas(usuario_id,chave,origem,stake_unidades,stake_centavos) VALUES(:uid,'migration','manual',1,10000) RETURNING id"
                ),
                {"uid": uid},
            )
            await conn.execute(
                text("""INSERT INTO result_operations(usuario_id,id,aposta_id,kind,fingerprint,before,receipt)
              VALUES(:uid,:id,:bet,'result','fixture','{}','{}')"""),
                {"uid": uid, "id": uuid4(), "bet": bet_id},
            )
            assert await conn.scalar(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname='result_operations'"
                )
            )
        with pytest.raises(Exception, match="preserve result intentions"):
            await asyncio.to_thread(command.downgrade, config, "j6main2026")
        async with engine.begin() as conn:
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "r200review2026"
            )
            await conn.execute(
                text(
                    "SELECT set_config('app.current_user_id',:uid,true),set_config('app.erase_user_data',:uid,true)"
                ),
                {"uid": str(uid)},
            )
            await conn.execute(text("DELETE FROM apostas WHERE usuario_id=:uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM usuarios WHERE id=:uid"), {"uid": uid})
        await asyncio.to_thread(command.downgrade, config, "j6main2026")
        await asyncio.to_thread(command.upgrade, config, "head")
    finally:
        await engine.dispose()


async def test_game_day_account_and_explicit_multicont_exception(
    api, owner, engine_admin, engine_app, como
):
    from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
    from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

    uid, old_account = owner
    async with como(engine_app, uid) as session:
        original = await ContaCasaRepo().get_by_id(session, uid, old_account)
        usage = await UsoContaCasaRepo().current_for_update(session, uid, original.casa_id)
        await UsoContaCasaRepo().close(session, usage.id, datetime(2026, 9, 21, tzinfo=UTC))
        second = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": uid,
                "casa_id": original.casa_id,
                "apelido": "Atual",
                "estado": "EM_USO",
            },
        )
        await UsoContaCasaRepo().open(
            session, uid, original.casa_id, second.id, datetime(2026, 9, 21, tzinfo=UTC)
        )
        await session.commit()
    for explicit, expected in [(None, old_account), (second.id, second.id)]:
        response = await api.client.post(
            "/api/v1/apostas",
            headers=api.headers(uid),
            json={
                "casa": "Betano",
                "odd": 2.0,
                "stake_unidades": 1.0,
                "data_aposta": "2026-10-10T12:00:00-03:00",
                "data_jogo": "2026-09-20T12:00:00-03:00",
                "conta_casa_ref": explicit,
            },
        )
        assert response.status_code == 201, response.text
        key = response.json()["aposta"]["chave"]
        body = await intent(api, uid, key)
        result = await api.post(uid, "/operacoes/" + str(uuid4()), body)
        assert result.status_code == 200, result.text
        assert result.json()["aposta"]["conta_contexto"]["id"] == str(expected)


async def test_account_erasure_removes_receipts_and_preimages(api, owner, engine_app, como):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    identity = str(uuid4())
    assert (
        await api.post(uid, "/operacoes/" + identity, await intent(api, uid, key))
    ).status_code == 200
    erased = await api.client.delete("/api/v1/usuario/me", headers=api.headers(uid))
    assert erased.status_code == 200, erased.text
    async with como(engine_app, uid) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM result_operations WHERE usuario_id=:uid"), {"uid": uid}
            )
            == 0
        )


async def test_cookie_csrf_and_real_revocation(
    api, banco, engine_admin, engine_app, como, tmp_path, monkeypatch
):
    from pydantic import SecretStr
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from bancaemdia.auth import middleware, transport
    from bancaemdia.auth.identity_crypto import digest
    from bancaemdia.auth.identity_service import IdentityService
    from tests.identity.support import key_settings

    role, password = "result_auth_" + uuid4().hex, uuid4().hex
    async with engine_admin.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"))
        await conn.execute(text(f"GRANT bancaemdia_auth TO {role}"))
    dburl = (
        make_url(banco.url_admin)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(dburl, poolclass=NullPool, hide_parameters=True)
    settings = key_settings(
        tmp_path,
        AUTH_ENABLED=True,
        AUTH_DATABASE_URL=SecretStr(dburl),
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
        async with AsyncSession(engine) as session, session.begin():
            user, identity = await service.provision(
                session,
                {
                    "iss": settings.OIDC_ISSUER,
                    "sub": uuid4().hex,
                    "email_verified": True,
                    "email": uuid4().hex + "@test.invalid",
                    "name": "Result fixture",
                },
            )
            sealed = service._credentials(
                sid, user, 0, {"refresh_token": "synthetic", "id_token": "synthetic"}
            )
            await session.execute(
                text(
                    "INSERT INTO auth_private.sessions(id,identity_id,cookie_hash,encrypted,expires_at,idle_until,access_until) VALUES(:id,:identity,:cookie,:encrypted,now()+interval '1 hour',now()+interval '1 hour',now()+interval '5 minutes')"
                ),
                {"id": sid, "identity": identity, "cookie": digest(cookie), "encrypted": sealed},
            )
        account = await _account(engine_admin, engine_app, como, user)
        bet_key = await seed(engine_app, como, user, account)
        headers = {
            "Cookie": settings.session_cookie + "=" + cookie,
            "Origin": "https://api.test:4443",
        }
        current = await api.client.get(P + "/apostas/" + bet_key, headers=headers)
        assert current.status_code == 200, current.text
        address = P + "/operacoes/" + str(uuid4())
        body = {"chave": bet_key, "revisao_esperada": current.json()["revisao"], "estado": "GREEN"}
        assert (await api.client.post(address, headers=headers, json=body)).status_code == 403
        headers["X-CSRF-Token"] = service.keys.csrf(cookie)
        accepted = await api.client.post(address, headers=headers, json=body)
        assert accepted.status_code == 200, accepted.text
        assert accepted.headers["Access-Control-Allow-Origin"] == "https://api.test:4443"
        assert (
            await api.client.get(address, headers={"Cookie": headers["Cookie"]})
        ).json() == accepted.json()
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE auth_private.sessions SET revoked_at=now() WHERE id=:id"), {"id": sid}
            )
        revoked = await api.client.get(address, headers={"Cookie": headers["Cookie"]})
        assert revoked.status_code == 401
        assert revoked.headers["cache-control"] == "private, no-store"
    finally:
        await engine.dispose()
        async with engine_admin.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))


async def test_detail_loads_revision_and_financial_eligibility_without_redundant_reads(
    api, owner, engine_app, como
):
    uid, account = owner
    key = await seed(engine_app, como, uid, account)
    before = await counts(engine_app, como, uid, key)
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and re.search(r"\bapostas\b", statement):
            queries.append(statement)

    event.listen(engine_app.sync_engine, "before_cursor_execute", capture)
    try:
        response = await api.get(uid, "/apostas/" + key)
    finally:
        event.remove(engine_app.sync_engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert response.json()["revisao"] == str(before[1].result_revision)
    assert response.json()["elegivel"] is True
    assert len(queries) == 2, queries  # Bet/revision/eligibility, then owned display context.
    assert await counts(engine_app, como, uid, key) == before
