from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from prometheus_client import REGISTRY

from bancaemdia.cache.extracao_cache import ExtracaoCache
from bancaemdia.extracao import cliente, rodada
from bancaemdia.extracao.cliente import Leitura, LeituraFalhouError
from bancaemdia.extracao.escada import Degrau
from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao
from bancaemdia.resilience import circuit_breaker

URL = "https://api.anthropic.com/v1/messages"


def _bom(**campos):
    return ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="Velez x Instituto",
        selecoes=[Selecao(mercado="Handicap", escolha="Instituto", odd=1.82)],
        odd_total=1.82,
        confianca=0.95,
    ).model_copy(update=campos)


def _ruim():
    return _bom(odd_total=9.9)


def _leitor(barato=None, caro=None, erro=None):
    class Leitor:
        modelo_escalonamento = "claude-sonnet-5"

        def ler(self, imagem, tipo="image/jpeg", *, legenda="", postada_em=None, escalonar=False):
            if erro is not None:
                raise erro
            if escalonar:
                return Leitura(tuple(caro or barato), "claude-sonnet-5", custo_usd=0.06)
            return Leitura(tuple(barato), "claude-haiku-4-5", custo_usd=0.012)

    return Leitor()


def _cache():
    class Redis:
        def __init__(self):
            self.dados = {}

        def get(self, nome):
            return self.dados.get(nome)

        def set(self, nome, valor, ex=None, nx=False):
            if nx and nome in self.dados:
                return None
            self.dados[nome] = valor.encode()
            return True

    return ExtracaoCache(Redis())


def _erro_da_api(classe, status):
    resposta = httpx2.Response(status, request=httpx2.Request("POST", URL))
    return classe("boom", response=resposta, body=None)


def _leitor_de_verdade(*respostas_por_modelo):
    pedidos = []
    respostas = dict(respostas_por_modelo)

    def handler(request):
        corpo = json.loads(request.content)
        pedidos.append(corpo["model"])
        return respostas[corpo["model"]]

    client = anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    leitor = cliente.LeitorDeBilhetes(
        client,
        "claude-haiku-4-5",
        "claude-sonnet-5",
        breaker=circuit_breaker.new_anthropic_breaker(),
        pausa=lambda _: None,
    )
    return leitor, pedidos


def _mensagem(texto):
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude",
            "content": [{"type": "text", "text": texto}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 400},
        },
    )


def test_approved_single_ticket_has_no_review() -> None:
    leitura = rodada.ler_mensagem(_leitor([_bom()]), b"foto")

    assert leitura == rodada.LeituraDaMensagem(
        bilhete=_bom(), degrau=Degrau.BARATO, custo_usd=pytest.approx(0.012)
    )


def test_failed_read_becomes_a_grave_review_with_its_cost() -> None:
    leitura = rodada.ler_mensagem(
        _leitor(erro=LeituraFalhouError("cortada", custo_usd=0.03)), b"foto"
    )

    assert leitura == rodada.LeituraDaMensagem(
        motivo="a leitura falhou (LeituraFalhouError) — preencha a odd olhando o print",
        grave=True,
        custo_usd=pytest.approx(0.03),
    )


@pytest.mark.parametrize(
    ("classe", "status"),
    [
        (anthropic.BadRequestError, 400),
        (anthropic.RequestTooLargeError, 413),
        (anthropic.UnprocessableEntityError, 422),
    ],
)
def test_errors_caused_by_the_slip_become_a_grave_review(classe, status) -> None:
    leitura = rodada.ler_mensagem(_leitor(erro=_erro_da_api(classe, status)), b"foto")

    assert leitura.grave
    assert (
        leitura.motivo == f"a leitura falhou ({classe.__name__}) — preencha a odd olhando o print"
    )


@pytest.mark.parametrize(
    "erro",
    [
        anthropic.APIConnectionError(request=httpx2.Request("POST", URL)),
        _erro_da_api(anthropic.InternalServerError, 500),
        _erro_da_api(anthropic.AuthenticationError, 401),
    ],
)
def test_other_errors_on_the_first_read_propagate_to_the_worker(erro) -> None:
    with pytest.raises(type(erro)):
        rodada.ler_mensagem(_leitor(erro=erro), b"foto")


