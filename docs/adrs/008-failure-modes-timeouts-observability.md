# ADR 008: Failure Modes, Timeouts, Retries & Observability — Circuit Breakers, OpenTelemetry, Rate Limiting

## Status
Proposed

## Context

Current state: **Single-process FastAPI + SQLite**, no timeouts, no circuit breakers, no structured observability. Only `scripts/conferir_numeros.py` validates data integrity.

Target: **Multi-tenant SaaS on AWS** with external dependencies (Anthropic API, betting sites via extension), background workers, and SLOs (ADR-001: P99 latency < 1s, error rate < 1%, RPO=0).

**External Dependencies:**
| Dependency | Criticality | Failure Mode |
|------------|-------------|--------------|
| Anthropic API | High (extraction) | 5xx, timeout, rate limit, cost spike |
| Betting sites (extension) | High (coleta casa) | HTML/JSON changes, auth expiry, downtime |
| PostgreSQL (RDS) | Critical | Primary down, replica lag, query timeout |
| Redis (future) | Medium | Queue backing, rate limit state |
| Network | All | Partition, DNS, latency spike |

**Current Gaps:**
- Zero HTTP timeouts (httpx default = infinite)
- Zero retry logic (failed extraction = lost batch)
- Zero circuit breakers (cascading failures)
- Logs: unstructured, no request correlation
- Metrics: none
- Tracing: none
- Rate limiting: none (abuse vector)

## Decision

We implement **comprehensive resilience and observability** at launch: **circuit breakers** (`pybreaker`) on all external calls, **centralized timeouts + retries** (`tenacity`) via config, **OpenTelemetry full stack** (auto-instrument + custom spans), **application-level rate limiting** (`slowapi` by `usuario_id`), and **server-authoritative clocks** with client drift logging.

## Consequences

### Positive
- **Failure isolation**: Circuit breaker prevents cascade; queue accumulates, processes on recovery
- **Predictable latency**: Timeouts bound all external calls; `statement_timeout` protects PG
- **Debuggability**: Distributed traces + structured logs + metrics = MTTR minutes not hours
- **Cost protection**: Anthropic circuit breaker + per-user daily cap (ADR-001) = zero surprise bills
- **Abuse resistance**: Rate limits by authenticated user, not IP
- **Clock hygiene**: Server = truth; client drift logged, never used for decisions

### Negative
- **Operational complexity**: OpenTelemetry, circuit breakers, rate limiters = more moving parts
- **Dependencies**: `pybreaker`, `tenacity`, `slowapi`, `opentelemetry-instrument`, `structlog`, `prometheus-client`
- **Redis for rate limiting**: Required when scaling beyond 1 worker (MVP: in-memory)
- **Alert tuning**: Initial thresholds will need adjustment (false positives/negatives)

### Neutral
- **AWS X-Ray vs OpenTelemetry**: OTel vendor-neutral; X-Ray exporter if needed
- **Client clock drift**: Logged not fixed; user education if chronic

## Options Considered

### Option A: Minimal (Logs Only) — *Rejected*
**Pros:** Simple
**Cons:** Blind to latency, errors, cost; debug = guesswork

### Option B: Logs + Metrics (Prometheus) — *Rejected*
**Pros:** Dashboards, alerts
**Cons:** No distributed tracing; can't debug "why this request failed"

### Option C: Full OpenTelemetry + Circuit Breakers + Rate Limiting — *Chosen*
**Pros:** Production-grade from day 1; auto-instrumentation covers 80%; custom spans for business logic
**Cons:** More setup; learning curve

### Option D: Circuit Breaker Custom Implementation — *Rejected*
**Pros:** Zero deps
**Cons:** `pybreaker` is mature, tested, has Prometheus metrics; reinventing = bugs

### Option E: Rate Limiting at AWS WAF Only — *Rejected*
**Pros:** Offloads app
**Cons:** IP-based (not user-based); can't enforce per-user Anthropic cost caps

## Compliance

- [ ] **Circuit Breakers**:
  - [ ] Anthropic client (`extracao/cliente.py`): `pybreaker` fail_max=5, reset_timeout=60s, exclude timeouts
  - [ ] `/coleta` endpoint: per-token breaker (fail_max=10, reset_timeout=30s)
  - [ ] Metrics: `circuit_state{gauge}`, `circuit_failures_total{counter}`
  - [ ] Health endpoint exposes breaker states
