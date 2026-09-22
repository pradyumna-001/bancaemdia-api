import asyncio
import math
import os
import subprocess
import sys

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, multiprocess

from bancaemdia.observability.alerts import (
    ALARM_SPECS,
    ENVIRONMENT_PLACEHOLDER,
    REPLICA_LAG_METRIC,
    REPLICA_LAG_UNAVAILABLE,
    SERVICE_PLACEHOLDER,
    AlarmSpec,
    Comparison,
    MetricSource,
    PrometheusScope,
    ReplicaLagMonitor,
    build_alarm_specs,
    observe_replica_lag,
)


def test_alarm_contract_covers_the_nine_issue_alarms() -> None:
    assert [spec.name for spec in ALARM_SPECS] == [
        "API_P99_Latency",
        "API_Error_Rate",
        "Replica_Lag",
        "Extraction_Queue_Depth",
        "Materialization_Queue_Depth",
        "Circuit_Breaker_Open",
        "Anthropic_Daily_Cost",
        "Revisao_Pendente_Spike",
        "DLQ_Depth",
    ]
    assert len({spec.name for spec in ALARM_SPECS}) == 9
    assert all(spec.evaluation_interval_seconds == 60 for spec in ALARM_SPECS)
    assert all(spec.pending_period_seconds >= 0 for spec in ALARM_SPECS)
    assert all(spec.recovery_period_seconds >= 0 for spec in ALARM_SPECS)
    assert all(SERVICE_PLACEHOLDER in spec.expression for spec in ALARM_SPECS)
    assert all(ENVIRONMENT_PLACEHOLDER in spec.expression for spec in ALARM_SPECS)


def test_http_alarm_queries_match_the_real_metric_shapes() -> None:
    specs = {spec.name: spec for spec in ALARM_SPECS}

    latency = specs["API_P99_Latency"]
    assert latency.source is MetricSource.HISTOGRAM
    assert latency.expression == (
        "histogram_quantile(0.99, sum(rate(http_request_duration_seconds"
        '{service="$service",environment="$environment"}[5m])))'
    )
    assert 'quantile="0.99"' not in latency.expression
    assert "_bucket" not in latency.expression
    assert latency.source_metrics == ("http_request_duration_seconds",)
    assert latency.pending_period_seconds == 300

    error_rate = specs["API_Error_Rate"]
    assert error_rate.source is MetricSource.COUNTER
    assert 'status=~"5.."' in error_rate.expression
    assert "clamp_min" in error_rate.expression
    assert error_rate.threshold == pytest.approx(0.01)


def test_business_counter_alarms_aggregate_away_sensitive_labels() -> None:
    specs = {spec.name: spec for spec in ALARM_SPECS}

    cost = specs["Anthropic_Daily_Cost"]
    assert cost.expression == (
        "sum(increase(anthropic_cost_usd_total"
        '{service="$service",environment="$environment"}[24h]))'
    )
    assert cost.threshold is None
    assert cost.threshold_ratio == pytest.approx(0.8)
    assert cost.pending_period_seconds == 0
    assert cost.recovery_period_seconds == 300

    reviews = specs["Revisao_Pendente_Spike"]
    assert reviews.expression == (
        "sum(increase(revisao_pendente_created_total"
        '{service="$service",environment="$environment"}[1h]))'
    )
    assert reviews.threshold == 100
    assert reviews.pending_period_seconds == 0
    assert reviews.recovery_period_seconds == 300
    assert "usuario_id" not in cost.expression
    assert "reason" not in reviews.expression


def test_queue_and_breaker_alarms_use_existing_bounded_labels() -> None:
    specs = {spec.name: spec for spec in ALARM_SPECS}

    assert 'queue="extraction"' in specs["Extraction_Queue_Depth"].expression
    assert 'queue="materialization"' in specs["Materialization_Queue_Depth"].expression
    assert 'queue="dead_letter"' in specs["DLQ_Depth"].expression
    assert specs["Replica_Lag"].expression.endswith(" >= 0)")
    breaker = specs["Circuit_Breaker_Open"]
    assert 'breaker="anthropic"' in breaker.expression
    assert breaker.expression.endswith(" == bool 1)")
    assert ") == bool 1" not in breaker.expression
    assert breaker.comparison is Comparison.GREATER_THAN
    assert breaker.threshold == pytest.approx(0.5)


def test_queries_can_be_safely_rendered_for_one_service_and_environment() -> None:
    specs = build_alarm_specs(PrometheusScope(service="bancaemdia-api", environment="prod.br"))

    for spec in specs:
        assert 'service="bancaemdia-api"' in spec.expression
        assert 'environment="prod.br"' in spec.expression
        assert "$service" not in spec.expression
        assert "$environment" not in spec.expression
    assert 'status=~"5..",service="bancaemdia-api",environment="prod.br"' in specs[1].expression


