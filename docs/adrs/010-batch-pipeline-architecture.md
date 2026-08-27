# ADR 010: Batch Pipeline Architecture — Dual Worker Pools, At-Least-Once + Idempotency, Shared Pool

## Status
Proposed

## Context

Current state: **`scripts/planilhar.py`** runs synchronously in a single process:
1. Parse Telegram export (zip/JSON) → raw messages
2. For each message with photo: **OCR → Haiku → Sonnet** (blocking, $, rate-limited)
3. Materialize: `eventos` + `apostas` + `revisao_pendente` (single transaction per bet)
4. Project: recalculate ROI, saldo, lucro (synchronous)
5. Generate Excel (synchronous)

**Projected Launch Volume**: 50 users × 200 bets/day = **10k bets/day** (~12k IA extractions/day with cache miss). Current sequential processing = hours.

**Requirements (ADR-001):**
- Extraction latency P95 < 5min for 200-bet batch
- Cost per bet measured and capped per user
- Financial correctness > availability
- Zero data loss

## Decision

We implement a **dual-pool async batch pipeline** using **Celery + Redis**:
- **Extraction Pool**: OCR → Haiku → Sonnet (CPU/IO bound, $, rate-limited by Anthropic)
- **Materialization Pool**: Receives extraction result → PG Primary transaction (DB bound, high throughput)
- **Semantics**: **At-least-once + idempotency** via natural keys (no transactional outbox)
- **Workers**: **Shared pool** with per-user rate limiting (maximizes cache hit rate)
- **Fault Tolerance**: `revisao_pendente` = business DLQ (extraction grave); Celery DLQ = technical DLQ
- **Triggers**: Event-driven (upload, coleta) + Manual CLI (reprocess)

## Consequences

### Positive
- **Cost isolation**: Extraction ($, slow) separated from Materialization (fast, free)
- **Horizontal scaling**: Independent worker pools; add extraction workers on cache miss spike
- **Cache efficiency**: Shared pool → 80%+ cross-tenant cache hit rate (100x cost reduction)
- **Resilience**: At-least-once + idempotency = safe retries; `revisao_pendente` absorbs business failures
- **Observability**: Per-stage metrics (duration, throughput, cost, cache hit, failures)
- **Replayability**: `conferir_numeros.py` validates end-to-end integrity; deterministic projection

### Negative
- **Operational complexity**: 2 queues, 2 pools, Redis broker, Celery deployment
- **Ordering dependency**: Materialization waits for Extraction completion
- **Dual DLQ**: Business (`revisao_pendente`) + Technical (Celery DLQ) monitoring needed
- **Rate limiting coordination**: Shared pool needs per-user + global limits

### Neutral
- **Exactly-once**: Not implemented; idempotency keys sufficient for financial correctness
- **Transactional outbox**: Deferred; not needed with natural key idempotency
- **Per-tenant pools**: Deferred; shared pool + rate limits adequate for launch

## Options Considered

### Option A: Single Process (Current) — *Rejected*
**Pros:** Simple
**Cons:** Hours for 10k bets; no scaling; extraction blocks DB; no resilience

### Option B: Single Async Pool (All Stages) — *Rejected*
**Pros:** Simpler than dual pool
**Cons:** Extraction $ blocks DB workers; rate limiting harder; cache hit doesn't free DB slots

### Option C: Dual Pool (Extraction + Materialization) — *Chosen*
**Pros:** Cost/throughput isolation; independent scaling; cache hit skips extraction pool
**Cons:** More components; ordering coordination

### Option D: Exactly-Once (Transactional Outbox) — *Rejected*
**Pros:** Theoretical guarantee
**Cons:** Complex (outbox table + poller + dedup consumer); natural keys already provide idempotency

### Option E: Per-Tenant Worker Pools — *Rejected*
**Pros:** Perfect isolation
**Cons:** Fragmented cache (more $); N× ops overhead; rate limits already isolate cost

## Compliance

- [ ] **Extraction Pool** (Celery queue: `extraction`):
  - [ ] Workers: 8 concurrent, `prefetch_multiplier=4`
  - [ ] Rate limit: per-user (10/min) + global (100/min) via token bucket
  - [ ] Circuit breaker: `pybreaker` on Anthropic client (ADR-008)
  - [ ] Timeout: 30s per call; 3 retries exp backoff + jitter
  - [ ] Cache: Redis `cache_key = (chat_id, message_id, versao_prompt)` TTL 30d
  - [ ] OCR → Haiku → (se conferência falha) → Sonnet
  - [ ] Output: JSON → `materialization` queue
