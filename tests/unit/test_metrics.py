from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import redis
from fastapi.testclient import TestClient
from kombu.transport import redis as kombu_redis
from prometheus_client import REGISTRY, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from bancaemdia import main
from bancaemdia.observability import metrics

DASHBOARD = Path(__file__).resolve().parents[2] / "grafana" / "dashboards" / "batch-pipeline.json"
QUEUES = ("extraction", "materialization", "dead_letter")
ISSUE_METRICS = [
    ("batch_job_duration_seconds", "histogram"),
    ("batch_failed_total", "counter"),
    ("batch_bets_processed_total", "counter"),
    ("revisao_pendente_created_total", "counter"),
    ("celery_queue_depth", "gauge"),
    ("anthropic_cost_usd_total", "counter"),
    ("anthropic_request_duration_seconds", "histogram"),
    ("extraction_cache_hit_total", "counter"),
    ("extraction_cache_miss_total", "counter"),
    ("circuit_breaker_state", "gauge"),
    ("pg_replication_lag_seconds", "gauge"),
]


def _broker(lists=None, failure=None):
    class Pipeline:
        def __init__(self, client):
            self.client = client
            self.keys = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def llen(self, key):
            self.keys.append(key)
            return self

        def execute(self):
            if failure is not None:
                raise failure
            self.client.reads.append(list(self.keys))
            return [self.client.lists.get(key, 0) for key in self.keys]

    class Redis:
        def __init__(self):
            self.lists = dict(lists or {})
            self.reads = []
            self.transactions = []

        def pipeline(self, transaction=True):
            self.transactions.append(transaction)
            return Pipeline(self)

    return Redis()


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _depths(collector):
    return {
        sample.labels["queue"]: sample.value
        for family in collector.collect()
        for sample in family.samples
    }


def _dashboard():
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _series_in(expression):
    without_labels = re.sub(r"\{[^}]*\}|\[[^\]]*\]|\b(?:by|without)\s*\([^)]*\)", " ", expression)
    return set(re.findall(r"\b[a-zA-Z_:][\w:]*\b(?!\s*\()", without_labels))


def _exported_series():
    suffixes = {"counter": ("_total",), "gauge": ("",), "histogram": ("_bucket", "_sum", "_count")}
    exported = {
        family.name + suffix
        for family in REGISTRY.collect()
        for suffix in suffixes.get(family.type, ())
    }
    return exported | {"celery_queue_depth"}


def test_queue_depth_sums_the_priority_lists_of_each_queue() -> None:
    broker = _broker({
        "extraction": 2,
        "extraction\x06\x163": 1,
        "extraction\x06\x169": 4,
        "dead_letter": 1,
    })

    depths = _depths(metrics.QueueDepthCollector(broker, QUEUES))

    assert depths == {"extraction": 7, "materialization": 0, "dead_letter": 1}
    assert broker.transactions == [False]
    assert len(broker.reads) == 1


def test_queue_keys_are_the_lists_kombu_writes() -> None:
    channel = kombu_redis.Channel.__new__(kombu_redis.Channel)

    for priority in kombu_redis.PRIORITY_STEPS:
        assert metrics.queue_key("extraction", priority) == channel._q_for_pri(
            "extraction", priority
        )


def test_redis_down_leaves_queue_depth_out_and_counts_the_error() -> None:
    errors = _sample("celery_queue_depth_error_total")
    collector = metrics.QueueDepthCollector(_broker(failure=redis.ConnectionError("down")), QUEUES)

    assert list(collector.collect()) == []
    assert _sample("celery_queue_depth_error_total") == pytest.approx(errors + 1)


def test_queue_depth_error_shows_up_in_the_scrape_that_hit_it(monkeypatch) -> None:
    monkeypatch.delenv(metrics.MULTIPROC_DIR, raising=False)
    errors = _sample("celery_queue_depth_error_total")
    collector = metrics.QueueDepthCollector(_broker(failure=redis.ConnectionError("down")), QUEUES)

    exposed = {
        sample.name: sample.value
        for family in text_string_to_metric_families(
            generate_latest(metrics.metrics_registry(collector)).decode()
        )
        for sample in family.samples
    }

    assert exposed["celery_queue_depth_error_total"] == pytest.approx(errors + 1)
    assert "celery_queue_depth" not in exposed


def test_blank_multiprocess_dir_fails_loudly_instead_of_serving_zeros(monkeypatch) -> None:
    monkeypatch.setenv(metrics.MULTIPROC_DIR, "")

    assert metrics.multiprocess_mode()
    with pytest.raises(ValueError, match="PROMETHEUS_MULTIPROC_DIR"):
        metrics.metrics_registry()