@pytest.mark.parametrize(
    ("service", "environment"),
    [
        ('api"} or vector(1)', "prod"),
        ("api", "prod\nmalicious"),
        ("api", ""),
        ("api", "x" * 129),
    ],
)
def test_scope_rejects_promql_injection_and_unbounded_values(
    service: str, environment: str
) -> None:
    with pytest.raises(ValueError, match="invalid Prometheus"):
        PrometheusScope(service=service, environment=environment)


def test_replica_lag_uses_a_filtered_sentinel_when_unavailable() -> None:
    observe_replica_lag(None)
    assert REGISTRY.get_sample_value(REPLICA_LAG_METRIC, {"role": "replica"}) == pytest.approx(
        REPLICA_LAG_UNAVAILABLE
    )

    observe_replica_lag(31.25)
    assert REGISTRY.get_sample_value(REPLICA_LAG_METRIC, {"role": "replica"}) == pytest.approx(
        31.25
    )

    observe_replica_lag(None)
    assert REGISTRY.get_sample_value(REPLICA_LAG_METRIC, {"role": "replica"}) == pytest.approx(
        REPLICA_LAG_UNAVAILABLE
    )


def test_replica_lag_unavailable_replaces_a_stale_multiprocess_value(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = (
        "from bancaemdia.observability.alerts import observe_replica_lag\n"
        "observe_replica_lag(42)\n"
        "observe_replica_lag(None)\n"
    )
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    subprocess.run(
        [sys.executable, "-c", script],
        env=os.environ.copy(),
        check=True,
    )
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)

    assert registry.get_sample_value(REPLICA_LAG_METRIC, {"role": "replica"}) == pytest.approx(
        REPLICA_LAG_UNAVAILABLE
    )


@pytest.mark.parametrize("value", [-0.1, math.nan, math.inf, True, "31"])
def test_replica_lag_rejects_unsafe_or_invalid_samples(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        observe_replica_lag(value)  # type: ignore[arg-type]


async def test_replica_lag_monitor_samples_without_request_traffic() -> None:
    observations: list[float | None] = []

    async def probe() -> float:
        await asyncio.sleep(0)
        return 12.5

    monitor = ReplicaLagMonitor(probe, observer=observations.append)

    assert await monitor.sample_once()
    assert observations == [12.5]


async def test_replica_lag_monitor_marks_unavailable_and_sanitizes_probe_errors() -> None:
    observations: list[float | None] = [10.0]
    errors: list[str] = []

    async def failing_probe() -> float:
        await asyncio.sleep(0)
        raise RuntimeError("postgres://user:secret@example.invalid")

    monitor = ReplicaLagMonitor(
        failing_probe,
        observer=observations.append,
        report_error=errors.append,
    )

    assert not await monitor.sample_once()
    assert observations == [10.0, None]
    assert errors == ["RuntimeError"]


async def test_replica_lag_monitor_is_periodic_and_cancellation_marks_unavailable() -> None:
    observations: list[float | None] = []
    sampled = asyncio.Event()
    calls = 0

    async def probe() -> float:
        nonlocal calls
        await asyncio.sleep(0)
        calls += 1
        sampled.set()
        return float(calls)

    monitor = ReplicaLagMonitor(probe, interval_seconds=30, observer=observations.append)
    task = asyncio.create_task(monitor.run())
    await sampled.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1
    assert observations == [1.0, None]


@pytest.mark.parametrize("interval", [0, -1, math.nan, math.inf, True])
def test_replica_lag_monitor_rejects_invalid_intervals(interval: object) -> None:
    async def probe() -> None:
        await asyncio.sleep(0)
        return None

    with pytest.raises(ValueError, match="finite and positive"):
        ReplicaLagMonitor(probe, interval_seconds=interval)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"expression": ""},
        {"source_metrics": ()},
        {"evaluation_interval_seconds": 0},
        {"pending_period_seconds": -1},
        {"recovery_period_seconds": -1},
        {"threshold": None, "threshold_ratio": None},
        {"threshold": 1.0, "threshold_ratio": 0.8},
        {"threshold": math.nan},
    ],
)
def test_alarm_spec_rejects_an_unsafe_or_ambiguous_contract(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "name": "Alarm",
        "expression": "metric > 0",
        "source_metrics": ("metric",),
        "source": MetricSource.GAUGE,
        "comparison": Comparison.GREATER_THAN,
        "threshold": 1.0,
        "threshold_ratio": None,
        "evaluation_interval_seconds": 60,
        "pending_period_seconds": 0,
        "recovery_period_seconds": 60,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        AlarmSpec(**values)  # type: ignore[arg-type]
