from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from prometheus_client import REGISTRY

from bancaemdia import main
from bancaemdia.api.v1 import coleta
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db

TOKEN = "token-da-extensao"
PLACED = 1785708161930
START = 1785709800000


def _bilhete(id_="20753556039", *, resultado="Lose", ganho=0.0, odd=1.90, **extra):
    bruto = {
        "id": id_,
        "bonusType": 0,
        "totalAmount": 160.0,
        "totalAmountWithCurrency": {"amount": 160.0, "currencyCode": "BRL"},
        "totalOdds": odd,
        "finalWinnings": ganho,
        "placedAt": PLACED,
        "finalBetResult": resultado,
        "settledAt": PLACED + 3_600_000,
        "legs": [
            {
                "legItems": [
                    {
                        "eventId": "86389413",
                        "eventName": "Internacional - Corinthians",
                        "startTime": START,
                        "selections": [{"description": "Mais de 41.5", "odds": odd}],
                    }
                ]
            }
        ],
        **extra,
    }
    if resultado is None:
        del bruto["finalBetResult"], bruto["settledAt"]
    return bruto


def _banco(recebidas_hoje=0):
    class Banco:
        def __init__(self):
            self.linhas = {}
            self.apostas = {}
            self.enfileiradas = []
            self.commits = 0
            self.sql = []

    banco = Banco()

    class ColetaTokenRepo:
        async def get_usuario_id_by_hash(self, session, token_hash):
            return 7 if token_hash == coleta.hash_do_token(TOKEN) else None

    class ColetaCasaRepo:
        async def count_received_since(self, session, usuario_id, desde):
            banco.desde = desde
            return recebidas_hoje

        async def get_by_identidade(self, session, usuario_id, casa_id, identidade):
            return banco.linhas.get((usuario_id, casa_id, identidade))

        async def upsert_idempotent(self, session, dados):
            chave = (dados["usuario_id"], dados["casa_id"], dados["identidade"])
            atual = banco.linhas.get(chave)
            if atual is not None and atual.hash_conteudo == dados["hash_conteudo"]:
                return None
            linha = SimpleNamespace(
                id=len(banco.linhas) + 1 if atual is None else atual.id,
                processado_em=None,
                **dados,
            )
            banco.linhas[chave] = linha
            return linha

    class CasaRepo:
        async def get_id_by_nome(self, session, nome):
            return {"Betano": 1, "KTO": 2, "bet365": 3}.get(nome)

    class ApostaRepo:
        async def get_by_chave(self, session, usuario_id, chave):
            return banco.apostas.get((usuario_id, chave))

    class Session:
        async def execute(self, statement, params=None):
            banco.sql.append((str(statement), params))

        async def commit(self):
            banco.commits += 1

    class Sessoes:
        async def abrir(self):
            yield Session()

    banco.repos = {
        "ColetaTokenRepo": ColetaTokenRepo,
        "ColetaCasaRepo": ColetaCasaRepo,
        "CasaRepo": CasaRepo,
        "ApostaRepo": ApostaRepo,
    }
    banco.sessao = Sessoes().abrir
    return banco


def _cliente(monkeypatch, banco):
    for nome, classe in banco.repos.items():
        monkeypatch.setattr(coleta, nome, classe)
    monkeypatch.setitem(main.app.dependency_overrides, get_db, banco.sessao)
    monkeypatch.setattr(
        coleta,
        "_enfileirar",
        lambda usuario_id, fila: banco.enfileiradas.append((usuario_id, fila)),
    )
    monkeypatch.setattr(coleta.limiter, "enabled", False)
    return TestClient(main.app)


def _enviar(cliente, bilhetes, *, casa="betano", token=TOKEN, caminho="/api/v1/coleta", **envio):
    corpo = {
        "contrato": 1,
        "casa": casa,
        "capturado_em": "2026-08-04T12:00:00Z",
        "apostas": bilhetes,
        **envio,
    }
    headers = {} if token is None else {coleta.TOKEN_HEADER: token}
    return cliente.post(caminho, json=corpo, headers=headers)


def _recebidas(casa, status):
    return (
        REGISTRY.get_sample_value("coleta_received_total", {"casa": casa, "status": status}) or 0.0
    )


def test_token_is_stored_as_an_hmac_of_the_server_secret(monkeypatch) -> None:
    esperado = hmac.new(b"TEST_COLETA_TOKEN_SECRET", TOKEN.encode(), hashlib.sha256).hexdigest()

    assert coleta.hash_do_token(TOKEN) == esperado
    monkeypatch.setenv("COLETA_TOKEN_SECRET", "outro segredo")
    get_settings.cache_clear()
    try:
        assert coleta.hash_do_token(TOKEN) != esperado
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("token", [None, "token de outra conta"])
def test_send_without_the_token_of_an_account_is_refused(monkeypatch, token) -> None:
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, banco), [_bilhete()], token=token)

    assert resposta.status_code == 403
    assert "o token não confere" in resposta.json()["erro"]
    assert (banco.linhas, banco.commits, banco.enfileiradas) == ({}, 0, [])


