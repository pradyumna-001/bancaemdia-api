from __future__ import annotations

import pytest

from bancaemdia.domain import materializar
from bancaemdia.domain.conferencias import Bilhete, Selecao, TipoBilhete
from bancaemdia.domain.materializar import CupomLido, LeituraRecebida


def _bilhete(**campos):
    base = {
        "casa": "Betano",
        "tipo": TipoBilhete.SIMPLES,
        "evento": "Velez x Instituto",
        "selecoes": (Selecao("Handicap", "Instituto", linha=-0.5, odd=1.82),),
        "odd_total": 1.82,
        "quando": "2026-07-24T19:00",
        "confianca": 0.95,
    }
    return Bilhete(**{**base, **campos})


def _apostas(leitura, **opcoes):
    padrao = {
        "chat_id": 100,
        "message_id": 200,
        "data": "2026-07-24T16:00:00",
        "valor_unidade_centavos": 10_000,
    }
    return materializar.apostas_da_leitura(leitura, **{**padrao, **opcoes})


def _criada(leitura=None, **mudancas):
    (nova,) = _apostas(leitura or LeituraRecebida(bilhete=_bilhete()))
    return nova, {**nova.payload, **mudancas}


@pytest.mark.parametrize(
    ("lido", "oficial"),
    [
        ("Betano", "Betano"),
        ("BETANO", "Betano"),
        ("bet 365", "bet365"),
        ("Vaidbet", "VaideBet"),
        ("Rei do Pitaco", "Pitaco"),
        ("esportiva", "Esportiva Bet"),
        ("Esportes da Sorte", "Esportes da Sorte"),
        ("betão", "Betão"),
    ],
)
def test_known_houses_and_variants_map_to_the_official_name(lido, oficial) -> None:
    assert materializar.casa_canonica(lido) == oficial


@pytest.mark.parametrize("lido", ["Rodri", "Jackpot365", "", "   ", None])
def test_unknown_or_empty_house_is_not_recognized(lido) -> None:
    assert materializar.casa_canonica(lido) is None


def test_telegram_reading_becomes_one_bet_with_the_original_payload() -> None:
    (aposta,) = _apostas(LeituraRecebida(bilhete=_bilhete()))

    assert (aposta.chave, aposta.origem, aposta.ordem) == ("t:100:200:0", "telegram", 0)
    assert aposta.payload == {
        "origem": "telegram",
        "data_aposta": "2026-07-24T16:00:00",
        "chat_id": 100,
        "message_id": 200,
        "ordem_na_mensagem": 0,
        "casa": "Betano",
        "tipster": None,
        "evento": "Velez x Instituto",
        "descricao": "Instituto (Handicap)",
        "mercado_bruto": "Handicap",
        "tipo_aposta": "SIMPLES",
        "odd": 1.82,
        "odd_original": None,
        "comeca_em": "2026-07-24T19:00",
        "stake_unidades": 0.0,
        "valor_unidade_centavos": 10_000,
        "freebet": False,
        "selecionada": True,
        "revisao_motivo": None,
        "revisao_grave": False,
    }


@pytest.mark.parametrize(
    "leitura",
    [
        LeituraRecebida(nao_e_aposta=True),
        LeituraRecebida(bilhete=_bilhete(ilegivel=True)),
        LeituraRecebida(),
        LeituraRecebida(cupons=(CupomLido(_bilhete(ilegivel=True)),)),
    ],
)
def test_readings_that_are_not_a_bet_create_nothing(leitura) -> None:
    assert _apostas(leitura) == []


def test_failed_reading_still_creates_a_grave_bet_to_fill_by_hand() -> None:
    (aposta,) = _apostas(LeituraRecebida(motivo="a leitura falhou (APIError)", grave=True))

    assert aposta.payload["descricao"] == materializar.BILHETE_NAO_LIDO
    assert aposta.payload["tipo_aposta"] == "SIMPLES"
    assert aposta.payload["odd"] is None
    assert (aposta.revisao_grave, aposta.confianca, aposta.casa) == (True, 0.0, None)


def test_unknown_house_stays_out_of_the_bet_and_is_explained() -> None:
    (aposta,) = _apostas(
        LeituraRecebida(bilhete=_bilhete(casa="Rodri"), motivo="confiança: 0.55 abaixo")
    )

    assert aposta.casa is None
    assert aposta.revisao_motivo == (
        "confiança: 0.55 abaixo · casa não reconhecida: li 'Rodri', que não é casa de apostas"
        " — preencha na planilha"
    )
    assert aposta.revisao_grave is False


def test_single_useful_coupon_carries_its_own_review() -> None:
    leitura = LeituraRecebida(
        bilhete=_bilhete(odd_total=9.0),
        motivo="da mensagem",
        cupons=(
            CupomLido(_bilhete(ilegivel=True)),
            CupomLido(_bilhete(odd_total=2.0), "odd x texto: diverge", True),
        ),
    )

    (aposta,) = _apostas(leitura)

    assert aposta.payload["odd"] == pytest.approx(2.0)
    assert aposta.revisao_motivo == "da mensagem · odd x texto: diverge"
    assert aposta.revisao_grave is True


