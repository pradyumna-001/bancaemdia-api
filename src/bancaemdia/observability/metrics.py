import mmap
import os
import platform
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anthropic
import redis
from fastapi import FastAPI
from kombu.transport.redis import PRIORITY_STEPS, Channel
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.multiprocess import MultiProcessCollector
from prometheus_client.registry import Collector
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator.metrics import Info

MULTIPROC_DIR = "PROMETHEUS_MULTIPROC_DIR"
# A extração espera a ficha do limitador dentro da tarefa, e essa espera chega a minutos.
BATCH_JOB_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
# Uma chamada inclui as novas tentativas do SDK: 4 tentativas de até 30 s, mais as pausas e o
# retry-after de até 60 s, passam de dois minutos.
ANTHROPIC_REQUEST_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 180.0, 300.0)
RATE_LIMIT_WAIT_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)
HTTP_REQUEST_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
UNMATCHED_HTTP_PATH: Final = "__unmatched__"
_KNOWN_HTTP_METHODS: Final = frozenset({
    "CONNECT",
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
    "TRACE",
})
_HTTP_INSTRUMENTATION_MARKER: Final = "_bancaemdia_http_metrics_installed"
# On Linux, client_python already publishes the Prometheus-standard counter family
# `process_cpu_seconds` (whose sample is `process_cpu_seconds_total`). Emitting an exact-name gauge
# beside it would produce two HELP/TYPE declarations with different types. Platforms where the
# default collector is unavailable get the portable fallback below instead.
_HAS_DEFAULT_PROCESS_CPU = any(
    family.name == "process_cpu_seconds" for family in REGISTRY.collect()
)


def _resident_memory_bytes() -> int | None:
    if platform.system() == "Windows":
        # `prometheus_client` reads Linux /proc and therefore omits process memory on Windows.
        # Keep the exact issue metric available to local operators and tests as well.
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("page_fault_count", wintypes.DWORD),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        try:
            win_dll = getattr(ctypes, "WinDLL", None)
            if win_dll is None:
                return None
            kernel32 = win_dll("kernel32", use_last_error=True)
            psapi = win_dll("psapi", use_last_error=True)
            get_current_process = kernel32.GetCurrentProcess
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info = psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL
            ok = get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb)
        except (AttributeError, OSError):
            return None
        return int(counters.working_set_size) if ok else None

    try:
        fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
        return int(fields[1]) * mmap.PAGESIZE
    except (IndexError, OSError, ValueError):
        return None


class SystemMetricsCollector:
    """Portable process metrics without colliding with client_python's Linux counter."""

    def collect(self) -> Iterator[Metric]:
        if not _HAS_DEFAULT_PROCESS_CPU:
            yield CounterMetricFamily(
                "process_cpu_seconds",
                "User and system CPU time consumed by this process in seconds",
                value=time.process_time(),
            )
        memory = _resident_memory_bytes()
        if memory is not None:
            yield GaugeMetricFamily(
                "process_memory_bytes",
                "Resident memory currently used by this process in bytes",
                value=memory,
            )


@dataclass(frozen=True, slots=True)
class HttpMetrics:
    """Collectors used by the FastAPI instrumentator.

    Keeping the collectors in a small value object lets tests use an isolated registry instead of
    registering the same global time series more than once.
    """

    registry: CollectorRegistry
    duration: Histogram
    requests: Counter


def create_http_metrics(registry: CollectorRegistry) -> HttpMetrics:
    """Create the HTTP collectors in an otherwise independent Prometheus registry."""

    return HttpMetrics(
        registry=registry,
        duration=Histogram(
            "http_request_duration_seconds",
            "Seconds spent serving an HTTP request",
            ["method", "path", "status"],
            buckets=HTTP_REQUEST_DURATION_BUCKETS,
            registry=registry,
        ),
        requests=Counter(
            "http_requests",
            "HTTP requests served",
            ["method", "path", "status"],
            registry=registry,
        ),
    )


_http_metrics = create_http_metrics(REGISTRY)
http_request_duration_seconds = _http_metrics.duration
http_requests_total = _http_metrics.requests

