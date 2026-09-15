from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bancaemdia.domain.conferencias import (
    Bilhete,
    ConferenciaResultado,
    Forca,
    Origem,
    Parecer,
    Selecao,
    TipoBilhete,
    Veredito,
    conferir,
    corrigir_ano,
    teto_de_odd,
)

SELECAO = Selecao("Handicap - Cartões", "Instituto AC Cordoba", linha=-0.5, odd=1.82)
POSTADA_EM = datetime(2026, 7, 24, 16, 0)


def _bilhete(
    *,
    casa: str | None = "Betano",
    tipo: TipoBilhete = TipoBilhete.SIMPLES,
    evento: str | None = "Velez Sarsfield x Instituto AC Cordoba",
    selecoes: tuple[Selecao, ...] = (SELECAO,),
    odd_total: float | None = 1.82,
    odd_original: float | None = None,
    quando: str | None = "2026-07-24T19:00",
    ilegivel: bool = False,
    confianca: float = 0.96,
) -> Bilhete:
    return Bilhete(
        casa=casa,
        tipo=tipo,
        evento=evento,
        selecoes=selecoes,
        odd_total=odd_total,
        odd_original=odd_original,
        quando=quando,
        ilegivel=ilegivel,
        confianca=confianca,
    )


def _multipla(*odds: float, odd_total: float, tipo: TipoBilhete = TipoBilhete.MULTIPLA) -> Bilhete:
    selecoes = tuple(
        Selecao("1X2", "Casa", odd=odd, evento=f"Time {i}A x Time {i}B")
        for i, odd in enumerate(odds)
    )
    return _bilhete(tipo=tipo, selecoes=selecoes, odd_total=odd_total)


def _pernas(quantas: int, odd_total: float, odd_original: float | None = None) -> Bilhete:
    selecoes = tuple(Selecao("Total de chutes", f"Jogador {i} 2+") for i in range(quantas))
    return _bilhete(
        tipo=TipoBilhete.CRIAR_APOSTA,
        selecoes=selecoes,
        odd_total=odd_total,
        odd_original=odd_original,
    )


def _cinco_pernas(folga: float) -> Bilhete:
    odds = (1.5, 1.8, 2.0, 1.3, 1.25)
    produto = 1.5 * 1.8 * 2.0 * 1.3 * 1.25
    return _multipla(*odds, odd_total=round(produto * (1 + folga), 2))


def _linha_igual(origem: Origem | None = None, odd: float = 1.5) -> Parecer:
    selecao = Selecao("Total de Gols", "Mais de 1.5", linha=1.5, odd=odd)
    return conferir(_bilhete(selecoes=(selecao,), odd_total=odd), origem=origem)


def _falhas(parecer: Parecer) -> set[str]:
    return {c.nome for c in parecer.falhas}


def _falha(parecer: Parecer, nome: str) -> ConferenciaResultado:
    return next(c for c in parecer.falhas if c.nome == nome)


def _conferencia(parecer: Parecer, nome: str) -> ConferenciaResultado:
    return next(c for c in parecer.conferencias if c.nome == nome)


def test_correct_bilhete_is_aprovado() -> None:
    parecer = conferir(_bilhete())
    assert parecer.veredito == Veredito.APROVADO
    assert parecer.falhas == []
    assert parecer.motivo is None
    assert parecer.grave is False


def test_fourteen_conferences_run_with_distinct_names() -> None:
    conferencias = conferir(_bilhete()).conferencias
    assert len(conferencias) == 14
    assert len({c.nome for c in conferencias}) == 14


@pytest.mark.parametrize(
    ("nome", "campo"),
    [
        ("coerência das odds", "odd_total"),
        ("legibilidade", "ilegivel"),
        ("campos essenciais", "odd_total"),
        ("odd ≠ linha", "odd_total"),
        ("evento plausível", "evento"),
        ("data do jogo", "quando"),
        ("odd turbinada", "odd_original"),
        ("faixa das odds", "odd_total"),
        ("teto de odd", "odd_total"),
        ("texto aproveitável", "evento"),
        ("descrição aproveitável", "selecoes"),
        ("confiança", "confianca"),
        ("casa x link", "casa"),
        ("odd x texto", "odd_total"),
    ],
)
def test_each_conference_points_to_its_campo(nome: str, campo: str) -> None:
    assert _conferencia(conferir(_bilhete()), nome).campo == campo


def test_motivo_joins_every_failure() -> None:
    parecer = conferir(_bilhete(confianca=0.55), casas_do_link=["bet365"])
    assert parecer.motivo == (
        "confiança: 0.55 abaixo do mínimo 0.80; "
        "casa x link: bilhete parece Betano, mas o link é de bet365"
    )


