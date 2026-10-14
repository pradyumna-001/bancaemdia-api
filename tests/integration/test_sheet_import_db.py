"""Mandatory real API/PostgreSQL/materializer proofs, without prepared financial results."""

import asyncio
import io
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.auth import middleware as authentication
from bancaemdia.auth.identity_config import identity_settings
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db
from bancaemdia.domain.sheet_xlsx import FIELDS
from bancaemdia.models.sheet_import import SheetPreview
from bancaemdia.services import sheet_import as service
from tests.integration.test_telegram_confirmation import _account

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]
PREFIX = "/api/v1/planilhas"
HEADERS = list(FIELDS)


async def test_migration_refuses_populated_downgrade_and_restores_empty_guards(
    banco_migracao: Any, monkeypatch: Any
) -> None:
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
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_user_id',:uid,true)"), {"uid": str(uid)}
            )
            await conn.execute(
                text("INSERT INTO usuarios(id,email,nome) VALUES(:uid,:email,'Migration fixture')"),
                {"uid": uid, "email": f"{uid}@test.invalid"},
            )
            await conn.execute(
                text(
                    "INSERT INTO sheet_sources(usuario_id,id,nome,version) VALUES(:uid,:id,'Fixture',0)"
                ),
                {"uid": uid, "id": uuid4()},
            )
        with pytest.raises(RuntimeError, match="identities cannot be removed"):
            await asyncio.to_thread(command.downgrade, config, "j6main2026")
        async with engine.begin() as conn:
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "u200sandbox2026"
            )
            await conn.execute(text("DELETE FROM usuarios WHERE id=:uid"), {"uid": uid})
        await asyncio.to_thread(command.downgrade, config, "j6main2026")
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON t.tgrelid=c.oid WHERE c.relname IN ('sheet_sources','sheet_previews','sheet_confirmations') AND t.tgname='billing_write_guard' AND t.tgtype=31"
                    )
                )
                == 3
            )
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_class WHERE relname IN ('sheet_sources','sheet_previews','sheet_confirmations') AND relrowsecurity AND relforcerowsecurity"
                    )
                )
                == 3
            )
    finally:
        await engine.dispose()


def xlsx(
    rows: list[list[Any]] | None = None,
    headers: list[str] | None = None,
    sheet: str = "Apostas",
    sheets: int = 1,
) -> bytes:
    workbook = Workbook()
    workbook.active.title = sheet
    for index in range(sheets):
        target = workbook.active if index == 0 else workbook.create_sheet(f"Aba{index}")
        target.append(headers or HEADERS)
        for row in rows or [values()]:
            target.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def values(identity: str = "BET-001", **changes: Any) -> list[Any]:
    fields = {
        "linha_id": identity,
        "casa": "Betano",
        "data_aposta": "2026-09-20T12:00:00-03:00",
        "data_jogo": "2026-09-22T12:00:00-03:00",
        "odd": 2.0,
        "stake_unidades": 1.0,
        "atualizada_em": "2026-10-12T12:00:00-03:00",
        "evento": "Casa x Fora",
        "descricao": None,
        "mercado_bruto": None,
        "freebet": False,
        "conta_casa_ref": None,
        **changes,
    }
    return [fields[field] for field in FIELDS]


@dataclass
class Api:
    client: httpx.AsyncClient
    key: Any

    def headers(self: Any, uid: int) -> dict[str, str]:
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
            headers={"kid": "sheet-test"},
        )
        return {"Authorization": "Bearer " + token}


@pytest.fixture
async def api(engine_app: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    from bancaemdia.api.v1 import sheet_import
    from bancaemdia.main import app

    monkeypatch.setenv("AUTH_ENABLED", "false")
    identity_settings.cache_clear()
    get_settings.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="sheet-test", alg="RS256", use="sig")
    cache = JWKSCache(None, "RS256")
    cache.keys = {"sheet-test": public}
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(authentication, "get_jwks_cache", lambda: cache)
    monkeypatch.setattr(sheet_import, "engine", engine_app)

    async def database() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id',:uid,true)"),
                {"uid": current_user_id.get() or ""},
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


async def source(api: Api, uid: int) -> UUID:
    identity = uuid4()
    result = await api.client.put(
        f"{PREFIX}/fontes/{identity}", json={"nome": "Fonte sintética"}, headers=api.headers(uid)
    )
    assert result.status_code == 200, result.text
    return identity


