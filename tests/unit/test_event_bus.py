from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from bancaemdia.domain import event_bus
from bancaemdia.domain.event_bus import ApostaCriada, EventBus


def _falhas():
    return REGISTRY.get_sample_value("event_handler_failed_total", {"event": "ApostaCriada"}) or 0.0


def test_event_reaches_only_the_handlers_of_its_type() -> None:
    bus = EventBus()
    recebidos = []
    bus.subscribe(ApostaCriada, recebidos.append)
    bus.subscribe(str, recebidos.append)
    evento = ApostaCriada(
        usuario_id=7, aposta_chave="t:1:2:0", origem="telegram", revisao_grave=False
    )

    bus.publish(evento)

    assert recebidos == [evento]


def test_failing_handler_does_not_stop_the_others() -> None:
    bus = EventBus()
    recebidos = []
    falhas = _falhas()

    def quebra(evento):
        raise RuntimeError("projeção fora do ar")

    bus.subscribe(ApostaCriada, quebra)
    bus.subscribe(ApostaCriada, recebidos.append)
    bus.publish(ApostaCriada(7, "t:1:2:0", "telegram", True))

    assert len(recebidos) == 1
    assert _falhas() == pytest.approx(falhas + 1)


def test_publishing_without_handlers_is_a_no_op() -> None:
    EventBus().publish(ApostaCriada(7, "t:1:2:0", "telegram", False))


def test_get_event_bus_is_one_per_process() -> None:
    assert event_bus.get_event_bus() is event_bus.get_event_bus()
