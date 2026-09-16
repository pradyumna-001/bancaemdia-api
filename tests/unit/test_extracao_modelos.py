from __future__ import annotations

import pytest

from bancaemdia.domain.conferencias import Bilhete, Origem, Selecao, TipoBilhete, Veredito, conferir
from bancaemdia.extracao import modelos


def _extracao(**campos):
    base = {
        "casa": "Betano",
        "tipo": "simples",
        "evento": "Velez Sarsfield x Instituto AC Cordoba",
        "selecoes": [
            {
                "mercado": "Handicap - Cartões",
                "escolha": "Instituto AC Cordoba -0.5",
                "linha": -0.5,
                "odd": 1.82,
                "evento": None,
            }
        ],
        "odd_total": 1.82,
        "odd_original": 1.7,
        "quando": "2026-07-24T19:00",
        "ilegivel": False,
        "confianca": 0.97,
    }
    return modelos.ExtracaoBilhete.model_validate({**base, **campos})


def test_para_bilhete_copies_every_field() -> None:
    assert _extracao().para_bilhete() == Bilhete(
        casa="Betano",
        tipo=TipoBilhete.SIMPLES,
        evento="Velez Sarsfield x Instituto AC Cordoba",
        selecoes=(
            Selecao(
                mercado="Handicap - Cartões",
                escolha="Instituto AC Cordoba -0.5",
                linha=-0.5,
                odd=1.82,
            ),
        ),
        odd_total=1.82,
        odd_original=1.7,
        quando="2026-07-24T19:00",
        ilegivel=False,
        confianca=0.97,
    )


@pytest.mark.parametrize(
    ("tipo", "esperado"),
    [
        ("simples", TipoBilhete.SIMPLES),
        ("multipla", TipoBilhete.MULTIPLA),
        ("criar_aposta", TipoBilhete.CRIAR_APOSTA),
        ("sistema", TipoBilhete.SISTEMA),
    ],
)
def test_para_bilhete_maps_the_model_tipo_to_the_domain(tipo, esperado) -> None:
    assert _extracao(tipo=tipo).para_bilhete().tipo is esperado


def test_rejects_a_tipo_outside_the_contract() -> None:
    with pytest.raises(ValueError, match="tipo"):
        _extracao(tipo="combinada")


def test_converted_ticket_runs_the_conferences() -> None:
    parecer = conferir(
        _extracao(odd_original=None).para_bilhete(),
        origem=Origem.IA,
        data_da_mensagem=None,
    )
    assert parecer.veredito is Veredito.APROVADO


def test_empty_reading_defaults() -> None:
    vazio = modelos.ExtracaoBilhete()
    assert vazio.selecoes == []
    assert vazio.tipo == "simples"
    assert vazio.confianca == pytest.approx(0.0)
    assert modelos.Cupons().cupons == []


def test_defaults_do_not_share_lists() -> None:
    um = modelos.ExtracaoBilhete()
    um.selecoes.append(modelos.Selecao(mercado="1X2", escolha="A"))
    assert modelos.ExtracaoBilhete().selecoes == []


def test_cupons_parse_the_three_coupon_example_of_the_prompt() -> None:
    cupons = modelos.Cupons.model_validate_json(
        '{"cupons": ['
        '{"casa": "Betano", "tipo": "simples", "evento": "Villarreal CF x Celta de Vigo",'
        ' "selecoes": [{"mercado": "Jogador - Total de chutes", "escolha": "Pablo Duran - 2+",'
        ' "linha": 2.0, "odd": 1.75, "evento": null}], "odd_total": 1.75, "confianca": 0.9},'
        '{"casa": "Betano", "tipo": "simples", "selecoes": [], "odd_total": 3.25},'
        '{"casa": "Betano", "tipo": "simples", "selecoes": [], "odd_total": 6.80}'
        "]}"
    )
    assert [c.odd_total for c in cupons.cupons] == [1.75, 3.25, 6.80]
    assert cupons.cupons[0].selecoes[0].escolha == "Pablo Duran - 2+"
