from __future__ import annotations

import pytest
from celery import signals
from kombu.exceptions import OperationalError

from bancaemdia.workers import celery_app


def _worker(*queues, prefetch_multiplier=3):
    class Queues:
        consume_from = dict.fromkeys(queues)

    class Conf:
        worker_prefetch_multiplier = 3

    class Amqp:
        pass

    class App:
        amqp = Amqp()
        conf = Conf()

    class Worker:
        app = App()

    Amqp.queues = Queues()
    Worker.prefetch_multiplier = prefetch_multiplier
    return Worker()


def _failed_task(delivery_info, is_eager=False):
    sent = []

    class App:
        def send_task(self, name, **options):
            sent.append((name, options))

    class Request:
        pass

    class Task:
        name = "materialization.materializar_aposta"
        app = App()
        request = Request()

    Request.delivery_info = delivery_info
    Request.is_eager = is_eager
    return Task(), sent


def _control(ping):
    timeouts = []

    class Inspect:
        def ping(self):
            return ping()

    class Control:
        def inspect(self, timeout):
            timeouts.append(timeout)
            return Inspect()

    return Control(), timeouts


def test_app_reads_urls_from_settings() -> None:
    conf = celery_app.app.conf

    assert conf.broker_url == celery_app.settings.CELERY_BROKER_URL
    assert conf.result_backend == celery_app.settings.CELERY_RESULT_BACKEND


def test_app_uses_json_and_utc() -> None:
    conf = celery_app.app.conf

    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"
    assert conf.accept_content == ["json"]
    assert conf.timezone == "UTC"
    assert conf.enable_utc is True


def test_app_acks_late_and_requeues_lost_tasks() -> None:
    conf = celery_app.app.conf

    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True


def test_workers_consume_extraction_and_materialization_only() -> None:
    names = [queue.name for queue in celery_app.app.conf.task_queues]

    assert names == ["extraction", "materialization"]
    assert celery_app.dead_letter_queue.name == "dead_letter"
    assert celery_app.dead_letter_queue.routing_key == "dead_letter"


def test_tasks_route_by_name_prefix() -> None:
    router = celery_app.app.amqp.router

    extraction = router.route({}, "extraction.extrair_bilhete")
    materialization = router.route({}, "materialization.materializar_aposta")

    assert extraction["queue"].name == "extraction"
    assert materialization["queue"].name == "materialization"


@pytest.mark.parametrize(
    ("queues", "expected"),
    [
        (("extraction",), 4),
        (("materialization",), 2),
        (("extraction", "materialization"), 2),
        (("dead_letter",), 3),
        ((), 3),
    ],
)
def test_prefetch_follows_consumed_queues(queues, expected) -> None:
    worker = _worker(*queues)

    celery_app.apply_queue_prefetch(worker)

    assert worker.prefetch_multiplier == expected


def test_prefetch_keeps_explicit_multiplier() -> None:
    worker = _worker("extraction", prefetch_multiplier=1)

    celery_app.apply_queue_prefetch(worker)

    assert worker.prefetch_multiplier == 1


def test_worker_init_signal_applies_prefetch() -> None:
    worker = _worker("extraction")

    signals.worker_init.send(sender=worker)

    assert worker.prefetch_multiplier == 4


def test_failed_task_is_sent_to_dead_letter() -> None:
    task, sent = _failed_task({"routing_key": "materialization"})

    celery_app.send_to_dead_letter(
        task,
        task_id="abc",
        exception=ValueError("boom"),
        args=[1, 2],
        kwargs={"midia_hash": "h"},
        traceback=None,
        einfo=None,
    )

    assert len(sent) == 1
    name, options = sent[0]
    assert name == "materialization.materializar_aposta"
    assert options["args"] == [1, 2]
    assert options["kwargs"] == {"midia_hash": "h"}
    assert options["queue"] is celery_app.dead_letter_queue
    assert options["headers"] == {
        "original_task_id": "abc",
        "original_queue": "materialization",
        "exception": "ValueError('boom')",
    }


def test_dead_letter_without_delivery_info_keeps_queue_unknown() -> None:
    task, sent = _failed_task(None)

    celery_app.send_to_dead_letter(
        task, task_id="abc", exception=RuntimeError("x"), args=[], kwargs={}
    )

    assert sent[0][1]["headers"]["original_queue"] is None


def test_eager_failure_is_not_sent_to_dead_letter() -> None:
    task, sent = _failed_task(None, is_eager=True)

    celery_app.send_to_dead_letter(
        task, task_id="abc", exception=RuntimeError("x"), args=[], kwargs={}
    )

    assert sent == []


def test_task_failure_signal_sends_to_dead_letter() -> None:
    task, sent = _failed_task({"routing_key": "extraction"})

    signals.task_failure.send(
        sender=task,
        task_id="abc",
        exception=ValueError("boom"),
        args=[],
        kwargs={},
        traceback=None,
        einfo=None,
    )

    assert len(sent) == 1
    assert sent[0][1]["headers"]["original_queue"] == "extraction"


def test_check_worker_health_true_when_a_worker_answers(monkeypatch) -> None:
    control, timeouts = _control(lambda: {"celery@host": {"ok": "pong"}})
    monkeypatch.setattr(celery_app.app, "control", control)

    assert celery_app.check_worker_health() is True
    assert timeouts == [1.0]


def test_check_worker_health_false_when_no_worker_answers(monkeypatch) -> None:
    control, _ = _control(lambda: None)
    monkeypatch.setattr(celery_app.app, "control", control)

    assert celery_app.check_worker_health(timeout=0.5) is False


def test_check_worker_health_false_when_broker_is_down(monkeypatch) -> None:
    def ping():
        raise OperationalError("connection refused")

    control, _ = _control(ping)
    monkeypatch.setattr(celery_app.app, "control", control)

    assert celery_app.check_worker_health() is False