def test_simples_with_different_odds_fails_coerencia() -> None:
    parecer = conferir(_bilhete(odd_total=1.95))
    falha = _falha(parecer, "coerência das odds")
    assert falha.forca == Forca.PROVA
    assert falha.mensagem == "simples: seleção 1.82 ≠ total 1.95"
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is True


def test_multipla_product_matches_total() -> None:
    assert conferir(_multipla(1.5, 2.0, 1.8, odd_total=5.4)).veredito == Veredito.APROVADO


def test_multipla_with_one_wrong_odd_is_caught() -> None:
    parecer = conferir(_multipla(1.5, 1.32, odd_total=2.73))
    assert _falha(parecer, "coerência das odds").mensagem == (
        "múltipla: 1.5 x 1.32 = 1.9800, mas o bilhete diz 2.73 (diferença 27.5%, tolerado 0.9%)"
    )
    assert parecer.veredito == Veredito.ESCALONAR


def test_house_rounding_is_tolerated() -> None:
    assert conferir(_multipla(1.5, 1.8, odd_total=2.71)).veredito == Veredito.APROVADO


def test_tolerance_grows_with_legs_but_only_as_far_as_rounding_goes() -> None:
    assert conferir(_cinco_pernas(0.012)).veredito == Veredito.APROVADO
    assert conferir(_cinco_pernas(0.02)).veredito != Veredito.APROVADO


def test_the_same_slack_is_an_alarm_in_two_legs_and_rounding_in_four() -> None:
    assert "coerência das odds" in _falhas(conferir(_multipla(2.0, 2.0, odd_total=4.03)))
    assert "coerência das odds" not in _falhas(
        conferir(_multipla(2.0, 2.0, 2.0, 2.0, odd_total=16.12))
    )


def test_two_legs_one_percent_off_is_caught() -> None:
    assert "coerência das odds" in _falhas(conferir(_multipla(1.83, 1.98, odd_total=3.66)))


def test_tolerance_is_derived_from_the_odds_themselves() -> None:
    assert "coerência das odds" not in _falhas(conferir(_multipla(1.01, 1.01, odd_total=1.03)))
    assert "coerência das odds" in _falhas(conferir(_multipla(10.0, 10.0, odd_total=101.0)))


def test_real_three_leg_rounding_is_not_an_alarm() -> None:
    assert "coerência das odds" not in _falhas(
        conferir(_multipla(5.0, 5.5, 5.331, odd_total=146.6))
    )


def test_digit_transposition_is_caught() -> None:
    assert "coerência das odds" in _falhas(conferir(_multipla(1.58, 1.98, odd_total=3.66)))


def test_tipo_takes_the_values_the_model_returns() -> None:
    lidos = ("simples", "multipla", "criar_aposta", "sistema")

    assert [TipoBilhete(tipo) for tipo in lidos] == list(TipoBilhete)


def test_criar_aposta_does_not_multiply() -> None:
    selecoes = tuple(
        Selecao(mercado, escolha, linha=linha, odd=odd, evento="Bolívar x Grêmio")
        for mercado, escolha, linha, odd in (
            ("Total de gols", "Mais de 2.5", 2.5, 2.0),
            ("Total cartões", "Mais de 3.5", 3.5, 1.75),
            ("Escanteios", "Mais de 8.5", 8.5, 3.0),
        )
    )
    bilhete = _bilhete(
        tipo=TipoBilhete.CRIAR_APOSTA, selecoes=selecoes, odd_total=5.5, odd_original=3.8
    )
    assert bilhete.odds_devem_multiplicar is False
    parecer = conferir(bilhete)
    assert parecer.veredito == Veredito.APROVADO
    assert _conferencia(parecer, "coerência das odds").mensagem == "não se aplica a criar_aposta"


def test_criar_aposta_without_leg_odds_is_aprovado() -> None:
    assert conferir(_pernas(3, 5.5, odd_original=3.8)).veredito == Veredito.APROVADO


def test_sistema_does_not_multiply() -> None:
    bilhete = _multipla(1.5, 2.0, odd_total=9.9, tipo=TipoBilhete.SISTEMA)
    assert bilhete.odds_devem_multiplicar is False
    assert _conferencia(conferir(bilhete), "coerência das odds").mensagem == (
        "não se aplica a sistema"
    )


def test_multipla_of_different_games_multiplies() -> None:
    bilhete = _multipla(1.5, 2.0, odd_total=9.9)
    assert bilhete.odds_devem_multiplicar is True
    assert bilhete.odds_das_selecoes == [1.5, 2.0]
    assert "coerência das odds" in _falhas(conferir(bilhete))


def test_eventos_distintos_normalizes_and_falls_back_to_the_bilhete() -> None:
    selecoes = (
        Selecao("1X2", "Vasco", odd=1.5, evento="Vasco x Fluminense"),
        Selecao("1X2", "Fluminense", odd=2.0, evento=" vasco X FLUMINENSE "),
        Selecao("1X2", "X", odd=1.1),
    )
    bilhete = _bilhete(tipo=TipoBilhete.MULTIPLA, selecoes=selecoes, odd_total=3.3)
    assert bilhete.eventos_distintos == {
        "vasco x fluminense",
        "velez sarsfield x instituto ac cordoba",
    }
    assert bilhete.odds_devem_multiplicar is True


