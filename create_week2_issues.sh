#!/usr/bin/env bash
# Create Week 2 Milestone + 10 Issues in bancaemdia-api repo
# Usage: chmod +x create_week2_issues.sh && ./create_week2_issues.sh

set -euo pipefail

REPO="pradyumna-001/bancaemdia-api"
MILESTONE="Week 2 — Async Pipeline (Celery + Redis + Dual Pools + Extraction)"

echo "📦 Creating milestone: $MILESTONE"
MILESTONE_NUMBER=$(gh api repos/$REPO/milestones \
  -f title="$MILESTONE" \
  -f description="Celery + Redis dual-pool pipeline: extraction (OCR→Haiku→Sonnet) + materialization (idempotent upsert). 200 bets < 5 min P95." \
  -f due_on="$(date -d '+14 days' -u +%Y-%m-%dT%H:%M:%SZ)" \
  --jq '.number')

echo "✅ Milestone created: #$MILESTONE_NUMBER"

create_issue() {
  local title="$1"
  local body="$2"

  echo "📝 Creating issue: $title"
  gh issue create -R "$REPO" \
    -t "$title" \
    -b "$body" \
    -m "$MILESTONE"
}

# Issue 1: Redis + Celery Infrastructure
create_issue \
  "[Week 2] Redis + Celery Infrastructure" \
  "**Labels**: week-2, infra, celery
**Size**: M (3-4 hours)

