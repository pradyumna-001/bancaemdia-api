from datetime import UTC, date, datetime

import pytest

from bancaemdia.cli import replay
from bancaemdia.cli.replay import ReplayInseguroError, _data, _selecionada
from bancaemdia.domain.materializar import projetar as materializar_projetar
from bancaemdia.domain.projecao import EventoIrrecuperavelError, projetar_validado


def _criacao(**extra: object) -> tuple[str, str, dict[str, object]]:
    return (
        "APOSTA_CRIADA",
        "ia",
        {
            "origem": "telegram",
            "stake_unidades": 1.0,
            "valor_unidade_centavos": 10_000,
            "odd": 2.0,
            "data_aposta": "2026-09-20T12:00:00-03:00",
            **extra,
        },
    )


@pytest.mark.parametrize(
    ("evento", "estado", "retorno"),
    [
        (("RESULTADO_REGISTRADO", "manual", {"estado": "GREEN"}), "GREEN", 20_000),
        (("RESULTADO_REGISTRADO", "manual", {"estado": "RED"}), "RED", 0),
        (("RESULTADO_REGISTRADO", "manual", {"estado": "ANULADA"}), "ANULADA", 10_000),
        (("APOSTA_ANULADA", "casa", {}), "ANULADA", 10_000),
        (("CASHOUT_REGISTRADO", "manual", {"retorno_centavos": 8_500}), "CASHOUT", 8_500),
    ],
)
def test_canonical_projection_covers_result_events(evento, estado, retorno) -> None:
    historico = [_criacao(), evento]
    atual, _ = projetar_validado(historico)
    assert (atual["estado"], atual["retorno_centavos"]) == (estado, retorno)
    assert materializar_projetar(historico)[0] == atual


def test_manual_correction_and_deleted_flag_survive_reread() -> None:
    estado, protegidos = projetar_validado([
        _criacao(),
        ("CORRECAO_MANUAL", "manual", {"odd": 3.0, "conta_casa_id": 7}),
        ("CORRECAO_MANUAL", "ia", {"versao_prompt": "extrair_bilhete_v3"}),
        ("APOSTA_CANCELADA", "manual", {"motivo": "você apagou esta aposta"}),
    ])
    assert estado["odd"] == pytest.approx(3.0)
    assert estado["conta_casa_id"] == 7
    assert estado["selecionada"] is False
    assert estado["versao_prompt"] == "extrair_bilhete_v3"
    assert {"odd", "conta_casa_id"} <= protegidos


def test_freebet_and_pending_are_replayed_without_invented_cash() -> None:
    pendente, _ = projetar_validado([_criacao(freebet=True)])
    green, _ = projetar_validado([
        _criacao(freebet=True),
        ("RESULTADO_REGISTRADO", "manual", {"estado": "GREEN"}),
    ])
    assert pendente.get("retorno_centavos") is None
    assert green["retorno_centavos"] == 10_000


def test_event_order_is_caller_order_even_when_timestamps_disagree() -> None:
    historico = [
        _criacao(),
        ("ODD_ALTERADA", "casa", {"para": 2.5}),
        ("ODD_ALTERADA", "manual", {"para": 3.0}),
    ]
    assert projetar_validado(historico)[0]["odd"] == pytest.approx(3.0)


@pytest.mark.parametrize(
    "historico",
    [
        [("ODD_ALTERADA", "ia", {"para": 2.0})],
        [_criacao(), _criacao()],
        [_criacao(), ("ODD_ALTERADA", "ia", {})],
        [_criacao(), ("CASHOUT_REGISTRADO", "manual", {})],
        [_criacao(), ("DESCONHECIDO", "ia", {})],
    ],
)
def test_incomplete_history_is_refused(historico) -> None:
    with pytest.raises(EventoIrrecuperavelError):
        projetar_validado(historico)


def test_business_window_is_inclusive_exclusive_and_brazilian() -> None:
    assert _data("2026-09-20T12:00:00") == datetime(2026, 9, 20, 15, tzinfo=UTC)
    inicio = _data(date(2026, 9, 20).isoformat())
    fim = _data(date(2026, 9, 21).isoformat())
    assert inicio is not None and fim is not None
    assert _selecionada(inicio, inicio, fim)
    assert not _selecionada(fim, inicio, fim)
    with pytest.raises(ReplayInseguroError):
        _data("not-a-date")


async def test_nuclear_replay_requires_environment_and_confirmation(monkeypatch) -> None:
    class Settings:
        APP_ENV = "production"

    monkeypatch.setattr(replay, "get_settings", lambda: Settings())
    with pytest.raises(ReplayInseguroError, match="development/testing"):
        await replay.reconstruir_tudo(confirm=True)

    Settings.APP_ENV = "testing"
    with pytest.raises(ReplayInseguroError, match="--confirm"):
        await replay.reconstruir_tudo(confirm=False)
