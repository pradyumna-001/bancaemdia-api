"""Real app, JWT and PostgreSQL: delete every bet as one reversible batch (#212)."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_public_key,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1 import apostas_lotes
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.cli.refresh_painel import refresh_painel
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.seed import seed_canonical
from bancaemdia.db.session import get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot
from bancaemdia.main import app

pytestmark = pytest.mark.xdist_group("postgres")
LOTE = "/api/v1/apostas/lotes"


@dataclass
class Api:
    http: httpx.AsyncClient
    admin: AsyncEngine
    app_engine: AsyncEngine
    token: Callable[[int], str]
    novo: Callable[[], Awaitable[int]]

    async def call(self, method: str, url: str, uid: int, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token(uid)}", **kw.pop("headers", {})}
        return await self.http.request(method, url, headers=headers, **kw)

    async def previa(self, uid: int) -> dict[str, Any]:
        response = await self.call("GET", f"{LOTE}/previa-apagar", uid)
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def apagar(
        self, uid: int, previa: dict[str, Any], chave: str | None = None, **muda: Any
    ) -> httpx.Response:
        corpo = {"acao": "apagar_todas", "confirmacao": previa["confirmacao"]}
        corpo["frase"] = previa["frase"]
        corpo.update(muda)
        return await self.call(
            "POST", LOTE, uid, json=corpo, headers={"Idempotency-Key": chave or uuid4().hex}
        )

    async def restaurar(self, uid: int, lote_id: str, chave: str | None = None) -> httpx.Response:
        return await self.call(
            "POST",
            f"{LOTE}/{lote_id}/restaurar",
            uid,
            headers={"Idempotency-Key": chave or uuid4().hex},
        )

    async def contagens(self, uid: int) -> tuple[int, int, int]:
        async with self.admin.connect() as conn:
            linha = (
                await conn.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE selecionada), count(*) FILTER (WHERE NOT "
                        "selecionada), (SELECT count(*) FROM eventos WHERE usuario_id = :uid) "
                        "FROM apostas WHERE usuario_id = :uid"
                    ),
                    {"uid": uid},
                )
            ).one()
        return int(linha[0]), int(linha[1]), int(linha[2])


async def _seed(engine: AsyncEngine, usuario_id: int, total: int) -> list[str]:
    async with engine.begin() as conn:
        casa = await conn.scalar(text("SELECT id FROM casas ORDER BY id LIMIT 1"))
        banca = await conn.scalar(
            text(
                "INSERT INTO bancas (usuario_id, nome, saldo_inicial_centavos) "
                "VALUES (:uid, :nome, 100000) RETURNING id"
            ),
            {"uid": usuario_id, "nome": f"Lote {uuid4().hex}"},
        )
        conta = await conn.scalar(
            text(
                "INSERT INTO contas_casa (usuario_id, casa_id, banca_id, apelido) "
                "VALUES (:uid, :casa, :banca, 'lote') RETURNING id"
            ),
            {"uid": usuario_id, "casa": casa, "banca": banca},
        )
        prefixo = uuid4().hex
        valores = {
            "uid": usuario_id,
            "prefixo": prefixo,
            "base": datetime(2026, 10, 10, tzinfo=UTC),
            "conta": conta,
            "banca": banca,
        }
        chave = "'lote-' || :prefixo || '-' || g::text"
        data = "CAST(:base AS timestamptz) - make_interval(days => g % 60)"
        estado = "CASE g % 3 WHEN 0 THEN 'GREEN' WHEN 1 THEN 'RED' ELSE 'PENDENTE' END"
        retorno = "CASE g % 3 WHEN 0 THEN 20000 WHEN 1 THEN 0 ELSE NULL END"
        await conn.execute(
            text(
                "INSERT INTO apostas (usuario_id, chave, origem, banca_id, conta_casa_id, "
                "data_aposta, data_jogo, stake_unidades, stake_centavos, valor_aposta_centavos, "
                "odd, retorno_centavos, estado) "
                f"SELECT :uid, {chave}, 'manual', :banca, :conta, {data}, {data}, 1, 10000, "
                f"10000, 2.0, {retorno}, {estado} FROM generate_series(1, {total}) g"
            ),
            valores,
        )
        await conn.execute(
            text(
                "INSERT INTO eventos (usuario_id, tipo, fonte, payload_json, aposta_chave) "
                "SELECT :uid, 'APOSTA_CRIADA', 'manual', jsonb_build_object('origem', 'manual', "
                "'stake_unidades', 1, 'valor_unidade_centavos', 10000, 'odd', 2.0, "
                "'casa', (SELECT nome FROM casas WHERE id = (SELECT casa_id FROM contas_casa "
                f"WHERE id = :conta)), 'data_aposta', {data}, 'data_jogo', {data}, "
                "'conta_casa_id', CAST(:conta AS bigint), 'conta_casa_ref', CAST(:conta AS bigint), "
                f"'conta_referencia_explicita', true), {chave} "
                f"FROM generate_series(1, {total}) g"
            ),
            valores,
        )
        await conn.execute(
            text(
                "INSERT INTO eventos (usuario_id, tipo, fonte, payload_json, aposta_chave) "
                "SELECT :uid, 'RESULTADO_REGISTRADO', 'manual', jsonb_build_object('estado', "
                f"{estado}, 'retorno_centavos', {retorno}), {chave} "
                f"FROM generate_series(1, {total}) g WHERE g % 3 <> 2"
            ),
            valores,
        )
        return list(
            await conn.scalars(
                text("SELECT chave FROM apostas WHERE usuario_id = :uid ORDER BY chave"),
                {"uid": usuario_id},
            )
        )


@pytest.fixture
async def api(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Api]:
    async with engine_admin.begin() as conn:
        await seed_canonical(conn)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    cache = JWKSCache(None, "RS256")
    cache.keys = {
        "lote": {
            **jwt.algorithms.RSAAlgorithm.to_jwk(load_pem_public_key(public), as_dict=True),
            "kid": "lote",
        }
    }
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)

    async def sessions() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            if user := current_user_id.get():
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user)}
                )
            yield session

    for dependency in (get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot):
        monkeypatch.setitem(app.dependency_overrides, dependency, sessions)
    settings = get_settings()

    def token(uid: int) -> str:
        claims = {
            "sub": str(uid),
            "aud": settings.JWT_AUDIENCE,
            "iss": settings.JWT_ISSUER,
            "exp": int(time.time()) + 600,
        }
        return jwt.encode(claims, private, "RS256", headers={"kid": "lote"})

    transport = httpx.ASGITransport(app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        yield Api(http, engine_admin, engine_app, token, novo_usuario)


async def _total_painel(api: Api, uid: int) -> int:
    await refresh_painel(api.admin)
    response = await api.call("GET", "/api/v1/painel?periodo=all&fresh=true", uid)
    assert response.status_code == 200, response.text
    return int(response.json()["resumo"]["total_apostas"])


async def test_thousands_of_bets_delete_and_restore_atomically_like_one_by_one(api: Api) -> None:
    uid = await api.novo()
    chaves = await _seed(api.admin, uid, 3_000)
    async with api.admin.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO revisao_pendente (usuario_id, motivo, extracao_bruta) "
                "VALUES (:uid, 'valor_duvidoso', jsonb_build_object('aposta_chave', CAST(:chave AS text)))"
            ),
            {"uid": uid, "chave": chaves[0]},
        )
    assert (await reconstruir_usuario(uid, engine=api.app_engine, dry_run=True)).alteradas == 0
    antes = await _total_painel(api, uid)
    assert antes == 3_000
    ativas, _, eventos_antes = await api.contagens(uid)

    previa = await api.previa(uid)
    assert previa["total"] == 3_000 and previa["frase"] == "APAGAR 3000 APOSTAS"
    chave = uuid4().hex
    resposta = await api.apagar(uid, previa, chave)
    assert resposta.status_code == 201, resposta.text
    lote = resposta.json()
    assert lote["apostas_afetadas"] == 3_000 and lote["restaurado_em"] is None
    assert await api.contagens(uid) == (0, 3_000, eventos_antes + 3_000)
    # O replay reconstrói exatamente o estado gravado pelo lote: nada a corrigir.
    assert (await reconstruir_usuario(uid, engine=api.app_engine, dry_run=True)).alteradas == 0
    assert await _total_painel(api, uid) == 0
    listada = await api.call("GET", "/api/v1/apostas?incluir_apagadas=true&page_size=1", uid)
    assert listada.json()["pagination"]["total"] == 3_000
    assert (await api.call("GET", "/api/v1/apostas?page_size=1", uid)).json()["pagination"][
        "total"
    ] == 0
    async with api.admin.connect() as conn:
        aberta = await conn.scalar(
            select(func.count())
            .select_from(models.RevisaoPendente)
            .where(
                models.RevisaoPendente.usuario_id == uid,
                models.RevisaoPendente.resolvido_em.is_(None),
            )
        )
    assert aberta == 0

    repetida = await api.apagar(uid, previa, chave)
    assert repetida.status_code == 201 and repetida.json() == lote
    assert (await api.contagens(uid))[2] == eventos_antes + 3_000

    restaurada = await api.restaurar(uid, lote["lote_id"])
    assert restaurada.status_code == 200
    assert restaurada.json()["apostas_restauradas"] == 3_000
    assert await api.contagens(uid) == (ativas, 0, eventos_antes + 6_000)
    assert (await reconstruir_usuario(uid, engine=api.app_engine, dry_run=True)).alteradas == 0
    assert await _total_painel(api, uid) == antes
    consultado = (await api.call("GET", f"{LOTE}/{lote['lote_id']}", uid)).json()
    assert consultado == restaurada.json()


async def test_restore_returns_only_what_the_batch_deleted_and_runs_once(api: Api) -> None:
    uid = await api.novo()
    chaves = await _seed(api.admin, uid, 4)
    assert (await api.call("DELETE", f"/api/v1/apostas/{chaves[0]}", uid)).status_code == 200
    lote = (await api.apagar(uid, await api.previa(uid))).json()
    assert lote["apostas_afetadas"] == 3
    assert (
        await api.call("POST", f"/api/v1/apostas/{chaves[1]}/restaurar", uid)
    ).status_code == 200
    chave = uuid4().hex
    restaurada = await api.restaurar(uid, lote["lote_id"], chave)
    assert restaurada.status_code == 200 and restaurada.json()["apostas_restauradas"] == 2
    ativas, apagadas, _ = await api.contagens(uid)
    # A apagada antes do lote continua apagada; a restaurada à mão não ganha segundo evento.
    assert (ativas, apagadas) == (3, 1)
    assert (await api.restaurar(uid, lote["lote_id"], chave)).json() == restaurada.json()
    de_novo = await api.restaurar(uid, lote["lote_id"])
    assert de_novo.status_code == 409 and de_novo.json() == {"detail": "lote_restaurado"}


async def test_confirmation_is_bound_to_the_exact_set_phrase_and_time(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = await api.novo()
    chaves = await _seed(api.admin, uid, 3)
    previa = await api.previa(uid)
    errada = await api.apagar(uid, previa, frase="APAGAR TUDO")
    assert errada.status_code == 422 and errada.json() == {"detail": "frase_incorreta"}
    adulterada = await api.apagar(uid, previa, confirmacao=previa["confirmacao"][:-2] + "00")
    assert adulterada.status_code == 422 and adulterada.json() == {"detail": "confirmacao_invalida"}
    outra = await api.novo()
    alheia = await api.apagar(outra, previa)
    assert alheia.status_code == 422 and alheia.json() == {"detail": "confirmacao_invalida"}

    assert (await api.call("DELETE", f"/api/v1/apostas/{chaves[0]}", uid)).status_code == 200
    mudou = await api.apagar(uid, previa)
    assert mudou.status_code == 409 and mudou.json() == {"detail": "confirmacao_desatualizada"}

    monkeypatch.setattr(apostas_lotes, "VALIDADE_SEGUNDOS", -1)
    vencida = await api.apagar(uid, await api.previa(uid))
    assert vencida.status_code == 409 and vencida.json() == {"detail": "confirmacao_expirada"}
    assert (await api.contagens(uid))[:2] == (2, 1)


async def test_idempotency_conflict_concurrent_batch_and_recovery(api: Api) -> None:
    uid = await api.novo()
    await _seed(api.admin, uid, 2)
    previa = await api.previa(uid)
    chave = uuid4().hex
    async with api.admin.connect() as conn:
        await conn.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:chave, 0))"),
            {"chave": f"apostas-lote:{uid}"},
        )
        try:
            ocupado = await api.apagar(uid, previa, chave)
            assert ocupado.status_code == 409 and ocupado.json() == {"detail": "lote_em_curso"}
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:chave, 0))"),
                {"chave": f"apostas-lote:{uid}"},
            )
    # A recusa não fica gravada: a mesma intenção passa quando o outro lote termina.
    feita = await api.apagar(uid, previa, chave)
    assert feita.status_code == 201
    conflito = await api.apagar(uid, previa, chave, frase="apagar 2 apostas ")
    assert conflito.status_code == 409 and conflito.json() == {"detail": "idempotency_key_conflict"}
    recuperada = await api.call("GET", f"{LOTE}/intencao", uid, headers={"Idempotency-Key": chave})
    assert recuperada.json() == {"operacao": "APAGAR", "status_code": 201, "resposta": feita.json()}
    ausente = await api.call("GET", f"{LOTE}/intencao", uid, headers={"Idempotency-Key": "nunca"})
    assert ausente.status_code == 404


async def test_batches_are_private_and_other_data_never_changes(api: Api) -> None:
    ana, bia = await api.novo(), await api.novo()
    await _seed(api.admin, ana, 2)
    bia_chaves = await _seed(api.admin, bia, 2)
    async with api.admin.begin() as conn:
        conta = await conn.scalar(
            text("SELECT id FROM contas_casa WHERE usuario_id = :uid"), {"uid": ana}
        )
        await conn.execute(
            text(
                "INSERT INTO movimentos (usuario_id, conta_casa_id, tipo, valor_centavos, "
                "ocorrido_em) VALUES (:uid, :conta, 'DEPOSITO', 5000, now())"
            ),
            {"uid": ana, "conta": conta},
        )
    saldo_antes = (await api.call("GET", "/api/v1/caixa/saldo", ana)).json()
    lote = (await api.apagar(ana, await api.previa(ana))).json()
    assert (await api.call("GET", f"{LOTE}/{lote['lote_id']}", bia)).status_code == 404
    assert (await api.restaurar(bia, lote["lote_id"])).status_code == 404
    assert (await api.contagens(bia))[:2] == (len(bia_chaves), 0)
    # Escopo: só apostas. Movimentos, contas e bancas ficam; o saldo, que soma os resultados das
    # apostas, volta exatamente ao anterior ao restaurar.
    async with api.admin.connect() as conn:
        movimentos = await conn.scalar(
            select(func.count())
            .select_from(models.Movimento)
            .where(models.Movimento.usuario_id == ana)
        )
        bancas = await conn.scalar(
            select(func.count()).select_from(models.Banca).where(models.Banca.usuario_id == ana)
        )
    assert (movimentos, bancas) == (1, 1)
    assert (await api.restaurar(ana, lote["lote_id"])).status_code == 200
    assert (await api.call("GET", "/api/v1/caixa/saldo", ana)).json() == saldo_antes


async def test_read_only_account_previews_but_never_deletes(api: Api) -> None:
    uid = await api.novo()
    await _seed(api.admin, uid, 2)
    previa = await api.previa(uid)
    async with api.admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("SELECT billing_activate_rollout()"))
        await conn.execute(
            text(
                "UPDATE assinaturas SET status='EXPIRED', trial_confirmed=false WHERE usuario_id=:uid"
            ),
            {"uid": uid},
        )
    try:
        assert (await api.call("GET", f"{LOTE}/previa-apagar", uid)).status_code == 200
        negada = await api.apagar(uid, previa)
        assert negada.status_code == 402 and negada.json() == {"detail": "account_read_only"}
        assert (await api.contagens(uid))[:2] == (2, 0)
    finally:
        async with api.admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:previous WHERE id=1"),
                {"previous": previous},
            )


async def test_failure_midway_leaves_no_partial_state(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = await api.novo()
    await _seed(api.admin, uid, 5)
    antes = await api.contagens(uid)
    previa = await api.previa(uid)

    async def quebra(*_args: object, **_kwargs: object) -> None:  # ruff: ignore[unused-async]
        raise RuntimeError("falha simulada depois de marcar as apostas")

    monkeypatch.setattr(apostas_lotes, "_eventos", quebra)
    falhou = await api.apagar(uid, previa)
    assert falhou.status_code == 500
    assert await api.contagens(uid) == antes
    async with api.admin.connect() as conn:
        recibos = await conn.scalar(
            select(func.count())
            .select_from(models.ApostaLote)
            .where(models.ApostaLote.usuario_id == uid)
        )
    assert recibos == 0


@pytest.mark.parametrize(
    "corpo,campo,codigo",
    [
        ({"confirmacao": "x", "frase": "x"}, "acao", "obrigatorio"),
        ({"acao": "apagar_todas", "frase": "x"}, "confirmacao", "obrigatorio"),
        ({"acao": "apagar_todas", "confirmacao": "x", "frase": ""}, "frase", "tamanho"),
        ({"acao": "apagar_tudo", "confirmacao": "x", "frase": "x"}, "acao", "invalido"),
        ({"acao": "apagar_todas", "confirmacao": "x", "frase": "x", "x": 1}, "x", "desconhecido"),
    ],
)
async def test_invalid_requests_are_refused_with_a_code_per_field(
    api: Api, corpo: dict[str, Any], campo: str, codigo: str
) -> None:
    uid = await api.novo()
    response = await api.call("POST", LOTE, uid, json=corpo, headers={"Idempotency-Key": "k"})
    assert response.status_code == 422
    assert response.json() == {
        "detail": "lote_invalido",
        "campos": [{"campo": campo, "codigo": codigo}],
    }
