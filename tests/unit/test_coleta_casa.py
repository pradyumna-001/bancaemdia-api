from __future__ import annotations

import pytest

from bancaemdia.coleta.leitores import LEITORES
from bancaemdia.coleta.leitura import Coletada, ColetaInvalidaError, Escolha
from bancaemdia.domain import coleta_casa
from bancaemdia.domain.coleta_casa import ApostaInvalidaError


def _coletada(**campos):
    padrao = {
        "identidade": "20753556039",
        "casa": "betano",
        "tipo": "SIMPLES",
        "odd": 1.90,
        "stake_centavos": 16000,
        "data_aposta": "2026-08-02T19:30:00",
        "escolhas": (Escolha(descricao="Mais de 41.5", mercado="Total de laterais"),),
        "evento": "Internacional - Corinthians",
        "mercado_bruto": "Total de laterais",
        "descricao": "Mais de 41.5 (Total de laterais)",
        "comeca_em": "2026-08-02T19:30:00",
    }
    return Coletada(**{**padrao, **campos})


def _tipos(eventos):
    return [(e.tipo, e.fonte) for e in eventos]


def test_house_name_of_the_send_is_lowercased_and_trimmed() -> None:
    assert coleta_casa.casa_do_envio(" KTO ") == "kto"


@pytest.mark.parametrize("casa", ["", "b", "bet ano", "betano/../x", "a" * 41, "-betano"])
def test_broken_house_name_is_refused_as_a_broken_send(casa) -> None:
    with pytest.raises(ColetaInvalidaError, match="envio quebrado"):
        coleta_casa.casa_do_envio(casa)


def test_key_is_the_house_and_its_ticket_identity() -> None:
    assert coleta_casa.chave_casa("betano", "20753556039") == "c:betano:20753556039"


def test_content_hash_ignores_key_order_and_follows_the_content() -> None:
    assert coleta_casa.hash_do_conteudo({"a": 1, "b": "ç"}) == coleta_casa.hash_do_conteudo({
        "b": "ç",
        "a": 1,
    })
    assert coleta_casa.hash_do_conteudo({"a": 1}) != coleta_casa.hash_do_conteudo({"a": 2})


@pytest.mark.parametrize(
    "bruto",
    [
        {"header": "nulo\x00"},
        {"nulo\x00": 1},
        {"legs": [{"eventName": "meio emoji \ud83d"}]},
        {"totalOdds": float("nan")},
        {"totalAmount": float("inf")},
    ],
)
def test_bet_postgres_cannot_store_is_refused_with_a_reason(bruto) -> None:
    with pytest.raises(ColetaInvalidaError, match="não dá para guardar"):
        coleta_casa.conferir_guardavel(bruto)


def test_bet_with_ordinary_text_and_numbers_can_be_stored() -> None:
    coleta_casa.conferir_guardavel({"a": [1, 2.5, "Grêmio ⚽", None, True], "b": {"c": ""}})


def test_official_house_name_leads_back_to_the_house_the_extension_sends() -> None:
    assert coleta_casa.casa_da_coleta("KTO") == "kto"
    assert coleta_casa.casa_da_coleta("Esportiva Bet") == "esportiva"
    assert coleta_casa.casa_da_coleta("Betano") == "betano"
    assert coleta_casa.casa_da_coleta("Bet365") is None


def test_valid_bet_passes_validation() -> None:
    coleta_casa.validar(_coletada(), 10_000)


@pytest.mark.parametrize(
    ("campos", "valor", "motivo"),
    [
        ({"odd": 1.0}, 10_000, "a odd tem de ser pelo menos 1.01"),
        ({"odd": 1500.0}, 10_000, "odd 1500 é alta demais para ser real"),
        ({"stake_centavos": 0}, 10_000, "o valor apostado tem de ser positivo"),
        ({"estado": "MEIO"}, 10_000, "estado desconhecido: 'MEIO'"),
        ({"estado": "CASHOUT"}, 10_000, "cashout precisa do valor que a casa pagou"),
        ({"casa": " "}, 10_000, "falta a casa de apostas"),
        ({}, 0, "o valor da unidade tem de ser positivo"),
    ],
)
def test_bet_that_cannot_be_counted_is_refused_with_every_reason(campos, valor, motivo) -> None:
    with pytest.raises(ApostaInvalidaError, match=motivo):
        coleta_casa.validar(_coletada(**campos), valor)


def test_all_reasons_are_joined() -> None:
    with pytest.raises(ApostaInvalidaError) as erro:
        coleta_casa.validar(_coletada(odd=1.0, stake_centavos=-1), 10_000)

    assert (
        str(erro.value) == "a odd tem de ser pelo menos 1.01; o valor apostado tem de ser positivo"
    )


def test_open_bet_is_created_in_units_of_its_day_with_the_house_as_source() -> None:
    (criada,) = coleta_casa.eventos_da_criacao(_coletada(), 2_000)

    assert (criada.tipo, criada.fonte, criada.confianca) == ("APOSTA_CRIADA", "casa", 1.0)
    assert criada.payload["origem"] == "casa"
    assert criada.payload["casa"] == "betano"
    assert criada.payload["stake_unidades"] == pytest.approx(8.0)
    assert criada.payload["valor_unidade_centavos"] == 2_000
    assert (criada.payload["revisao_motivo"], criada.payload["revisao_grave"]) == (None, False)
    assert criada.payload["tipo_aposta"] == "SIMPLES"


