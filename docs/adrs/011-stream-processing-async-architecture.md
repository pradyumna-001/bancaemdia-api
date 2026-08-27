# ADR 011: Stream Processing & Async Architecture — Redis Streams, 202 Polling, Celery Unified

## Status
Proposed

## Context

Current state: **Synchronous FastAPI + BackgroundTasks**. Key async flows missing:
- **Coleta casa**: `POST /coleta` blocks until materialization done
- **Extração IA**: Runs in `planilhar.py` synchronously (ADR-010 moves to Celery)
- **Background jobs**: Email, webhooks, cleanup — no queue
- **Projection refresh**: Cron only (ADR-003)

**Existing Event Sourcing**: `eventos` append-only log = source of truth. `projecao.reconstruir()` = deterministic replay.

**Requirements (ADR-001):**
- Extraction latency P95 < 5min for 200-bet batch
- Coleta casa response < 100ms (user clicks "enviar" in extension)
- Financial correctness > availability
- Zero data loss

## Decision

We adopt **Redis Streams** as message broker (already provisioned for rate limiting/cache), **202 Accepted + Polling** for extension coleta, **Pydantic event schemas** with implicit versioning, **Cron 5min + Read-After-Write Primary** for projections, **Celery unified** for batch + background + stream consumers, **at-least-once + idempotency** semantics, and **dual DLQ** (technical + business).

## Consequences

### Positive
- **Zero new infrastructure**: Redis Streams on existing ElastiCache
- **Extension compatible**: 202 + polling works in browser (no public endpoint needed)
- **Unified worker stack**: One Celery deployment, one broker, one monitoring (Flower)
- **Cost efficiency**: Shared pool + rate limits (ADR-010); Redis Streams included
- **Resilience**: At-least-once + natural key idempotency = safe retries
- **Observability**: Stream lag metrics, consumer group lag, DLQ alerts

### Negative
- **Redis Streams limitations**: No native DLQ (custom), limited replay (retention), AOF persistence not as strong as Kafka
- **Polling overhead**: Extension polls every 2-30s (acceptable for <5min processing)
- **Cron projection stale**: ≤5min for "other users' data" (mitigated: own writes via Primary routing)
- **Celery complexity**: More config than RQ/BackgroundTasks; but already needed for batch

### Neutral
- **Schema Registry**: Deferred; Pydantic models sufficient for single-language stack
- **Event-driven projection refresh**: Deferred; Cron 5min + read-after-write adequate
- **Exactly-once**: Not implemented; idempotency keys sufficient
- **Kafka/RabbitMQ**: Evaluated when throughput > 100k msg/s or multi-language consumers

## Options Considered

### Option A: FastAPI BackgroundTasks Only — *Rejected*
**Pros:** Zero new deps
**Cons:** No retry, no persistence, no scaling, blocks worker, no visibility

### Option B: RabbitMQ — *Rejected*
**Pros:** Robust queues, DLQ native, priority queues
**Cons:** New managed service ($ + ops); Redis already does the job for MVP scale

### Option C: Kafka (MSK) — *Rejected*
**Pros:** Infinite scale, replay, durability
**Cons:** Overkill ($100+/month + ops); MVP = 10k msgs/day

### Option D: Redis Streams + Celery Unified — *Chosen*
**Pros:** Zero new infra; handles MVP scale; unified stack
**Cons:** Weaker durability; custom DLQ; limited replay

### Option E: 202 + Webhook Callback — *Rejected*
**Pros:** Push = lower latency
**Cons:** Extension runs in browser (no public endpoint); polling is only viable option

## Compliance

- [ ] **Message Broker**: Redis Streams on existing ElastiCache
  - [ ] Streams: `extraction`, `materialization`, `background`, `dead_letter`
  - [ ] Consumer groups: `extraction-workers`, `materialization-workers`, `background-workers`
  - [ ] Retention: `MAXLEN ~ 100000` per stream (~10 days at peak)
  - [ ] Persistence: AOF everysec + RDB daily