async def upload(
    api: Api, uid: int, source_id: UUID, content: bytes | None = None, identity: UUID | None = None
) -> httpx.Response:
    return await api.client.post(
        f"{PREFIX}/fontes/{source_id}/previas/{identity or uuid4()}",
        headers=api.headers(uid),
        files={
            "arquivo": (
                "../../synthetic.xlsx",
                xlsx() if content is None else content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


async def confirm(
    api: Api, uid: int, preview: dict[str, Any], identity: UUID | None = None
) -> httpx.Response:
    return await api.client.post(
        f"{PREFIX}/confirmacoes/{identity or uuid4()}",
        json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]},
        headers=api.headers(uid),
    )


async def snapshot(engine: AsyncEngine, como: Any, uid: int) -> str:
    async with como(engine, uid) as session:
        value = await service.fingerprint(session, uid)
        assert isinstance(value, str)
        return value


async def test_preview_remap_and_read_pages_have_zero_financial_effects(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    before = await snapshot(engine_app, como, uid)
    preview = await upload(api, uid, sid, xlsx([values(), values("BET-002")]))
    assert preview.status_code == 201, preview.text
    state = preview.json()
    assert state["counts"]["criadas"] == 2 and state["total_rows"] == 2
    assert preview.headers["cache-control"] == "private, no-store"
    assert state["state"] == "ready"
    remap = await api.client.post(
        f"{PREFIX}/previas/{state['id']}/remapear/{uuid4()}",
        headers=api.headers(uid),
        json={
            "expected_revision": state["revision"],
            "sheet": "Apostas",
            "mapping": state["mapping"],
        },
    )
    assert remap.status_code == 201, remap.text
    for page in (1, 2, 3):
        rows = await api.client.get(
            f"{PREFIX}/previas/{remap.json()['id']}/linhas",
            headers=api.headers(uid),
            params={"page": page, "page_size": 1},
        )
        assert rows.status_code == 200 and rows.json()["total"] == 2
        assert len(rows.json()["data"]) == (1 if page <= 2 else 0)
    assert await snapshot(engine_app, como, uid) == before


async def test_swapped_columns_are_corrected_without_losing_invalid_values(
    api: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    row = values()
    row[HEADERS.index("casa")], row[HEADERS.index("odd")] = (
        row[HEADERS.index("odd")],
        row[HEADERS.index("casa")],
    )
    preview = (await upload(api, uid, sid, xlsx([row]))).json()
    assert preview["state"] == "invalid" and preview["counts"]["recusadas"] == 1
    rows = (
        await api.client.get(f"{PREFIX}/previas/{preview['id']}/linhas", headers=api.headers(uid))
    ).json()["data"]
    assert rows[0]["raw"][HEADERS.index("odd")] == "Betano"
    mapping = preview["mapping"]
    mapping["casa"], mapping["odd"] = mapping["odd"], mapping["casa"]
    fixed = await api.client.post(
        f"{PREFIX}/previas/{preview['id']}/remapear/{uuid4()}",
        headers=api.headers(uid),
        json={"expected_revision": preview["revision"], "sheet": "Apostas", "mapping": mapping},
    )
    assert fixed.status_code == 201 and fixed.json()["state"] == "ready", fixed.text
    applied = await confirm(api, uid, fixed.json())
    assert applied.status_code == 200 and applied.json()["counts"]["criadas"] == 1, applied.text


async def test_valid_confirmation_real_materializer_lost_response_and_same_intent(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    preview = (await upload(api, uid, sid)).json()
    identity = uuid4()
    missing = await api.client.get(f"{PREFIX}/confirmacoes/{identity}", headers=api.headers(uid))
    assert missing.status_code == 404
    applied = await confirm(api, uid, preview, identity)
    assert applied.status_code == 200, applied.text
    assert applied.json()["state"] == "completed" and applied.json()["atomic"]
    assert applied.json()["rows"][0]["review_ids"]
    before = await snapshot(engine_app, como, uid)
    recovered = await api.client.get(f"{PREFIX}/confirmacoes/{identity}", headers=api.headers(uid))
    assert recovered.status_code == 200 and recovered.json() == applied.json()
    assert (await confirm(api, uid, preview, identity)).json() == applied.json()
    assert await snapshot(engine_app, como, uid) == before
    async with como(engine_app, uid) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM apostas WHERE usuario_id=:uid"), {"uid": uid}
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM eventos WHERE usuario_id=:uid"), {"uid": uid}
            )
            == 1
        )


async def test_concurrent_same_confirmation_never_replays_financial_facts(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    preview = (await upload(api, uid, await source(api, uid))).json()
    identity = uuid4()
    responses = await asyncio.gather(*(confirm(api, uid, preview, identity) for _ in range(4)))
    assert all(response.status_code in {200, 409} for response in responses), [
        r.text for r in responses
    ]
    assert any(response.status_code == 200 for response in responses)
    recovered = await api.client.get(f"{PREFIX}/confirmacoes/{identity}", headers=api.headers(uid))
    assert recovered.status_code == 200 and recovered.json()["counts"]["criadas"] == 1
    async with como(engine_app, uid) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM eventos WHERE usuario_id=:uid"), {"uid": uid}
            )
            == 1
        )


@pytest.mark.parametrize("change", ["file", "mapping", "hash", "revision"])
async def test_changed_file_mapping_hash_or_revision_refuses_old_confirmation(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any, change: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    preview = (await upload(api, uid, sid)).json()
    if change == "file":
        assert (await upload(api, uid, sid, xlsx([values(odd=2.5)]))).status_code == 201
    elif change in {"mapping", "revision"}:
        remap = await api.client.post(
            f"{PREFIX}/previas/{preview['id']}/remapear/{uuid4()}",
            headers=api.headers(uid),
            json={
                "expected_revision": preview["revision"] + (1 if change == "revision" else 0),
                "sheet": "Apostas",
                "mapping": preview["mapping"],
            },
        )
        if change == "revision":
            assert remap.status_code == 409
            return
        assert remap.status_code == 201
    else:
        preview["preview_hash"] = "0" * 64
    before = await snapshot(engine_app, como, uid)
    assert (await confirm(api, uid, preview)).status_code == 409
    assert await snapshot(engine_app, como, uid) == before


async def test_expired_preview_and_result_tombstones_cannot_reapply(
    api: Any, engine_admin: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    preview = (await upload(api, uid, await source(api, uid))).json()
    async with engine_admin.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sheet_previews SET expires_at=now()-interval '1 second' WHERE usuario_id=:uid"
            ),
            {"uid": uid},
        )
    before = await snapshot(engine_app, como, uid)
    assert (await confirm(api, uid, preview)).status_code == 410
    assert await snapshot(engine_app, como, uid) == before


async def test_financial_concurrent_change_marks_obsolete_and_requires_new_validation(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    first = (await upload(api, uid, sid)).json()
    assert (await confirm(api, uid, first)).status_code == 200
    preview = (
        await upload(api, uid, sid, xlsx([values(atualizada_em="2026-10-13T12:00:00-03:00")]))
    ).json()
    key = (
        await api.client.get(f"{PREFIX}/previas/{preview['id']}/linhas", headers=api.headers(uid))
    ).json()["data"][0]["bet_key"]
    changed = await api.client.patch(
        f"/api/v1/apostas/{key}",
        headers=api.headers(uid),
        json={"descricao": "Alteração concorrente"},
    )
    assert changed.status_code == 200, changed.text
    before = await snapshot(engine_app, como, uid)
    assert (await confirm(api, uid, preview)).status_code == 409
    state = await api.client.get(f"{PREFIX}/previas/{preview['id']}", headers=api.headers(uid))
    assert state.json()["state"] == "obsolete"
    assert await snapshot(engine_app, como, uid) == before


@pytest.mark.parametrize("layout", ["same", "reorder", "rename", "insert", "remove"])
async def test_stable_source_row_ids_survive_layout_changes_without_overwriting(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any, layout: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    rows = [values("BET-A"), values("BET-B", odd=3.0)]
    first = (await upload(api, uid, sid, xlsx(rows))).json()
    result = await confirm(api, uid, first)
    assert result.status_code == 200, result.text
    keys = {row["linha_id"]: row["bet_key"] for row in result.json()["rows"]}
    rows = (
        rows[::-1]
        if layout == "reorder"
        else [values("BET-C"), *rows]
        if layout == "insert"
        else rows[:1]
        if layout == "remove"
        else rows
    )
    preview = (
        await upload(
            api, uid, sid, xlsx(rows, sheet="Renomeada" if layout == "rename" else "Apostas")
        )
    ).json()
    applied = await confirm(api, uid, preview)
    assert applied.status_code == 200, applied.text
    assert applied.json()["counts"]["criadas"] == (1 if layout == "insert" else 0)
    assert applied.json()["counts"]["ignoradas"] == (1 if layout == "remove" else 2)
    for row in applied.json()["rows"]:
        if row["linha_id"] in keys:
            assert row["bet_key"] == keys[row["linha_id"]]
    async with como(engine_app, uid) as session:
        assert await session.scalar(
            text("SELECT count(*) FROM apostas WHERE usuario_id=:uid"), {"uid": uid}
        ) == (3 if layout == "insert" else 2)
        assert await session.scalar(
            text("SELECT count(*) FROM eventos WHERE usuario_id=:uid"), {"uid": uid}
        ) == (3 if layout == "insert" else 2)


async def test_newer_versions_update_and_equal_or_older_versions_ignore(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    for index, updated in enumerate((
        "2026-10-12T12:00:00-03:00",
        "2026-10-13T12:00:00-03:00",
        "2026-10-13T12:00:00-03:00",
        "2026-10-11T12:00:00-03:00",
    )):
        preview = (
            await upload(api, uid, sid, xlsx([values(atualizada_em=updated, odd=2.0 + index)]))
        ).json()
        result = await confirm(api, uid, preview)
        assert result.status_code == 200, result.text
        assert (
            result.json()["counts"][("criadas", "atualizadas", "ignoradas", "ignoradas")[index]]
            == 1
        )
    async with como(engine_app, uid) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM eventos WHERE usuario_id=:uid"), {"uid": uid}
            )
            == 2
        )
        assert await session.scalar(
            text("SELECT odd FROM apostas WHERE usuario_id=:uid"), {"uid": uid}
        ) == pytest.approx(3.0)


async def test_freebet_and_unknown_optional_values_are_not_invented(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    preview = (
        await upload(api, uid, await source(api, uid), xlsx([values(freebet=True, data_jogo=None)]))
    ).json()
    row = (
        await api.client.get(f"{PREFIX}/previas/{preview['id']}/linhas", headers=api.headers(uid))
    ).json()["data"][0]
    assert row["normalized"]["data_jogo"] is None and row["normalized"]["descricao"] is None
    assert row["stake_centavos"] == 0 and row["valor_aposta_centavos"] > 0 and row["review_reason"]
    applied = await confirm(api, uid, preview)
    assert applied.status_code == 200 and applied.json()["rows"][0]["review_ids"]
    async with como(engine_app, uid) as session:
        bet = (
            await session.execute(
                text(
                    "SELECT stake_centavos,valor_aposta_centavos,data_jogo FROM apostas WHERE usuario_id=:uid"
                ),
                {"uid": uid},
            )
        ).one()
        assert bet[0] == 0 and bet[1] > 0 and bet[2] is None


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate_header",
        "duplicate_id",
        "date",
        "decimal",
        "formula",
        "text_formula",
        "null",
        "overflow",
        "boolean",
    ],
)
async def test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply(
    api: Any, engine_app: Any, como: Any, novo_usuario: Any, invalid: Any
) -> None:
    uid = await novo_usuario()
    rows, headers = [values()], HEADERS.copy()
    if invalid == "duplicate_header":
        headers[HEADERS.index("odd")] = "casa"
    elif invalid == "duplicate_id":
        rows.append(values())
    else:
        field, value = {
            "date": ("data_aposta", "bad-date"),
            "decimal": ("stake_unidades", "bad-money"),
            "formula": ("odd", "=1+1"),
            "text_formula": ("descricao", "@unsafe"),
            "null": ("stake_unidades", None),
            "overflow": ("stake_unidades", "1e308"),
            "boolean": ("stake_unidades", True),
        }[invalid]
        rows[0][HEADERS.index(field)] = value
    preview = await upload(api, uid, await source(api, uid), xlsx(rows, headers))
    assert preview.status_code == 201 and preview.json()["state"] == "invalid", preview.text
    before = await snapshot(engine_app, como, uid)
    assert (await confirm(api, uid, preview.json())).status_code == 422
    page = await api.client.get(
        f"{PREFIX}/previas/{preview.json()['id']}/linhas", headers=api.headers(uid)
    )
    assert page.json()["total"] == len(rows) and any(row["errors"] for row in page.json()["data"])
    assert await snapshot(engine_app, como, uid) == before


@pytest.mark.parametrize("content", [b"", b"not-xlsx", b"PK\x03\x04broken"])
async def test_empty_corrupt_or_truncated_xlsx_is_refused(
    api: Any, novo_usuario: Any, content: Any
) -> None:
    uid = await novo_usuario()
    assert (await upload(api, uid, await source(api, uid), content)).status_code == 422


@pytest.mark.parametrize("limit", ["bytes", "rows", "columns", "sheets", "expansion", "external"])
async def test_bounded_structures_and_malicious_archives(
    api: Any, novo_usuario: Any, limit: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    if limit == "bytes":
        content = b"x" * (5 * 1024 * 1024 + 1)
    elif limit == "rows":
        content = xlsx([values(f"BET-{i}") for i in range(1001)])
    elif limit == "columns":
        content = xlsx(
            [values() + [None] * 52 + ["too far"]], HEADERS + [f"Column{i}" for i in range(53)]
        )
    elif limit == "sheets":
        content = xlsx(sheets=11)
    else:
        stream = io.BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "x")
            archive.writestr("xl/workbook.xml", "x")
            archive.writestr(
                "xl/externalLinks/link.xml" if limit == "external" else "xl/worksheets/large.xml",
                "x" * 100000,
            )
        content = stream.getvalue()
    result = await upload(api, uid, sid, content)
    assert result.status_code == (422 if limit == "external" else 413), result.text


async def test_preview_identity_conflict_and_source_reconciliation(
    api: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    identity = uuid4()
    content = xlsx()
    first = await upload(api, uid, sid, content, identity)
    assert (await upload(api, uid, sid, content, identity)).json() == first.json()
    assert (await upload(api, uid, sid, xlsx([values(odd=3)]), identity)).status_code == 409
    listed = await api.client.get(f"{PREFIX}/fontes", headers=api.headers(uid))
    assert str(sid) in {item["id"] for item in listed.json()["data"]}
    assert (await api.client.get(f"{PREFIX}/fontes/{sid}", headers=api.headers(uid))).json()[
        "origem_id"
    ] == f"web-xlsx:{sid}"


async def test_owner_isolation_for_source_preview_rows_remap_and_confirmation(
    api: Any, engine_app: Any, engine_admin: Any, como: Any, novo_usuario: Any
) -> None:
    uid, other = await novo_usuario(), await novo_usuario()
    sid = await source(api, uid)
    preview = (await upload(api, uid, sid)).json()
    identity = uuid4()
    assert (await confirm(api, uid, preview, identity)).status_code == 200
    for path in (
        f"/fontes/{sid}",
        f"/previas/{preview['id']}",
        f"/previas/{preview['id']}/linhas",
        f"/confirmacoes/{identity}",
    ):
        assert (await api.client.get(PREFIX + path, headers=api.headers(other))).status_code == 404
    assert (await confirm(api, other, preview)).status_code == 404
    remap = await api.client.post(
        f"{PREFIX}/previas/{preview['id']}/remapear/{uuid4()}",
        headers=api.headers(other),
        json={
            "expected_revision": preview["revision"],
            "sheet": "Apostas",
            "mapping": preview["mapping"],
        },
    )
    assert remap.status_code == 404
    async with como(engine_app, other) as session:
        assert await session.get(SheetPreview, (uid, UUID(preview["id"]))) is None
    async with engine_admin.connect() as conn:
        role = (
            await conn.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='bancaemdia_app'")
            )
        ).one()
        assert role == (False, False)
        assert (
            await conn.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('sheet_sources','sheet_previews','sheet_confirmations')"
                )
            )
        ).all() == [(True, True)] * 3


