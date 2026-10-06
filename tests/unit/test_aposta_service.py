from __future__ import annotations

import pytest

from bancaemdia.domain.aposta_service import (
    ApostaInvalidaError,
    eventos_da_correcao,
    eventos_da_exclusao,
    eventos_da_restauracao,
    eventos_do_resultado,
    mudancas,
    validar_correcao,
    validar_resultado,
)
from bancaemdia.domain.materializar import MOTIVO_APAGADA, projetar

CRIACAO = {
    "origem": "telegram",
    "casa": "Betano",
    "odd": 1.82,
    "stake_unidades": 1.0,
    "valor_unidade_centavos": 10_000,
    "freebet": False,
    "selecionada": True,
}


def _historico(*eventos: tuple[str, str, dict[str, object]]) -> list[tuple[str, str, dict]]:
    return [("APOSTA_CRIADA", "ia", dict(CRIACAO)), *eventos]


def test_only_what_changed_becomes_a_correction() -> None:
    atual = {"odd": 1.82, "tipster_id": 3}

    assert mudancas(atual, {"odd": 1.82, "tipster_id": 3}) == {}
    assert mudancas(atual, {"odd": 1.95}) == {"odd": 1.95}
    assert mudancas(atual, {"tipster_id": None}) == {"tipster_id": None}


def test_a_correction_that_changes_nothing_writes_no_event() -> None:
    assert eventos_da_correcao({}) == []


def test_a_correction_is_written_as_a_decision_of_the_person() -> None:
    [evento] = eventos_da_correcao({"odd": 1.95})

    assert (evento.tipo, evento.fonte, evento.payload) == (
        "CORRECAO_MANUAL",
        "manual",
        {"odd": 1.95},
    )


def test_what_the_person_corrected_survives_a_later_reading() -> None:
    historico = _historico(("CORRECAO_MANUAL", "manual", {"odd": 1.95, "tipster_id": 7}))

    estado, protegidos = projetar(historico)

    # É a fonte "manual" que faz a releitura automática nunca desfazer o que a pessoa digitou.
    assert estado["odd"] == pytest.approx(1.95)
    assert protegidos == {"odd", "tipster_id"}


def test_a_field_that_is_not_the_persons_to_change_is_refused_by_name() -> None:
    with pytest.raises(ApostaInvalidaError, match="campo que não pode ser corrigido: retorno"):
        validar_correcao({"retorno": 100}, {})


@pytest.mark.parametrize(
    ("pedido", "recado"),
    [
        ({"odd": 1.0}, "a odd tem de ser pelo menos"),
        ({"odd": None}, "a odd tem de ser pelo menos"),
        ({"odd": 1001}, "alta demais para ser real"),
        ({"stake_unidades": 0}, "a stake em unidades tem de ser positiva"),
        ({"comissao_centavos": -1}, "a comissão não pode ser negativa"),
    ],
)
def test_a_correction_that_makes_no_sense_is_refused_in_portuguese(pedido, recado) -> None:
    with pytest.raises(ApostaInvalidaError, match=recado):
        validar_correcao(pedido, {})


