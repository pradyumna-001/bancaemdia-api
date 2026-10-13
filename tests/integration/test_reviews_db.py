"""Required real HTTP/PostgreSQL/RLS acceptance, including the server-produced blocker."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.v1.apostas import _aplicar, _historico
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db_primary_snapshot
from bancaemdia.domain.materializar import EventoNovo, projetar
from bancaemdia.models.review_operation import ReviewOperation
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.services import reviews as service
from bancaemdia.workers import web_print as print_worker
from tests.integration.cruzamento import test_consolidacao as real
from tests.integration.test_aposta_foto_db import image_bytes
from tests.integration.test_results_db import api as original_api
from tests.integration.test_results_db import owner as owner
from tests.integration.test_results_db import seed
from tests.integration.test_web_print_db import asset_id, reading, submit

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]
P = "/api/v1/revisao-operacional"


@pytest.fixture
async def api(engine_app, monkeypatch):
    from bancaemdia.main import app

    async def snapshot():
        async with AsyncSession(
            engine_app.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            ),
            expire_on_commit=False,
        ) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id',:u,true)"),
                {"u": current_user_id.get() or ""},
            )
            yield session

    async for api in original_api.__wrapped__(engine_app, monkeypatch):
        app.dependency_overrides[get_db_primary_snapshot] = snapshot
        monkeypatch.setattr("bancaemdia.api.v1.web_print.celery.send_task", lambda *a, **kw: None)
        yield api


async def get(api, uid, path, **kw):
    return await api.client.get(P + path, headers=api.headers(uid), **kw)


async def post(api, uid, body, identity=None):
    return await api.client.post(
        P + "/operacoes/" + str(identity or uuid4()), headers=api.headers(uid), json=body
    )


async def view(api, uid, rid):
    response = await get(api, uid, "/revisoes/" + str(rid))
    assert response.status_code == 200, response.text
    return response.json()


async def decision(api, uid, rid, action="CORRIGIR", **kw):
    current = await view(api, uid, rid)
    return {"revisao_id": str(rid), "precondicao": current["precondicao"], "acao": action, **kw}


async def review(engine, como, uid, key, **changes):
    async with como(engine, uid) as session:
        raw = {"aposta_chave": key, **changes.pop("raw", {})}
        r = await RevisaoPendenteRepo().create(
            session,
            {
                "usuario_id": uid,
                "motivo": changes.pop("motivo", "leitura incompleta"),
                "extracao_bruta": raw,
                **changes,
            },
        )
        await session.commit()
        return r.id


async def ordinary(engine, como, uid, account, **changes):
    key = await seed(
        engine,
        como,
        uid,
        account,
        revisao_grave=True,
        revisao_motivo="leitura incompleta",
        **changes,
    )
    return key, await review(engine, como, uid, key)


async def evidence(engine, como, uid):
    async with como(engine, uid) as s:
        return {
            t: (
                await s.execute(
                    text(f"SELECT to_jsonb(t) FROM {t} t WHERE usuario_id=:u ORDER BY id"),
                    {"u": uid},
                )
            )
            .scalars()
            .all()
            for t in (
                "apostas",
                "eventos",
                "aposta_consolidacoes",
                "revisao_pendente",
                "review_operations",
            )
        }


async def server_pair(engine_app, engine_admin, como, uid, *, identity=True):
    house, ids = await real.accounts(engine_admin, engine_app, uid)
    ticket = "review-" + uuid4().hex
    hid = await real.intake_house(engine_app, uid, house, real.house_payload(ticket))
    async with como(engine_app, uid) as s:
        h = await s.get(models.Aposta, hid)
        user = await UsuarioRepo().get_by_id(s, uid)
        history = await _historico(s, uid, h.chave)
        e = EventoNovo("CORRECAO_MANUAL", "manual", {"tipster": "Tipster Casa"})
        state, _ = projetar([*history, (e.tipo, e.fonte, e.payload)])
        saved = await _aplicar(s, user, h.chave, [e], state)
        from bancaemdia.services.cruzamento_candidatos import generate

        await generate(s, uid, saved, state)
        await s.commit()
    tg = real.telegram_payload(ticket, identity=identity)
    tg["bilhete"]["tipster"] = "Tipster Telegram"
    tid = await real.intake_telegram(engine_app, uid, tg)
    async with como(engine_app, uid) as s:
        r = await s.scalar(
            select(models.RevisaoPendente).where(
                models.RevisaoPendente.usuario_id == uid,
                models.RevisaoPendente.motivo.in_([
                    "consolidacao_pendente",
                    "cruzamento_candidato",
                ]),
                models.RevisaoPendente.resolvido_em.is_(None),
            )
        )
        assert r is not None, "server must produce a real conflicting-tipster review"
        return r.id, hid, tid, ids[0]


@pytest.mark.parametrize("choice", ["casa", "telegram"])
async def test_server_conflicting_tipsters_publish_choices_and_one_financial_fact(
    api, engine_app, engine_admin, como, novo_usuario, choice
):
    uid = await novo_usuario()
    rid, _hid, _tid, account = await server_pair(engine_app, engine_admin, como, uid)
    before = await evidence(engine_app, como, uid)
    legacy = await api.client.post(
        f"/api/v1/revisao/{rid}/resolver", headers=api.headers(uid), json={"acao": "MESMA"}
    )
    assert legacy.status_code == 409 and "escolha explícita" in legacy.text
    assert await evidence(engine_app, como, uid) == before
    v = await view(api, uid, rid)
    assert v["familia"] == "consolidacao" and v["par"]["escolha_tipster_obrigatoria"]
    assert [o["fonte"] for o in v["par"]["tipster_opcoes"]] == ["casa", "telegram"]
    assert [o["tipster"] for o in v["par"]["tipster_opcoes"]] == [
        "Tipster Casa",
        "Tipster Telegram",
    ]
    missing = await post(
        api, uid, {"revisao_id": str(rid), "precondicao": v["precondicao"], "acao": "MESMA"}
    )
    assert missing.json() == {"detail": "review_tipster_required", "campo": "tipster_choice"}
    assert await evidence(engine_app, como, uid) == before
    response = await post(api, uid, await decision(api, uid, rid, "MESMA", tipster_choice=choice))
    assert response.status_code == 200, response.text
    totals, relations = await real.financial(engine_app, uid)
    assert totals["count"] == 1 and totals["stake"] == 10000 and totals["exposure"] == 10000
    assert len(relations) == 1 and relations[0].conta_casa_id == account
    assert relations[0].contexto["tipster"] == (
        "Tipster Casa" if choice == "casa" else "Tipster Telegram"
    )
    assert relations[0].evidencia["fontes"]["casa"]["tipster"] == "Tipster Casa"
    assert relations[0].evidencia["fontes"]["telegram"]["tipster"] == "Tipster Telegram"
    after = await evidence(engine_app, como, uid)
    assert all(e in after["eventos"] for e in before["eventos"])
    assert (await view(api, uid, rid))["acoes"] == []


@pytest.mark.parametrize("action", ["DISTINTAS", "RECUSAR_VINCULO"])
async def test_modern_reject_link_keeps_both_original_financial_facts(
    api, engine_app, engine_admin, como, novo_usuario, action
):
    uid = await novo_usuario()
    rid, *_ = await server_pair(engine_app, engine_admin, como, uid, identity=False)
    before = (await real.financial(engine_app, uid))[0]
    response = await post(api, uid, await decision(api, uid, rid, action))
    assert response.status_code == 200, response.text
    totals, relations = await real.financial(engine_app, uid)
    assert totals == before and totals["count"] == 2 and relations[0].estado == "rejected"
    assert all(b["selecionada"] for b in (await evidence(engine_app, como, uid))["apostas"])


async def test_legacy_resolver_accepts_explicit_tipster_without_ids(
    api, engine_app, engine_admin, como, novo_usuario
):
    uid = await novo_usuario()
    rid, *_ = await server_pair(engine_app, engine_admin, como, uid)
    response = await api.client.post(
        f"/api/v1/revisao/{rid}/resolver",
        headers=api.headers(uid),
        json={"acao": "MESMA", "tipster_choice": "telegram"},
    )
    assert response.status_code == 200, response.text
    assert (await real.financial(engine_app, uid))[0]["count"] == 1


@pytest.mark.parametrize("choice", ["casa", "telegram"])
async def test_explicit_side_never_borrows_other_tipster_identity(
    api, engine_app, engine_admin, como, novo_usuario, choice
):
    async with engine_admin.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tipsters(nome) VALUES('Tipster Casa'),('Tipster Telegram') ON CONFLICT DO NOTHING"
            )
        )
        rows = await c.execute(
            text("SELECT nome,id FROM tipsters WHERE nome IN ('Tipster Casa','Tipster Telegram')")
        )
        ids = dict(rows.all())
    uid = await novo_usuario()
    rid, *_ = await server_pair(engine_app, engine_admin, como, uid)
    v = await view(api, uid, rid)
    assert v["par"]["tipster_opcoes"][0]["tipster"] == "Tipster Casa"
    r = await post(api, uid, await decision(api, uid, rid, "MESMA", tipster_choice=choice))
    assert r.status_code == 200, r.text
    relation = (await real.financial(engine_app, uid))[1][0]
    name = "Tipster Casa" if choice == "casa" else "Tipster Telegram"
    assert relation.contexto["tipster_id"] == ids[name] and relation.contexto["tipster"] == name


@pytest.mark.parametrize("action", ["MESMA", "DISTINTAS"])
async def test_legacy_pending_pair_adopts_shared_fact_or_restores_distinct_tip(
    api, engine_app, engine_admin, como, novo_usuario, action
):
    uid = await novo_usuario()
    rid, hid, tid, *_ = await server_pair(engine_app, engine_admin, como, uid, identity=False)
    async with como(engine_app, uid) as s:
        old = await s.get(models.RevisaoPendente, rid)
        old.resolvido_em = datetime.now(UTC)
        h, t = await s.get(models.Aposta, hid), await s.get(models.Aposta, tid)
        user = await UsuarioRepo().get_by_id(s, uid)
        event = EventoNovo(
            "SELECAO_ALTERADA", "manual", {"selecionada": False, "duvida_de_par": True}
        )
        state, _ = projetar([
            *await _historico(s, uid, t.chave),
            (event.tipo, event.fonte, event.payload),
        ])
        await _aplicar(s, user, t.chave, [event], state)
        await s.execute(
            text("UPDATE apostas SET duvida_de_par=true WHERE usuario_id=:u AND id=:i"),
            {"u": uid, "i": tid},
        )
        r = await RevisaoPendenteRepo().create(
            s,
            {
                "usuario_id": uid,
                "motivo": "par legado pendente",
                "extracao_bruta": {"aposta_chave": t.chave, "parceira_suspeita": h.chave},
            },
        )
        await s.commit()
        rid = r.id
    v = await view(api, uid, rid)
    assert v["familia"] == "par_legado" and v["par"]["tipo"] == "legado"
    result = await post(
        api,
        uid,
        await decision(
            api, uid, rid, action, **({"tipster_choice": "telegram"} if action == "MESMA" else {})
        ),
    )
    assert result.status_code == 200, result.text
    totals, _ = await real.financial(engine_app, uid)
    assert totals["count"] == (1 if action == "MESMA" else 2)
    assert not any(b["duvida_de_par"] for b in (await evidence(engine_app, como, uid))["apostas"])


async def test_correction_with_private_photo_safe_fields_and_exact_money(
    api, owner, engine_app, como, novo_usuario
):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    content = image_bytes()
    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    async with como(engine_app, uid) as s:
        from bancaemdia.repositories.mensagem_repo import MidiaArquivoRepo, MidiaRepo

        await MidiaRepo().upsert_idempotent(s, digest, "image/png", len(content))
        await MidiaArquivoRepo().upsert_idempotent(s, digest, content)
        await s.execute(
            text(
                "UPDATE revisao_pendente SET midia_hash=:h,extracao_bruta=extracao_bruta||CAST(:raw AS jsonb) WHERE id=:i AND usuario_id=:u"
            ),
            {
                "h": digest,
                "i": rid,
                "u": uid,
                "raw": json.dumps({
                    "provider_debug": "secret-signed-url",
                    "ai_cost": 12,
                    "token": "private-secret",
                }),
            },
        )
        await s.commit()
    v = await view(api, uid, rid)
    assert v["familia"] == "leitura" and v["fontes"][0]["stake_centavos"] == "10000"
    assert all(
        x not in json.dumps(v)
        for x in ("provider_debug", "ai_cost", "private-secret", "extracao_bruta", "midia_hash")
    )
    photo = await api.client.get(v["foto_url"], headers=api.headers(uid))
    assert photo.status_code == 200 and photo.content == content
    other = await novo_usuario()
    assert (await api.client.get(v["foto_url"], headers=api.headers(other))).status_code == 404
    response = await post(api, uid, await decision(api, uid, rid, aposta_corrigida={"odd": 2.5}))
    assert response.status_code == 200, response.text
    assert (await view(api, uid, rid))["foto_url"] is None
    assert (await api.client.get(v["foto_url"], headers=api.headers(uid))).status_code == 404
    assert (await get(api, uid, "/fila")).json()["total_global"] == 0


@pytest.mark.parametrize("count", [0, 2])
async def test_account_absent_ambiguous_without_photo_no_implicit_choice(
    api, engine_app, engine_admin, como, novo_usuario, count
):
    uid = await novo_usuario()
    _house, ids = await real.accounts(engine_admin, engine_app, uid, count=count)
    key = await seed(engine_app, como, uid, None)
    if count == 2:
        # The current PostgreSQL constraint prevents introducing new overlapping
        # usage rows. Exercise the real ambiguity producer (historical evidence),
        # then reconcile against the authoritative matrix that is current now.
        from bancaemdia.domain.account_attribution import AccountUsage, resolve_account
        from bancaemdia.domain.account_review import sync_account_review

        result = resolve_account(
            datetime(2026, 9, 20, tzinfo=UTC), [AccountUsage(i, None, None) for i in ids]
        )
        assert result.status.value == "AMBIGUOUS"
        async with como(engine_app, uid) as s:
            await sync_account_review(s, uid, key, result)
            rid = (await RevisaoPendenteRepo().list_abertas_by_aposta_chave(s, uid, key))[0].id
            await s.commit()
    else:
        rid = await review(
            engine_app, como, uid, key, raw={"tipo_revisao": "conta", "status": "NONE"}
        )
    v = await view(api, uid, rid)
    assert v["familia"] == "conta" and v["foto_url"] is None
    assert datetime.fromisoformat(v["conta"]["instante"]) == datetime(2026, 9, 20, 15, tzinfo=UTC)
    assert len(v["conta"]["candidatos"]) == count
    assert v["conta"]["status"] == "NONE"
    assert v["conta"]["status_na_abertura"] == ("NONE" if count == 0 else "AMBIGUOUS")
    before = await evidence(engine_app, como, uid)
    result = await post(api, uid, await decision(api, uid, rid))
    assert result.status_code == 422
    assert await evidence(engine_app, como, uid) == before
    if count:
        result = await post(
            api, uid, await decision(api, uid, rid, aposta_corrigida={"conta_casa_id": ids[1]})
        )
        assert result.status_code == 200, result.text
    else:
        assert v["acoes"] == [] and v["conta"]["encaminhamento"] == "cadastrar_conta_na_casa"


async def test_historical_closed_no_holder_account_uses_game_day_and_explicit_exception(
    api, engine_app, engine_admin, como, novo_usuario
):
    uid = await novo_usuario()
    house, ids = await real.accounts(engine_admin, engine_app, uid, count=2)
    async with como(engine_app, uid) as s:
        from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

        use = await UsoContaCasaRepo().open(
            s, uid, house, ids[0], real.PLACEMENT - timedelta(days=1)
        )
        await UsoContaCasaRepo().close(s, use.id, real.PLACEMENT + timedelta(days=1))
        await UsoContaCasaRepo().open(s, uid, house, ids[1], real.PLACEMENT + timedelta(days=1))
        await s.execute(
            text(
                "UPDATE contas_casa SET estado='ENCERRADA',ativa=false WHERE id=:i AND usuario_id=:u"
            ),
            {"i": ids[0], "u": uid},
        )
        await s.commit()
    key = await seed(engine_app, como, uid, None, data_jogo=real.GAME.isoformat())
    rid = await review(engine_app, como, uid, key, raw={"tipo_revisao": "conta"})
    v = await view(api, uid, rid)
    assert v["conta"]["status"] == "UNIQUE"
    a, b = v["conta"]["candidatos"]
    assert a["estado"] == "ENCERRADA" and a["titular"] is None and a["elegivel_escolha_explicita"]
    assert not a["coincide_no_dia_do_jogo"] and b["coincide_no_dia_do_jogo"]
    result = await post(
        api, uid, await decision(api, uid, rid, aposta_corrigida={"conta_casa_id": ids[0]})
    )
    assert result.status_code == 200, result.text
    async with como(engine_app, uid) as s:
        state, _ = projetar(await _historico(s, uid, key))
        assert state["conta_casa_id"] == ids[0] and state["conta_casa_ref"] == ids[0]


@pytest.mark.parametrize("action", ["CORRIGIR", "DESCARTAR"])
async def test_unmaterialized_print_routes_to_existing_real_materializer(
    api, engine_app, engine_admin, como, novo_usuario, monkeypatch, action
):
    uid = await novo_usuario()
    await real.accounts(engine_admin, engine_app, uid)
    monkeypatch.setattr(print_worker, "extrair_bilhete", lambda *a, **kw: reading(stake=None))
    i, item, r = await submit(api, uid, image_bytes())
    assert r.status_code == 202, r.text
    asset = await asset_id(engine_app, como, uid, i, item)
    assert await print_worker.process_once(engine_app, uid, asset)
    async with como(engine_app, uid) as s:
        review = await s.scalar(
            select(models.RevisaoPendente).where(
                models.RevisaoPendente.usuario_id == uid,
                models.RevisaoPendente.resolvido_em.is_(None),
            )
        )
        assert review is not None
        rid = review.id
    v = await view(api, uid, rid)
    assert (
        v["familia"] == "print_incompleto"
        and v["encaminhamento"] == "resolver_print"
        and v["fontes"] == []
    )
    photo = await api.client.get(v["foto_url"], headers=api.headers(uid))
    assert photo.status_code == 200
    fields = {"stake_unidades": 1.0, "odd": 2.0} if action == "CORRIGIR" else None
    response = await post(
        api,
        uid,
        await decision(api, uid, rid, action, **({"aposta_corrigida": fields} if fields else {})),
    )
    assert response.status_code == 200, response.text
    assert len((await evidence(engine_app, como, uid))["apostas"]) == (
        1 if action == "CORRIGIR" else 0
    )
    assert (response.json()["aposta_chave"] is None) == (action == "DESCARTAR")


async def test_unknown_reason_explicit_support_without_arbitrary_actions(
    api, owner, engine_app, como
):
    uid, _ = owner
    rid = await review(engine_app, como, uid, "missing", motivo="new-family secret provider dump")
    v = await view(api, uid, rid)
    assert v["familia"] == "nao_suportada" and v["acoes"] == [] and v["encaminhamento"] == "suporte"
    assert "secret" not in json.dumps(v)
    assert (await post(api, uid, await decision(api, uid, rid))).json()[
        "detail"
    ] == "review_unsupported"


async def test_opened_interval_brazil_bounds_filtered_counts_and_consumption_restart(
    api, owner, engine_app, como
):
    uid, account = owner
    ids = []
    for hour in (12, 13, 14):
        _key, rid = await ordinary(engine_app, como, uid, account)
        async with como(engine_app, uid) as s:
            await s.execute(
                text("UPDATE revisao_pendente SET criado_em=:t WHERE usuario_id=:u AND id=:i"),
                {"t": datetime(2026, 10, 1, hour, tzinfo=UTC), "u": uid, "i": rid},
            )
            await s.commit()
        ids.append(rid)
    await review(engine_app, como, uid, "missing")
    params = {
        "familia": "leitura",
        "desde": "2026-10-01T09:00:00",
        "ate": "2026-10-01T11:00:00",
        "page_size": 1,
    }
    page = (await get(api, uid, "/fila", params=params)).json()
    assert page["total_filtrado"] == 2 and page["total_global"] == 4
    assert page["por_familia_filtrado"] == {"leitura": 2} and page["data"][0]["id"] == str(ids[0])
    while True:
        page = (await get(api, uid, "/fila", params={"familia": "leitura", "page_size": 1})).json()
        if not page["data"]:
            break
        v = page["data"][0]
        r = await post(
            api, uid, {"revisao_id": v["id"], "precondicao": v["precondicao"], "acao": "CORRIGIR"}
        )
        assert r.status_code == 200, r.text
    assert (await get(api, uid, "/fila", params={"familia": "leitura"})).json()[
        "total_filtrado"
    ] == 0
    assert (await get(api, uid, "/fila", params={"page": 50})).json()["total_global"] == 1


@pytest.mark.parametrize(
    "writer", ["raw_bet", "event", "review", "account", "usage", "legacy_endpoint"]
)
async def test_stale_evidence_legacy_intermediate_writers_no_partial_effects(
    api, owner, engine_app, como, writer
):
    uid, account = owner
    key, rid = await ordinary(engine_app, como, uid, account)
    body = await decision(api, uid, rid)
    if writer == "legacy_endpoint":
        r = await api.client.patch(
            "/api/v1/apostas/" + key, headers=api.headers(uid), json={"odd": 2.75}
        )
        assert r.status_code == 200, r.text
    else:
        async with como(engine_app, uid) as s:
            if writer == "raw_bet":
                await s.execute(
                    text("UPDATE apostas SET odd=odd WHERE usuario_id=:u AND chave=:k"),
                    {"u": uid, "k": key},
                )
            elif writer == "event":
                from bancaemdia.repositories.evento_repo import EventoRepo

                await EventoRepo().append(
                    s,
                    {
                        "usuario_id": uid,
                        "tipo": "CORRECAO_MANUAL",
                        "fonte": "manual",
                        "payload_json": {},
                        "aposta_chave": key,
                        "chat_id": None,
                        "message_id": None,
                        "confianca": None,
                    },
                )
            elif writer == "review":
                await s.execute(
                    text("UPDATE revisao_pendente SET motivo=motivo WHERE id=:i AND usuario_id=:u"),
                    {"i": rid, "u": uid},
                )
            elif writer == "account":
                await s.execute(
                    text("UPDATE contas_casa SET apelido=apelido WHERE id=:i AND usuario_id=:u"),
                    {"i": account, "u": uid},
                )
            else:
                await s.execute(
                    text("UPDATE usos_conta_casa SET vigente_de=vigente_de WHERE usuario_id=:u"),
                    {"u": uid},
                )
            await s.commit()
    before = await evidence(engine_app, como, uid)
    response = await post(api, uid, body)
    assert response.status_code == 409 and response.json()["detail"] == "review_evidence_changed", (
        response.text
    )
    assert await evidence(engine_app, como, uid) == before


async def test_concurrent_decisions_close_once_and_duplicate_intention_recovers(
    api, owner, engine_app, como
):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    body = await decision(api, uid, rid)
    ident = uuid4()
    responses = await asyncio.gather(post(api, uid, body, ident), post(api, uid, body, ident))
    assert any(r.status_code == 200 for r in responses), [r.text for r in responses]
    saved = next(r for r in responses if r.status_code == 200).json()
    assert (await post(api, uid, body, ident)).json() == saved
    assert (await get(api, uid, "/operacoes/" + str(ident))).json() == saved
    before = await evidence(engine_app, como, uid)
    assert (await post(api, uid, {**body, "acao": "DESCARTAR"}, ident)).json()[
        "detail"
    ] == "review_intention_conflict"
    assert (await post(api, uid, body)).json()["detail"] == "review_already_resolved"
    assert await evidence(engine_app, como, uid) == before
    assert sum(e["tipo"] == "REVISAO_RESOLVIDA" for e in before["eventos"]) == 1


async def test_real_response_lost_after_commit_get_receipt_without_replay(
    api, owner, engine_app, como
):
    from bancaemdia.main import app

    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    body, ident = await decision(api, uid, rid), uuid4()
    captured = bytearray()

    async def disconnect(scope, receive, send):
        async def collect(message):
            if message["type"] == "http.response.body":
                captured.extend(message.get("body", b""))
            await send(message)

        await app(scope, receive, collect)
        raise httpx.ReadError("response lost after commit")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disconnect), base_url="https://api.test"
    ) as lost:
        with pytest.raises(httpx.ReadError):
            await lost.post(P + "/operacoes/" + str(ident), headers=api.headers(uid), json=body)
    assert json.loads(captured)["operacao_id"] == str(ident)
    before = await evidence(engine_app, como, uid)
    assert (await get(api, uid, "/operacoes/" + str(ident))).json() == json.loads(captured)
    assert await evidence(engine_app, como, uid) == before


async def test_legacy_writer_resolution_not_attributed_to_unknown_intention(
    api, owner, engine_app, como
):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    body = await decision(api, uid, rid)
    r = await api.client.post(
        f"/api/v1/revisao/{rid}/resolver", headers=api.headers(uid), json={"acao": "CORRIGIR"}
    )
    assert r.status_code == 200, r.text
    assert (await post(api, uid, body)).json()["detail"] == "review_already_resolved"
    assert (await get(api, uid, "/operacoes/" + str(uuid4()))).status_code == 404


async def test_domain_busy_has_retry_after_and_no_changes(api, owner, engine_app, como):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    body = await decision(api, uid, rid)
    before = await evidence(engine_app, como, uid)
    async with como(engine_app, uid) as blocker:
        await blocker.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"), {"k": f"pareador:{uid}"}
        )
        r = await post(api, uid, body)
        assert r.json()["detail"] == "review_busy" and r.headers["Retry-After"] == "1"
    assert await evidence(engine_app, como, uid) == before


async def test_other_owner_read_write_recovery_media_and_forced_rls(
    api, owner, engine_app, como, novo_usuario
):
    uid, account = owner
    other = await novo_usuario()
    _key, rid = await ordinary(engine_app, como, uid, account)
    body, ident = await decision(api, uid, rid), uuid4()
    assert (await get(api, other, "/revisoes/" + str(rid))).status_code == 404
    assert (await post(api, other, body)).status_code == 404
    assert (
        await api.client.get(f"/api/v1/revisao/{rid}/foto", headers=api.headers(other))
    ).status_code == 404
    assert (await post(api, uid, body, ident)).status_code == 200
    assert (await get(api, other, "/operacoes/" + str(ident))).status_code == 404
    async with como(engine_app, other) as s:
        assert await s.get(ReviewOperation, (uid, ident)) is None
    assert (await get(api, other, "/fila")).json()["total_global"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"odd": -1},
        {"conta_casa_id": True},
        {"provider_token": "private-secret"},
        {"revisao_grave": False},
    ],
)
async def test_invalid_correction_safe_code_field_no_internal_dumps(
    api, owner, engine_app, como, fields
):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    before = await evidence(engine_app, como, uid)
    r = await post(api, uid, await decision(api, uid, rid, aposta_corrigida=fields))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "review_correction_invalid"
    assert "private-secret" not in r.text and all(
        x not in r.json() for x in ("input", "ctx", "msg")
    )
    assert await evidence(engine_app, como, uid) == before


@pytest.mark.parametrize(
    "query",
    [
        {"idade_dias": 7},
        {"desde": "2026-10-02T12:00:00Z", "ate": "2026-10-01T12:00:00Z"},
        {"familia": "future"},
    ],
)
async def test_invalid_or_ignored_age_filters_are_refused(api, owner, query):
    r = await get(api, owner[0], "/fila", params=query)
    assert r.status_code == 422 and r.json()["detail"] == "review_validation_invalid"
    assert set(r.json()) == {"detail", "campo"}


async def test_401_reauth_without_mutation(api, owner, engine_app, como):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    body = await decision(api, uid, rid)
    before = await evidence(engine_app, como, uid)
    assert (await api.client.get(P + "/fila")).status_code == 401
    response = await api.client.post(P + "/operacoes/" + str(uuid4()), json=body)
    assert response.status_code == 401 and response.headers["cache-control"] == "private, no-store"
    assert await evidence(engine_app, como, uid) == before


async def test_402_preserves_reads_receipt_and_form_without_payment_replay(
    api, owner, engine_app, engine_admin, como
):
    from bancaemdia.db.session import get_db
    from bancaemdia.main import app

    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    body = await decision(api, uid, rid)
    ident = uuid4()
    saved = await post(api, uid, body, ident)
    assert saved.status_code == 200, saved.text
    _key, rid2 = await ordinary(engine_app, como, uid, account)
    pending = await decision(api, uid, rid2)
    async with engine_admin.connect() as conn:
        outer = await conn.begin()
        old = dict(app.dependency_overrides)
        try:
            await conn.execute(text("SELECT billing_activate_rollout()"))
            await conn.execute(text("SET LOCAL ROLE bancaemdia_app"))

            async def database():
                async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
                    await s.execute(
                        text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(uid)}
                    )
                    yield s

            app.dependency_overrides[get_db] = database
            app.dependency_overrides[get_db_primary_snapshot] = database
            assert (await get(api, uid, "/fila")).status_code == 200
            assert (await view(api, uid, rid2))["estado"] == "aberta"
            assert (await get(api, uid, "/operacoes/" + str(ident))).json() == saved.json()
            assert (await post(api, uid, pending)).status_code == 402
            assert (await post(api, uid, body, ident)).status_code == 402
            assert (await get(api, uid, "/revisoes/" + str(rid2))).status_code == 200
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(old)
            await outer.rollback()
    assert (await view(api, uid, rid2))["estado"] == "aberta"


@pytest.mark.parametrize("invalid", ["wrong_owner", "wrong_house"])
async def test_explicit_account_string_revalidated_safe_error(
    api, owner, engine_app, engine_admin, como, novo_usuario, invalid
):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    foreign = await novo_usuario()
    _, ids = await real.accounts(engine_admin, engine_app, foreign)
    ident = ids[0]
    if invalid == "wrong_house":
        async with engine_admin.begin() as c:
            house = await c.scalar(
                text("INSERT INTO casas(nome,dominio) VALUES(:n,:d) RETURNING id"),
                {"n": uuid4().hex, "d": uuid4().hex + ".test"},
            )
        async with como(engine_app, uid) as s:
            ident = await s.scalar(
                text(
                    "INSERT INTO contas_casa(usuario_id,casa_id,apelido) VALUES(:u,:h,'Other house') RETURNING id"
                ),
                {"u": uid, "h": house},
            )
            await s.commit()
    body = await decision(api, uid, rid, conta_casa_id=str(ident))
    before = await evidence(engine_app, como, uid)
    r = await post(api, uid, body)
    assert r.status_code == 422 and r.json()["campo"] == "conta_casa_id", r.text
    assert await evidence(engine_app, como, uid) == before


async def test_account_explicit_string_exact_bigint_not_coerced_by_client(
    api, owner, engine_app, como
):
    uid, account = owner
    big = 2**53 + 3
    async with como(engine_app, uid) as s:
        await s.execute(
            text(
                "INSERT INTO contas_casa(id,usuario_id,casa_id,apelido) SELECT :b,usuario_id,casa_id,'Large exact ID' FROM contas_casa WHERE id=:a AND usuario_id=:u"
            ),
            {"b": big, "a": account, "u": uid},
        )
        await s.commit()
    key = await seed(engine_app, como, uid, None)
    rid = await review(engine_app, como, uid, key, raw={"tipo_revisao": "conta"})
    v = await view(api, uid, rid)
    assert any(c["id"] == str(big) for c in v["conta"]["candidatos"])
    result = await post(api, uid, await decision(api, uid, rid, conta_casa_id=str(big)))
    assert result.status_code == 200, result.text
    async with como(engine_app, uid) as s:
        assert (
            await s.scalar(
                text("SELECT conta_casa_id FROM apostas WHERE usuario_id=:u AND chave=:k"),
                {"u": uid, "k": key},
            )
            == big
        )


@pytest.mark.parametrize("change", ["candidate", "competitor", "link", "dictionary"])
async def test_pair_and_context_changes_invalidate_seen_decision(
    api, engine_app, engine_admin, como, novo_usuario, change
):
    uid = await novo_usuario()
    rid, hid, tid, account = await server_pair(engine_app, engine_admin, como, uid)
    body = await decision(api, uid, rid, "MESMA", tipster_choice="telegram")
    if change == "competitor":
        await seed(engine_app, como, uid, account)
    elif change == "dictionary":
        async with engine_admin.begin() as c:
            await c.execute(
                text("INSERT INTO tipsters(nome) VALUES(:n)"), {"n": "Context " + uuid4().hex}
            )
    else:
        async with como(engine_app, uid) as s:
            if change == "candidate":
                await s.execute(
                    text("UPDATE cruzamento_candidatos SET score=score WHERE usuario_id=:u"),
                    {"u": uid},
                )
            else:
                from bancaemdia.domain.consolidacao_aposta import reject_pair

                await reject_pair(s, uid, hid, tid, actor_id=uid, reason="independent writer")
            await s.commit()
    before = await evidence(engine_app, como, uid)
    r = await post(api, uid, body)
    assert r.status_code == 409 and r.json()["detail"] in {
        "review_evidence_changed",
        "review_already_resolved",
    }, r.text
    assert await evidence(engine_app, como, uid) == before


async def test_atomic_rollback_when_receipt_flush_fails_in_real_database(
    api, owner, engine_app, como, monkeypatch
):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    body = await decision(api, uid, rid, aposta_corrigida={"odd": 3.0})
    before = await evidence(engine_app, como, uid)
    original = AsyncSession.flush

    async def failure(session, *a, **kw):
        if any(isinstance(obj, ReviewOperation) for obj in session.new):
            await session.execute(text("SELECT 1/0"))
        return await original(session, *a, **kw)

    monkeypatch.setattr(AsyncSession, "flush", failure)
    r = await post(api, uid, body)
    assert r.status_code == 503, r.text
    assert await evidence(engine_app, como, uid) == before


@pytest.mark.parametrize("invalid", ["mime", "size", "corrupt"])
async def test_private_review_media_mime_size_and_corruption_limits(
    api, owner, engine_app, como, invalid
):
    import hashlib

    from bancaemdia.repositories.mensagem_repo import MidiaArquivoRepo, MidiaRepo

    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    content = (
        b"broken raster"
        if invalid == "corrupt"
        else b"x" * (20 * 1024 * 1024 + 1)
        if invalid == "size"
        else image_bytes()
    )
    mime = "text/html" if invalid == "mime" else "image/png"
    digest = hashlib.sha256(content).hexdigest()
    async with como(engine_app, uid) as s:
        await MidiaRepo().upsert_idempotent(s, digest, mime, len(content))
        await MidiaArquivoRepo().upsert_idempotent(s, digest, content)
        await s.execute(
            text("UPDATE revisao_pendente SET midia_hash=:h WHERE id=:i AND usuario_id=:u"),
            {"h": digest, "i": rid, "u": uid},
        )
        await s.commit()
    v = await view(api, uid, rid)
    r = await api.client.get(v["foto_url"], headers=api.headers(uid))
    assert r.status_code == 404 and r.headers["cache-control"] == "private, no-store"


async def test_receipt_expiry_immutable_audit_and_forced_rls(
    api, owner, engine_app, como, monkeypatch
):
    from sqlalchemy.exc import DBAPIError

    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account)
    ident = uuid4()
    assert (await post(api, uid, await decision(api, uid, rid), ident)).status_code == 200
    before = await evidence(engine_app, como, uid)
    async with como(engine_app, uid) as s:
        assert await s.scalar(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname='review_operations'"
            )
        )
        with pytest.raises(DBAPIError, match="preserve result intention audit"):
            await s.execute(
                text("UPDATE review_operations SET fingerprint='x' WHERE usuario_id=:u AND id=:i"),
                {"u": uid, "i": ident},
            )
    clock = service.now()
    monkeypatch.setattr(service, "now", lambda: clock + timedelta(days=91))
    assert (await get(api, uid, "/operacoes/" + str(ident))).status_code == 410
    assert await evidence(engine_app, como, uid) == before


async def test_unsupported_family_even_when_bet_is_under_review(api, owner, engine_app, como):
    uid, account = owner
    key = await seed(engine_app, como, uid, account, revisao_grave=True)
    rid = await review(engine_app, como, uid, key, raw={"tipo_revisao": "future_family"})
    v = await view(api, uid, rid)
    assert v["familia"] == "nao_suportada" and v["acoes"] == []


async def test_null_return_and_freebet_face_preserved(api, owner, engine_app, como):
    uid, account = owner
    key, rid = await ordinary(engine_app, como, uid, account, freebet=True)
    v = await view(api, uid, rid)
    source = v["fontes"][0]
    assert (
        source["stake_centavos"] == "0"
        and source["valor_aposta_centavos"] == "10000"
        and source["retorno_centavos"] is None
    )
    assert (await post(api, uid, await decision(api, uid, rid))).status_code == 200
    async with como(engine_app, uid) as s:
        state, _ = projetar(await _historico(s, uid, key))
        assert state.get("retorno_centavos") is None and state["freebet"]


@pytest.mark.parametrize("field", ["odd", "stake_centavos"])
async def test_fresh_read_of_row_only_writer_cannot_overwrite_financial_evidence(
    api, owner, engine_app, como, field
):
    uid, account = owner
    key, rid = await ordinary(engine_app, como, uid, account)
    async with como(engine_app, uid) as s:
        if field == "odd":
            await s.execute(
                text("UPDATE apostas SET odd=3.0 WHERE usuario_id=:u AND chave=:k"),
                {"u": uid, "k": key},
            )
        else:
            await s.execute(
                text("UPDATE apostas SET stake_centavos=50000 WHERE usuario_id=:u AND chave=:k"),
                {"u": uid, "k": key},
            )
        await s.commit()
    v = await view(api, uid, rid)
    assert v["acoes"] == [] and v["encaminhamento"] == "suporte"
    before = await evidence(engine_app, como, uid)
    r = await post(
        api, uid, await decision(api, uid, rid, aposta_corrigida={"descricao": "Nova descrição"})
    )
    assert r.status_code == 409 and r.json()["detail"] == "review_projection_conflict", r.text
    assert await evidence(engine_app, como, uid) == before


async def test_unknown_legacy_reading_stake_remains_null_until_human_completion(
    api, owner, engine_app, como
):
    uid, account = owner
    _key, rid = await ordinary(engine_app, como, uid, account, stake_unidades=None, odd=None)
    v = await view(api, uid, rid)
    assert (
        v["fontes"][0]["stake_unidades"] is None
        and v["fontes"][0]["stake_centavos"] is None
        and v["fontes"][0]["valor_aposta_centavos"] is None
    )
    before = await evidence(engine_app, como, uid)
    r = await post(api, uid, await decision(api, uid, rid))
    assert r.status_code == 422 and r.json()["detail"] == "review_correction_invalid", r.text
    assert await evidence(engine_app, como, uid) == before
    r = await post(
        api,
        uid,
        await decision(api, uid, rid, aposta_corrigida={"stake_unidades": 1.0, "odd": 2.0}),
    )
    assert r.status_code == 200, r.text
    closed = await view(api, uid, rid)
    assert closed["familia"] == "leitura" and closed["estado"] == "resolvida"


async def test_account_choice_does_not_approve_independent_extraction_review(
    api, owner, engine_app, como
):
    uid, account = owner
    key, reading_rid = await ordinary(
        engine_app, como, uid, account, duvida_de_par=True, selecionada=False
    )
    rid = await review(engine_app, como, uid, key, raw={"tipo_revisao": "conta"})
    r = await post(api, uid, await decision(api, uid, rid, conta_casa_id=str(account)))
    assert r.status_code == 200, r.text
    assert (await view(api, uid, reading_rid))["estado"] == "aberta"
    async with como(engine_app, uid) as s:
        assert not await s.scalar(
            text("SELECT selecionada FROM apostas WHERE usuario_id=:u AND chave=:k"),
            {"u": uid, "k": key},
        )
        assert await s.scalar(
            text("SELECT revisao_grave FROM apostas WHERE usuario_id=:u AND chave=:k"),
            {"u": uid, "k": key},
        )


async def test_review_cookie_csrf_and_real_revocation(
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

    role, password = "review_auth_" + uuid4().hex, uuid4().hex
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
        _, accounts = await real.accounts(engine_admin, engine_app, user)
        account = accounts[0]
        _bet_key, rid = await ordinary(engine_app, como, user, account)
        headers = {
            "Cookie": settings.session_cookie + "=" + cookie,
            "Origin": "https://api.test:4443",
        }
        current = await api.client.get(P + "/revisoes/" + str(rid), headers=headers)
        assert current.status_code == 200, current.text
        address = P + "/operacoes/" + str(uuid4())
        body = {
            "revisao_id": str(rid),
            "precondicao": current.json()["precondicao"],
            "acao": "CORRIGIR",
        }
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