## Files
- \`docker-compose.yml\` (add Redis service)
- \`src/bancaemdia/workers/celery_app.py\`
- \`src/bancaemdia/workers/__init__.py\`

## Tasks
- [ ] Add Redis to \`docker-compose.yml\` (image: \`redis:7-alpine\`, port 6379)
- [ ] Celery app config:
  - Broker: \`redis://redis:6379/0\`
  - Result backend: \`redis://redis:6379/1\`
  - Task serializer: \`json\`
  - Timezone: \`UTC\`
  - Task routes: \`extraction.*\` → \`extraction\` queue, \`materialization.*\` → \`materialization\` queue
  - Worker prefetch: \`extraction=4\`, \`materialization=2\`
  - \`task_acks_late=True\`, \`task_reject_on_worker_lost=True\`
  - Dead letter queue: \`dead_letter\` queue
- [ ] Flower monitoring (optional, port 5555)
- [ ] Health check task: \`celery -A workers.celery_app inspect ping\`

## Acceptance
\`docker compose up -d redis && celery -A workers.celery_app worker -Q extraction,materialization -l info\` starts without errors"

# Issue 2: Extraction Worker — Core Pipeline
create_issue \
  "[Week 2] Extraction Worker — Core Pipeline (OCR → Haiku → Sonnet)" \
  "**Labels**: week-2, extraction, ai, porting
**Size**: L (6-8 hours)

## Files
- \`src/bancaemdia/workers/extraction.py\`
- \`src/bancaemdia/extracao/escada.py\` (port from \`planilhador/extracao/escada.py\`)
- \`src/bancaemdia/extracao/cliente.py\` (port from \`planilhador/extracao/cliente.py\`)
- \`src/bancaemdia/extracao/rodada.py\` (port from \`planilhador/extracao/rodada.py\`)
- \`src/bancaemdia/extracao/prompts/\` (copy prompt templates)
- \`src/bancaemdia/extracao/modelos.py\` (port Pydantic schemas)

## Tasks
- [ ] Port \`escada.py\`: cache → OCR → Haiku → Sonnet orchestration
- [ ] Port \`cliente.py\`: Anthropic client with:
  - \`pybreaker\` circuit breaker (fail_max=5, reset_timeout=60s, exclude timeouts)
  - \`tenacity\` retry: exponential backoff + jitter, max 3 attempts
  - Centralized timeouts from config
  - Cost tracking per call (tokens × model pricing)
- [ ] Port \`rodada.py\`: single bilhete extraction + 14 conferences validation
- [ ] Port \`modelos.py\`: \`ExtracaoBilhete\`, \`Selecao\`, \`MercadoExtraido\`, etc.
- [ ] Copy prompts from \`planilhador/extracao/prompts/\` (versioned)
- [ ] OCR integration (optional, flag \`--economico\` per AGENTS.md)

## Acceptance
Unit test with mocked Anthropic returns structured \`ExtracaoBilhete\`; conferences run"

# Issue 3: Cache Layer — Redis Extraction Cache
create_issue \
  "[Week 2] Cache Layer — Redis Extraction Cache" \
  "**Labels**: week-2, cache, redis
**Size**: M (2-3 hours)

## Files
- \`src/bancaemdia/cache/extracao_cache.py\`
- \`src/bancaemdia/workers/extraction.py\` (integrate cache)

## Tasks
- [ ] Cache key: \`ext:{chat_id}:{message_id}:{versao_prompt}\` (SHA256 of image + caption)
- [ ] TTL: 30 days (\`30 * 86400\` seconds)
- [ ] Cache hit → return cached result, increment \`extraction_cache_hit\` metric
- [ ] Cache miss → proceed to extraction, store result on success
- [ ] Invalidation: on prompt version change, clear old version keys
- [ ] Metrics: \`extraction_cache_hit_total\`, \`extraction_cache_miss_total\` (Prometheus counters)

## Acceptance
Re-processing same image → cache hit, no Anthropic call, metric increments"

# Issue 4: Rate Limiting — Per-User + Global Anthropic Limits
create_issue \
  "[Week 2] Rate Limiting — Per-User + Global Anthropic Limits" \
  "**Labels**: week-2, rate-limiting, redis
**Size**: M (3-4 hours)

## Files
- \`src/bancaemdia/rate_limit/anthropic_limiter.py\`
- \`src/bancaemdia/workers/extraction.py\` (integrate limiter)

## Tasks
- [ ] Token bucket in Redis:
  - Per-user: \`rl:anthropic:user:{usuario_id}\` — 10 req/min
  - Global: \`rl:anthropic:global\` — 100 req/min
- [ ] \`acquire(user_id, tokens=1)\` — blocks until tokens available (async)
- [ ] Refill rate: \`limit / window\` per second
- [ ] Fair queuing: FIFO per user, global bucket shared
- [ ] Metrics: \`rate_limit_wait_seconds\` (histogram), \`rate_limit_exceeded_total\` (counter)
- [ ] Configurable via \`Settings\`: \`ANTHROPIC_RATE_USER=10\`, \`ANTHROPIC_RATE_GLOBAL=100\`, \`ANTHROPIC_WINDOW=60\`

## Acceptance
Burst test 200 concurrent requests → queued, not rejected; per-user isolation verified"

# Issue 5: Circuit Breaker — Anthropic Client Resilience
create_issue \
  "[Week 2] Circuit Breaker — Anthropic Client Resilience" \
  "**Labels**: week-2, circuit-breaker, resilience
**Size**: S (2-3 hours)

## Files
- \`src/bancaemdia/resilience/circuit_breaker.py\`
- \`src/bancaemdia/extracao/cliente.py\` (integration)

## Tasks
- [ ] \`pybreaker.CircuitBreaker\` for Anthropic:
  - \`fail_max=5\` (5 failures → open)
  - \`reset_timeout=60\` (seconds to half-open)
  - \`exclude=[httpx.TimeoutException]\` (timeouts don't count as failures)
- [ ] Prometheus listener: \`circuit_breaker_state\` gauge (0=closed, 1=open, 2=half-open)
- [ ] Health endpoint exposes breaker state: \`GET /health\` includes \`circuit_breakers: {anthropic: \"closed\"}\`
- [ ] Integration: wrap \`chamar_anthropic()\` call with breaker
- [ ] Fallback: on open breaker, queue task with exponential backoff retry

## Acceptance
Simulate 5xx errors → breaker opens, metric shows \`state=1\`, requests queued"

# Issue 6: Materialization Worker — Idempotent Upsert + Event Emission
create_issue \
  "[Week 2] Materialization Worker — Idempotent Upsert + Event Emission" \
  "**Labels**: week-2, materialization, worker, porting
**Size**: L (5-6 hours)

## Files
- \`src/bancaemdia/workers/materialization.py\`
- \`src/bancaemdia/domain/materializar.py\` (port from \`planilhador/dominio/materializar.py\`)
- \`src/bancaemdia/domain/event_bus.py\` (simple in-process event bus)

## Tasks
- [ ] Celery task: \`materializar_aposta(usuario_id, extracao_json, midia_hash)\`
- [ ] Queue: \`materialization\`, prefetch=2, max_retries=3
- [ ] **Idempotent upsert** (per ADR-007):
  - Natural key: \`UNIQUE (usuario_id, chat_id, message_id, ordem_na_mensagem)\` for export
  - \`ON CONFLICT (...) DO UPDATE SET ... WHERE excluded.atualizada_em > table.atualizada_em\`
  - Handle all 5 origins + coleta casa (different unique constraints)
- [ ] Single transaction per bet (ADR-007): \`async with session.begin():\`
- [ ] Emit \`ApostaCriada\` event → triggers projection refresh (Week 3)
- [ ] Handle \`ConferenciaGrave\` → create \`RevisaoPendente\` (business DLQ), don't retry
- [ ] Metrics: \`materialization_duration_seconds\`, \`materialization_failed_total{reason}\`

## Acceptance
Duplicate re-send → no duplicate rows, \`atualizada_em\` updated; grave conference → \`revisao_pendente\` created"

# Issue 7: Batch Metrics — Prometheus + Grafana Dashboards
create_issue \
  "[Week 2] Batch Metrics — Prometheus + Grafana Dashboards" \
  "**Labels**: week-2, metrics, observability
**Size**: M (3-4 hours)

## Files
- \`src/bancaemdia/observability/metrics.py\`
- \`grafana/dashboards/batch-pipeline.json\` (provisioned)

## Metrics to implement
- [ ] \`batch_job_duration_seconds{stage=\"extraction|materialization|projection\", status=\"success|failed\"}\` (Histogram)
- [ ] \`batch_throughput_bets_per_second\` (Gauge)
- [ ] \`extraction_cache_hit_rate\` (Gauge)
- [ ] \`extraction_cost_usd_per_bet\` (Gauge)
- [ ] \`batch_failed_total{stage, reason}\` (Counter)
- [ ] \`revisao_pendente_created_total{reason}\` (Counter)
- [ ] \`celery_queue_depth{queue=\"extraction|materialization|dead_letter\"}\` (Gauge)
- [ ] \`anthropic_cost_usd_total{usuario_id}\` (Counter)
- [ ] \`anthropic_request_duration_seconds{model, status}\` (Histogram)

## Grafana Dashboard (provisioned via ConfigMap or file)
- [ ] Panel 1: Extraction throughput (bets/min)
- [ ] Panel 2: Cache hit rate (%)
- [ ] Panel 3: Cost per bet (USD)
- [ ] Panel 4: Queue depths
- [ ] Panel 5: Revisão pendente by reason
- [ ] Panel 6: P95 latency per stage

## Acceptance
Metrics exposed at \`/metrics\`; dashboard shows live data during batch run"

# Issue 8: Coleta Casa Integration — Bypass Extraction, Direct to Materialization
create_issue \
  "[Week 2] Coleta Casa Integration — Bypass Extraction, Direct to Materialization" \
  "**Labels**: week-2, coleta-casa, webhook
**Size**: M (3-4 hours)

## Files
- \`src/bancaemdia/api/v1/coleta.py\` (FastAPI endpoint)
- \`src/bancaemdia/workers/materialization.py\` (handle coleta payload)
- \`src/bancaemdia/domain/coleta_casa.py\` (dedup + validation logic)

## Tasks
- [ ] \`POST /api/v1/coleta\` endpoint:
  - Auth: \`ColetaToken\` validation (HMAC)
  - Payload: \`{casa, identidade, hash_conteudo, bruto_json, capturado_em}\`
  - Idempotent: \`UNIQUE (usuario_id, casa_id, identidade)\` + \`hash_conteudo\` for \`open→settled\`
  - Response: \`202 {coleta_id, status: \"queued\"}\`
- [ ] Direct enqueue to \`materialization\` queue (bypasses extraction)
- [ ] Dedup logic: same \`identidade\` + same \`hash_conteudo\` → 200 no-op; different hash → update
- [ ] Validation: \`casa\` must be in \`casas\` table; \`bruto_json\` schema check
- [ ] Rate limit: 10/min per token (extension token, not user JWT)
- [ ] Metrics: \`coleta_received_total{casa, status}\`, \`coleta_dedup_total\`

## Acceptance
Extension sends bilhete → appears in painel within 30s; duplicate → no-op"

# Issue 9: Reprocess CLI — Manual Reprocessing & Releitura Geral
create_issue \
  "[Week 2] Reprocess CLI — Manual Reprocessing & Releitura Geral" \
  "**Labels**: week-2, cli, reprocessing
**Size**: M (2-3 hours)

## Files
- \`src/bancaemdia/cli/reprocess.py\`
- \`scripts/reprocessar_usuario.py\` (entry point)

## Tasks
- [ ] \`reprocessar_usuario(usuario_id, desde=None, ate=None, versao_prompt=None)\`:
  - Query \`eventos\` + \`apostas\` for user in date range
  - Re-enqueue to \`extraction\` queue with new \`versao_prompt\` (or current)
  - Chunk size: 100 bets per task (avoid memory)
- [ ] \`reler_todas(versao_prompt)\` — admin only, full releitura
  - Pagination: \`LIMIT 1000 OFFSET ...\`
  - Progress logging every 100
- [ ] \`reprocessar_coleta(coleta_id)\` — re-process single coleta casa
- [ ] Dry-run mode: \`--dry-run\` logs what would be enqueued
- [ ] Cost estimation before run: \`estimated_bets * cost_per_bet\`

## Acceptance
CLI runs, enqueues correct tasks, cost estimate shown"

# Issue 10: Integration Test — Full Pipeline
create_issue \
  "[Week 2] Integration Test — Full Pipeline (Upload → Extraction → Materialization → Painel)" \
  "**Labels**: week-2, testing, integration
**Size**: L (4-6 hours)

## Files
- \`tests/integration/test_full_pipeline.py\`
- \`tests/fixtures/\` (sample exports, images, coleta payloads)

## Tasks
- [ ] Test fixture: Telegram export ZIP with 10 messages (mixed: photos, text, edits)
- [ ] Test fixture: 5 coleta casa payloads (Betano, bet365, Betfair, Sportingbet, Novibet)
- [ ] Integration test flow:
  1. \`POST /upload\` → job_id
  2. Poll \`/webhook/upload-complete\` or wait for Celery
  3. Verify \`apostas\` created in DB (correct count, fields)
  4. Verify \`eventos\` appended (append-only)
  5. Verify \`revisao_pendente\` for known-bad images
  6. \`POST /coleta\` → verify materialization queue
  7. \`GET /painel\` → verify aggregates match \`conferir_numeros.py\` logic
- [ ] Performance assertion: 200 bets < 5 min (P95)
- [ ] Cache assertion: re-run same export → cache hit rate > 80%
- [ ] Cost assertion: total cost ≈ \`bets * \$0.0054\`

## Acceptance
\`pytest tests/integration/test_full_pipeline.py -v\` passes; all assertions met"

echo ""
echo "🎉 All 10 issues + 1 milestone created!"
echo "🔗 View at: https://github.com/$REPO/issues"
echo "📋 Milestone: https://github.com/$REPO/milestone/$MILESTONE_NUMBER"