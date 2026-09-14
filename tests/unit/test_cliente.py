from __future__ import annotations

import json
from datetime import datetime

import anthropic
import httpx2
import pybreaker
import pytest
from pydantic import TypeAdapter

from bancaemdia.config import get_settings
from bancaemdia.extracao import cliente
from bancaemdia.extracao.modelos import Cupons, ExtracaoBilhete

CUPOM = {
    "casa": "Betano",
    "tipo": "simples",
    "evento": "Velez x Instituto",
    "selecoes": [
        {"mercado": "Handicap", "escolha": "Instituto", "linha": None, "odd": 1.82, "evento": None}
    ],
    "odd_total": 1.82,
    "odd_original": None,
    "quando": None,
    "ilegivel": False,
    "confianca": 0.95,
}


def _mensagem(texto=None, stop_reason="end_turn", **uso):
    usage = {
        "input_tokens": 1000,
        "output_tokens": 400,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        **uso,
    }
    content = [] if texto is None else [{"type": "text", "text": texto}]
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        },
    )


def _cupons(*cupons):
    return _mensagem(json.dumps({"cupons": list(cupons)}))


def _erro(status):
    return httpx2.Response(
        status,
        json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        headers={"retry-after-ms": "1"},
    )


def _leitor(*respostas, max_retries=0, breaker=None):
    pedidos = []
    fila = list(respostas)

    def handler(request):
        pedidos.append(json.loads(request.content))
        resposta = fila.pop(0) if len(fila) > 1 else fila[0]
        if resposta == "timeout":
            raise httpx2.ReadTimeout("slow", request=request)
        return resposta

    client = anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=max_retries,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    leitor = cliente.LeitorDeBilhetes(
        client,
        "claude-haiku-4-5",
        "claude-sonnet-5",
        breaker=breaker or cliente.novo_breaker(),
        pausa=lambda _: None,
    )
    return leitor, pedidos


def test_ler_returns_structured_extracao_bilhete() -> None:
    leitor, pedidos = _leitor(_cupons(CUPOM))

    leitura = leitor.ler(
        b"foto", "image/png", legenda=" 1u odd 1.82 ", postada_em=datetime(2026, 7, 24, 16, 0)
    )

    assert leitura.bilhetes == (ExtracaoBilhete.model_validate(CUPOM),)
    assert leitura.modelo == "claude-haiku-4-5"
    assert len(pedidos) == 1
    pedido = pedidos[0]
    assert pedido["model"] == "claude-haiku-4-5"
    assert pedido["max_tokens"] == cliente.MAX_TOKENS
    assert pedido["system"] == [
        {"type": "text", "text": cliente.carregar_prompt(), "cache_control": {"type": "ephemeral"}}
    ]
    assert pedido["output_config"] == {
        "format": {"type": "json_schema", "schema": cliente.ESQUEMA_DOS_CUPONS}
    }
    imagem, data, legenda, instrucao = pedido["messages"][0]["content"]
    assert imagem == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "Zm90bw=="},
    }
    assert data["text"].startswith("Este print foi enviado em 24/07/2026 às 16:00.")
    assert legenda["text"].endswith("NÃO deve ser extraída da imagem.\n\n1u odd 1.82")
    assert instrucao["text"] == (
        "Leia TODOS os cupons de aposta desta imagem, na ordem em que aparecem"
        " (de cima para baixo)."
    )


def test_schema_is_the_one_the_sdk_parse_helper_would_send() -> None:
    assert cliente.ESQUEMA_DOS_CUPONS == anthropic.transform_schema(
        TypeAdapter(Cupons).json_schema()
    )


def test_ler_prices_the_call_including_cache_tokens() -> None:
    resposta = _mensagem(
        json.dumps({"cupons": [CUPOM]}),
        input_tokens=1000,
        output_tokens=400,
        cache_read_input_tokens=5000,
        cache_creation_input_tokens=200,
    )
    leitor, _ = _leitor(resposta)

    leitura = leitor.ler(b"foto")

    assert (
        leitura.tokens_entrada,
        leitura.tokens_saida,
        leitura.tokens_cache_lidos,
        leitura.tokens_cache_gravados,
    ) == (1000, 400, 5000, 200)
    assert leitura.custo_usd == pytest.approx((1000 + 200 * 1.25 + 5000 * 0.1 + 400 * 5) / 1e6)


