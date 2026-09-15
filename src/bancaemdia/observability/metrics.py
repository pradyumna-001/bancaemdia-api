import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import anthropic
import redis
from kombu.transport.redis import PRIORITY_STEPS, Channel
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.multiprocess import MultiProcessCollector
from prometheus_client.registry import Collector

MULTIPROC_DIR = "PROMETHEUS_MULTIPROC_DIR"
# A extração espera a ficha do limitador dentro da tarefa, e essa espera chega a minutos.
BATCH_JOB_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
# Uma chamada inclui as novas tentativas do SDK: 4 tentativas de até 30 s, mais as pausas e o
# retry-after de até 60 s, passam de dois minutos.
ANTHROPIC_REQUEST_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 180.0, 300.0)
RATE_LIMIT_WAIT_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

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
        MultiProcessCollector(registry)
    else:
        registry.register(REGISTRY)
    return registry