def test_same_event_marked_as_multipla_does_not_multiply() -> None:
    selecoes = (
        Selecao("Gols", "Mais de 2.5", odd=2.0, evento="Bolívar x Grêmio"),
        Selecao("Cartões", "Mais de 3.5", odd=1.75, evento="Bolívar x Grêmio"),
    )
    bilhete = _bilhete(tipo=TipoBilhete.MULTIPLA, selecoes=selecoes, odd_total=2.9)
    assert bilhete.odds_devem_multiplicar is False
    assert conferir(bilhete).veredito == Veredito.APROVADO


def test_multipla_with_a_single_visible_odd_is_compared_like_a_simples() -> None:
    selecoes = (
        Selecao("1X2", "Vasco", odd=1.5, evento="Vasco x Fluminense"),
        Selecao("1X2", "Santos", evento="Santos x Bahia"),
    )
    bilhete = _bilhete(tipo=TipoBilhete.MULTIPLA, selecoes=selecoes, odd_total=9.9)
    assert bilhete.odds_devem_multiplicar is False
    falha = _falha(conferir(bilhete), "coerência das odds")
    assert falha.mensagem == "simples: seleção 1.5 ≠ total 9.9"


def test_fewer_than_two_selections_never_multiply() -> None:
    assert Bilhete().odds_devem_multiplicar is False
    assert _bilhete(tipo=TipoBilhete.MULTIPLA).odds_devem_multiplicar is False


def test_no_leg_odds_means_nothing_to_prove() -> None:
    parecer = conferir(_bilhete(selecoes=(Selecao("1X2", "A", evento="A x B"),)))
    coerencia = _conferencia(parecer, "coerência das odds")
    assert coerencia.passou is True
    assert coerencia.mensagem == "sem dados para conferir"
    assert parecer.veredito == Veredito.APROVADO


def test_ilegivel_is_not_a_bet() -> None:
    parecer = conferir(_bilhete(ilegivel=True))
    assert parecer.veredito == Veredito.NAO_E_APOSTA
    assert _falha(parecer, "legibilidade").forca == Forca.ERRO
    assert _falha(parecer, "legibilidade").mensagem == "a IA marcou a imagem como ilegível"


def test_ilegivel_beats_every_other_failure() -> None:
    parecer = conferir(_bilhete(ilegivel=True, selecoes=(), odd_total=None, confianca=0.1))
    assert parecer.veredito == Veredito.NAO_E_APOSTA
    assert _falhas(parecer) == {"legibilidade", "campos essenciais", "confiança"}


def test_missing_selections_is_essential() -> None:
    falha = _falha(conferir(_bilhete(selecoes=())), "campos essenciais")
    assert falha.forca == Forca.ERRO
    assert falha.campo == "selecoes"
    assert falha.mensagem == "nenhuma seleção"


def test_missing_odd_total_is_essential() -> None:
    falha = _falha(conferir(_bilhete(odd_total=None)), "campos essenciais")
    assert falha.campo == "odd_total"
    assert falha.mensagem == "odd total"


def test_missing_both_lists_both() -> None:
    parecer = conferir(_bilhete(selecoes=(), odd_total=None))
    assert _falha(parecer, "campos essenciais").mensagem == "nenhuma seleção, odd total"
    assert parecer.grave is True


def test_selection_without_odd_is_accepted() -> None:
    parecer = conferir(_bilhete(selecoes=(Selecao("1X2", "A", evento="A x B"),)))
    assert "campos essenciais" not in _falhas(parecer)


def test_odd_equal_to_line_is_erro_when_ocr_read_it() -> None:
    parecer = _linha_igual(Origem.OCR)
    falha = _falha(parecer, "odd ≠ linha")
    assert falha.forca == Forca.ERRO
    assert falha.mensagem == (
        "odd 1.5 é igual à linha do mercado (1.5) — provável troca de um pelo outro"
    )
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is True


def test_odd_equal_to_line_is_confirmar_when_ia_read_it() -> None:
    parecer = _linha_igual(Origem.IA)
    falha = _falha(parecer, "odd ≠ linha")
    assert falha.forca == Forca.CONFIRMAR
    assert falha.mensagem == (
        "odd 1.5 é igual à linha do mercado (1.5) — coincidência comum em aposta de jogador;"
        " se o print mostra essa odd, é só confirmar"
    )
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is False


def test_undeclared_origem_gets_the_hard_rule() -> None:
    assert _falha(_linha_igual(), "odd ≠ linha").forca == Forca.ERRO


