from collections.abc import Mapping, Sequence
from functools import lru_cache

import redis
from celery import Celery, Task, signals
from celery.worker import WorkController
from kombu import Queue
from kombu.exceptions import OperationalError
from prometheus_client import start_http_server

from bancaemdia.config import get_settings
from bancaemdia.observability.logging import clear_request_context, configure_logging
from bancaemdia.observability.metrics import (
    MULTIPROC_DIR,
    QueueDepthCollector,
    metrics_registry,
    multiprocess_mode,
)
from bancaemdia.observability.tracing import configure_tracing, shutdown_owned_tracing

settings = get_settings()

EXTRACTION_QUEUE = "extraction"
MATERIALIZATION_QUEUE = "materialization"
DEAD_LETTER_QUEUE = "dead_letter"
TIMEOUT_SECONDS = 2.0
MULTIPROCESS_POOLS = ("prefork", "processes")

QUEUE_PREFETCH = {EXTRACTION_QUEUE: 4, MATERIALIZATION_QUEUE: 2}

app = Celery(
    "bancaemdia",
    include=[
        "bancaemdia.workers.extraction",
        "bancaemdia.workers.materialization",
        "bancaemdia.workers.upload",
        "bancaemdia.workers.telegram",
    ],
)
app.conf.update(
    broker_url=settings.CELERY_BROKER_URL,
    result_backend=settings.CELERY_RESULT_BACKEND,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_routes={
        "extraction.*": {"queue": EXTRACTION_QUEUE},
        "materialization.*": {"queue": MATERIALIZATION_QUEUE},
        "telegram.*": {"queue": MATERIALIZATION_QUEUE},
    },
    beat_schedule={
        "telegram-transport-tick": {"task": "telegram.tick", "schedule": 5.0},
    },
    task_queues=(
        Queue(EXTRACTION_QUEUE, routing_key=EXTRACTION_QUEUE),
        Queue(MATERIALIZATION_QUEUE, routing_key=MATERIALIZATION_QUEUE),
    ),
)

dead_letter_queue = Queue(DEAD_LETTER_QUEUE, routing_key=DEAD_LETTER_QUEUE)


def apply_queue_prefetch(sender: WorkController, **kwargs: object) -> None:
    if sender.prefetch_multiplier != sender.app.conf.worker_prefetch_multiplier:
        return
    prefetch = [
        QUEUE_PREFETCH[name]
        for name in sender.app.amqp.queues.consume_from
        if name in QUEUE_PREFETCH
    ]
    if prefetch:
        sender.prefetch_multiplier = min(prefetch)


def send_to_dead_letter(
    sender: Task,
    task_id: str,
    exception: BaseException,
    args: Sequence[object],
    kwargs: Mapping[str, object],
    **extra: object,
) -> None:
    if sender.request.is_eager:
        return
    delivery_info = sender.request.delivery_info or {}
    sender.app.send_task(
        sender.name,
        args=args,
        kwargs=kwargs,
        queue=dead_letter_queue,
        headers={
            "original_task_id": task_id,
            "original_queue": delivery_info.get("routing_key"),
            "exception": repr(exception),
        },
    )


def check_worker_health(timeout: float = 1.0) -> bool:
    try:
        return bool(app.control.inspect(timeout=timeout).ping())
    except OperationalError:
        return False


def start_metrics_server(sender: WorkController, **kwargs: object) -> None:
    # A API e os trabalhadores rodam em tarefas separadas do ECS, sem disco em comum: o /metrics
    # da API não enxerga os arquivos dos trabalhadores, então cada trabalhador expõe os seus.
    port = get_settings().WORKER_METRICS_PORT
    if port is None:
        return
    pool = sender.pool_cls if isinstance(sender.pool_cls, str) else sender.pool_cls.__module__
    # No prefork as tarefas rodam nos filhos; sem os arquivos do modo multiprocesso, o processo pai
    # responderia 200 com tudo zerado durante o lote.
    if pool.split(":")[0].rsplit(".", 1)[-1] in MULTIPROCESS_POOLS and not multiprocess_mode():
        raise RuntimeError(
            f"a {pool} worker needs {MULTIPROC_DIR} to serve metrics on WORKER_METRICS_PORT"
        )
    start_http_server(port, registry=metrics_registry())


def configure_worker_observability(**kwargs: object) -> None:
    """Initialize exporters after Celery forks and instrument the worker's lazy DB engine."""

    # Import here so each child creates and instruments its own asyncpg engine. Constructing the
    # engine or BatchSpanProcessor in the parent would leave forked children with inherited state.
    from bancaemdia.workers.materialization import get_engine

    worker_settings = get_settings()
    clear_request_context()
    configure_logging(worker_settings.LOG_LEVEL)
    configure_tracing(
        engines=(get_engine(),),
        service_name="bancaemdia-worker",
        environment=worker_settings.APP_ENV,
        otlp_endpoint=worker_settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )


def configure_worker_logging(**kwargs: object) -> None:
    """Replace Celery's text handlers after its parent/task loggers are initialized."""

    clear_request_context()
    configure_logging(get_settings().LOG_LEVEL)


def shutdown_worker_observability(**kwargs: object) -> None:
    """Drain the child process span batch before billiard terminates it."""

    shutdown_owned_tracing()


@lru_cache
def get_queue_depth_collector() -> QueueDepthCollector:
    client = redis.Redis.from_url(
        get_settings().CELERY_BROKER_URL,
        socket_timeout=TIMEOUT_SECONDS,
        socket_connect_timeout=TIMEOUT_SECONDS,
    )
    return QueueDepthCollector(client, (EXTRACTION_QUEUE, MATERIALIZATION_QUEUE, DEAD_LETTER_QUEUE))


signals.worker_init.connect(apply_queue_prefetch)
signals.worker_init.connect(start_metrics_server)
signals.after_setup_logger.connect(configure_worker_logging)
signals.after_setup_task_logger.connect(configure_worker_logging)
signals.worker_process_init.connect(configure_worker_observability)
signals.worker_process_shutdown.connect(shutdown_worker_observability)
signals.task_failure.connect(send_to_dead_letter)
