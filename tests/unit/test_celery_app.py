from __future__ import annotations

import pytest
from celery import signals
from celery.concurrency import prefork
from kombu.exceptions import OperationalError

from bancaemdia.config import get_settings
from bancaemdia.workers import celery_app


def _worker(*queues, prefetch_multiplier=3, pool_cls="solo"):
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
    Worker.pool_cls = pool_cls
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


def test_worker_observability_is_initialized_with_the_child_engine(monkeypatch) -> None:
    from bancaemdia.workers import materialization

    engine = object()
    configured = []
    cleared = []
    monkeypatch.setattr(materialization, "get_engine", lambda: engine)
    monkeypatch.setattr(celery_app, "clear_request_context", lambda: cleared.append(True))
    monkeypatch.setattr(celery_app, "configure_logging", lambda level: configured.append(level))
    monkeypatch.setattr(
        celery_app,
        "configure_tracing",
        lambda **options: configured.append(options),
    )

    celery_app.configure_worker_observability()

    assert cleared == [True]
    assert configured == [
        celery_app.settings.LOG_LEVEL,
        {
            "engines": (engine,),
            "service_name": "bancaemdia-worker",
            "environment": celery_app.settings.APP_ENV,
            "otlp_endpoint": celery_app.settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        },
    ]


def test_celery_parent_and_task_loggers_are_reconfigured_as_json(monkeypatch) -> None:
    configured = []
    cleared = []
    monkeypatch.setattr(celery_app, "clear_request_context", lambda: cleared.append(True))
    monkeypatch.setattr(celery_app, "configure_logging", lambda level: configured.append(level))

    signals.after_setup_logger.send(sender=object())
    signals.after_setup_task_logger.send(sender=object())

    assert cleared == [True, True]
    assert configured == [celery_app.settings.LOG_LEVEL, celery_app.settings.LOG_LEVEL]


def test_worker_process_shutdown_flushes_tracing(monkeypatch) -> None:
    flushed = []
    monkeypatch.setattr(celery_app, "shutdown_owned_tracing", lambda: flushed.append(True))

    celery_app.shutdown_worker_observability()

    assert flushed == [True]


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


@pytest.mark.parametrize(("port", "expected"), [(None, []), ("9808", [9808])])
def test_worker_serves_metrics_only_when_a_port_is_set(monkeypatch, port, expected) -> None:
    started = []

    def start_http_server(port, registry):
        started.append(port)
        assert registry.get_sample_value("extraction_cache_hit_total") is not None

    if port is None:
        monkeypatch.delenv("WORKER_METRICS_PORT", raising=False)
    else:
        monkeypatch.setenv("WORKER_METRICS_PORT", port)
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.setattr(celery_app, "start_http_server", start_http_server)
    get_settings.cache_clear()
    try:
        signals.worker_init.send(sender=_worker("extraction"))
    finally:
        get_settings.cache_clear()

    assert started == expected


@pytest.mark.parametrize(
    "pool_cls", ["prefork", "processes", "celery.concurrency.prefork:TaskPool", prefork.TaskPool]
)
def test_prefork_worker_refuses_to_serve_metrics_without_multiprocess_mode(
    monkeypatch, pool_cls
) -> None:
    started = []
    monkeypatch.setenv("WORKER_METRICS_PORT", "9808")
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.setattr(
        celery_app, "start_http_server", lambda port, registry: started.append(port)
    )
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="PROMETHEUS_MULTIPROC_DIR"):
            celery_app.start_metrics_server(_worker("extraction", pool_cls=pool_cls))
    finally:
        get_settings.cache_clear()

    assert started == []


def test_prefork_worker_serves_the_multiprocess_files(monkeypatch, tmp_path) -> None:
    started = []
    monkeypatch.setenv("WORKER_METRICS_PORT", "9808")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setattr(
        celery_app, "start_http_server", lambda port, registry: started.append(port)
    )
    get_settings.cache_clear()
    try:
        celery_app.start_metrics_server(_worker("extraction", pool_cls="prefork"))
    finally:
        get_settings.cache_clear()

    assert started == [9808]


def test_get_queue_depth_collector_reads_the_broker_url_with_short_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker-host:6381/2")
    get_settings.cache_clear()
    celery_app.get_queue_depth_collector.cache_clear()
    try:
        collector = celery_app.get_queue_depth_collector()
    finally:
        get_settings.cache_clear()
        celery_app.get_queue_depth_collector.cache_clear()

    options = collector.client.connection_pool.connection_kwargs
    assert (options["host"], options["port"], options["db"]) == ("broker-host", 6381, 2)
    assert options["socket_timeout"] == celery_app.TIMEOUT_SECONDS
    assert options["socket_connect_timeout"] == celery_app.TIMEOUT_SECONDS
    assert collector.queues == ("extraction", "materialization", "dead_letter")


def test_check_worker_health_false_when_broker_is_down(monkeypatch) -> None:
    def ping():
        raise OperationalError("connection refused")

    control, _ = _control(ping)
    monkeypatch.setattr(celery_app.app, "control", control)

    assert celery_app.check_worker_health() is False
