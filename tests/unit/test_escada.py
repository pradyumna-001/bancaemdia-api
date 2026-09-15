from __future__ import annotations

from datetime import datetime

import anthropic
import httpx2
import pytest

from bancaemdia.domain.conferencias import Forca, Veredito
from bancaemdia.extracao import escada
from bancaemdia.extracao.cliente import Leitura, LeituraFalhouError
from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao

QUANDO = datetime(2026, 7, 24, 16, 0)


def _bom(**campos):
    return ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="Velez x Instituto",
        selecoes=[Selecao(mercado="Handicap", escolha="Instituto", odd=1.82)],
        odd_total=1.82,
        confianca=0.95,
    ).model_copy(update=campos)


def _ruim(**campos):
    return ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="A x B",
        selecoes=[Selecao(mercado="1X2", escolha="A", odd=1.5)],
        odd_total=9.9,
        confianca=0.95,
    ).model_copy(update=campos)


def _ilegivel():
    return ExtracaoBilhete(ilegivel=True)


def _leitor(barato, caro=None, erro_caro=None):
    class Leitor:
        def __init__(self):
            self.chamadas = []

        def ler(self, imagem, tipo="image/jpeg", *, legenda="", postada_em=None, escalonar=False):
            self.chamadas.append((imagem, tipo, legenda, postada_em, escalonar))
            if escalonar:
                if erro_caro is not None:
                    raise erro_caro
                return Leitura(tuple(caro or barato), "claude-sonnet-5", custo_usd=0.06)
            return Leitura(tuple(barato), "claude-haiku-4-5", custo_usd=0.012)

    return Leitor()


def _extrair(leitor, **opcoes):
    return escada.extrair(leitor, b"foto", "image/png", legenda="1u", postada_em=QUANDO, **opcoes)


def test_cheap_read_that_checks_stops_at_the_cheap_step() -> None:
    leitor = _leitor([_bom()])

    saida = _extrair(leitor)

    assert saida.degrau is escada.Degrau.BARATO
    assert saida.custo_usd == pytest.approx(0.012)
    assert saida.parecer.veredito is Veredito.APROVADO
    assert leitor.chamadas == [(b"foto", "image/png", "1u", QUANDO, False)]


def test_cheap_read_that_does_not_check_escalates() -> None:
    leitor = _leitor([_ruim()], [_bom()])

    saida = _extrair(leitor)

    assert saida.degrau is escada.Degrau.CARO
    assert saida.custo_usd == pytest.approx(0.072)
    assert saida.bilhete.odd_total == pytest.approx(1.82)
    assert [c[-1] for c in leitor.chamadas] == [False, True]


def test_expensive_read_that_still_fails_is_returned_for_review() -> None:
    saida = _extrair(_leitor([_ruim()], [_ruim()]))

    assert saida.degrau is escada.Degrau.CARO
    assert saida.parecer.veredito is Veredito.ESCALONAR


def test_failed_expensive_read_keeps_the_cost_already_spent() -> None:
    falha = LeituraFalhouError("cortada", custo_usd=0.03)

    with pytest.raises(escada.EscalonamentoFalhouError) as erro:
        _extrair(_leitor([_ruim()], erro_caro=falha))

    assert erro.value.custo_usd == pytest.approx(0.042)
    assert erro.value.__cause__ is falha


def test_provider_error_on_the_expensive_read_keeps_the_cheap_cost() -> None:
    falha = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com")
    )

    with pytest.raises(escada.EscalonamentoFalhouError) as erro:
        _extrair(_leitor([_ruim()], erro_caro=falha))

    assert erro.value.custo_usd == pytest.approx(0.012)
    assert erro.value.__cause__ is falha


def test_weak_failure_does_not_escalate() -> None:
    leitor = _leitor([_bom(casa="bet365")])

    saida = _extrair(leitor, casas_do_link=["Betano"])

    assert saida.degrau is escada.Degrau.BARATO
    assert saida.parecer.veredito is Veredito.REVISAO
    assert len(leitor.chamadas) == 1


def test_weak_and_strong_failure_together_escalate() -> None:
    leitor = _leitor([_ruim(casa="bet365")], [_bom()])

    assert _extrair(leitor, casas_do_link=["Betano"]).degrau is escada.Degrau.CARO


def test_low_confidence_goes_to_review_not_to_the_expensive_model() -> None:
    leitor = _leitor([_bom(confianca=0.55)], [_bom()])

    saida = _extrair(leitor, confianca_minima=0.80)

    assert saida.degrau is escada.Degrau.BARATO
    assert saida.parecer.veredito is Veredito.REVISAO
    assert len(leitor.chamadas) == 1


def test_low_confidence_with_a_read_error_escalates() -> None:
    leitor = _leitor([_ruim(confianca=0.55)], [_bom()])

    assert _extrair(leitor, confianca_minima=0.80).degrau is escada.Degrau.CARO


def test_cited_odd_that_disagrees_does_not_escalate() -> None:
    saida = _extrair(_leitor([_bom()]), odds_do_texto=[2.10])

    assert saida.degrau is escada.Degrau.BARATO
    assert saida.parecer.veredito is Veredito.REVISAO


def test_one_bad_coupon_escalates_the_whole_image() -> None:
    leitor = _leitor([_bom(), _ruim()], [_bom(), _bom(odd_total=1.82)])

    saida = _extrair(leitor)

    assert saida.degrau is escada.Degrau.CARO
    assert len(saida.pares) == 2


def test_illegible_card_next_to_a_coupon_is_dropped() -> None:
    saida = _extrair(_leitor([_bom(), _ilegivel()]))

    assert saida.bilhetes == (_bom(),)
    assert saida.degrau is escada.Degrau.BARATO


def test_single_illegible_read_is_the_answer() -> None:
    leitor = _leitor([_ilegivel()])

    saida = _extrair(leitor)

    assert saida.parecer.veredito is Veredito.NAO_E_APOSTA
    assert saida.degrau is escada.Degrau.BARATO
    assert len(leitor.chamadas) == 1


def test_empty_read_is_treated_as_illegible() -> None:
    saida = _extrair(_leitor([]))

    assert saida.bilhetes == (_ilegivel(),)
    assert saida.parecer.veredito is Veredito.NAO_E_APOSTA


def test_principal_is_first_ticket_and_first_unapproved_opinion() -> None:
    saida = _extrair(_leitor([_bom(), _bom(confianca=0.55)]))

    assert saida.bilhete == _bom()
    assert saida.parecer is saida.pareceres[1]
    assert saida.parecer.veredito is Veredito.REVISAO


def test_readings_are_checked_as_ai_readings() -> None:
    igual_a_linha = _bom(selecoes=[Selecao(mercado="Chutes", escolha="2+", linha=1.82, odd=1.82)])

    (parecer,) = escada.conferir_cupons([igual_a_linha], postada_em=QUANDO)

    (linha,) = [c for c in parecer.conferencias if c.nome == "odd ≠ linha"]
    assert not linha.passou
    assert linha.forca is Forca.CONFIRMAR


def test_pode_parar_aqui_only_stops_escalation() -> None:
    aprovado, escalar = escada.conferir_cupons([_bom(), _ruim()])

    assert escada.pode_parar_aqui(aprovado)
    assert not escada.pode_parar_aqui(escalar)


def test_sem_ilegiveis_keeps_the_first_when_all_are_illegible() -> None:
    primeiro = ExtracaoBilhete(ilegivel=True, casa="A")

    assert escada.sem_ilegiveis([primeiro, _ilegivel()]) == (primeiro,)
    assert escada.sem_ilegiveis([_bom()]) == (_bom(),)