def test_every_billed_attempt_is_priced() -> None:
    cortada = _mensagem(json.dumps({"cupons": [CUPOM]}), stop_reason="max_tokens")
    leitor, pedidos = _leitor(cortada, _cupons(CUPOM, CUPOM))

    leitura = leitor.ler(b"foto")

    assert len(pedidos) == 2
    assert len(leitura.bilhetes) == 2
    assert (leitura.tokens_entrada, leitura.tokens_saida) == (2000, 800)
    assert leitura.custo_usd == pytest.approx(cliente.calcular_custo("claude-haiku-4-5", 2000, 800))


def test_escalonar_reads_with_the_escalation_model() -> None:
    leitor, pedidos = _leitor(_cupons(CUPOM))

    leitura = leitor.ler(b"foto", escalonar=True)

    assert pedidos[0]["model"] == "claude-sonnet-5"
    assert leitura.modelo == "claude-sonnet-5"
    assert leitura.custo_usd == pytest.approx(cliente.calcular_custo("claude-sonnet-5", 1000, 400))


def test_every_coupon_of_the_image_is_kept_in_order() -> None:
    segundo = {**CUPOM, "odd_total": 3.25}
    leitor, _ = _leitor(_cupons(CUPOM, segundo))

    assert [b.odd_total for b in leitor.ler(b"foto").bilhetes] == [1.82, 3.25]


def test_image_without_coupons_becomes_one_illegible_ticket() -> None:
    leitor, _ = _leitor(_mensagem('{"cupons": []}'))

    assert leitor.ler(b"foto").bilhetes == (ExtracaoBilhete(ilegivel=True),)


def test_answer_without_text_becomes_one_illegible_ticket() -> None:
    leitor, _ = _leitor(_mensagem())

    assert leitor.ler(b"foto").bilhetes == (ExtracaoBilhete(ilegivel=True),)


@pytest.mark.parametrize(
    ("modelo", "esperado"),
    [("claude-haiku-4-5", 0.0075), ("claude-sonnet-5", 0.015), ("claude-opus-5", 0.0375)],
)
def test_calcular_custo_uses_current_prices(modelo, esperado) -> None:
    assert cliente.calcular_custo(modelo, 2500, 1000) == pytest.approx(esperado)


def test_calcular_custo_rejects_an_unpriced_model() -> None:
    with pytest.raises(KeyError):
        cliente.calcular_custo("claude-unknown", 1, 1)


@pytest.mark.parametrize(
    ("modelo", "modelo_escalonamento"),
    [("claude-unknown", "claude-sonnet-5"), ("claude-haiku-4-5", "claude-opus-4-8")],
)
def test_leitor_rejects_an_unpriced_model_before_any_call(modelo, modelo_escalonamento) -> None:
    with pytest.raises(ValueError, match="sem preço cadastrado"):
        cliente.LeitorDeBilhetes(None, modelo, modelo_escalonamento)


def test_montar_conteudo_skips_missing_date_and_blank_caption() -> None:
    conteudo = cliente.montar_conteudo(b"foto", "image/jpeg", "   ")

    assert [bloco["type"] for bloco in conteudo] == ["image", "text"]


def test_montar_conteudo_cuts_the_caption() -> None:
    legenda = cliente.montar_conteudo(b"foto", "image/jpeg", "  " + "a" * 700 + "  ")[1]

    assert legenda["text"].endswith("\n\n" + "a" * cliente.LIMITE_DA_LEGENDA)


@pytest.mark.parametrize(
    ("nome", "tipo"),
    [
        ("print.JPG", "image/jpeg"),
        ("print.jpeg", "image/jpeg"),
        ("print.png", "image/png"),
        ("print.gif", "image/gif"),
        ("print.webp", "image/webp"),
        ("sem_extensao", "image/jpeg"),
        ("", "image/jpeg"),
    ],
)
def test_tipo_da_imagem_follows_the_extension(nome, tipo) -> None:
    assert cliente.tipo_da_imagem(nome) == tipo