- [ ] **Coleta Casa Endpoint**:
  - [ ] `POST /coleta` → validates token + schema (<50ms) → publishes `ColetaRecebida` → returns `202 {job_id}`
  - [ ] `GET /coleta/status/{job_id}` → returns `{status, resultado?, erro?}`
  - [ ] Extension polls: 2s → 4s → 8s → 16s → 30s (max) → timeout 5min
  - [ ] Status TTL: 1 hour in Redis
- [ ] **Event Schemas** (`events/schemas.py`):
  - [ ] Base: `event_id` (ULID), `event_type`, `occurred_at`, `version=1`
  - [ ] `ExportUploaded`, `ColetaRecebida`, `ExtracaoConcluida`, `ApostaCriada`, `RevisaoPendenteCriada`, `BatchJobConcluido`
  - [ ] Evolution: optional fields only; breaking = new `event_type` v2
  - [ ] Validation: Pydantic at producer + consumer
- [ ] **Projection Refresh**:
  - [ ] Cron 5min via `pg_cron` (ADR-003): `REFRESH MATERIALIZED VIEW CONCURRENTLY ...`
  - [ ] Read-after-write: Primary routing for 5s post-write (ADR-005)
  - [ ] Dashboard badge: `now() - pg_last_xact_replay_timestamp()` as "atualizado há Xs"
- [ ] **Celery Unified** (extends ADR-010):
  - [ ] Queues: `extraction`, `materialization`, `background`, `dead_letter`
  - [ ] Workers: 8 extraction, 16 materialization, 4 background
  - [ ] Beat: `limpar_sessoes` (daily 3AM), `verificar_tetos` (5min)
  - [ ] Monitoring: Flower on `:5555` (auth protected)
- [ ] **Stream Semantics**:
  - [ ] At-least-once: `task_acks_late=True`, `task_reject_on_worker_lost=True`
  - [ ] Idempotency: Natural keys per ADR-007
    - `ColetaRecebida`: `(usuario_id, casa, identidade)` + `hash_conteudo`
    - `ExtracaoConcluida`: `(chat_id, message_id, versao_prompt)`
    - `ApostaCriada`: MV refresh = full recalc (idempotent)
- [ ] **Dual DLQ**:
  - [ ] **Technical DLQ** (`dead_letter` stream): Worker crash, poison pill, OOM
    - Alert: PagerDuty on `celery_dlq_depth > 0`
    - Replay: CLI `replay_dlq --queue=dead_letter --limit=100`
  - [ ] **Business DLQ** (`revisao_pendente` table): Conference grave, odd inválida
    - User resolves in UI → auto-reprocess
    - Metric: `revisao_pendente_created_total{reason}`
    - Alert: Dashboard badge "X pendências"
- [ ] **Monitoring** (extends ADR-008):
  - [ ] `redis_stream_lag{stream,group}` (Gauge) — consumer group lag
  - [ ] `stream_consumer_duration_seconds{stream}` (Histogram)
  - [ ] `coleta_polling_latency_seconds` (Histogram)
  - [ ] `celery_dlq_depth` (Gauge)
  - [ ] `projection_refresh_duration_seconds` (Histogram)

## Implementation Patterns