- [ ] **Materialization Pool** (Celery queue: `materialization`):
  - [ ] Workers: 16 concurrent, `prefetch_multiplier=2`
  - [ ] 1 transaction per bet (ADR-007); `FOR UPDATE ORDER BY id`
  - [ ] Idempotent upsert: `ON CONFLICT (usuario_id, chat_id, message_id, ordem) DO UPDATE WHERE excluded.atualizada_em > table.atualizada_em`
  - [ ] Emits `ApostaCriada` event → triggers projection refresh
- [ ] **Idempotency Keys** (natural, per ADR-007):
  - [ ] Export: `UNIQUE (usuario_id, chat_id, message_id, ordem_na_mensagem)`
  - [ ] Print: `UNIQUE (usuario_id, midia_hash, ordem)` partial
  - [ ] Manual: `UNIQUE (usuario_id, chave)` (UUID)
  - [ ] Planilha: `UNIQUE (usuario_id, linha_hash)`
  - [ ] Coleta casa: `UNIQUE (usuario_id, casa, identidade)` + `hash_conteudo`
- [ ] **Fault Tolerance**:
  - [ ] Transient errors (timeout, 5xx): Celery retry (max 3, exp backoff)
  - [ ] Conference grave: **Direct to `revisao_pendente`** (no retry); user resolves with photo
  - [ ] Permanent errors (bad image): Log + alert; no retry
  - [ ] Celery DLQ: `task_reject_on_worker_lost=True` + `dead_letter_queue` Redis → ops alert
  - [ ] `revisao_pendente` reasons: `baixa_confianca`, `odd_invalida`, `conferencia_grave`, `campo_faltando`
- [ ] **Metrics** (Prometheus, extends ADR-008):
  - [ ] `batch_job_duration_seconds{stage=extraction|materialization|projection, status=success|failed}` (Histogram)
  - [ ] `batch_throughput_bets_per_second` (Gauge)
  - [ ] `extraction_cache_hit_rate` (Gauge)
  - [ ] `extraction_cost_usd_per_bet` (Gauge)
  - [ ] `batch_failed_total{stage, reason}` (Counter)
  - [ ] `revisao_pendente_created_total{reason}` (Counter)
  - [ ] `celery_queue_depth{queue}` (Gauge)
- [ ] **Triggers**:
  - [ ] **Upload export**: `POST /upload` → S3 → `ExportUploaded` event → `extraction` queue
  - [ ] **Coleta casa**: `POST /coleta` → validate → `materialization` queue (bypasses extraction)
  - [ ] **Reprocess manual**: CLI `celery -A tasks call reprocessar_usuario --args='[usuario_id]'`
  - [ ] **Releitura geral (prompt vN)**: Admin UI → enqueue chunks of 1000
- [ ] **Projection Refresh**: Materialized views refreshed via `pg_cron` every 5min (ADR-003); `CONCURRENTLY`
- [ ] **Monitoring**: CloudWatch alarms on `batch_job_duration_p99 > 300s`, `extraction_cache_hit_rate < 0.5`, `revisao_pendente_created > 100/hr`

## Implementation Patterns

### Celery App Setup
```python
# tasks/celery_app.py
from celery import Celery
from kombu import Queue

app = Celery("bancaemdia")
app.conf.update(
    broker_url=settings.REDIS_URL,
    result_backend=settings.REDIS_URL,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=4,
    task_routes={
        "tasks.extraction.*": {"queue": "extraction"},
        "tasks.materialization.*": {"queue": "materialization"},
    },
    task_queues=(
        Queue("extraction", routing_key="extraction"),
        Queue("materialization", routing_key="materialization"),
        Queue("dead_letter", routing_key="dead_letter"),
    ),
)
```