def test_carregar_prompt_is_the_v3_prompt() -> None:
    prompt = cliente.carregar_prompt()

    assert cliente.VERSAO_PROMPT == "extrair_bilhete_v3"
    assert "a imagem pode ter VÁRIOS cupons" in prompt
    assert '{"cupons": [...]}' in prompt


def test_truncated_json_is_retried_then_fails_with_what_it_spent() -> None:
    leitor, pedidos = _leitor(_mensagem('{"cupons": [{"casa": "Bet'))

    with pytest.raises(cliente.LeituraFalhouError, match="veio incompleta") as falha:
        leitor.ler(b"foto")

    assert len(pedidos) == cliente.TENTATIVAS_DE_LEITURA
    assert falha.value.custo_usd == pytest.approx(
        cliente.calcular_custo("claude-haiku-4-5", 3000, 1200)
    )


def test_response_always_cut_fails_loudly() -> None:
    leitor, pedidos = _leitor(_mensagem(json.dumps({"cupons": [CUPOM]}), stop_reason="max_tokens"))

    with pytest.raises(cliente.LeituraFalhouError, match="veio cortada"):
        leitor.ler(b"foto")

    assert len(pedidos) == cliente.TENTATIVAS_DE_LEITURA


def test_refusal_fails_instead_of_becoming_not_a_bet() -> None:
    leitor, _ = _leitor(_mensagem(stop_reason="refusal"))

    with pytest.raises(cliente.LeituraFalhouError, match="recusou"):
        leitor.ler(b"foto")


def test_network_errors_are_retried_by_the_sdk_only() -> None:
    leitor, pedidos = _leitor(_erro(529), max_retries=2)

    with pytest.raises(anthropic.APIStatusError):
        leitor.ler(b"foto")

    assert len(pedidos) == 3


def test_breaker_opens_after_five_server_failures() -> None:
    breaker = cliente.novo_breaker()
    leitor, pedidos = _leitor(_erro(500), breaker=breaker)

    for _ in range(4):
        with pytest.raises(anthropic.InternalServerError):
            leitor.ler(b"foto")
    with pytest.raises(pybreaker.CircuitBreakerError):
        leitor.ler(b"foto")
    with pytest.raises(pybreaker.CircuitBreakerError):
        leitor.ler(b"foto")

    assert len(pedidos) == 5
    assert breaker.current_state == "open"


@pytest.mark.parametrize(
    ("resposta", "erro"),
    [
        ("timeout", anthropic.APITimeoutError),
        (_erro(400), anthropic.BadRequestError),
        (_erro(413), anthropic.RequestTooLargeError),
        (_erro(422), anthropic.UnprocessableEntityError),
        (_mensagem('{"cupons": [{"casa"'), cliente.LeituraFalhouError),
    ],
)
def test_breaker_ignores_timeouts_bad_requests_and_bad_json(resposta, erro) -> None:
    breaker = cliente.novo_breaker()
    leitor, _ = _leitor(resposta, breaker=breaker)

    for _ in range(6):
        with pytest.raises(erro):
            leitor.ler(b"foto")

    assert breaker.current_state == "closed"


def test_novo_breaker_matches_the_issue() -> None:
    breaker = cliente.novo_breaker()

    assert breaker.fail_max == 5
    assert breaker.reset_timeout == 60
    assert anthropic.APITimeoutError in breaker.excluded_exceptions
    assert cliente.novo_breaker() is not breaker


def test_get_leitor_reads_settings(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_TIMEOUT", "12")
    monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", "4")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("ANTHROPIC_ESCALATION_MODEL", "claude-opus-5")
    get_settings.cache_clear()
    cliente.get_leitor.cache_clear()
    try:
        leitor = cliente.get_leitor()
    finally:
        get_settings.cache_clear()
        cliente.get_leitor.cache_clear()

    assert leitor.client.max_retries == 4
    assert leitor.client.timeout == 12
    assert (leitor.modelo, leitor.modelo_escalonamento) == ("claude-haiku-4-5", "claude-opus-5")
    assert leitor.breaker is cliente.anthropic_breaker
