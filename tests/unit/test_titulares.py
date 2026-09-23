from datetime import UTC, datetime, timedelta, timezone

import pytest

from bancaemdia.domain.titulares import TrocaInvalidaError, TrocaPedido, _digest


def test_switch_normalizes_timezones_and_requires_explicit_state() -> None:
    local = datetime(2026, 9, 23, 12, tzinfo=timezone(timedelta(hours=-3)))
    utc = datetime(2026, 9, 23, 15, tzinfo=UTC)
    first = TrocaPedido(1, 2, 3, local, "LIMITADA").normalizado()
    second = TrocaPedido(1, 2, 3, utc, "LIMITADA").normalizado()
    assert first == second
    assert _digest(first) == _digest(second)
    with pytest.raises(TrocaInvalidaError):
        TrocaPedido(1, 2, 3, datetime(2026, 9, 23, 15), "LIMITADA").normalizado()
    with pytest.raises(TrocaInvalidaError):
        TrocaPedido(1, 2, 2, utc, "DISPONIVEL").normalizado()