def test_every_coupon_of_a_multi_coupon_image_is_its_own_grave_bet() -> None:
    aviso = (
        "a foto tem 2 cupons e o texto 0 stake(s) — preencha a stake de cada cupom olhando a foto"
    )
    leitura = LeituraRecebida(
        bilhete=_bilhete(odd_total=1.5),
        cupons=(
            CupomLido(_bilhete(odd_total=1.5)),
            CupomLido(_bilhete(casa="Rodri", odd_total=3.0), "confiança: baixa"),
        ),
    )

    primeira, segunda = _apostas(leitura)

    assert [primeira.chave, segunda.chave] == ["t:100:200:0", "t:100:200:1"]
    assert [primeira.payload["odd"], segunda.payload["odd"]] == pytest.approx([1.5, 3.0])
    assert primeira.revisao_motivo == aviso
    assert segunda.revisao_motivo == (
        f"confiança: baixa · {aviso} · casa não reconhecida: li 'Rodri', que não é casa de"
        " apostas — preencha na planilha"
    )
    assert primeira.revisao_grave and segunda.revisao_grave


def test_description_and_market_skip_what_was_not_read() -> None:
    bilhete = _bilhete(selecoes=(Selecao("—", ""), Selecao("Gols", "Mais de 2.5")))

    assert materializar.descricao(bilhete) == "Mais de 2.5 (Gols)"
    assert materializar.mercado_principal(bilhete) == "Gols"
    assert materializar.descricao(_bilhete(selecoes=())) == materializar.SEM_SELECAO_LIDA
    assert materializar.mercado_principal(_bilhete(selecoes=())) is None


def test_projetar_folds_the_history_and_protects_what_the_person_fixed() -> None:
    estado, protegidos = materializar.projetar([
        ("APOSTA_CRIADA", "ia", {"odd": 1.8, "casa": "Betano", "revisao_motivo": None}),
        ("ODD_ALTERADA", "export", {"de": 1.8, "para": 1.9}),
        ("CORRECAO_MANUAL", "manual", {"evento": "A x B"}),
        ("CORRECAO_MANUAL", "ia", {"descricao": "lida de novo"}),
        ("APOSTA_CANCELADA", "export", {"motivo": "a mensagem não tem mais esta aposta"}),
        ("STAKE_ALTERADA", "export", {"de": 0.0, "para": 2.0}),
        ("RESULTADO_REGISTRADO", "export", {"estado": "GREEN"}),
    ])

    assert estado["odd"] == pytest.approx(1.9)
    assert estado["stake_unidades"] == pytest.approx(2.0)
    assert (estado["casa"], estado["evento"], estado["descricao"]) == (
        "Betano",
        "A x B",
        "lida de novo",
    )
    assert estado["revisao_motivo"] == "a mensagem não tem mais esta aposta"
    assert estado["revisao_grave"] is False
    assert protegidos == {"evento"}


def test_bet_the_person_deleted_keeps_its_review_state() -> None:
    estado, _ = materializar.projetar([
        ("APOSTA_CRIADA", "ia", {"revisao_motivo": "coerência: diverge", "revisao_grave": True}),
        ("APOSTA_CANCELADA", "manual", {"motivo": materializar.MOTIVO_APAGADA}),
    ])

    assert (estado["revisao_motivo"], estado["revisao_grave"]) == ("coerência: diverge", True)


def test_resend_with_nothing_new_emits_nothing() -> None:
    nova, atual = _criada()

    assert materializar.eventos_da_releitura(nova, atual, set()) == []


def test_changed_odd_is_a_fact_of_the_world() -> None:
    nova, atual = _criada(odd=1.5)

    (evento,) = materializar.eventos_da_releitura(nova, atual, set())

    assert (evento.tipo, evento.fonte) == ("ODD_ALTERADA", "export")
    assert evento.payload == {"de": 1.5, "para": 1.82}


def test_odd_within_rounding_is_not_a_change() -> None:
    nova, atual = _criada(odd=1.823)

    assert materializar.eventos_da_releitura(nova, atual, set()) == []


def test_missing_odd_is_a_better_reading() -> None:
    (falhou,) = _apostas(LeituraRecebida(motivo="a leitura falhou (APIError)", grave=True))
    (nova,) = _apostas(LeituraRecebida(bilhete=_bilhete()))

    (evento,) = materializar.eventos_da_releitura(nova, falhou.payload, set())

    assert (evento.tipo, evento.fonte, evento.confianca) == ("CORRECAO_MANUAL", "ia", 0.95)
    assert evento.payload == {
        "casa": "Betano",
        "evento": "Velez x Instituto",
        "descricao": "Instituto (Handicap)",
        "comeca_em": "2026-07-24T19:00",
        "mercado_bruto": "Handicap",
        "odd": 1.82,
        "revisao_motivo": None,
        "revisao_grave": False,
    }


