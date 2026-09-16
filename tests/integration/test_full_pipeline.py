from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import anthropic
import httpx2
import pytest
import redis
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia import main
from bancaemdia.api.v1 import coleta
from bancaemdia.cache.extracao_cache import ExtracaoCache
from bancaemdia.config import get_settings
from bancaemdia.db.seed import seed_canonical
from bancaemdia.db.session import get_db
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.extracao.cliente import LeitorDeBilhetes, calcular_custo
from bancaemdia.rate_limit.anthropic_limiter import AnthropicLimiter
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.coleta_token_repo import ColetaTokenRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.resilience.circuit_breaker import new_anthropic_breaker
from bancaemdia.workers import celery_app, extraction, materialization

# The limiter's global bucket is one Redis key for every user, and test_rate_limit_redis counts on
# that bucket: the two modules take turns.
pytestmark = pytest.mark.xdist_group("redis")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
IMAGE = "redis:7-alpine"
BRASIL = timezone(timedelta(hours=-3))
POSTADA_EM = "2026-09-10T21:00:00"
CHAT = 100
USO = {
    "input_tokens": 1000,
    "output_tokens": 400,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}
LOTE = ("simples",) * 6 + ("dois_cupons", "odds_incoerentes", "recusa", "ilegivel")
CASAS = ("betano", "betfair", "bet365", "sportingbet", "novibet")


def _docker_available() -> bool:
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception:
        return False
    return True