- [ ] **Timeouts** (centralized in `config.py` / Pydantic Settings):
  - [ ] `HTTP_TIMEOUT_CONNECT=5`, `HTTP_TIMEOUT_READ=30`
  - [ ] `ANTHROPIC_TIMEOUT=30`, `ANTHROPIC_MAX_RETRIES=3`
  - [ ] `PG_STATEMENT_TIMEOUT_PRIMARY=5000` (ms), `PG_STATEMENT_TIMEOUT_REPLICA=30000`
  - [ ] `BACKGROUND_JOB_TIMEOUT=300` (s), `BACKGROUND_MAX_RETRIES=3`
- [ ] **Retries** (`tenacity`):
  - [ ] Anthropic: `wait_exponential(multiplier=1, min=1, max=4) + jitter`, `stop_after_attempt(3)`, `retry_if_exception_type(Timeout, HTTPStatusError)`
  - [ ] HTTP generic: `wait_exponential(multiplier=0.5, min=0.5, max=2)`, `stop_after_attempt(2)`
  - [ ] Background jobs: `wait_fixed(30)`, `stop_after_attempt(3)`, dead letter queue on final failure
  - [ ] Idempotency keys required for all retried POSTs (natural keys per ADR-007)
- [ ] **Observability**:
  - [ ] **Logs**: `structlog` JSON output; fields: `timestamp`, `level`, `logger`, `message`, `request_id`, `usuario_id`, `trace_id`, `span_id`
  - [ ] **Metrics**: `prometheus-fastapi-instrumentator` + custom:
    - `http_request_duration_seconds{method,path,status}` (Histogram)
    - `anthropic_request_duration_seconds{model,status}` (Histogram)
    - `anthropic_cost_usd_total{usuario_id}` (Counter)
    - `extraction_queue_depth` (Gauge)
    - `circuit_breaker_state{breaker}` (Gauge: 0=closed, 1=open, 2=half-open)
    - `pg_replication_lag_seconds` (Gauge)
    - `rate_limit_exceeded_total{endpoint}` (Counter)
  - [ ] **Tracing**: `opentelemetry-instrument` auto (FastAPI, SQLAlchemy, httpx, Redis); custom spans for:
    - `planilhar.lote` (attributes: `lote_size`, `usuario_id`)
    - `extracao.chamar_anthropic` (attributes: `model`, `versao_prompt`, `chat_id`, `message_id`)
    - `cruzamento.pareador` (attributes: `aposta_nova_id`, `aposta_existente_id`, `resultado`)
    - `coleta.casa` (attributes: `casa`, `quantidade`, `usuario_id`)
  - [ ] **Alerting** (CloudWatch → SNS → Slack/PagerDuty):
    - `http_p99_latency > 1s` for 5min
    - `error_rate > 1%` for 5min
    - `replica_lag > 30s` for 5min
    - `extraction_queue_depth > 100` for 10min
    - `circuit_breaker_state{anthropic} == 1` (OPEN)
    - `anthropic_daily_cost_usd > 80%_limit`
- [ ] **Health Checks**:
  - [ ] `GET /health` (liveness): process alive
  - [ ] `GET /ready` (readiness): PG primary writable, Anthropic reachable (HEAD), queue depth < threshold
- [ ] **Clock Synchronization**:
  - [ ] Server: NTP (AWS default)
  - [ ] Extension: sends `capturado_em`; server records `recebido_em = now()`
  - [ ] Drift validation: log WARNING if `abs(recebido_em - capturado_em) > 300s`
  - [ ] Never use `capturado_em` for ordering, dedup, or temporal logic
- [ ] **Rate Limiting** (`slowapi`):
  - [ ] Key function: `usuario_id` (from JWT/session)
  - [ ] Limits: `/coleta` 10/min; `/api/v1/*` 100/min; `/upload` 1/5min + 50MB
  - [ ] Storage: in-memory (MVP single worker); Redis (`slowapi.RedisBackend`) when multi-worker
  - [ ] AWS WAF: Managed rules (CommonRuleSet) only; no custom rate rules

## Implementation Patterns

### Circuit Breaker (Anthropic)
```python
# extracao/cliente.py
import pybreaker
from prometheus_client import Gauge

breaker_state = Gauge("circuit_breaker_state", "0=closed, 1=open, 2=half-open", ["breaker"])

class PrometheusListener(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        state_map = {"closed": 0, "open": 1, "half_open": 2}
        breaker_state.labels(breaker=cb.name).set(state_map.get(new_state, -1))

anthropic_breaker = pybreaker.CircuitBreaker(
    name="anthropic",
    fail_max=5,
    reset_timeout=60,
    exclude=[httpx.TimeoutException],
    listeners=[PrometheusListener()]
)

@anthropic_breaker
async def chamar_anthropic(payload: dict, model: str) -> dict:
    async with httpx.AsyncClient(timeout=config.ANTHROPIC_TIMEOUT) as client:
        resp = await client.post(ANTHROPIC_URL, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
```

