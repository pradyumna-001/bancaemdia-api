from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from bancaemdia.observability.metrics import event_handler_failures


@dataclass(frozen=True)
class ApostaCriada:
    usuario_id: int
    aposta_chave: str
    origem: str
    revisao_grave: bool


class EventBus:
    def __init__(self) -> None:
        self.handlers: defaultdict[type[Any], list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event_type: type[Any], handler: Callable[[Any], None]) -> None:
        self.handlers[event_type].append(handler)

    def publish(self, event: object) -> None:
        for handler in tuple(self.handlers[type(event)]):
            try:
                handler(event)
            except Exception:
                event_handler_failures.labels(event=type(event).__name__).inc()


@lru_cache
def get_event_bus() -> EventBus:
    return EventBus()