@pytest.fixture(scope="module")
def servidor_redis() -> Iterator[redis.Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    if url:
        yield redis.Redis.from_url(url)
        return
    if not _docker_available():
        pytest.skip("integration tests need Docker or TEST_REDIS_URL pointing at a Redis")
    from testcontainers.community.redis import RedisContainer

    with RedisContainer(IMAGE) as container:
        yield container.get_client()


@pytest.fixture
def tarefas_no_banco(banco, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", banco.url_app)
    get_settings.cache_clear()
    materialization.get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    materialization.get_engine.cache_clear()


def _ia_falsa():
    respostas = json.loads((FIXTURES / "anthropic" / "respostas.json").read_text(encoding="utf-8"))

    class IA:
        def __init__(self):
            self.leituras = {}
            self.pedidos = []

        def responder(self, request):
            corpo = json.loads(request.content)
            imagem = base64.b64decode(corpo["messages"][0]["content"][0]["source"]["data"])
            resposta = respostas[self.leituras[imagem]]
            self.pedidos.append(corpo["model"])
            cupons = resposta["cupons"]
            return httpx2.Response(
                200,
                json={
                    "id": f"msg_{len(self.pedidos)}",
                    "type": "message",
                    "role": "assistant",
                    "model": corpo["model"],
                    "content": []
                    if cupons is None
                    else [{"type": "text", "text": json.dumps({"cupons": cupons})}],
                    "stop_reason": resposta["stop_reason"],
                    "stop_sequence": None,
                    "usage": USO,
                },
            )

    return IA()


@pytest.fixture
def ia(servidor_redis, tarefas_no_banco, monkeypatch):
    falsa = _ia_falsa()
    leitor = LeitorDeBilhetes(
        anthropic.Anthropic(
            api_key="sk-ant-test",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(
                transport=httpx2.MockTransport(falsa.responder)
            ),
        ),
        "claude-haiku-4-5",
        "claude-sonnet-5",
        breaker=new_anthropic_breaker(),
        pausa=lambda _: None,
    )
    cache = ExtracaoCache(servidor_redis)
    # The fake answers at once and the limits are lifted, so the tests time the workers and
    # PostgreSQL. At the default 10 reads per user per minute, one user's 200th read waits ~19 min.
    limiter = AnthropicLimiter(servidor_redis, 1_000_000, 1_000_000, 60)
    monkeypatch.setattr(extraction, "get_leitor", lambda: leitor)
    monkeypatch.setattr(extraction, "get_cache", lambda: cache)
    monkeypatch.setattr(extraction, "get_limiter", lambda: limiter)
    return falsa


def _mensagens(ia, tipos: Iterable[str]) -> list[tuple[int, bytes]]:
    mensagens = []
    for message_id, tipo in enumerate(tipos, start=1):
        imagem = f"print-{uuid4().hex}".encode()
        ia.leituras[imagem] = tipo
        mensagens.append((message_id, imagem))
    return mensagens


def _extrair(usuario: int, message_id: int, imagem: bytes, legenda: str) -> dict[str, object]:
    return extraction.extrair_bilhete_task.apply(
        kwargs={
            "usuario_id": usuario,
            "imagem_base64": base64.b64encode(imagem).decode("ascii"),
            "nome_do_arquivo": f"photo_{message_id}@10-09-2026_21-00-00.jpg",
            "legenda": legenda,
            "postada_em": POSTADA_EM,
            "chat_id": CHAT,
            "message_id": message_id,
        }
    ).get()


def _materializar(usuario: int, extracao: dict[str, object], imagem: bytes) -> dict[str, object]:
    return materialization.materializar_aposta_task.apply(
        kwargs={
            "usuario_id": usuario,
            "extracao_json": extracao,
            "midia_hash": hashlib.sha256(imagem).hexdigest(),
        }
    ).get()


async def _processar(
    usuario: int, mensagens: list[tuple[int, bytes]], legenda: str = ""
) -> list[tuple[dict[str, object], dict[str, object]]]:
    lidas = []
    for message_id, imagem in mensagens:
        extracao = await asyncio.to_thread(_extrair, usuario, message_id, imagem, legenda)
        # Nothing enqueues the materialization after the extraction until the upload of issue #26;
        # the test hands each reading on the way that chain will.
        gravada = await asyncio.to_thread(_materializar, usuario, extracao, imagem)
        lidas.append((extracao, gravada))
    return lidas


def _amostra(nome: str, **rotulos: str) -> float:
    return REGISTRY.get_sample_value(nome, rotulos) or 0.0


async def test_prints_read_by_the_ai_become_bets_with_their_creation_events(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario, ia
) -> None:
    usuario = await novo_usuario()

    lidas = await _processar(usuario, _mensagens(ia, LOTE))

    async with como(engine_app, usuario) as session:
        apostas = {a.chave: a for a in await ApostaRepo().list_by_usuario(session, usuario)}
        eventos = {
            chave: await EventoRepo().list_by_aposta_chave(session, usuario, chave)
            for chave in apostas
        }
    assert [len(gravada["apostas"]) for _, gravada in lidas] == [1, 1, 1, 1, 1, 1, 2, 1, 1, 0]
    assert sorted(apostas) == sorted([*(f"t:{CHAT}:{m}:0" for m in range(1, 10)), f"t:{CHAT}:7:1"])
    assert {(a.origem, a.stake_centavos, a.estado) for a in apostas.values()} == {
        ("telegram", 0, "PENDENTE")
    }
    assert {a.data_aposta for a in apostas.values()} == {datetime(2026, 9, 10, 21, tzinfo=BRASIL)}
    assert [apostas[f"t:{CHAT}:{m}:0"].odd for m in range(1, 8)] == pytest.approx(
        [1.82] * 6 + [2.1]
    )
    assert apostas[f"t:{CHAT}:7:1"].odd == pytest.approx(1.75)
    assert apostas[f"t:{CHAT}:9:0"].odd is None
    assert {
        chave: [(e.tipo, e.fonte, e.chat_id, e.message_id) for e in historico]
        for chave, historico in eventos.items()
    } == {chave: [("APOSTA_CRIADA", "ia", CHAT, int(chave.split(":")[2]))] for chave in apostas}


async def test_prints_that_fail_their_checks_open_reviews_and_an_illegible_one_is_no_bet(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario, ia
) -> None:
    usuario = await novo_usuario()
    mensagens = _mensagens(ia, LOTE)

    lidas = await _processar(usuario, mensagens)

    async with como(engine_app, usuario) as session:
        revisoes = await RevisaoPendenteRepo().list_by_usuario(session, usuario)
        graves = {
            a.chave for a in await ApostaRepo().list_by_usuario(session, usuario) if a.revisao_grave
        }
    da_imagem = {hashlib.sha256(imagem).hexdigest(): message_id for message_id, imagem in mensagens}
    motivos = {
        (da_imagem[r.midia_hash], r.extracao_bruta["aposta_chave"]): r.motivo for r in revisoes
    }
    assert [extracao["degrau"] for extracao, _ in lidas] == [
        *["BARATO"] * 7,
        "CARO",
        None,
        "BARATO",
    ]
    assert sorted(motivos) == [
        (7, f"t:{CHAT}:7:0"),
        (7, f"t:{CHAT}:7:1"),
        (8, f"t:{CHAT}:8:0"),
        (9, f"t:{CHAT}:9:0"),
    ]
    assert len(revisoes) == len(motivos)
    assert motivos[7, f"t:{CHAT}:7:0"].startswith("a foto tem 2 cupons")
    assert motivos[8, f"t:{CHAT}:8:0"].startswith("coerência das odds")
    assert motivos[9, f"t:{CHAT}:9:0"].startswith("a leitura falhou")
    assert graves == {f"t:{CHAT}:7:0", f"t:{CHAT}:7:1", f"t:{CHAT}:8:0", f"t:{CHAT}:9:0"}
    extracao, gravada = lidas[9]
    assert (extracao["nao_e_aposta"], gravada["apostas"]) == (True, [])


async def test_what_the_ai_billed_the_user_adds_up_call_by_call_and_bet_by_bet(
    novo_usuario: NovoUsuario, ia
) -> None:
    usuario = await novo_usuario()
    custo = _amostra("anthropic_cost_usd_total", usuario_id=str(usuario))
    extraidas = _amostra("batch_bets_processed_total", stage="extraction")
    materializadas = _amostra("batch_bets_processed_total", stage="materialization")

    lidas = await _processar(usuario, _mensagens(ia, LOTE))

    custo = _amostra("anthropic_cost_usd_total", usuario_id=str(usuario)) - custo
    apostas = sum(len(gravada["apostas"]) for _, gravada in lidas)
    assert Counter(ia.pedidos) == {"claude-haiku-4-5": 12, "claude-sonnet-5": 1}
    assert custo == pytest.approx(sum(extracao["custo_usd"] for extracao, _ in lidas))
    assert custo == pytest.approx(
        sum(
            calcular_custo(modelo, USO["input_tokens"], USO["output_tokens"])
            for modelo in ia.pedidos
        )
    )
    assert apostas == 10
    assert _amostra("batch_bets_processed_total", stage="extraction") - extraidas == apostas
    assert (
        _amostra("batch_bets_processed_total", stage="materialization") - materializadas == apostas
    )


async def test_reading_the_same_prints_again_comes_from_the_cache_and_appends_nothing(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario, ia
) -> None:
    usuario = await novo_usuario()
    mensagens = _mensagens(ia, LOTE)
    await _processar(usuario, mensagens)
    async with como(engine_app, usuario) as session:
        eventos = await EventoRepo().list_by_usuario(session, usuario)
        revisoes = await RevisaoPendenteRepo().list_by_usuario(session, usuario)
    acertos = _amostra("extraction_cache_hit_total")
    faltas = _amostra("extraction_cache_miss_total")
    ia.pedidos.clear()

    lidas = await _processar(usuario, mensagens)

    acertos = _amostra("extraction_cache_hit_total") - acertos
    faltas = _amostra("extraction_cache_miss_total") - faltas
    async with como(engine_app, usuario) as session:
        eventos_depois = await EventoRepo().list_by_usuario(session, usuario)
        revisoes_depois = await RevisaoPendenteRepo().list_by_usuario(session, usuario)
    assert (acertos, faltas) == (9, 1)
    assert acertos / (acertos + faltas) > 0.8
    assert ia.pedidos == ["claude-haiku-4-5"] * 3
    assert [extracao["degrau"] for extracao, _ in lidas] == [*["CACHE"] * 8, None, "CACHE"]
    assert [(gravada["criadas"], gravada["eventos"]) for _, gravada in lidas] == [(0, 0)] * 10
    assert eventos_depois == eventos
    assert revisoes_depois == revisoes


async def test_an_edited_caption_that_changes_the_reading_is_appended_to_the_bets_history(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario, ia
) -> None:
    usuario = await novo_usuario()
    mensagens = _mensagens(ia, ["simples"])
    chave = f"t:{CHAT}:1:0"
    await _processar(usuario, mensagens, legenda="1u")
    async with como(engine_app, usuario) as session:
        (criacao,) = await EventoRepo().list_by_aposta_chave(session, usuario, chave)
    ((_, imagem),) = mensagens
    ia.leituras[imagem] = "odd_corrigida"

    ((_, gravada),) = await _processar(usuario, mensagens, legenda="1u odd 1.95")

    async with como(engine_app, usuario) as session:
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario, chave)
        aposta = await ApostaRepo().get_by_chave(session, usuario, chave)
    assert (gravada["criadas"], gravada["eventos"]) == (0, 1)
    assert eventos[0] == criacao
    assert [(e.tipo, e.fonte, e.payload_json) for e in eventos[1:]] == [
        ("ODD_ALTERADA", "export", {"de": pytest.approx(1.82), "para": pytest.approx(1.95)})
    ]
    assert aposta is not None and aposta.odd == pytest.approx(1.95)


async def test_two_hundred_prints_become_bets_in_under_five_minutes(
    engine_app: AsyncEngine, como: Como, novo_usuario: NovoUsuario, ia
) -> None:
    usuario = await novo_usuario()
    mensagens = _mensagens(ia, ["simples"] * 200)
    extraidas = _amostra("batch_job_duration_seconds_count", stage="extraction", status="success")
    materializadas = _amostra(
        "batch_job_duration_seconds_count", stage="materialization", status="success"
    )

    comeco = time.perf_counter()
    await _processar(usuario, mensagens)
    segundos = time.perf_counter() - comeco

    async with como(engine_app, usuario) as session:
        (telegram,) = await ApostaRepo().count_by_origem(session, usuario)
    assert segundos < 300
    assert (telegram.origem, telegram.apostas, telegram.mensagens) == ("telegram", 200, 200)
    assert (
        _amostra("batch_job_duration_seconds_count", stage="extraction", status="success")
        - extraidas
    ) == 200
    assert (
        _amostra("batch_job_duration_seconds_count", stage="materialization", status="success")
        - materializadas
    ) == 200


def _envio(casa: str) -> dict[str, object]:
    return json.loads((FIXTURES / "coleta" / f"{casa}.json").read_text(encoding="utf-8"))


async def _token(engine: AsyncEngine, como: Como, usuario: int) -> str:
    token = f"tok-{uuid4().hex}"
    async with como(engine, usuario) as session:
        await ColetaTokenRepo().create(session, usuario, coleta.hash_do_token(token))
        await session.commit()
    return token


def _sessoes(url: str):
    class Sessoes:
        async def abrir(self):
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with AsyncSession(engine) as session:
                    yield session
            finally:
                await engine.dispose()

    return Sessoes().abrir


def _fila():
    class Fila:
        def __init__(self):
            self.tarefas = []

        def send_task(self, nome, args=None, kwargs=None, **opcoes):
            self.tarefas.append((nome, kwargs, opcoes))

    return Fila()


def _rodar(nome: str, kwargs: dict[str, object]) -> dict[str, object]:
    return celery_app.app.tasks[nome].apply(kwargs=kwargs).get()


async def test_house_sends_reach_the_materialization_queue_and_become_bets(
    banco,
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    tarefas_no_banco,
    monkeypatch,
) -> None:
    async with engine_admin.begin() as conn:
        await seed_canonical(conn)
    usuario = await novo_usuario()
    token = await _token(engine_app, como, usuario)
    fila = _fila()
    monkeypatch.setattr(coleta.celery, "send_task", fila.send_task)
    monkeypatch.setattr(coleta.limiter, "enabled", False)
    monkeypatch.setitem(main.app.dependency_overrides, get_db, _sessoes(banco.url_app))
    cliente = TestClient(main.app)

    respostas = {}
    for casa in CASAS:
        resposta = await asyncio.to_thread(
            cliente.post, "/coleta", json=_envio(casa), headers={coleta.TOKEN_HEADER: token}
        )
        respostas[casa] = (resposta.status_code, resposta.json())
    gravadas = [await asyncio.to_thread(_rodar, nome, kwargs) for nome, kwargs, _ in fila.tarefas]

    async with como(engine_app, usuario) as session:
        apostas = await ApostaRepo().list_by_usuario(session, usuario, origem="casa")
        guardadas = {}
        for casa in CASAS:
            casa_id = await CasaRepo().get_id_by_nome(session, casa_canonica(casa))
            guardadas[casa] = len(await ColetaCasaRepo().list_by_casa(session, usuario, casa_id))
    assert {casa: (status, r["novas_contando"]) for casa, (status, r) in respostas.items()} == {
        "betano": (200, 2),
        "betfair": (200, 2),
        "bet365": (200, 0),
        "sportingbet": (200, 0),
        "novibet": (200, 0),
    }
    assert {
        casa: [s["guardadas"] for s in r["sem_leitor"]] for casa, (_, r) in respostas.items()
    } == {
        "betano": [],
        "betfair": [],
        "bet365": [1],
        "sportingbet": [1],
        "novibet": [1],
    }
    assert [nome for nome, _, _ in fila.tarefas] == ["materialization.materializar_coleta"] * 4
    assert {kwargs["usuario_id"] for _, kwargs, _ in fila.tarefas} == {usuario}
    assert [
        celery_app.app.amqp.router.route(opcoes, nome)["queue"].name
        for nome, _, opcoes in fila.tarefas
    ] == ["materialization"] * 4
    assert [gravada["criada"] for gravada in gravadas] == [True] * 4
    assert sorted((a.chave, a.estado, a.stake_centavos, a.retorno_centavos) for a in apostas) == [
        ("c:betano:90000000001", "GREEN", 16000, 30400),
        ("c:betano:90000000002", "PENDENTE", 5000, None),
        ("c:betfair:2700000001", "RED", 3995, 0),
        ("c:betfair:2700000002", "PENDENTE", 2000, None),
    ]
    assert guardadas == {"betano": 2, "betfair": 2, "bet365": 1, "sportingbet": 1, "novibet": 1}


def test_a_telegram_export_uploaded_to_the_api_is_read_and_reported_complete() -> None:
    pytest.skip(
        "POST /upload, the Telegram export reader and /webhook/upload-complete arrive with"
        " issue #26, and the export ZIP fixture with them"
    )


def test_painel_totals_match_the_conferir_numeros_logic() -> None:
    pytest.skip("GET /painel arrives with issue #30; conferir_numeros.py has not been ported")
