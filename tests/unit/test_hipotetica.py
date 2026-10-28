"""Domain boundary regressions unavailable in a valid FK-constrained database."""

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from bancaemdia.domain.financeiro import Aposta, Estado, retorno_padrao_exato
from bancaemdia.domain.hipotetica import Dica, Fato, comparar


@pytest.mark.parametrize(
    "vinculos,motivo", [([None], "dica_indisponivel"), ([Dica(20), Dica(30)], "vinculo_ambiguo")]
)
def test_missing_or_multiple_evidence_never_selects_arbitrary_tip(
    vinculos: list[Dica | None], motivo: str
) -> None:
    real = Fato(1, "casa", Estado.GREEN, 100, 100, 200, "2", historico=True)
    result: Any = comparar([real], {1: vinculos})
    assert result["estado"] == "indisponivel"
    assert result["hipotetico"]["lucro_centavos"] is None
    assert result["limitacoes"] == [{"motivo": motivo, "quantidade": 1}]
    assert real.custo == 100 and real.odd == "2" and real.retorno == 200


def test_partial_comparison_does_not_publish_sum_of_safe_subset() -> None:
    real = Fato(1, "casa", Estado.GREEN, 100, 100, 200, "2", historico=True)
    other = replace(real, id=2, estado=Estado.CASHOUT, retorno=125)
    result: Any = comparar([real, other], {1: [Dica(20)], 2: [Dica(20)]})
    assert result["estado"] == "parcial"
    assert result["apostas_consideradas"] == 2 and result["apostas_alteradas"] == 1
    assert result["apostas_indisponiveis"] == 1
    assert result["real"]["lucro_centavos"] == "125"
    assert result["hipotetico"]["lucro_centavos"] is None
    assert result["diferencas"]["lucro_centavos"] is None


@pytest.mark.parametrize("estado", list(Estado))
@pytest.mark.parametrize("freebet", [False, True])
def test_standard_settlement_is_shared_with_existing_financial_core(
    estado: Estado, freebet: bool
) -> None:
    legacy = Aposta(1, 101, odd=2.5, estado=estado, freebet=freebet)
    assert (
        retorno_padrao_exato(101, Decimal("2.5"), estado, freebet=freebet)
        == legacy.retorno_calculado()
    )


def test_exact_half_settlement_above_binary_integer_precision() -> None:
    assert retorno_padrao_exato(9007199254740995, Decimal("1"), Estado.MEIO_RED) == 4503599627370498
