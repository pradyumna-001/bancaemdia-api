from __future__ import annotations

import pytest

from bancaemdia.coleta import altenar, betano, betfair, betmgm, kambi, leitores, superbet
from bancaemdia.coleta.leitura import ColetaInvalidaError


def test_every_house_the_extension_sends_has_a_reader() -> None:
    assert set(leitores.LEITORES) == {"betano", "superbet", "betmgm", "betfair", "kto", "esportiva"}


def test_houses_on_a_shared_platform_use_its_reader() -> None:
    assert leitores.LEITORES["kto"] is kambi.ler
    assert leitores.LEITORES["esportiva"] is altenar.ler


@pytest.mark.parametrize(
    "ler", [betano.ler, superbet.ler, betmgm.ler, betfair.ler, kambi.ler, altenar.ler]
)
def test_every_reader_refuses_an_empty_object_with_a_reason(ler) -> None:
    with pytest.raises(ColetaInvalidaError) as erro:
        ler({})

    assert str(erro.value)
    assert "Error" not in str(erro.value)