def test_force_follows_the_origem_even_when_the_conference_passes() -> None:
    assert _conferencia(conferir(_bilhete(), origem=Origem.IA), "odd ≠ linha").forca == (
        Forca.CONFIRMAR
    )
    assert _conferencia(conferir(_bilhete()), "odd ≠ linha").forca == Forca.ERRO


def test_coerencia_alone_would_not_catch_odd_equal_to_line() -> None:
    assert "coerência das odds" not in _falhas(_linha_igual())


def test_player_line_equal_to_odd_is_not_grave_when_ia_read_it() -> None:
    selecao = Selecao("Defesas do goleiro", "Martinez 4+ defesas", linha=4.0, odd=4.0)
    parecer = conferir(_bilhete(selecoes=(selecao,), odd_total=4.0), origem=Origem.IA)
    assert _falha(parecer, "odd ≠ linha").forca == Forca.CONFIRMAR
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is False


def test_odd_within_half_a_cent_of_the_line_still_counts_as_equal() -> None:
    falha = _falha(_linha_igual(Origem.OCR, odd=1.504), "odd ≠ linha")
    assert falha.mensagem == (
        "odd 1.504 é igual à linha do mercado (1.5) — provável troca de um pelo outro"
    )
    assert "odd ≠ linha" not in _falhas(_linha_igual(Origem.OCR, odd=1.51))


def test_line_different_from_odd_passes() -> None:
    assert "odd ≠ linha" not in _falhas(conferir(_bilhete()))


def test_without_line_there_is_nothing_to_compare() -> None:
    parecer = conferir(_bilhete(selecoes=(Selecao("Vencedor", "Flamengo", odd=1.82),)))
    assert "odd ≠ linha" not in _falhas(parecer)


@pytest.mark.parametrize(
    "lixo",
    [
        "22/07/2026-21:27",
        "Ponte Preta x RS0.00",
        "Aposta",
        "R$50retorna R$140",
        "2.70》2.80",
        "Flamengo x Vasco >> 2.10",
        "Flamengo @ 1.82",
        "Sam Patterson x Menos de 1.5 Assaltos",
        "Sam Patterson -Por KO,TKO ou DQ x Menosde 1.5Assaltos",
        "Metododevitoria Especiais x Steve Erceg",
        "Inglaterra x Menosde2.5",
        "16:00",
        "abc",
        "Corinthians x Vencedor do encontro",
        "Classificacao x Termina entre os 3 primeiros",
        "AOVIVO Volta Redonda RJ x Sao Goncalo Rj",
        "Longo Prazo x Copa do Mundo 2026",
        "Colombia x Gana -Luis Diaz Marca ou Dar",
        "Brasil x Itália - Neymar a Marcar",
        "Time A x Time B Todos ganham",
    ],
)
def test_garbage_evento_is_caught(lixo: str) -> None:
    parecer = conferir(_bilhete(evento=lixo))
    falha = _falha(parecer, "evento plausível")
    assert falha.forca == Forca.ERRO
    assert falha.mensagem.startswith("não parece confronto: ")
    assert parecer.veredito == Veredito.ESCALONAR


@pytest.mark.parametrize(
    "bom",
    [
        "Chapecoense x Flamengo",
        "Botafogo-RJ x Vitória",
        "Boca Juniors x O'Higgins",
        "Leclerc, charles x Piastri, oscar",
        "Marca Marina x Sporting",
        "Real @ Barcelona",
        "Treino Livre 2",
        "Grande Prêmio da Hungria - Treino 1",
    ],
)
def test_real_evento_passes(bom: str) -> None:
    assert "evento plausível" not in _falhas(conferir(_bilhete(evento=bom)))


def test_absent_evento_is_not_implausible() -> None:
    parecer = conferir(_bilhete(evento=None))
    assert "evento plausível" not in _falhas(parecer)
    assert "texto aproveitável" in _falhas(parecer)


def test_garbage_in_selection_evento_is_caught() -> None:
    selecao = Selecao("1X2", "X", odd=1.82, evento="R$ 50,00")
    assert "evento plausível" in _falhas(conferir(_bilhete(selecoes=(selecao,))))


def test_long_garbage_is_cut_in_the_message() -> None:
    lixo = "Basquete & Copa Do Mundo Especiais do dia de jogo x Wimbledon e mais"
    assert _falha(conferir(_bilhete(evento=lixo)), "evento plausível").mensagem == (
        f"não parece confronto: {lixo[:50]!r}"
    )


def test_glued_player_name_is_a_known_limit_caught_by_descricao() -> None:
    selecao = Selecao("—", "3.20", odd=4.0)
    parecer = conferir(
        _bilhete(evento="Argentina x Cabo Verde-Lionel Messi", selecoes=(selecao,), odd_total=4.0)
    )
    assert "evento plausível" not in _falhas(parecer)
    assert "descrição aproveitável" in _falhas(parecer)
    assert parecer.veredito == Veredito.ESCALONAR