def test_expensive_read_failure_after_a_paid_cheap_read_is_not_retried() -> None:
    leitor, pedidos = _leitor_de_verdade(
        ("claude-haiku-4-5", _mensagem(json.dumps({"cupons": [_ruim().model_dump()]}))),
        ("claude-sonnet-5", httpx2.Response(500, json={"type": "error", "error": {}})),
    )

    leitura = rodada.ler_mensagem(leitor, b"foto")

    assert pedidos == ["claude-haiku-4-5", "claude-sonnet-5"]
    assert leitura == rodada.LeituraDaMensagem(
        motivo="a leitura falhou (InternalServerError) — preencha a odd olhando o print",
        grave=True,
        custo_usd=pytest.approx(cliente.calcular_custo("claude-haiku-4-5", 1000, 400)),
    )


def test_not_a_bet_keeps_the_cost_and_no_ticket() -> None:
    leitura = rodada.ler_mensagem(_leitor([ExtracaoBilhete(ilegivel=True)]), b"foto")

    assert leitura.nao_e_aposta
    assert leitura.bilhete is None
    assert leitura.motivo is None
    assert leitura.custo_usd == pytest.approx(0.012)


def test_grave_opinion_after_escalation_holds_the_ticket() -> None:
    leitura = rodada.ler_mensagem(_leitor([_ruim()], [_ruim()]), b"foto")

    assert leitura.degrau is Degrau.CARO
    assert leitura.grave
    assert leitura.motivo.startswith("coerência das odds:")
    assert leitura.custo_usd == pytest.approx(0.072)


def test_multi_coupon_image_carries_each_opinion() -> None:
    leitura = rodada.ler_mensagem(_leitor([_bom(), _bom(confianca=0.55)]), b"foto")

    primeiro, segundo = leitura.cupons
    assert primeiro == rodada.CupomLido(_bom())
    assert segundo.motivo == "confiança: 0.55 abaixo do mínimo 0.80"
    assert not segundo.grave
    assert leitura.bilhete == _bom()
    assert leitura.motivo == segundo.motivo


def test_para_json_is_json_serializable() -> None:
    leitura = rodada.ler_mensagem(_leitor([_bom(), _bom(confianca=0.55)]), b"foto")

    dados = json.loads(json.dumps(leitura.para_json()))

    assert dados["degrau"] == "BARATO"
    assert dados["bilhete"]["odd_total"] == pytest.approx(1.82)
    assert [c["grave"] for c in dados["cupons"]] == [False, False]
    assert rodada.LeituraDaMensagem().para_json() == {
        "bilhete": None,
        "motivo": None,
        "grave": False,
        "cupons": [],
        "degrau": None,
        "custo_usd": 0.0,
        "nao_e_aposta": False,
    }


def test_reprocessing_the_same_image_hits_the_cache_without_calling_anthropic() -> None:
    leitor, pedidos = _leitor_de_verdade(
        ("claude-haiku-4-5", _mensagem(json.dumps({"cupons": [_bom().model_dump()]}))),
    )
    cache = _cache()
    hits = REGISTRY.get_sample_value("extraction_cache_hit_total") or 0.0

    primeira = rodada.ler_mensagem(leitor, b"foto", legenda="1u", cache=cache)
    segunda = rodada.ler_mensagem(leitor, b"foto", legenda="1u", cache=cache)

    assert pedidos == ["claude-haiku-4-5"]
    assert (primeira.degrau, segunda.degrau) == (Degrau.BARATO, Degrau.CACHE)
    assert primeira.custo_usd == pytest.approx(
        cliente.calcular_custo("claude-haiku-4-5", 1000, 400)
    )
    assert segunda.custo_usd == pytest.approx(0.0)
    assert segunda.bilhete == primeira.bilhete
    assert REGISTRY.get_sample_value("extraction_cache_hit_total") == pytest.approx(hits + 1)