### Redis Streams Producer
```python
# events/publisher.py
import ulid
from datetime import datetime


async def publish_event(redis, stream: str, event: BaseModel):
    await redis.xadd(
        stream,
        {
            "event_id": str(ulid.new()),
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "version": str(event.version),
            "payload": event.model_dump_json(),
        },
        maxlen=100000,
        approximate=True,
    )


# Usage in endpoints
@router.post("/coleta", status_code=202)
async def receber_coleta(request: Request, payload: ColetaPayload):
    # Quick validation
    job_id = str(ulid.new())

    event = ColetaRecebida(
        event_type="ColetaRecebida",
        job_id=job_id,
        usuario_id=request.state.usuario_id,
        casa=payload.casa,
        identidade=payload.identidade,
        hash_conteudo=payload.hash_conteudo,
        bruto_json=payload.bruto_json,
    )

    await publish_event(redis, "stream:materialization", event)

    # Initial status
    await redis.hset(
        f"coleta:status:{job_id}",
        mapping={
            "status": "queued",
            "usuario_id": str(request.state.usuario_id),
            "created_at": datetime.utcnow().isoformat(),
        },
    )
    await redis.expire(f"coleta:status:{job_id}", 3600)

    return {"job_id": job_id, "status": "queued"}
```

### Redis Streams Consumer (Celery Task)
```python
# tasks/stream_consumers.py
from celery import shared_task


@shared_task(bind=True, max_retries=3, default_retry_delay=30, queue="materialization")
async def consume_coleta_recebida(self, event_data: dict):
    event = ColetaRecebida.model_validate(event_data)

    # Idempotency check (natural key)
    exists = await repositorio.coleta_existe(
        event.usuario_id, event.casa, event.identidade, event.hash_conteudo
    )
    if exists:
        return {"status": "duplicate", "action": "skipped"}

    # Process (same as current synchronous /coleta logic)
    await processar_coleta_casa(event)

    # Update status for polling
    await redis.hset(
        f"coleta:status:{event.job_id}",
        mapping={"status": "completed", "completed_at": datetime.utcnow().isoformat()},
    )

    return {"status": "processed"}
```

### Celery Beat Schedule
```python
# tasks/celery_app.py
from celery.schedules import crontab

app.conf.beat_schedule = {
    "limpar-sessoes-expiradas": {
        "task": "tasks.background.limpar_sessoes_expiradas",
        "schedule": crontab(hour=3, minute=0),
    },
    "verificar-tetos-anthropic": {
        "task": "tasks.background.verificar_tetos_anthropic",
        "schedule": 300.0,  # 5min
    },
    "metricas-stream-lag": {
        "task": "tasks.monitoring.coletar_stream_lag",
        "schedule": 60.0,  # 1min
    },
}
```

### DLQ Replay CLI
```python
# cli/dlq.py
@cli.command()
def replay_dlq(queue: str = "dead_letter", limit: int = 100, dry_run: bool = False):
    """Reprocessa mensagens da DLQ técnica."""
    stream_key = f"stream:{queue}"
    
    messages = await redis.xrange(stream_key, count=limit)
    for msg_id, data in messages:
        event = json.loads(data["payload"])
        original_queue = event.get("original_queue", "materialization")
        
        if not dry_run:
            # Re-publish to original stream
            await redis.xadd(f"stream:{original_queue}", data)
            # Remove from DLQ
            await redis.xdel(stream_key, msg_id)
            click.echo(f"Replayed {msg_id} → {original_queue}")
        else:
            click.echo(f"[DRY RUN] Would replay {msg_id} → {original_queue}")
```

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** Stream throughput > 100k msg/day, projection stale complaints, multi-language consumers, exactly-once audit requirement
- **Related ADRs:** ADR-001 (QARs), ADR-003 (Storage), ADR-004 (Schema), ADR-005 (Replication), ADR-008 (Failure Modes), ADR-009 (Consistency), ADR-010 (Batch), ADR-012 (Evolvability)
- **Open Items:** Redis Streams vs Kafka benchmark at scale; priority queue for paid tiers; event-driven projection refresh for "minhas apostas"

## References

- DDIA Ch 11: Stream Processing (event streaming, windowing, joins, exactly-once)
- Redis Streams docs: https://redis.io/docs/data-types/streams/
- Celery docs: https://docs.celeryq.dev/
- `planilhador/dominio/coleta.py` — current synchronous coleta logic
- `planilhador/dominio/projecao.py` — projection/replay logic
- `extensao/envio.js` — extension polling implementation