def test_f1_session_escalates_for_text_but_the_money_still_counts() -> None:
    selecao = Selecao("Top 3 - Pilotos", "Kimi Antonelli (ITA/Mercedes)", odd=1.72)
    parecer = conferir(_bilhete(evento="Treino Livre 2", selecoes=(selecao,), odd_total=1.72))
    assert _falhas(parecer) == {"texto aproveitável"}
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is False


def test_game_on_the_same_day_passes() -> None:
    parecer = conferir(_bilhete(quando="2026-07-24T19:00"), data_da_mensagem=POSTADA_EM)
    assert "data do jogo" not in _falhas(parecer)


def test_game_far_from_the_message_is_caught() -> None:
    parecer = conferir(_bilhete(quando="2025-03-11T19:00"), data_da_mensagem=POSTADA_EM)
    falha = _falha(parecer, "data do jogo")
    assert falha.forca == Forca.ERRO
    assert falha.mensagem == "jogo em 11/03/2025 está a -500 dias da mensagem"
    assert parecer.grave is True


def test_window_is_two_days_before_and_thirty_after() -> None:
    assert "data do jogo" not in _falhas(
        conferir(_bilhete(quando="2026-07-22T16:00"), data_da_mensagem=POSTADA_EM)
    )
    assert "data do jogo" not in _falhas(
        conferir(_bilhete(quando="2026-08-23T16:00"), data_da_mensagem=POSTADA_EM)
    )
    assert "data do jogo" in _falhas(
        conferir(_bilhete(quando="2026-07-21T16:00"), data_da_mensagem=POSTADA_EM)
    )
    assert "data do jogo" in _falhas(
        conferir(_bilhete(quando="2026-08-24T16:00"), data_da_mensagem=POSTADA_EM)
    )


def test_invalid_date_format_is_caught() -> None:
    parecer = conferir(_bilhete(quando="24/07 19:00"), data_da_mensagem=POSTADA_EM)
    assert _falha(parecer, "data do jogo").mensagem == "data em formato inválido: '24/07 19:00'"


def test_no_date_on_either_side_passes() -> None:
    parecer = conferir(_bilhete(quando=None), data_da_mensagem=POSTADA_EM)
    assert parecer.veredito == Veredito.APROVADO
    assert conferir(_bilhete(quando="2020-01-01T00:00")).veredito == Veredito.APROVADO


def test_wrong_year_with_right_day_and_month_passes() -> None:
    parecer = conferir(_bilhete(quando="2024-07-24T19:00"), data_da_mensagem=POSTADA_EM)
    data = _conferencia(parecer, "data do jogo")
    assert data.passou is True
    assert data.mensagem == "ano ausente no bilhete; assumido 2026"
    assert parecer.veredito == Veredito.APROVADO


def test_wrong_day_still_fails() -> None:
    parecer = conferir(_bilhete(quando="2024-02-11T19:00"), data_da_mensagem=POSTADA_EM)
    assert "data do jogo" in _falhas(parecer)


def test_game_across_new_year_uses_the_next_year() -> None:
    parecer = conferir(
        _bilhete(quando="2020-01-05T19:00"), data_da_mensagem=datetime(2026, 12, 28, 16, 0)
    )
    data = _conferencia(parecer, "data do jogo")
    assert data.passou is True
    assert data.mensagem == "ano ausente no bilhete; assumido 2027"


def test_leap_day_that_exists_in_the_message_year_passes() -> None:
    postada_em = datetime(2028, 2, 27, 12, 0)
    parecer = conferir(_bilhete(quando="2024-02-29T12:00"), data_da_mensagem=postada_em)
    data = _conferencia(parecer, "data do jogo")
    assert data.passou is True
    assert data.mensagem == "ano ausente no bilhete; assumido 2028"
    assert corrigir_ano(_bilhete(quando="2024-02-29T12:00"), postada_em) == "2028-02-29T12:00"


def test_leap_day_that_does_not_exist_in_the_message_year_fails() -> None:
    parecer = conferir(
        _bilhete(quando="2024-02-29T12:00"), data_da_mensagem=datetime(2026, 2, 27, 12, 0)
    )
    assert "data do jogo" in _falhas(parecer)


def test_timezones_are_ignored_on_both_sides() -> None:
    parecer = conferir(
        _bilhete(quando="2026-07-24T19:00+00:00"),
        data_da_mensagem=datetime(2026, 7, 24, 16, 0, tzinfo=UTC),
    )
    assert "data do jogo" not in _falhas(parecer)


def test_corrigir_ano_fills_the_missing_year() -> None:
    assert corrigir_ano(_bilhete(quando="2024-07-24T19:00"), POSTADA_EM) == "2026-07-24T19:00"


