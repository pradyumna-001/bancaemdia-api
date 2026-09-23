import pytest

from bancaemdia.domain.materializar import MOTIVO_APAGADA
from bancaemdia.domain.revisao_service import (
    ResolucaoInvalidaError,
    chave_da_aposta,
    evento_da_resolucao,
    planejar_resolucao,
)


def _estado(**extra: object) -> dict[str, object]:
    return {
        "odd": 1.8,
        "stake_unidades": 1.0,
        "revisao_motivo": "odd duvidosa",
        "revisao_grave": True,
        **extra,
    }


def test_review_key_only_comes_from_a_non_empty_raw_extraction() -> None:
    assert chave_da_aposta({"aposta_chave": "t:1:2:0"}) == "t:1:2:0"
    assert chave_da_aposta({"aposta_chave": ""}) is None
    assert chave_da_aposta({"aposta_chave": 7}) is None
    assert chave_da_aposta(None) is None


def test_empty_correction_means_confirm_and_clears_the_queue() -> None:
    plano = planejar_resolucao("corrigir", None, _estado())

    assert plano.acao == "CORRIGIR"
    assert plano.campos_corrigidos == ()
    assert [(evento.tipo, evento.fonte, evento.payload) for evento in plano.eventos] == [
        (
            "CORRECAO_MANUAL",
            "manual",
            {"revisao_motivo": None, "revisao_grave": False},
        )
    ]


def test_confirming_a_distinct_pair_restores_the_bet_to_totals() -> None:
    plano = planejar_resolucao("CORRIGIR", None, _estado(duvida_de_par=True, selecionada=False))

    assert plano.eventos[-1].tipo == "SELECAO_ALTERADA"
    assert plano.eventos[-1].payload == {"selecionada": True, "duvida_de_par": False}


def test_correction_records_only_changed_user_fields_and_also_clears_review() -> None:
    plano = planejar_resolucao(
        "CORRIGIR",
        {"odd": 2.25, "stake_unidades": 1.0},
        _estado(),
    )

    assert plano.campos_corrigidos == ("odd",)
    assert len(plano.eventos) == 1
    assert plano.eventos[0].payload == {
        "odd": 2.25,
        "revisao_motivo": None,
        "revisao_grave": False,
    }


@pytest.mark.parametrize("campo", ["revisao_motivo", "revisao_grave"])
def test_client_cannot_control_review_flags(campo: str) -> None:
    with pytest.raises(ResolucaoInvalidaError, match="controlado pela fila"):
        planejar_resolucao("CORRIGIR", {campo: False}, _estado())


def test_discard_clears_review_then_uses_the_reversible_delete_event() -> None:
    plano = planejar_resolucao("DESCARTAR", None, _estado())

    assert plano.campos_corrigidos == ()
    assert [(evento.tipo, evento.payload) for evento in plano.eventos] == [
        ("CORRECAO_MANUAL", {"revisao_motivo": None, "revisao_grave": False}),
        ("APOSTA_CANCELADA", {"motivo": MOTIVO_APAGADA}),
    ]


def test_discard_rejects_a_correction() -> None:
    with pytest.raises(ResolucaoInvalidaError, match="não aceita correções"):
        planejar_resolucao("DESCARTAR", {"odd": 2.0}, _estado())


def test_cashout_is_not_confirmed_without_the_amount_paid() -> None:
    with pytest.raises(ResolucaoInvalidaError, match="cashout precisa do valor pago"):
        planejar_resolucao("CORRIGIR", {"estado": "CASHOUT"}, _estado())


def test_resolution_audit_describes_the_review_action_and_changed_fields() -> None:
    evento = evento_da_resolucao(8, "CORRIGIR", "odd duvidosa", ("odd", "stake_unidades"))

    assert (evento.tipo, evento.fonte) == ("REVISAO_RESOLVIDA", "manual")
    assert evento.payload == {
        "revisao_id": 8,
        "acao": "CORRIGIR",
        "motivo": "odd duvidosa",
        "campos_corrigidos": ["odd", "stake_unidades"],
    }