apostas_created_total = Counter(
    "apostas_created",
    "Bets created, by ingestion origin and initial state",
    ["origem", "estado"],
)
caixa_movimentos_total = Counter(
    "caixa_movimentos",
    "Cash ledger entries created, by movement type",
    ["tipo"],
)
system_metrics = SystemMetricsCollector()
REGISTRY.register(system_metrics)

batch_job_duration = Histogram(
    "batch_job_duration_seconds",
    "Seconds a pipeline task took, by stage and outcome",
    ["stage", "status"],
    buckets=BATCH_JOB_BUCKETS,
)
batch_failures = Counter(
    "batch_failed",
    "Pipeline task attempts that raised, by stage and exception",
    ["stage", "reason"],
)
# Vazão, taxa de acerto do cache e custo por aposta saem destes contadores no PromQL: cada filho
# do prefork teria o seu gauge, e taxa ou razão de vários processos não se soma.
batch_bets_processed = Counter(
    "batch_bets_processed", "Bets that went through a pipeline stage", ["stage"]
)
revisao_pendente_created = Counter(
    "revisao_pendente_created", "Reviews opened for grave bets, by reason", ["reason"]
)
anthropic_cost = Counter(
    "anthropic_cost_usd", "US dollars billed by Anthropic, by user", ["usuario_id"]
)
anthropic_request_duration = Histogram(
    "anthropic_request_duration_seconds",
    "Seconds an Anthropic Messages call took, SDK retries included",
    ["model", "status"],
    buckets=ANTHROPIC_REQUEST_BUCKETS,
)
queue_depth_errors = Counter(
    "celery_queue_depth_error", "Redis errors while reading Celery queue depths"
)

coleta_received = Counter(
    "coleta_received",
    "House bets received from the browser extension, by outcome",
    ["casa", "status"],
)
coleta_dedup = Counter("coleta_dedup", "House bets resent with the same content")

cache_hits = Counter("extraction_cache_hit", "Extraction readings served from the Redis cache")
cache_misses = Counter("extraction_cache_miss", "Extraction readings not found in the Redis cache")
cache_errors = Counter(
    "extraction_cache_error", "Redis errors and unreadable entries in the extraction cache"
)

rate_limit_wait = Histogram(
    "rate_limit_wait_seconds",
    "Seconds a request waited for Anthropic rate-limit tokens",
    buckets=RATE_LIMIT_WAIT_BUCKETS,
)
rate_limit_exceeded = Counter(
    "rate_limit_exceeded",
    "Requests that found an Anthropic rate-limit bucket empty and had to wait",
    ["scope"],
)
rate_limit_errors = Counter(
    "rate_limit_error", "Redis errors that let an Anthropic request through unmetered"
)
rate_limiter_fallback_total = Counter(
    "rate_limiter_fallback",
    "Transitions from the shared rate-limit storage to the in-memory fallback",
    ["policy", "storage_backend"],
)

# Cada processo só vê as transições que ele mesmo causou; entre os arquivos do modo multiprocesso
# vale a escrita mais recente.
circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state: 0=closed, 1=open, 2=half-open",
    ["breaker"],
    multiprocess_mode="mostrecent",
)

# Contam as mesmas tentativas que batch_*{stage="materialization"}, com os motivos agrupados em
# payload_invalido, banco e inesperado em vez do nome da exceção.
materialization_duration = Histogram(
    "materialization_duration_seconds", "Seconds to materialize one extraction reading"
)
materialization_failures = Counter(
    "materialization_failed", "Extraction readings that failed to materialize", ["reason"]
)

event_handler_failures = Counter(
    "event_handler_failed", "In-process event handlers that raised", ["event"]
)


def _http_method(method: str) -> str:
    method = method.upper()
    return method if method in _KNOWN_HTTP_METHODS else "OTHER"


def _http_instrumentation(metrics: HttpMetrics) -> Callable[[Info], None]:
    def observe(info: Info) -> None:
        # `modified_handler` is the route template resolved by the instrumentator. Unmatched URLs
        # are deliberately collapsed to one sentinel; the raw request URL must never be a label.
        path = info.modified_handler
        if not path.startswith("/"):
            path = UNMATCHED_HTTP_PATH
        labels = {
            "method": _http_method(info.method),
            "path": path,
            "status": info.modified_status,
        }
        metrics.requests.labels(**labels).inc()
        metrics.duration.labels(**labels).observe(info.modified_duration)

    return observe