def test_settled_bet_is_created_with_what_the_house_paid() -> None:
    eventos = coleta_casa.eventos_da_criacao(_coletada(estado="GREEN", retorno_centavos=30400), 1)

    assert _tipos(eventos) == [("APOSTA_CRIADA", "casa"), ("RESULTADO_REGISTRADO", "casa")]
    assert eventos[1].payload == {
        "estado": "GREEN",
        "comissao_centavos": 0,
        "retorno_centavos": 30400,
    }


def test_settled_bet_without_the_paid_value_leaves_the_return_to_the_formula() -> None:
    eventos = coleta_casa.eventos_da_criacao(_coletada(estado="RED", retorno_centavos=None), 1)

    assert eventos[1].payload == {"estado": "RED", "comissao_centavos": 0}


def test_cashout_is_created_with_its_own_event() -> None:
    eventos = coleta_casa.eventos_da_criacao(_coletada(estado="CASHOUT", retorno_centavos=95), 1)

    assert eventos[1].tipo == "CASHOUT_REGISTRADO"
    assert eventos[1].payload == {"retorno_centavos": 95}


def test_held_bet_is_created_grave_with_its_reason() -> None:
    (criada,) = coleta_casa.eventos_da_criacao(_coletada(motivo_retencao="bônus"), 10_000)

    assert (criada.payload["revisao_motivo"], criada.payload["revisao_grave"]) == ("bônus", True)


def test_resend_without_changes_creates_nothing() -> None:
    atual = {"estado": "RED", "retorno_centavos": 0}

    assert (
        coleta_casa.eventos_do_resultado(_coletada(estado="RED", retorno_centavos=0), atual) == []
    )


def test_open_bet_that_settles_records_the_result() -> None:
    (evento,) = coleta_casa.eventos_do_resultado(
        _coletada(estado="GREEN", retorno_centavos=30400), {}
    )

    assert evento.payload == {"estado": "GREEN", "comissao_centavos": 0, "retorno_centavos": 30400}


def test_result_follows_the_latest_capture_including_a_reopened_bet() -> None:
    corrigida = coleta_casa.eventos_do_resultado(
        _coletada(estado="RED", retorno_centavos=0), {"estado": "GREEN", "retorno_centavos": 30400}
    )
    reaberta = coleta_casa.eventos_do_resultado(
        _coletada(), {"estado": "RED", "retorno_centavos": 0}
    )

    assert corrigida[0].payload["estado"] == "RED"
    assert reaberta[0].payload == {
        "estado": "PENDENTE",
        "comissao_centavos": 0,
        "retorno_centavos": None,
    }


def test_new_paid_value_alone_is_a_change() -> None:
    (evento,) = coleta_casa.eventos_do_resultado(
        _coletada(estado="CASHOUT", retorno_centavos=90),
        {"estado": "CASHOUT", "retorno_centavos": 95},
    )

    assert evento.payload == {"retorno_centavos": 90}


def test_reason_to_hold_travels_with_the_settlement_but_never_overwrites_one() -> None:
    coletada = _coletada(estado="GREEN", retorno_centavos=None, motivo_retencao="sem valor pago")

    nova = coleta_casa.eventos_do_resultado(coletada, {"estado": "PENDENTE"})
    ja_tinha = coleta_casa.eventos_do_resultado(
        coletada, {"estado": "GREEN", "revisao_motivo": "x"}
    )

    assert _tipos(nova) == [("RESULTADO_REGISTRADO", "casa"), ("CORRECAO_MANUAL", "casa")]
    assert nova[1].payload == {"revisao_motivo": "sem valor pago", "revisao_grave": True}
    assert ja_tinha == []


def test_message_for_a_house_without_a_reader_names_the_houses_read_today() -> None:
    recado = coleta_casa.recado_sem_leitor("bet365", 1, LEITORES)

    assert recado.startswith("guardei 1 aposta da bet365 e ainda não sei ler o formato dela")
    assert "nada entrou nas suas contas" in recado
    assert recado.endswith("betano, betfair, betmgm, esportiva, kto, superbet")
    assert "guardei 3 apostas" in coleta_casa.recado_sem_leitor("bet365", 3, LEITORES)


def test_daily_limit_is_off_when_unset_and_says_when_it_frees() -> None:
    assert coleta_casa.recado_de_teto(9_999, 0) is None
    assert coleta_casa.recado_de_teto(4_999, 5_000) is None

    recado = coleta_casa.recado_de_teto(5_000, 5_000)

    assert recado is not None
    assert "5000 apostas hoje" in recado
    assert "amanhã" in recado


def test_result_json_keeps_the_contract_the_extension_reads() -> None:
    resultado = coleta_casa.Resultado(novas_contando=2, recusadas=[{"posicao": 0, "motivo": "x"}])

    assert resultado.para_json() == {
        "antes_do_inicio": 0,
        "novas_contando": 2,
        "iguais_a_existentes": 0,
        "em_duvida": 0,
        "atualizadas": 0,
        "ja_conhecidas": 0,
        "recusadas": [{"posicao": 0, "motivo": "x"}],
        "sem_leitor": [],
    }