def test_daily_limit_is_checked_before_the_send_is_read(monkeypatch) -> None:
    banco = _banco(recebidas_hoje=5000)
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.post("/api/v1/coleta", content=b"{", headers={coleta.TOKEN_HEADER: TOKEN})

    assert resposta.status_code == 429
    assert "amanhã" in resposta.json()["erro"]
    assert banco.desde == datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert ("SELECT set_config('app.current_user_id', :uid, true)", {"uid": "7"}) in banco.sql


@pytest.mark.parametrize(
    ("envio", "motivo"),
    [
        ({"contrato": 2}, "não conhece o formato"),
        ({"apostas": "nope"}, "sem a lista de apostas"),
        ({"casa": "bet ano"}, "envio quebrado"),
        ({"casa": "rodri"}, "não conheço a casa 'rodri'"),
    ],
)
def test_broken_send_is_refused_with_a_reason_the_extension_shows(
    monkeypatch, envio, motivo
) -> None:
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, banco), [_bilhete()], **envio)

    assert resposta.status_code == 400
    assert motivo in resposta.json()["erro"]
    assert "detail" not in resposta.json()
    assert banco.linhas == {}


def test_send_that_is_not_json_or_too_big_is_refused(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    headers = {coleta.TOKEN_HEADER: TOKEN}

    quebrado = cliente.post("/api/v1/coleta", content=b"{", headers=headers)
    monkeypatch.setattr(coleta, "TAMANHO_MAXIMO", 10)
    grande = _enviar(cliente, [_bilhete()])
    monkeypatch.setattr(coleta, "TAMANHO_MAXIMO", 5 * 1024 * 1024)
    monkeypatch.setattr(coleta, "APOSTAS_POR_ENVIO", 1)
    demais = _enviar(cliente, [_bilhete("1"), _bilhete("2")])

    assert (quebrado.status_code, quebrado.json()["erro"]) == (
        400,
        "não entendi o que a extensão mandou",
    )
    assert (grande.status_code, demais.status_code) == (400, 400)
    assert "grande demais" in grande.json()["erro"]
    assert "apostas demais" in demais.json()["erro"]


def test_new_bet_is_stored_and_sent_to_materialization(monkeypatch) -> None:
    banco = _banco()
    novas = _recebidas("betano", "nova")

    resposta = _enviar(_cliente(monkeypatch, banco), [_bilhete()])

    assert resposta.status_code == 200
    assert resposta.json() == {
        "antes_do_inicio": 0,
        "novas_contando": 1,
        "iguais_a_existentes": 0,
        "em_duvida": 0,
        "atualizadas": 0,
        "ja_conhecidas": 0,
        "recusadas": [],
        "sem_leitor": [],
    }
    (linha,) = banco.linhas.values()
    assert (linha.usuario_id, linha.casa_id, linha.identidade) == (7, 1, "20753556039")
    assert linha.bruto_json == _bilhete()
    assert (banco.commits, banco.enfileiradas) == (1, [(7, [1])])
    assert _recebidas("betano", "nova") == pytest.approx(novas + 1)


def test_resend_of_a_processed_bet_is_known_and_not_queued_again(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    dedup = REGISTRY.get_sample_value("coleta_dedup_total") or 0.0
    _enviar(cliente, [_bilhete()])
    next(iter(banco.linhas.values())).processado_em = datetime.now(UTC)

    resposta = _enviar(cliente, [_bilhete()])

    assert resposta.json()["ja_conhecidas"] == 1
    assert banco.enfileiradas == [(7, [1]), (7, [])]
    assert REGISTRY.get_sample_value("coleta_dedup_total") == pytest.approx(dedup + 1)


def test_resend_of_a_bet_not_processed_yet_goes_back_to_the_queue(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    _enviar(cliente, [_bilhete()])

    resposta = _enviar(cliente, [_bilhete()])

    assert resposta.json()["ja_conhecidas"] == 1
    assert banco.enfileiradas == [(7, [1]), (7, [1])]


def test_bet_captured_again_after_settling_is_updated_and_queued(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    _enviar(cliente, [_bilhete(resultado=None)])

    resposta = _enviar(cliente, [_bilhete(resultado="Win", ganho=304.0)])

    assert resposta.json()["atualizadas"] == 1
    (linha,) = banco.linhas.values()
    assert linha.bruto_json["finalBetResult"] == "Win"
    assert banco.enfileiradas == [(7, [1]), (7, [1])]


def test_unreadable_bet_is_refused_without_stopping_the_others(monkeypatch) -> None:
    banco = _banco()
    sem_id = _bilhete()
    del sem_id["id"]

    resposta = _enviar(_cliente(monkeypatch, banco), [sem_id, _bilhete("2")])

    assert resposta.json()["recusadas"] == [
        {"posicao": 0, "motivo": "a aposta veio sem identificador (`id`)"}
    ]
    assert resposta.json()["novas_contando"] == 1
    assert banco.enfileiradas == [(7, [1])]


@pytest.mark.parametrize("casa", ["betano", "bet365"])
def test_bet_that_cannot_be_stored_is_refused_without_losing_the_send(monkeypatch, casa) -> None:
    banco = _banco()

    resposta = _enviar(
        _cliente(monkeypatch, banco), [_bilhete("1", header="nulo\x00"), _bilhete("2")], casa=casa
    )

    (recusada,) = resposta.json()["recusadas"]
    assert recusada["posicao"] == 0
    assert "não dá para guardar" in recusada["motivo"]
    assert len(banco.linhas) == 1
    assert banco.commits == 1


def test_bet_the_product_cannot_count_keeps_its_raw_proof_but_is_not_queued(monkeypatch) -> None:
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, banco), [_bilhete(odd=1.0)])

    assert resposta.json()["recusadas"] == [
        {
            "posicao": 0,
            "identidade": "20753556039",
            "motivo": "a betano mandou uma aposta que o planilhador não aceita: a odd tem de ser"
            " pelo menos 1.01",
        }
    ]
    assert len(banco.linhas) == 1
    assert banco.enfileiradas == [(7, [])]


def test_new_capture_of_an_existing_bet_skips_the_creation_rules(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    _enviar(cliente, [_bilhete(resultado=None)])
    banco.apostas[7, "c:betano:20753556039"] = SimpleNamespace(estado="PENDENTE")

    resposta = _enviar(cliente, [_bilhete(odd=1.0)])

    assert (resposta.json()["atualizadas"], resposta.json()["recusadas"]) == (1, [])
    assert banco.enfileiradas == [(7, [1]), (7, [1])]


def test_house_without_a_reader_keeps_the_raw_bets_and_says_so(monkeypatch) -> None:
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, banco), [{"ticket": 1}, "não é objeto"], casa="bet365")

    corpo = resposta.json()
    assert resposta.status_code == 200
    ((sem_leitor),) = corpo["sem_leitor"]
    assert (sem_leitor["casa"], sem_leitor["guardadas"]) == ("bet365", 1)
    assert sem_leitor["recado"].startswith("guardei 1 aposta da bet365")
    assert corpo["recusadas"][0]["posicao"] == 1
    (linha,) = banco.linhas.values()
    assert linha.identidade == linha.hash_conteudo
    assert banco.enfileiradas == [(7, [])]


def test_queue_down_after_storing_answers_so_the_extension_sends_again(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)

    def fora_do_ar(usuario_id, fila):
        raise OperationalError("broker down")

    monkeypatch.setattr(coleta, "_enfileirar", fora_do_ar)

    resposta = _enviar(cliente, [_bilhete()])

    assert resposta.status_code == 503
    assert "próximo envio" in resposta.json()["erro"]
    assert (len(banco.linhas), banco.commits) == (1, 1)


def test_path_of_the_current_extension_still_receives_the_send(monkeypatch) -> None:
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, banco), [_bilhete()], caminho="/coleta")

    assert (resposta.status_code, resposta.json()["novas_contando"]) == (200, 1)


def test_too_many_sends_from_one_token_are_refused_with_a_reason(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    monkeypatch.setattr(coleta.limiter, "enabled", True)
    monkeypatch.setenv("COLETA_RATE_LIMIT", "2/minute")
    get_settings.cache_clear()
    coleta.limiter.reset()
    try:
        respostas = [_enviar(cliente, [_bilhete()]) for _ in range(3)]
        de_outro = _enviar(cliente, [_bilhete()], token="token de outra conta")
    finally:
        coleta.limiter.reset()
        get_settings.cache_clear()

    assert [r.status_code for r in respostas] == [200, 200, 429]
    assert "envios demais" in respostas[2].json()["erro"]
    assert de_outro.status_code == 403


def test_limit_key_never_carries_the_raw_token() -> None:
    request = SimpleNamespace(headers={coleta.TOKEN_HEADER: TOKEN})

    assert coleta.chave_do_limite(request) == hashlib.sha256(TOKEN.encode()).hexdigest()