def instrument_http_metrics(app: FastAPI, metrics: HttpMetrics | None = None) -> Instrumentator:
    """Install HTTP metrics on ``app`` once.

    The optional collector bundle is intended for isolated application tests. Production should
    omit it so the endpoint and the middleware expose the same default registry.
    """

    installed = getattr(app.state, _HTTP_INSTRUMENTATION_MARKER, None)
    if isinstance(installed, Instrumentator):
        return installed

    selected = metrics or _http_metrics
    instrumentator = Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=False,
        should_group_untemplated=True,
        registry=selected.registry,
    )
    instrumentator.add(_http_instrumentation(selected)).instrument(app)
    setattr(app.state, _HTTP_INSTRUMENTATION_MARKER, instrumentator)
    return instrumentator


@contextmanager
def observe_stage(stage: str) -> Iterator[None]:
    started = time.perf_counter()
    status = "failed"
    try:
        yield
        status = "success"
    except Exception as error:
        batch_failures.labels(stage=stage, reason=type(error).__name__).inc()
        raise
    finally:
        batch_job_duration.labels(stage=stage, status=status).observe(time.perf_counter() - started)


@contextmanager
def observe_anthropic_request(model: str) -> Iterator[None]:
    started = time.perf_counter()
    status = "error"
    try:
        yield
        status = "200"
    except anthropic.APIStatusError as error:
        status = str(error.status_code)
        raise
    except anthropic.APITimeoutError:
        status = "timeout"
        raise
    except anthropic.APIConnectionError:
        status = "connection_error"
        raise
    finally:
        anthropic_request_duration.labels(model=model, status=status).observe(
            time.perf_counter() - started
        )


def queue_key(queue: str, priority: int) -> str:
    return f"{queue}{Channel.sep}{priority}" if priority else queue


class QueueDepthCollector:
    def __init__(self, client: redis.Redis, queues: Sequence[str]) -> None:
        self.client = client
        self.queues = tuple(queues)

    def collect(self) -> Iterator[GaugeMetricFamily]:
        # O kombu guarda cada faixa de prioridade numa lista própria no Redis; a fila é a soma
        # delas, como no `_size` do transporte.
        try:
            with self.client.pipeline(transaction=False) as pipe:
                for queue in self.queues:
                    for priority in PRIORITY_STEPS:
                        pipe.llen(queue_key(queue, priority))
                sizes = pipe.execute()
        except redis.RedisError:
            queue_depth_errors.inc()
            return
        depth = GaugeMetricFamily(
            "celery_queue_depth",
            "Tasks waiting in each Celery queue on the broker",
            labels=["queue"],
        )
        steps = len(PRIORITY_STEPS)
        for index, queue in enumerate(self.queues):
            depth.add_metric([queue], sum(sizes[index * steps : (index + 1) * steps]))
        yield depth


def multiprocess_mode() -> bool:
    # O mesmo teste do prometheus_client: com a variável presente, mesmo vazia, os processos já
    # gravam em arquivo, e ler o REGISTRY daria zero sem erro nenhum.
    return MULTIPROC_DIR in os.environ or MULTIPROC_DIR.lower() in os.environ


def metrics_registry(*collectors: Collector) -> CollectorRegistry:
    registry = CollectorRegistry()
    # Os coletores extras são lidos primeiro: o das filas conta os próprios erros, e o contador
    # tem de sair na mesma coleta em que o erro aconteceu.
    for collector in collectors:
        registry.register(collector)
    # No prefork cada filho do Celery tem o seu REGISTRY; com PROMETHEUS_MULTIPROC_DIR os processos
    # gravam em arquivos, e quem expõe soma os arquivos de todos.
    if multiprocess_mode():
        # Process aliases describe this metrics-serving process, not a value to sum across the
        # worker files. Register them directly so they remain present in prefork mode too.
        registry.register(system_metrics)
        ProcessCollector(registry=registry)
        MultiProcessCollector(registry)
    else:
        registry.register(REGISTRY)
    return registry
