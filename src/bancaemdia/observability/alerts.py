"""Typed Prometheus contract for the issue #33 alert policy.

HTTP latency reaches CloudWatch as a cumulative OTLP native histogram.  Its
rolling p99 is therefore calculated from ``rate`` over the base metric name,
then summed across request labels and replicas before ``histogram_quantile``.
There is no ``quantile=\"0.99\"`` label and no exported ``_bucket`` scalar in
that backend.  Expressions also aggregate away route, user, and review-reason
labels before alerting, keeping cardinality bounded and user identifiers out of
the operational dimensions.
"""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import structlog
from prometheus_client import Gauge

REPLICA_LAG_METRIC: Final = "pg_replication_lag_seconds"
REPLICA_ROLE: Final = "replica"
REPLICA_LAG_UNAVAILABLE: Final = -1.0
SERVICE_PLACEHOLDER: Final = "$service"
ENVIRONMENT_PLACEHOLDER: Final = "$environment"
_SAFE_SCOPE_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


class MetricSource(StrEnum):
    """Shape of the Prometheus source consumed by an alarm."""

    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    COUNTER = "counter"


class Comparison(StrEnum):
    GREATER_THAN = "GreaterThanThreshold"


@dataclass(frozen=True, slots=True)
class PrometheusScope:
    """Low-cardinality resource labels attached by the metrics pipeline."""

    service: str
    environment: str

    def __post_init__(self) -> None:
        for name, value in (("service", self.service), ("environment", self.environment)):
            if _SAFE_SCOPE_VALUE.fullmatch(value) is None:
                raise ValueError(f"invalid Prometheus {name}")


@dataclass(frozen=True, slots=True)
class AlarmSpec:
    """Provider-neutral contract for one CloudWatch PromQL alarm."""

    name: str
    expression: str
    source_metrics: tuple[str, ...]
    source: MetricSource
    comparison: Comparison
    threshold: float | None
    threshold_ratio: float | None
    evaluation_interval_seconds: int
    pending_period_seconds: int
    recovery_period_seconds: int

    def __post_init__(self) -> None:
        if not self.name or not self.expression or not self.source_metrics:
            raise ValueError("alarm name, expression, and source metrics are required")
        if self.evaluation_interval_seconds <= 0:
            raise ValueError("alarm evaluation interval must be positive")
        if self.pending_period_seconds < 0 or self.recovery_period_seconds < 0:
            raise ValueError("alarm pending and recovery periods cannot be negative")
        if (self.threshold is None) == (self.threshold_ratio is None):
            raise ValueError("alarm needs exactly one fixed or ratio threshold")
        value = self.threshold if self.threshold is not None else self.threshold_ratio
        if value is None or not math.isfinite(value) or value < 0:
            raise ValueError("alarm threshold must be finite and non-negative")


def _selector(service: str, environment: str, *matchers: str) -> str:
    labels = [*matchers, f'service="{service}"', f'environment="{environment}"']
    return "{" + ",".join(labels) + "}"


