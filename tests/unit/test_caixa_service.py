from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from bancaemdia.domain.caixa_service import (
    MovimentoInvalidoError,
    aposta_temporal,
    movimento_temporal,
    planejar_movimento,
    saldo_da_conta,
    saldo_por_conta,
)
from bancaemdia.domain.registros import Aposta, Movimento


@pytest.mark.parametrize("tipo", ["deposito", "DEPOSITO", "DePoSiTo", " deposito "])
def test_deposito_publico_e_normalizado_e_persistido_positivo(tipo: str) -> None:
    plano = planejar_movimento(tipo, 100_000, conta_casa_id=7)

    assert plano.tipo == "DEPOSITO"
    assert plano.transferencia_id is None
    lancamento = plano.lancamentos[0]
    assert (
        lancamento.conta_casa_id,
        lancamento.tipo,
        lancamento.valor_centavos,
        lancamento.transferencia_id,
    ) == (7, "DEPOSITO", 100_000, None)


def test_saque_recebe_valor_positivo_e_persiste_negativo() -> None:
    plano = planejar_movimento("saque", 20_000, conta_casa_id=7)

    assert plano.lancamentos[0].valor_centavos == -20_000


@pytest.mark.parametrize("valor", [15_000, -15_000])
def test_ajuste_conserva_o_sinal_e_pode_ser_da_banca(valor: int) -> None:
    plano = planejar_movimento("ajuste", valor)

    assert plano.lancamentos[0].conta_casa_id is None
    assert plano.lancamentos[0].valor_centavos == valor


def test_transferencia_vira_dois_lancamentos_opostos_com_o_mesmo_id() -> None:
    plano = planejar_movimento("transferencia", 30_000, conta_casa_id=7, conta_casa_destino_id=9)

    assert isinstance(plano.transferencia_id, UUID)
    assert [lancamento.conta_casa_id for lancamento in plano.lancamentos] == [7, 9]
    assert [lancamento.tipo for lancamento in plano.lancamentos] == [
        "TRANSFERENCIA",
        "TRANSFERENCIA",
    ]
    assert [lancamento.valor_centavos for lancamento in plano.lancamentos] == [
        -30_000,
        30_000,
    ]
    assert {lancamento.transferencia_id for lancamento in plano.lancamentos} == {
        plano.transferencia_id
    }


@pytest.mark.parametrize(
    ("tipo", "valor", "conta", "destino"),
    [
        ("bonus", 100, 1, None),
        ("desconhecido", 100, 1, None),
        ("deposito", 0, 1, None),
        ("saque", -100, 1, None),
        ("deposito", -100, 1, None),
        ("transferencia", -100, 1, 2),
        ("deposito", 100, None, None),
        ("saque", 100, None, None),
        ("transferencia", 100, None, 2),
        ("transferencia", 100, 1, None),
        ("transferencia", 100, 1, 1),
        ("deposito", 100, 1, 2),
        ("deposito", True, 1, None),
        ("deposito", 100, True, None),
        ("deposito", 9_223_372_036_854_775_808, 1, None),
        ("ajuste", -9_223_372_036_854_775_809, None, None),
        ("deposito", 100, 9_223_372_036_854_775_808, None),
    ],
)
def test_pedidos_invalidos_nao_viram_plano(
    tipo: str, valor: int, conta: int | None, destino: int | None
) -> None:
    with pytest.raises(MovimentoInvalidoError):
        planejar_movimento(tipo, valor, conta, destino)


def _movimento(
    *,
    valor: int = 100_000,
    tipo: str = "DEPOSITO",
    ocorrido_em: datetime = datetime(2026, 7, 1, 12, tzinfo=UTC),
) -> Movimento:
    return Movimento(
        id=1,
        usuario_id=3,
        conta_casa_id=7,
        tipo=tipo,
        valor_centavos=valor,
        ocorrido_em=ocorrido_em,
        descricao=None,
    )


def _aposta(
    *,
    stake_centavos: int = 10_000,
    retorno_centavos: int | None = 20_000,
    estado: str = "GREEN",
    data_aposta: datetime | None = datetime(2026, 7, 2, 12, tzinfo=UTC),
) -> Aposta:
    agora = datetime(2026, 7, 2, 12, tzinfo=UTC)
    return Aposta(
        id=2,
        usuario_id=3,
        chave="m:1",
        chat_id=None,
        message_id=None,
        ordem_na_mensagem=0,
        midia_hash=None,
        banca_id=None,
        conta_casa_id=7,
        tipster_id=None,
        time_casa_id=None,
        time_fora_id=None,
        mercado_id=None,
        competicao_id=None,
        data_aposta=data_aposta,
        data_jogo=None,
        stake_unidades=0.333333333,
        stake_centavos=stake_centavos,
        valor_aposta_centavos=stake_centavos,
        odd=2.0,
        retorno_centavos=retorno_centavos,
        estado=estado,
        origem="manual",
        freebet=False,
        duvida_de_par=False,
        parceira_chave=None,
        duplicada_de=None,
        revisao_grave=False,
        selecionada=True,
        criada_em=agora,
        atualizada_em=agora,
    )


def test_adaptador_preserva_stake_em_centavos_sem_refazer_a_conta_float() -> None:
    adaptada = aposta_temporal(_aposta(stake_centavos=12_347))

    assert adaptada.stake_centavos == 12_347


def test_adaptador_preserva_stake_acima_da_precisao_do_float() -> None:
    stake = 9_007_199_254_740_993

    adaptada = aposta_temporal(_aposta(stake_centavos=stake))

    assert adaptada.stake_centavos == stake


def test_adaptadores_convertem_o_instante_para_o_dia_do_brasil() -> None:
    instante = datetime(2026, 7, 2, 1, 30, tzinfo=UTC)

    assert movimento_temporal(_movimento(ocorrido_em=instante)).ocorrido_em == date(2026, 7, 1)
    assert aposta_temporal(_aposta(data_aposta=instante)).data_aposta == date(2026, 7, 1)


def test_saldo_reusa_a_fonte_temporal_sem_contar_o_green_duas_vezes() -> None:
    resultado = saldo_da_conta(7, [_aposta()], [_movimento()])

    assert resultado.saldo_centavos == 110_000


def test_saldo_por_conta_e_o_mesmo_contrato_e_respeita_o_corte() -> None:
    resultado = saldo_por_conta(
        7,
        [_aposta(data_aposta=datetime(2026, 7, 3, 2, tzinfo=UTC))],
        [_movimento()],
        datetime(2026, 7, 2, 2, tzinfo=UTC),
    )

    assert resultado.saldo_centavos == 100_000