async def test_429_retry_after_and_safe_503_do_not_invent_result(
    api: Any, novo_usuario: Any, monkeypatch: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    monkeypatch.setattr(get_settings(), "SHEET_DAILY_PREVIEW_LIMIT", 1)
    assert (await upload(api, uid, sid)).status_code == 201
    limited = await upload(api, uid, sid)
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "86400", limited.text

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        await args[0].execute(text("SELECT 1/0"))

    monkeypatch.setattr(service, "plan", unavailable)
    monkeypatch.setattr(get_settings(), "SHEET_DAILY_PREVIEW_LIMIT", 20)
    result = await upload(api, uid, sid)
    assert result.status_code == 503 and result.headers["Retry-After"] == "5"


async def test_unauthenticated_is_401_and_catalog_publishes_backend_types(
    api: Any, novo_usuario: Any
) -> None:
    assert (await api.client.get(PREFIX + "/campos")).status_code == 401
    uid = await novo_usuario()
    result = await api.client.get(PREFIX + "/campos", headers=api.headers(uid))
    assert result.status_code == 200
    catalog = result.json()
    assert catalog["max_operation_seconds"] == 30 and catalog["max_parse_seconds"] == 10
    assert {field["field"] for field in catalog["fields"] if field["required"]} == {
        "linha_id",
        "casa",
        "data_aposta",
        "odd",
        "stake_unidades",
        "atualizada_em",
    }


async def test_parser_time_budget_refuses_without_changing_financial_snapshot(
    api: Any, novo_usuario: Any, engine_app: Any, como: Any, monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    from bancaemdia.domain import sheet_xlsx

    uid = await novo_usuario()
    sid = await source(api, uid)
    before = await snapshot(engine_app, como, uid)
    ticks = iter(range(0, 100000, 11))
    monkeypatch.setattr(sheet_xlsx, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    refused = await upload(api, uid, sid)
    assert refused.status_code == 413 and refused.json()["detail"] == "xlsx_time_limit"
    assert await snapshot(engine_app, como, uid) == before


async def test_actual_lost_confirmation_response_is_reconciled_without_replay(
    api: Any, novo_usuario: Any, engine_app: Any, como: Any
) -> None:
    from bancaemdia.main import app

    uid = await novo_usuario()
    preview = (await upload(api, uid, await source(api, uid))).json()
    identity = uuid4()
    underlying = httpx.ASGITransport(app=app)

    class LostResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self: Any, request: httpx.Request) -> httpx.Response:
            response = await underlying.handle_async_request(request)
            assert response.status_code == 200
            await response.aclose()
            raise httpx.ReadError("synthetic lost response after commit")

    async with httpx.AsyncClient(transport=LostResponse(), base_url="https://api.test") as client:
        with pytest.raises(httpx.ReadError):
            await client.post(
                f"{PREFIX}/confirmacoes/{identity}",
                headers=api.headers(uid),
                json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]},
            )
    before = await snapshot(engine_app, como, uid)
    recovered = await api.client.get(f"{PREFIX}/confirmacoes/{identity}", headers=api.headers(uid))
    assert recovered.status_code == 200 and recovered.json()["counts"]["criadas"] == 1
    assert await snapshot(engine_app, como, uid) == before