def build_alarm_specs(scope: PrometheusScope | None = None) -> tuple[AlarmSpec, ...]:
    """Build all nine alarms with canonical or concrete deployment labels.

    With no scope the expressions retain the exact ``$service`` and
    ``$environment`` placeholders consumed by infrastructure.  Passing a scope
    renders safe concrete PromQL, which is useful for validation and tooling.
    """

    service = SERVICE_PLACEHOLDER if scope is None else scope.service
    environment = ENVIRONMENT_PLACEHOLDER if scope is None else scope.environment
    http = _selector(service, environment)
    http_5xx = _selector(service, environment, 'status=~"5.."')
    replica = _selector(service, environment, f'role="{REPLICA_ROLE}"')
    extraction = _selector(service, environment, 'queue="extraction"')
    materialization = _selector(service, environment, 'queue="materialization"')
    dead_letter = _selector(service, environment, 'queue="dead_letter"')
    breaker = _selector(service, environment, 'breaker="anthropic"')

    return (
        AlarmSpec(
            name="API_P99_Latency",
            expression=(
                f"histogram_quantile(0.99, sum(rate(http_request_duration_seconds{http}[5m])))"
            ),
            source_metrics=("http_request_duration_seconds",),
            source=MetricSource.HISTOGRAM,
            comparison=Comparison.GREATER_THAN,
            threshold=1.0,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=300,
            recovery_period_seconds=300,
        ),
        AlarmSpec(
            name="API_Error_Rate",
            expression=(
                f"sum(rate(http_requests_total{http_5xx}[5m])) / "
                f"clamp_min(sum(rate(http_requests_total{http}[5m])), 1e-9)"
            ),
            source_metrics=("http_requests_total",),
            source=MetricSource.COUNTER,
            comparison=Comparison.GREATER_THAN,
            threshold=0.01,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=300,
            recovery_period_seconds=300,
        ),
        AlarmSpec(
            name="Replica_Lag",
            expression=f"max(pg_replication_lag_seconds{replica} >= 0)",
            source_metrics=(REPLICA_LAG_METRIC,),
            source=MetricSource.GAUGE,
            comparison=Comparison.GREATER_THAN,
            threshold=30.0,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=300,
            recovery_period_seconds=300,
        ),
        AlarmSpec(
            name="Extraction_Queue_Depth",
            expression=f"max(celery_queue_depth{extraction})",
            source_metrics=("celery_queue_depth",),
            source=MetricSource.GAUGE,
            comparison=Comparison.GREATER_THAN,
            threshold=100.0,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=600,
            recovery_period_seconds=300,
        ),
        AlarmSpec(
            name="Materialization_Queue_Depth",
            expression=f"max(celery_queue_depth{materialization})",
            source_metrics=("celery_queue_depth",),
            source=MetricSource.GAUGE,
            comparison=Comparison.GREATER_THAN,
            threshold=50.0,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=600,
            recovery_period_seconds=300,
        ),
        AlarmSpec(
            name="Circuit_Breaker_Open",
            # CloudWatch has no equality comparison operator. PromQL's bool modifier maps
            # every OPEN=1 series to 1 before max aggregates replicas. Comparing after max
            # would let a stale HALF_OPEN=2 series mask a concurrently OPEN replica.
            expression=f"max(circuit_breaker_state{breaker} == bool 1)",
            source_metrics=("circuit_breaker_state",),
            source=MetricSource.GAUGE,
            comparison=Comparison.GREATER_THAN,
            threshold=0.5,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=0,
            recovery_period_seconds=60,
        ),
        AlarmSpec(
            name="Anthropic_Daily_Cost",
            expression=f"sum(increase(anthropic_cost_usd_total{http}[24h]))",
            source_metrics=("anthropic_cost_usd_total",),
            source=MetricSource.COUNTER,
            comparison=Comparison.GREATER_THAN,
            threshold=None,
            threshold_ratio=0.8,
            evaluation_interval_seconds=60,
            pending_period_seconds=0,
            recovery_period_seconds=300,
        ),
        AlarmSpec(
            name="Revisao_Pendente_Spike",
            expression=f"sum(increase(revisao_pendente_created_total{http}[1h]))",
            source_metrics=("revisao_pendente_created_total",),
            source=MetricSource.COUNTER,
            comparison=Comparison.GREATER_THAN,
            threshold=100.0,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=0,
            recovery_period_seconds=300,
        ),
        AlarmSpec(
            name="DLQ_Depth",
            expression=f"max(celery_queue_depth{dead_letter})",
            source_metrics=("celery_queue_depth",),
            source=MetricSource.GAUGE,
            comparison=Comparison.GREATER_THAN,
            threshold=0.0,
            threshold_ratio=None,
            evaluation_interval_seconds=60,
            pending_period_seconds=300,
            recovery_period_seconds=300,
        ),
    )


ALARM_SPECS: Final = build_alarm_specs()


# A fixed label prevents client_python from materializing a misleading zero
# before the first real observation.  ``mostrecent`` keeps only the freshest
# writer when the registry runs in multiprocess mode; the -1 sentinel is filtered
# out by PromQL and avoids ``Gauge.remove()``, which multiprocess mode cannot do.
pg_replication_lag_seconds = Gauge(
    REPLICA_LAG_METRIC,
    "Seconds between the PostgreSQL replica and its primary; -1 means unavailable",
    ["role"],
    multiprocess_mode="mostrecent",
)


def observe_replica_lag(seconds: float | None) -> None:
    """Publish fresh replica lag, or an explicitly filtered unavailable sentinel."""

    if seconds is None:
        pg_replication_lag_seconds.labels(role=REPLICA_ROLE).set(REPLICA_LAG_UNAVAILABLE)
        return
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError("replica lag must be a number")
    numeric_seconds = float(seconds)
    if not math.isfinite(numeric_seconds) or numeric_seconds < 0:
        raise ValueError("replica lag must be finite and non-negative")
    pg_replication_lag_seconds.labels(role=REPLICA_ROLE).set(numeric_seconds)


ReplicaLagProbe = Callable[[], Awaitable[float | None]]
ReplicaLagObserver = Callable[[float | None], None]
AsyncSleep = Callable[[float], Awaitable[None]]
ProbeErrorReporter = Callable[[str], None]


def _report_replica_probe_error(error_name: str) -> None:
    # Only the exception class is logged: driver messages may contain a DSN or
    # other connection details that must not reach centralized logs.
    structlog.get_logger().warning("replica_lag_probe_failed", error=error_name)


class ReplicaLagMonitor:
    """Periodically sample replica lag independently of request traffic."""

    def __init__(
        self,
        probe: ReplicaLagProbe,
        *,
        interval_seconds: float = 30.0,
        observer: ReplicaLagObserver = observe_replica_lag,
        sleep: AsyncSleep = asyncio.sleep,
        report_error: ProbeErrorReporter = _report_replica_probe_error,
    ) -> None:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("replica lag monitor interval must be finite and positive")
        self._probe = probe
        self._interval_seconds = float(interval_seconds)
        self._observer = observer
        self._sleep = sleep
        self._report_error = report_error

    async def sample_once(self) -> bool:
        """Take one sample, converting probe failures into missing telemetry."""

        try:
            seconds = await self._probe()
            self._observer(seconds)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._observer(None)
            self._report_error(type(error).__name__)
            return False
        return seconds is not None

    async def run(self) -> None:
        """Sample immediately and then on every interval until cancelled."""

        try:
            while True:
                await self.sample_once()
                await self._sleep(self._interval_seconds)
        finally:
            # A cancelled task can coexist briefly with the metrics endpoint;
            # mark the sample unavailable instead of presenting stale lag as current.
            self._observer(None)