@pytest.mark.parametrize("campo", ["odd", "stake_unidades", "comissao_centavos"])
@pytest.mark.parametrize("valor", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_never_reaches_the_event_log(campo: str, valor: float) -> None:
    with pytest.raises(ApostaInvalidaError):
        validar_correcao({campo: valor}, {})


def test_a_settle_names_the_state_and_only_what_was_sent() -> None:
    [sem_comissao] = eventos_do_resultado("GREEN")
    [com_comissao] = eventos_do_resultado("GREEN", comissao_centavos=0)

    # `None` quer dizer "não estou falando da comissão": mandar 0 apagaria a que já estava lá.
    assert sem_comissao.payload == {"estado": "GREEN"}
    assert com_comissao.payload == {"estado": "GREEN", "comissao_centavos": 0}
    assert (sem_comissao.tipo, sem_comissao.fonte) == ("RESULTADO_REGISTRADO", "manual")


def test_an_omitted_commission_does_not_erase_the_one_already_recorded() -> None:
    criada = dict(CRIACAO, comissao_centavos=500)
    historico = [("APOSTA_CRIADA", "ia", criada)]

    with_omitido, _ = projetar([
        *historico,
        *((e.tipo, e.fonte, e.payload) for e in eventos_do_resultado("GREEN")),
    ])
    with_zerado, _ = projetar([
        *historico,
        *((e.tipo, e.fonte, e.payload) for e in eventos_do_resultado("GREEN", comissao_centavos=0)),
    ])

    assert with_omitido["comissao_centavos"] == 500
    assert with_omitido["retorno_centavos"] == 18_200 - 500
    assert with_zerado["retorno_centavos"] == 18_200


def test_a_cashout_is_the_value_the_house_paid_and_is_never_recalculated() -> None:
    [evento] = eventos_do_resultado("CASHOUT", 12_345)

    estado, _ = projetar(_historico((evento.tipo, evento.fonte, evento.payload)))

    assert evento.tipo == "CASHOUT_REGISTRADO"
    assert (estado["estado"], estado["retorno_centavos"]) == ("CASHOUT", 12_345)


def test_a_cashout_without_the_value_is_refused() -> None:
    with pytest.raises(ApostaInvalidaError, match="cashout precisa do valor que a casa pagou"):
        validar_resultado("CASHOUT", None, None)


def test_a_cashout_value_only_makes_sense_with_a_cashout() -> None:
    with pytest.raises(ApostaInvalidaError, match="só vale com estado CASHOUT"):
        validar_resultado("GREEN", None, 100)


def test_an_unknown_state_is_refused_by_name() -> None:
    with pytest.raises(ApostaInvalidaError, match="estado desconhecido: 'GANHOU'"):
        validar_resultado("GANHOU", None, None)


@pytest.mark.parametrize(
    ("estado", "retorno"),
    [
        ("GREEN", 18_200),
        ("RED", 0),
        ("ANULADA", 10_000),
        ("MEIO_GREEN", 14_100),
        ("MEIO_RED", 5_000),
    ],
)
def test_the_money_of_each_state_comes_from_the_formula_not_from_the_route(estado, retorno) -> None:
    [evento] = eventos_do_resultado(estado)

    projetado, _ = projetar(_historico((evento.tipo, evento.fonte, evento.payload)))

    # A rota não calcula nada: quem calcula é a fórmula portada na issue #10.
    assert projetado["retorno_centavos"] == retorno


def test_deleting_takes_the_bet_out_of_the_accounts_and_restoring_brings_it_back() -> None:
    apagar = eventos_da_exclusao()
    restaurar = eventos_da_restauracao()

    apagada, _ = projetar(_historico((apagar[0].tipo, apagar[0].fonte, apagar[0].payload)))
    voltou, _ = projetar(
        _historico(
            (apagar[0].tipo, apagar[0].fonte, apagar[0].payload),
            (restaurar[0].tipo, restaurar[0].fonte, restaurar[0].payload),
        )
    )

    assert apagar[0].payload == {"motivo": MOTIVO_APAGADA}
    assert apagada["selecionada"] is False
    # Apagar não é revisar: quem apagou não tem nada a confirmar na fila.
    assert apagada.get("revisao_motivo") is None
    assert voltou["selecionada"] is True


def test_a_bet_nobody_deleted_is_selected() -> None:
    estado, _ = projetar(_historico())

    assert estado["selecionada"] is True


def test_a_cancellation_with_a_reason_still_opens_a_review() -> None:
    historico = _historico(("APOSTA_CANCELADA", "casa", {"motivo": "a casa anulou o evento"}))

    estado, _ = projetar(historico)

    assert estado["revisao_motivo"] == "a casa anulou o evento"
    assert estado["selecionada"] is True


def test_every_id_the_person_can_choose_reaches_the_bet() -> None:
    from bancaemdia.domain.aposta_service import CAMPOS_CORRIGIVEIS, CAMPOS_DE_ID

    # Uma lista só: com duas cópias, a competição corrigida virava evento e nunca chegava na linha.
    assert set(CAMPOS_DE_ID) == {c for c in CAMPOS_CORRIGIVEIS if c.endswith("_id")}
    assert "competicao_id" in CAMPOS_DE_ID


@pytest.mark.parametrize(
    ("pedido", "recado"),
    [
        ({"data_aposta": "ontem"}, "tem de ser uma data"),
        ({"data_jogo": 20260920}, "tem de ser uma data"),
        ({"tipster_id": "três"}, "tipster_id tem de ser um número"),
        ({"conta_casa_id": True}, "conta_casa_id tem de ser um número"),
        ({"mercado_id": 2**63}, "mercado_id tem de caber em um BIGINT"),
        ({"competicao_id": -(2**63) - 1}, "competicao_id tem de caber em um BIGINT"),
        ({"odd": "alta"}, "a odd tem de ser pelo menos"),
        ({"stake_unidades": "uma"}, "a stake em unidades tem de ser positiva"),
        ({"comissao_centavos": "cinco"}, "a comissão não pode ser negativa"),
        ({"freebet": "sim"}, "freebet só aceita sim ou não"),
        ({"estado": "GANHOU"}, "estado desconhecido"),
        ({"descricao": 7}, "descricao tem de ser texto"),
    ],
)
def test_a_value_of_the_wrong_kind_is_refused_before_it_becomes_an_event(pedido, recado) -> None:
    # Sem isto o valor só aparecia na hora de gravar, como erro 500 e com o histórico já sujo.
    with pytest.raises(ApostaInvalidaError, match=recado):
        validar_correcao(pedido, {})


def test_a_cashout_does_not_take_a_commission() -> None:
    with pytest.raises(ApostaInvalidaError, match="cashout não usa comissão"):
        validar_resultado("CASHOUT", None, 10_000, comissao_centavos=100)