def test_corrigir_ano_keeps_a_date_that_is_already_close() -> None:
    assert corrigir_ano(_bilhete(quando="2026-07-25T19:00"), POSTADA_EM) == "2026-07-25T19:00"


def test_corrigir_ano_keeps_a_date_it_cannot_fix() -> None:
    assert corrigir_ano(_bilhete(quando="2024-02-11T19:00"), POSTADA_EM) == "2024-02-11T19:00"


def test_corrigir_ano_drops_the_offset_it_was_given() -> None:
    assert corrigir_ano(_bilhete(quando="2024-07-24T19:00-03:00"), POSTADA_EM) == (
        "2026-07-24T19:00"
    )


def test_corrigir_ano_without_a_usable_date_returns_none() -> None:
    assert corrigir_ano(_bilhete(quando=None), POSTADA_EM) is None
    assert corrigir_ano(_bilhete(quando="24/07 19:00"), POSTADA_EM) is None


def test_struck_odd_smaller_than_the_live_one_is_right() -> None:
    selecao = Selecao("Total cartões", "Mais de 3.5", linha=3.5, odd=1.55, evento="A x B")
    bilhete = _bilhete(odd_total=1.55, odd_original=1.44, selecoes=(selecao,))
    assert conferir(bilhete).veredito == Veredito.APROVADO


def test_inverted_turbinada_is_caught() -> None:
    selecao = Selecao("Cartoes", "Mais", odd=1.44, evento="A x B")
    parecer = conferir(_bilhete(odd_total=1.44, odd_original=1.55, selecoes=(selecao,)))
    falha = _falha(parecer, "odd turbinada")
    assert falha.forca == Forca.ERRO
    assert falha.mensagem == "a riscada (1.55) deveria ser MENOR que a valendo (1.44)"
    assert parecer.veredito == Veredito.ESCALONAR


def test_equal_struck_and_live_odd_is_also_wrong() -> None:
    assert "odd turbinada" in _falhas(conferir(_bilhete(odd_original=1.82)))


@pytest.mark.parametrize("odd", [0.5, 1.0, 1000.5, 5000.0])
def test_odd_out_of_range_is_caught(odd: float) -> None:
    selecao = Selecao("B", "C", odd=odd, evento="A x B")
    falha = _falha(conferir(_bilhete(odd_total=odd, selecoes=(selecao,))), "faixa das odds")
    assert falha.forca == Forca.ERRO
    assert falha.mensagem == f"odd fora da faixa plausível: [{odd}, {odd}]"


@pytest.mark.parametrize("odd", [1.01, 1000.0])
def test_range_limits_are_inclusive(odd: float) -> None:
    selecao = Selecao("B", "C", odd=odd, evento="A x B")
    assert "faixa das odds" not in _falhas(conferir(_bilhete(odd_total=odd, selecoes=(selecao,))))


def test_odd_above_1000_is_caught_by_the_range_not_the_ceiling() -> None:
    parecer = conferir(_pernas(3, 51950.0))
    assert "faixa das odds" in _falhas(parecer)
    assert parecer.grave is True


def test_single_leg_ceiling_is_fifty() -> None:
    assert "teto de odd" not in _falhas(conferir(_pernas(1, 50.0)))
    assert "teto de odd" in _falhas(conferir(_pernas(1, 50.5)))


@pytest.mark.parametrize("odd", [51.95, 51.80, 100.0, 737.10])
def test_high_single_leg_odd_escalates_but_still_counts(odd: float) -> None:
    parecer = conferir(_pernas(1, odd))
    falha = _falha(parecer, "teto de odd")
    assert falha.forca == Forca.CONFIRMAR
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is False
    assert "numa perna só" in falha.mensagem
    assert "acima de 50" in falha.mensagem


def test_messi_101_is_confirmed_not_held_out_of_the_roi() -> None:
    selecao = Selecao("Especiais", "Messi marca 3+ e Argentina campeã")
    parecer = conferir(_bilhete(selecoes=(selecao,), odd_total=101.0))
    falha = _falha(parecer, "teto de odd")
    assert falha.forca == Forca.CONFIRMAR
    assert falha.mensagem == (
        "odd 101 numa perna só é fora do comum (acima de 50)"
        " — se o print diz isso mesmo, é só confirmar"
    )
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is False


@pytest.mark.parametrize(("pernas", "odd"), [(2, 476.0), (2, 141.0), (8, 400.0), (3, 146.6)])
def test_legitimate_multi_leg_odds_are_not_flagged(pernas: int, odd: float) -> None:
    assert "teto de odd" not in _falhas(conferir(_pernas(pernas, odd)))


def test_multi_leg_ceiling_is_five_hundred() -> None:
    assert "teto de odd" not in _falhas(conferir(_pernas(2, 500.0)))
    assert "teto de odd" in _falhas(conferir(_pernas(2, 500.5)))