### Extraction Task
```python
# tasks/extraction.py
from celery import shared_task
from tenacity import retry, stop_after_attempt, wait_exponential_jitter


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue="extraction")
@retry(
    wait=wait_exponential_jitter(initial=1, max=4),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
    reraise=True,
)
def extract_bilhete(
    self,
    usuario_id: int,
    chat_id: int,
    message_id: int,
    versao_prompt: str,
    foto_bytes: bytes,
    midia_hash: str,
):
    cache_key = f"ext:{chat_id}:{message_id}:{versao_prompt}"

    # Cache hit
    cached = redis.get(cache_key)
    if cached:
        EXTRACTION_CACHE_HIT_RATE.inc()
        return json.loads(cached)

    # Rate limit per user + global
    await rate_limiter.acquire(f"anthropic:{usuario_id}", limit=10, window=60)
    await rate_limiter.acquire("anthropic:global", limit=100, window=60)

    # Extract with circuit breaker
    resultado = await chamar_anthropic_com_fallback(foto_bytes, versao_prompt)

    # Validate conferences
    try:
        conferencias.validar(resultado)
    except ConferenceGrave as e:
        # Business DLQ: revisao_pendente
        await repositorio.criar_revisao_pendente(
            usuario_id=usuario_id,
            midia_hash=midia_hash,
            motivo=f"conferencia_grave: {e}",
            extracao_bruta=resultado,
        )
        REVISAO_PENDENTE_CREATED.labels(reason="conferencia_grave").inc()
        return {"status": "revisao_pendente"}

    # Cache & forward
    redis.setex(cache_key, 30 * 86400, json.dumps(resultado))
    EXTRACTION_CACHE_HIT_RATE.dec()  # miss

    # Chain to materialization
    materializar_aposta.delay(usuario_id, resultado, midia_hash)

    return {"status": "extracted"}


# Chain: extraction → materialization
from celery import chain


def processar_mensagem(usuario_id, chat_id, message_id, versao_prompt, foto_bytes, midia_hash):
    chain(
        extract_bilhete.s(usuario_id, chat_id, message_id, versao_prompt, foto_bytes, midia_hash),
        materializar_aposta.s(),
    ).apply_async()
```

### Materialization Task
```python
# tasks/materialization.py
@shared_task(bind=True, max_retries=3, default_retry_delay=30, queue="materialization")
async def materializar_aposta(self, usuario_id: int, extracao_json: dict, midia_hash: str):
    async with async_session() as session:
        async with session.begin():  # 1 tx per bet (ADR-007)
            await repositorio.upsert_aposta_idempotente(
                session, usuario_id, extracao_json, midia_hash
            )
            # Emite evento para projection trigger
            await event_bus.publish(
                "ApostaCriada", {"usuario_id": usuario_id, "aposta_chave": extracao_json["chave"]}
            )
```

### Idempotent Upsert
```python
# repositorio.py
async def upsert_aposta_idempotente(session, usuario_id, extracao, midia_hash):
    stmt = (
        pg_insert(Aposta)
        .values(
            usuario_id=usuario_id,
            chat_id=extracao["chat_id"],
            message_id=extracao["message_id"],
            ordem_na_mensagem=extracao["ordem"],
            midia_hash=midia_hash,
            # ... all fields from extracao
            atualizada_em=datetime.utcnow(),
        )
        .on_conflict_do_update(
            index_elements=["usuario_id", "chat_id", "message_id", "ordem_na_mensagem"],
            set_={k: v for k, v in extracao.items() if k not in IMMUTABLE_FIELDS},
            where=pg_insert(Aposta).excluded.atualizada_em > Aposta.atualizada_em,
        )
        .returning(Aposta.id)
    )
    return (await session.execute(stmt)).scalar_one()
```

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** Extraction queue depth > 1000 sustained, cache hit rate < 50%, per-tenant isolation needed, exactly-once required for audit
- **Related ADRs:** ADR-001 (QARs), ADR-002 (Data Model), ADR-003 (Storage), ADR-007 (Transactions), ADR-008 (Failure Modes), ADR-009 (Consistency), ADR-011 (Stream Processing)
- **Open Items:** Exact Celery worker autoscaling rules; Redis cluster vs single; chunk size for releitura geral; priority queues for paid users

## References

- DDIA Ch 10: Batch Processing (MapReduce, exactly-once, partitioning, fault tolerance)
- Celery docs: https://docs.celeryq.dev/
- `scripts/planilhar.py` — current synchronous pipeline
- `planilhador/extracao/rodada.py` — extraction orchestration
- `planilhador/dominio/materializar.py` — current materialization
- `planilhador/dominio/projecao.py` — projection/replay
- `scripts/conferir_numeros.py` — integrity verification