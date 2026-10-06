from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import Workbook

from bancaemdia.domain.planilha_import import PlanilhaInvalidaError, ler_planilha


def _arquivo(*linhas: tuple[object, ...]) -> bytes:
    livro = Workbook()
    aba = livro.active
    assert aba is not None
    aba.title = "Apostas"
    aba.append(("casa", "data_aposta", "odd", "stake_unidades", "atualizada_em", "freebet"))
    for linha in linhas:
        aba.append(linha)
    memoria = BytesIO()
    livro.save(memoria)
    return memoria.getvalue()


def test_chave_permanece_estavel_entre_reimportacoes() -> None:
    primeira = _arquivo(("Betano", datetime(2026, 9, 20), 1.8, 2, datetime(2026, 9, 21), "sim"))
    segunda = _arquivo(("Betano", datetime(2026, 9, 20), 2.1, 2, datetime(2026, 9, 22), "sim"))
    inicial = ler_planilha(primeira, "planilha-principal")[0]
    atualizada = ler_planilha(segunda, "planilha-principal")[0]
    assert inicial.chave == atualizada.chave
    assert inicial.chave != ler_planilha(segunda, "outra-origem")[0].chave
    assert atualizada.odd == pytest.approx(2.1)
    assert atualizada.atualizada_em.tzinfo is not None
    assert atualizada.freebet is True


@pytest.mark.parametrize(
    "linha",
    [
        ("Betano", "2026-09-20", 0, 2, "2026-09-21", False),
        ("Betano", "2026-09-20", 1.8, -1, "2026-09-21", False),
        ("Betano", "2026-09-20", 1.8, 2, "inválida", False),
        ("Betano", "2026-09-20", 1.8, 2, "2026-09-21", "talvez"),
    ],
)
def test_linha_invalida_e_recusada(linha: tuple[object, ...]) -> None:
    with pytest.raises(PlanilhaInvalidaError, match="linha 2"):
        ler_planilha(_arquivo(linha), "planilha-principal")


def test_arquivo_e_origins_invalidas() -> None:
    with pytest.raises(PlanilhaInvalidaError, match="Excel inválido"):
        ler_planilha("não é excel".encode(), "origem")
    with pytest.raises(PlanilhaInvalidaError, match="origem_id"):
        ler_planilha(_arquivo(), " ")