def test_ceiling_message_says_how_many_legs() -> None:
    motivo = conferir(_pernas(3, 900.0)).motivo
    assert motivo is not None
    assert "em 3 pernas" in motivo
    assert "acima de 500" in motivo


def test_ceiling_without_odd_or_selections_has_nothing_to_check() -> None:
    assert "teto de odd" not in _falhas(conferir(_bilhete(selecoes=(), odd_total=900.0)))
    assert "teto de odd" not in _falhas(conferir(_bilhete(odd_total=None)))


@pytest.mark.parametrize(
    ("pernas", "teto"), [(0, 50.0), (1, 50.0), (2, 500.0), (8, 500.0), (10, 500.0), (11, 550.0)]
)
def test_teto_de_odd_by_legs(pernas: int, teto: float) -> None:
    assert teto_de_odd(pernas) == pytest.approx(teto)


def test_no_evento_escalates_without_holding_the_money() -> None:
    parecer = conferir(_bilhete(evento=None))
    falha = _falha(parecer, "texto aproveitável")
    assert falha.forca == Forca.TEXTO
    assert falha.mensagem == "não dá para saber qual jogo é: nenhum confronto foi lido"
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.motivo is not None
    assert parecer.grave is False


def test_one_side_of_the_confronto_is_not_enough() -> None:
    parecer = conferir(_bilhete(evento="Ceara"))
    assert _falha(parecer, "texto aproveitável").mensagem == (
        "só um lado do confronto foi lido: 'Ceara' — falta saber contra quem"
    )
    assert parecer.veredito == Veredito.ESCALONAR


def test_one_sided_evento_is_cut_at_forty_chars_in_the_message() -> None:
    evento = "Sociedade Esportiva e Recreativa Caxias do Sul"
    assert _falha(conferir(_bilhete(evento=evento)), "texto aproveitável").mensagem == (
        f"só um lado do confronto foi lido: {evento[:40]!r} — falta saber contra quem"
    )


@pytest.mark.parametrize(
    "confronto",
    [
        "Chapecoense x Flamengo",
        "América Mineiro v Ceará",
        "Mirassol - Grêmio RS",
        "Mirassol \u2013 Gr\u00eamio RS",
        "Vasco vs Fluminense",
        "Boca Juniors X River Plate",
        "Real @ Barcelona",
        "Bochum versus Antwerp",
    ],
)
def test_full_confronto_passes(confronto: str) -> None:
    assert "texto aproveitável" not in _falhas(conferir(_bilhete(evento=confronto)))


def test_confronto_in_a_selection_also_counts() -> None:
    selecoes = (
        Selecao("1X2", "Vasco", odd=1.5, evento="Vasco x Fluminense"),
        Selecao("1X2", "Santos", odd=2.0, evento="Santos x Bahia"),
    )
    bilhete = _bilhete(evento=None, tipo=TipoBilhete.MULTIPLA, selecoes=selecoes, odd_total=3.0)
    assert "texto aproveitável" not in _falhas(conferir(bilhete))


def test_blank_evento_counts_as_absent() -> None:
    assert "texto aproveitável" in _falhas(conferir(_bilhete(evento="   ")))


def test_selection_that_is_only_a_number_escalates() -> None:
    parecer = conferir(_bilhete(selecoes=(Selecao("—", "2.00", odd=3.0),), odd_total=3.0))
    falha = _falha(parecer, "descrição aproveitável")
    assert falha.forca == Forca.TEXTO
    assert falha.mensagem == (
        "não dá para saber o que foi apostado: a seleção é só um número ('2.00')"
    )
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is False


@pytest.mark.parametrize("solto", ["3.20", "2,00", "1256", " 4.00 ", ""])
def test_the_shapes_of_a_bare_number(solto: str) -> None:
    parecer = conferir(_bilhete(selecoes=(Selecao("", solto, odd=3.0),), odd_total=3.0))
    assert "descrição aproveitável" in _falhas(parecer)


def test_number_inside_a_market_choice_passes() -> None:
    selecao = Selecao("Total de Gols", "Mais de 2.5", odd=3.0)
    assert "descrição aproveitável" not in _falhas(
        conferir(_bilhete(selecoes=(selecao,), odd_total=3.0))
    )


def test_bare_number_with_a_market_read_passes() -> None:
    selecao = Selecao("Luis Díaz Marca ou Dá Assistência na Partida", "2.00", odd=3.0)
    assert "descrição aproveitável" not in _falhas(
        conferir(_bilhete(selecoes=(selecao,), odd_total=3.0))
    )


def test_one_usable_selection_saves_the_bilhete() -> None:
    selecoes = (Selecao("—", "2.00", odd=2.0), Selecao("1X2", "Vasco", odd=1.5))
    assert "descrição aproveitável" not in _falhas(
        conferir(_bilhete(selecoes=selecoes, odd_total=3.0))
    )