async def test_read_only_reads_previous_imports_but_denies_new_preview_and_confirmation(
    api: Any, engine_admin: Any, novo_usuario: Any
) -> None:
    from bancaemdia.api.v1.sheet_import import sheet_db
    from bancaemdia.main import app

    uid = await novo_usuario()
    sid = await source(api, uid)
    previous = (await upload(api, uid, sid)).json()
    identity = uuid4()
    assert (await confirm(api, uid, previous, identity)).status_code == 200
    pending = (
        await upload(api, uid, sid, xlsx([values(atualizada_em="2026-10-13T12:00:00-03:00")]))
    ).json()
    async with engine_admin.connect() as conn:
        outer = await conn.begin()
        old = dict(app.dependency_overrides)
        try:
            await conn.execute(text("SELECT billing_activate_rollout()"))
            await conn.execute(text("SET LOCAL ROLE bancaemdia_app"))

            async def database() -> AsyncIterator[AsyncSession]:
                async with AsyncSession(
                    bind=conn, join_transaction_mode="create_savepoint"
                ) as session:
                    await session.execute(
                        text("SELECT set_config('app.current_user_id',:uid,true)"),
                        {"uid": str(uid)},
                    )
                    yield session

            app.dependency_overrides[get_db] = database
            app.dependency_overrides[sheet_db] = database
            assert (
                await api.client.get(f"{PREFIX}/previas/{pending['id']}", headers=api.headers(uid))
            ).status_code == 200
            assert (
                await api.client.get(f"{PREFIX}/confirmacoes/{identity}", headers=api.headers(uid))
            ).status_code == 200
            assert (await upload(api, uid, sid)).status_code == 402
            assert (await confirm(api, uid, pending)).status_code == 402
            async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
                from sqlalchemy.exc import DBAPIError

                await session.execute(
                    text("SELECT set_config('app.current_user_id',:uid,true)"), {"uid": str(uid)}
                )
                with pytest.raises(DBAPIError) as error:
                    await session.execute(
                        text("UPDATE sheet_sources SET nome='forbidden' WHERE usuario_id=:uid"),
                        {"uid": uid},
                    )
                assert getattr(error.value.orig, "sqlstate", None) == "P0402"
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(old)
            await outer.rollback()


