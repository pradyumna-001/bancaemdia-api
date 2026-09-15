from collections.abc import Mapping, Sequence

from celery import Celery, Task, signals
from celery.worker import WorkController
from kombu import Queue
from kombu.exceptions import OperationalError

from bancaemdia.config import get_settings

settings = get_settings()

EXTRACTION_QUEUE = "extraction"
MATERIALIZATION_QUEUE = "materialization"
DEAD_LETTER_QUEUE = "dead_letter"

QUEUE_PREFETCH = {EXTRACTION_QUEUE: 4, MATERIALIZATION_QUEUE: 2}

app = Celery(
    "bancaemdia",
    include=["bancaemdia.workers.extraction", "bancaemdia.workers.materialization"],
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


signals.worker_init.connect(apply_queue_prefetch)
signals.task_failure.connect(send_to_dead_letter)