def test_all_bare_numbers_are_listed() -> None:
    selecoes = (Selecao("—", "3.20", odd=2.0), Selecao("", " 2.00", odd=1.5))
    falha = _falha(conferir(_bilhete(selecoes=selecoes, odd_total=3.0)), "descrição aproveitável")
    assert falha.mensagem.endswith("('3.20, 2.00')")


def test_no_selections_is_not_a_descricao_problem() -> None:
    assert "descrição aproveitável" not in _falhas(conferir(_bilhete(selecoes=())))


def test_low_confidence_goes_to_review_not_to_a_better_model() -> None:
    parecer = conferir(_bilhete(confianca=0.55))
    falha = _falha(parecer, "confiança")
    assert falha.forca == Forca.DUVIDA
    assert falha.mensagem == "0.55 abaixo do mínimo 0.80"
    assert parecer.veredito == Veredito.REVISAO
    assert parecer.grave is False


def test_confidence_threshold_is_configurable() -> None:
    parecer = conferir(_bilhete(confianca=0.55), confianca_minima=0.50)
    assert parecer.veredito == Veredito.APROVADO
    assert "confiança" not in _falhas(conferir(_bilhete(confianca=0.80)))


def test_casa_different_from_link_is_review_not_escalation() -> None:
    parecer = conferir(_bilhete(casa="Betano"), casas_do_link=["bet365"])
    falha = _falha(parecer, "casa x link")
    assert falha.forca == Forca.FRACA
    assert falha.mensagem == "bilhete parece Betano, mas o link é de bet365"
    assert parecer.veredito == Veredito.REVISAO
    assert parecer.grave is False


def test_casa_matching_the_link_passes() -> None:
    parecer = conferir(_bilhete(casa="Betano"), casas_do_link=["Betano"])
    assert parecer.veredito == Veredito.APROVADO


def test_casa_matches_by_containment_ignoring_case() -> None:
    assert "casa x link" not in _falhas(conferir(_bilhete(casa="Vupi"), casas_do_link=["Vupi Bet"]))
    assert "casa x link" not in _falhas(
        conferir(_bilhete(casa="Vupi Bet"), casas_do_link=["bet365", " vupi "])
    )


def test_without_link_or_casa_there_is_nothing_to_compare() -> None:
    assert "casa x link" not in _falhas(conferir(_bilhete(casa="Betano")))
    assert "casa x link" not in _falhas(conferir(_bilhete(casa=None), casas_do_link=["bet365"]))


def test_odd_different_from_text_is_review() -> None:
    parecer = conferir(_bilhete(odd_total=1.82), odds_do_texto=[2.50])
    falha = _falha(parecer, "odd x texto")
    assert falha.forca == Forca.FRACA
    assert falha.mensagem == "bilhete diz 1.82, texto cita 2.5"
    assert parecer.veredito == Veredito.REVISAO


def test_odd_within_two_percent_of_text_passes() -> None:
    parecer = conferir(_bilhete(odd_total=1.82), odds_do_texto=[1.83])
    assert parecer.veredito == Veredito.APROVADO
    assert "odd x texto" in _falhas(conferir(_bilhete(odd_total=1.82), odds_do_texto=[1.90]))


def test_any_cited_odd_close_enough_passes() -> None:
    assert "odd x texto" not in _falhas(
        conferir(_bilhete(odd_total=1.82), odds_do_texto=[9.99, 1.82])
    )


def test_without_cited_odds_there_is_nothing_to_compare() -> None:
    assert "odd x texto" not in _falhas(conferir(_bilhete(odd_total=None), odds_do_texto=[2.5]))


def test_all_weak_failures_are_fraca() -> None:
    parecer = conferir(_bilhete(casa="Betano"), casas_do_link=["bet365"], odds_do_texto=[9.99])
    assert parecer.veredito == Veredito.REVISAO
    assert _falhas(parecer) == {"casa x link", "odd x texto"}
    assert {c.forca for c in parecer.falhas} == {Forca.FRACA}


def test_evidence_of_error_beats_doubt_and_divergence() -> None:
    parecer = conferir(_bilhete(odd_total=9.99, confianca=0.10), casas_do_link=["bet365"])
    assert parecer.veredito == Veredito.ESCALONAR
    assert parecer.grave is True


def test_odd_zero_is_out_of_range_without_dividing_by_zero() -> None:
    parecer = conferir(
        _bilhete(odd_total=0.0, selecoes=(Selecao("1X2", "A", odd=0.0, evento="A x B"),)),
        odds_do_texto=[1.5],
    )
    assert _falha(parecer, "faixa das odds").mensagem == "odd fora da faixa plausível: [0.0, 0.0]"
    assert parecer.grave is True
    assert _conferencia(parecer, "coerência das odds").mensagem == "sem dados para conferir"
    assert "odd x texto" not in _falhas(parecer)