async def test_confirmation_identity_with_incompatible_preview_is_refused(
    api: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    identity = uuid4()
    first = (await upload(api, uid, sid)).json()
    assert (await confirm(api, uid, first, identity)).status_code == 200
    second = (await upload(api, uid, sid, xlsx([values(odd=3)]))).json()
    assert (await confirm(api, uid, second, identity)).status_code == 409


async def test_duplicate_mapping_and_unsupported_sheet_are_explicit(
    api: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    preview = (await upload(api, uid, await source(api, uid))).json()
    mapping = preview["mapping"]
    mapping["odd"] = mapping["casa"]
    remap = await api.client.post(
        f"{PREFIX}/previas/{preview['id']}/remapear/{uuid4()}",
        headers=api.headers(uid),
        json={"expected_revision": 1, "sheet": "Apostas", "mapping": mapping},
    )
    assert remap.status_code == 201 and remap.json()["state"] == "invalid"
    assert any(error["code"] == "duplicate_mapping" for error in remap.json()["mapping_errors"])
    result = await api.client.post(
        f"{PREFIX}/previas/{remap.json()['id']}/remapear/{uuid4()}",
        headers=api.headers(uid),
        json={"expected_revision": 2, "sheet": "Unknown", "mapping": mapping},
    )
    assert result.status_code == 422 and result.json()["detail"] == "sheet_not_found"


async def test_legacy_direct_import_is_preserved_and_cannot_mix_registered_source(
    api: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    for registered, status in ((False, 200), (True, 422)):
        result = await api.client.post(
            "/api/v1/apostas/importar-planilha",
            headers=api.headers(uid),
            files={
                "arquivo": (
                    "legacy.xlsx",
                    xlsx(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"origem_id": f"web-xlsx:{sid}" if registered else "legacy-" + uuid4().hex},
        )
        assert result.status_code == status, result.text


async def test_missing_file_extra_fields_and_active_xml_have_safe_errors(
    api: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    sid = await source(api, uid)
    path = f"{PREFIX}/fontes/{sid}/previas/{uuid4()}"
    assert (
        await api.client.post(
            path, headers=api.headers(uid), files={"wrong": ("test.xlsx", xlsx())}
        )
    ).status_code == 422
    assert (
        await api.client.post(
            path,
            headers=api.headers(uid),
            files={"arquivo": ("test.xlsx", xlsx())},
            data={"extra": "unsafe"},
        )
    ).status_code == 400
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<!DOCTYPE x [<!ENTITY y 'unsafe'>]>")
        archive.writestr("xl/workbook.xml", "x")
    assert (await upload(api, uid, sid, buffer.getvalue())).status_code == 422
    encoded = io.BytesIO()
    with ZipFile(encoded, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            "<?xml version='1.0' encoding='UTF-16'?><!DOCTYPE x [<!ENTITY y 'unsafe'>]><x>&y;</x>".encode(
                "utf-16"
            ),
        )
        archive.writestr("xl/workbook.xml", "x")
    assert (await upload(api, uid, sid, encoded.getvalue())).status_code == 422


async def test_brazilian_decimals_and_synthetic_fixture_are_server_normalized(
    api: Any, novo_usuario: Any
) -> None:
    from pathlib import Path

    uid = await novo_usuario()
    sid = await source(api, uid)
    preview = (
        await upload(api, uid, sid, xlsx([values(odd="2,50", stake_unidades="1,25")]))
    ).json()
    row = (
        await api.client.get(f"{PREFIX}/previas/{preview['id']}/linhas", headers=api.headers(uid))
    ).json()["data"][0]
    assert row["normalized"]["odd"] == pytest.approx(2.5)
    assert row["normalized"]["stake_unidades"] == pytest.approx(1.25)
    fixture = Path(__file__).resolve().parents[1] / "fixtures/sheet_import/synthetic-import.xlsx"
    assert (await upload(api, uid, sid, fixture.read_bytes())).status_code == 201


async def test_expiration_purge_keeps_finances_and_durable_identity(
    api: Any, engine_admin: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    uid = await novo_usuario()
    preview = (await upload(api, uid, await source(api, uid))).json()
    identity = uuid4()
    assert (await confirm(api, uid, preview, identity)).status_code == 200
    before = await snapshot(engine_app, como, uid)
    async with engine_admin.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sheet_previews SET expires_at=now()-interval '1 second' WHERE usuario_id=:uid"
            ),
            {"uid": uid},
        )
        await conn.execute(
            text(
                "UPDATE sheet_confirmations SET expires_at=now()-interval '1 second' WHERE usuario_id=:uid"
            ),
            {"uid": uid},
        )
        assert await conn.scalar(text("SELECT sheet_import_purge()")) >= 2
        assert (
            await conn.scalar(
                text("SELECT private_result FROM sheet_confirmations WHERE usuario_id=:uid"),
                {"uid": uid},
            )
            is None
        )
    assert (await confirm(api, uid, preview, identity)).status_code == 410
    assert (
        await api.client.get(f"{PREFIX}/confirmacoes/{identity}", headers=api.headers(uid))
    ).status_code == 410
    assert await snapshot(engine_app, como, uid) == before


async def test_game_day_account_and_explicit_multicontas(
    api: Any, engine_admin: Any, engine_app: Any, como: Any, novo_usuario: Any
) -> None:
    from datetime import UTC, datetime

    from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
    from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

    uid = await novo_usuario()
    old_id = await _account(engine_admin, engine_app, como, uid)
    async with como(engine_app, uid) as session:
        old = await ContaCasaRepo().get_by_id(session, uid, old_id)
        assert old is not None
        usage = await UsoContaCasaRepo().current_for_update(session, uid, old.casa_id)
        assert usage is not None
        switch = datetime(2026, 9, 21, tzinfo=UTC)
        await UsoContaCasaRepo().close(session, usage.id, switch)
        new = await ContaCasaRepo().create(
            session, {"usuario_id": uid, "casa_id": old.casa_id, "apelido": "Depois"}
        )
        await UsoContaCasaRepo().open(session, uid, old.casa_id, new.id, switch)
        await session.commit()
        new_id = new.id
    sid = await source(api, uid)
    preview = (
        await upload(
            api, uid, sid, xlsx([values("DEFAULT"), values("EXPLICIT", conta_casa_ref=old_id)])
        )
    ).json()
    rows = (
        await api.client.get(f"{PREFIX}/previas/{preview['id']}/linhas", headers=api.headers(uid))
    ).json()["data"]
    assert [row["conta_casa_id"] for row in rows] == [new_id, old_id]
    applied = await confirm(api, uid, preview)
    assert applied.status_code == 200, applied.text


async def test_cookie_csrf_and_real_revocation(
    api: Any, banco: Any, engine_admin: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    from pydantic import SecretStr
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from bancaemdia.auth import middleware, transport
    from bancaemdia.auth.identity_crypto import digest
    from bancaemdia.auth.identity_service import IdentityService
    from tests.identity.support import key_settings

    role, password = "sheet_auth_" + uuid4().hex, uuid4().hex
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
                    "name": "Sheet fixture",
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
        source_identity = uuid4()
        address = f"{PREFIX}/fontes/{source_identity}"
        headers = {
            "Cookie": settings.session_cookie + "=" + cookie,
            "Origin": "https://api.test:4443",
        }
        preflight = await api.client.options(
            address,
            headers={
                "Origin": "https://api.test:4443",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type,x-csrf-token",
            },
        )
        assert (
            preflight.status_code == 204
            and "PUT" in preflight.headers["Access-Control-Allow-Methods"]
        )
        body = {"nome": "Cookie source"}
        assert (await api.client.put(address, headers=headers, json=body)).status_code == 403
        headers["X-CSRF-Token"] = service.keys.csrf(cookie)
        accepted = await api.client.put(address, headers=headers, json=body)
        assert accepted.status_code == 200, accepted.text
        assert accepted.headers["Access-Control-Allow-Origin"] == "https://api.test:4443"
        assert (
            await api.client.get(address, headers={"Cookie": headers["Cookie"]})
        ).status_code == 200
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