### Timeouts & Retries (Centralized Config)
```python
# config.py
class Settings(BaseSettings):
    # HTTP
    HTTP_TIMEOUT_CONNECT: float = 5.0
    HTTP_TIMEOUT_READ: float = 30.0
    
    # Anthropic
    ANTHROPIC_TIMEOUT: float = 30.0
    ANTHROPIC_MAX_RETRIES: int = 3
    
    # PostgreSQL
    PG_STATEMENT_TIMEOUT_PRIMARY: int = 5000  # ms
    PG_STATEMENT_TIMEOUT_REPLICA: int = 30000
    
    # Background
    BACKGROUND_JOB_TIMEOUT: int = 300
    BACKGROUND_MAX_RETRIES: int = 3

# httpx client singleton
httpx_client = httpx.AsyncClient(
    timeout=httpx.Timeout(
        connect=settings.HTTP_TIMEOUT_CONNECT,
        read=settings.HTTP_TIMEOUT_READ
    ),
    limits=httpx.Limits(max_connections=20, max_keepalive=10)
)

# tenacity retry decorator
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

def anthropic_retry():
    return retry(
        wait=wait_exponential_jitter(initial=1, max=4),
        stop=stop_after_attempt(settings.ANTHROPIC_MAX_RETRIES),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
        reraise=True
    )
```

### OpenTelemetry Setup
```bash
# requirements.txt
opentelemetry-instrument
opentelemetry-exporter-otlp
opentelemetry-sdk
opentelemetry-instrumentation-fastapi
opentelemetry-instrumentation-sqlalchemy
opentelemetry-instrumentation-httpx
opentelemetry-instrumentation-redis

# Run
opentelemetry-instrument \
  --traces_exporter otlp \
  --metrics_exporter otlp \
  --logs_exporter otlp \
  --service_name bancaemdia-api \
  fastapi run app.py
```

```python
# Custom spans
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

async def processar_lote(usuario_id: int, apostas: list):
    with tracer.start_as_current_span("planilhar.lote") as span:
        span.set_attribute("usuario_id", usuario_id)
        span.set_attribute("lote_size", len(apostas))
        # ...
```

### Rate Limiting
```python
# main.py
from slowapi import Limiter
from slowapi.util import get_remote_address

def get_user_id(request: Request) -> str:
    return request.state.usuario_id  # set by auth middleware

limiter = Limiter(key_func=get_user_id, storage_uri="memory://")  # Redis:// when multi-worker
app.state.limiter = limiter

@router.post("/coleta")
@limiter.limit("10/minute")
async def coleta(request: Request, payload: ColetaPayload):
    ...

@router.post("/api/v1/apostas")
@limiter.limit("100/minute")
async def criar_aposta(request: Request, aposta: ApostaCreate):
    ...
```

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** Circuit breaker false positives/negatives, alert fatigue, multi-worker deployment (Redis for rate limiting), cost anomaly detection needed
- **Related ADRs:** ADR-001 (QARs), ADR-003 (Storage), ADR-005 (Replication), ADR-007 (Transactions), ADR-010 (Batch Pipeline), ADR-011 (Stream Processing)
- **Open Items:** Exact alert thresholds (tune post-launch); DLQ implementation for background jobs; OpenTelemetry sampling rate (100% launch, 10% scale)

## References

- DDIA Ch 8: Distributed Systems Challenges (failures, timeouts, retries, observability)
- `pybreaker` docs: https://pybreaker.readthedocs.io/
- `tenacity` docs: https://tenacity.readthedocs.io/
- `slowapi` docs: https://github.com/lauralx/slowapi
- OpenTelemetry Python: https://opentelemetry.io/docs/instrumentation/python/
- AWS X-Ray vs OpenTelemetry: https://docs.aws.amazon.com/xray/latest/devguide/xray-otel.html
- `structlog`: https://www.structlog.org/
- `planilhador/extracao/cliente.py` — current Anthropic client (no timeouts, no breaker)
- `planilhador/banco/conexao.py` — current PG connection (no statement_timeout)