def test_successful_stage_is_timed() -> None:
    count = _sample("batch_job_duration_seconds_count", stage="test", status="success")

    with metrics.observe_stage("test"):
        pass

    assert _sample("batch_job_duration_seconds_count", stage="test", status="success") == (
        pytest.approx(count + 1)
    )


def test_failed_stage_is_timed_and_counted_by_exception() -> None:
    count = _sample("batch_job_duration_seconds_count", stage="test", status="failed")
    failures = _sample("batch_failed_total", stage="test", reason="ValueError")

    with pytest.raises(ValueError, match="boom"), metrics.observe_stage("test"):
        raise ValueError("boom")

    assert _sample("batch_job_duration_seconds_count", stage="test", status="failed") == (
        pytest.approx(count + 1)
    )
    assert _sample("batch_failed_total", stage="test", reason="ValueError") == pytest.approx(
        failures + 1
    )


def test_registry_without_a_multiprocess_dir_serves_this_process(monkeypatch) -> None:
    monkeypatch.delenv(metrics.MULTIPROC_DIR, raising=False)
    collector = metrics.QueueDepthCollector(_broker({"dead_letter": 2}), QUEUES)

    registry = metrics.metrics_registry(collector)

    assert registry.get_sample_value("extraction_cache_hit_total") is not None
    assert registry.get_sample_value("celery_queue_depth", {"queue": "dead_letter"}) == 2


def test_registry_with_a_multiprocess_dir_sums_the_files_of_every_process(
    monkeypatch, tmp_path
) -> None:
    script = (
        "from prometheus_client import Counter\n"
        "Counter('batch_bets_processed', 'Bets', ['stage']).labels(stage='extraction').inc({})\n"
    )
    for amount in (2, 5):
        subprocess.run(
            [sys.executable, "-c", script.format(amount)],
            env={**os.environ, metrics.MULTIPROC_DIR: str(tmp_path)},
            check=True,
        )
    monkeypatch.setenv(metrics.MULTIPROC_DIR, str(tmp_path))

    registry = metrics.metrics_registry()

    assert registry.get_sample_value(
        "batch_bets_processed_total", {"stage": "extraction"}
    ) == pytest.approx(7.0)
    assert registry.get_sample_value("extraction_cache_hit_total") is None
    assert registry.get_sample_value("process_cpu_seconds_total") is not None
    assert registry.get_sample_value("process_memory_bytes") is not None


def test_metrics_endpoint_exposes_the_pipeline_metrics(monkeypatch) -> None:
    refreshed = []

    def breaker_states():
        refreshed.append(True)
        return {"anthropic": "closed"}

    broker = _broker({"extraction": 3})
    monkeypatch.delenv(metrics.MULTIPROC_DIR, raising=False)
    monkeypatch.setattr(main, "breaker_states", breaker_states)
    monkeypatch.setattr(
        main, "get_queue_depth_collector", lambda: metrics.QueueDepthCollector(broker, QUEUES)
    )

    response = TestClient(main.app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert list(text_string_to_metric_families(response.text))
    for name, kind in ISSUE_METRICS:
        assert f"# TYPE {name} {kind}" in response.text
    assert 'celery_queue_depth{queue="extraction"} 3.0' in response.text
    assert refreshed == [True]


def test_dashboard_has_the_six_panels_of_the_issue() -> None:
    dashboard = _dashboard()

    assert (dashboard["uid"], dashboard["id"]) == ("batch-pipeline", None)
    assert [panel["title"] for panel in dashboard["panels"]] == [
        "Extraction throughput (bets/min)",
        "Cache hit rate (%)",
        "Cost per bet (USD)",
        "Queue depths",
        "Revisão pendente by reason (per hour)",
        "P95 latency per stage",
    ]
    (variable,) = dashboard["templating"]["list"]
    assert (variable["name"], variable["type"], variable["query"]) == (
        "datasource",
        "datasource",
        "prometheus",
    )
    source = {"type": "prometheus", "uid": "${datasource}"}
    for panel in dashboard["panels"]:
        assert panel["datasource"] == source
        assert all(target["datasource"] == source for target in panel["targets"])
    corners = [(panel["gridPos"]["x"], panel["gridPos"]["y"]) for panel in dashboard["panels"]]
    assert len(set(corners)) == len(corners)


def test_dashboard_queries_only_series_this_code_exports() -> None:
    expressions = [
        target["expr"] for panel in _dashboard()["panels"] for target in panel["targets"]
    ]

    series = set().union(*(_series_in(expression) for expression in expressions))

    assert "batch_job_duration_seconds_bucket" in series
    assert series <= _exported_series()