def test_what_the_person_fixed_is_never_overwritten() -> None:
    nova, atual = _criada(odd=1.5, evento="Meu evento", revisao_motivo="velho")

    eventos = materializar.eventos_da_releitura(nova, atual, {"odd", "evento"})

    assert eventos == []


def test_missing_date_is_filled_once_from_the_message() -> None:
    nova, sem_data = _criada(data_aposta=None)
    _, com_data = _criada()

    (evento,) = materializar.eventos_da_releitura(nova, sem_data, set())

    assert (evento.tipo, evento.fonte) == ("CORRECAO_MANUAL", "export")
    assert evento.payload == {"data_aposta": "2026-07-24T16:00:00"}
    assert materializar.eventos_da_releitura(nova, com_data, set()) == []
    assert materializar.eventos_da_releitura(nova, sem_data, {"data_aposta"}) == []


def test_review_mark_follows_todays_reading() -> None:
    nova, atual = _criada(revisao_motivo="confiança baixa", revisao_grave=True)

    (evento,) = materializar.eventos_da_releitura(nova, atual, set())

    assert evento.payload == {"revisao_motivo": None, "revisao_grave": False}


def test_failed_resend_only_fills_the_date() -> None:
    (nova,) = _apostas(LeituraRecebida(motivo="a leitura falhou (APIError)", grave=True))
    _, atual = _criada(data_aposta=None)

    (evento,) = materializar.eventos_da_releitura(nova, atual, set())

    assert evento.payload == {"data_aposta": "2026-07-24T16:00:00"}


def _da_casa(*resultados):
    criada = (
        "APOSTA_CRIADA",
        "casa",
        {"origem": "casa", "odd": 1.9, "stake_unidades": 1.6, "valor_unidade_centavos": 10_000},
    )
    return materializar.projetar([criada, *resultados])[0]


def test_paid_value_from_the_house_is_a_fact() -> None:
    estado = _da_casa((
        "RESULTADO_REGISTRADO",
        "casa",
        {"estado": "GREEN", "retorno_centavos": 29000},
    ))

    assert (estado["estado"], estado["retorno_centavos"], estado["retorno_informado"]) == (
        "GREEN",
        29000,
        True,
    )


def test_result_without_a_paid_value_follows_the_formula() -> None:
    estado = _da_casa(("RESULTADO_REGISTRADO", "casa", {"estado": "GREEN", "comissao_centavos": 0}))

    assert (estado["retorno_centavos"], estado["retorno_informado"]) == (30400, False)


def test_reopened_bet_clears_the_return_and_the_fact_mark() -> None:
    estado = _da_casa(
        ("CASHOUT_REGISTRADO", "casa", {"retorno_centavos": 95}),
        ("RESULTADO_REGISTRADO", "casa", {"estado": "PENDENTE", "retorno_centavos": None}),
    )

    assert (estado["estado"], estado["retorno_centavos"], estado["retorno_informado"]) == (
        "PENDENTE",
        None,
        False,
    )


def test_cashout_keeps_what_was_received_even_when_the_stake_changes() -> None:
    estado = _da_casa(
        ("CASHOUT_REGISTRADO", "casa", {"retorno_centavos": 95}),
        ("STAKE_ALTERADA", "manual", {"de": 1.6, "para": 3.2}),
    )

    assert (estado["estado"], estado["retorno_centavos"]) == ("CASHOUT", 95)


def test_stake_changed_after_the_result_recalculates_the_return() -> None:
    estado = _da_casa(
        ("RESULTADO_REGISTRADO", "export", {"estado": "GREEN"}),
        ("STAKE_ALTERADA", "manual", {"de": 1.6, "para": 3.2}),
    )

    assert estado["retorno_centavos"] == 60800


def test_return_fixed_by_hand_is_a_fact() -> None:
    estado = _da_casa(
        ("RESULTADO_REGISTRADO", "casa", {"estado": "GREEN"}),
        ("CORRECAO_MANUAL", "manual", {"retorno_centavos": 12345}),
        ("ODD_ALTERADA", "export", {"de": 1.9, "para": 2.5}),
    )

    assert (estado["retorno_centavos"], estado["retorno_informado"]) == (12345, True)


def test_unknown_state_has_no_return() -> None:
    estado = _da_casa(("RESULTADO_REGISTRADO", "casa", {"estado": "MEIO"}))

    assert estado["retorno_centavos"] is None


def test_telegram_history_without_results_gains_no_result_fields() -> None:
    estado, _ = materializar.projetar([
        ("APOSTA_CRIADA", "ia", {"origem": "telegram", "odd": 1.82, "stake_unidades": 0.0}),
        ("ODD_ALTERADA", "export", {"de": 1.82, "para": 1.9}),
    ])

    assert "estado" not in estado
    assert "retorno_centavos" not in estado
