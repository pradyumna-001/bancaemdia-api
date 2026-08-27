# ADR 013: 4-Week Backend Scope — Decoupled API, New Repository

## Status
Proposed

## Context

**Goal**: Build a production-ready backend in **4 weeks** (single dev, AI-assisted) in a **new repository**, completely decoupled from the current frontend (`planilhador/web/`). The backend must support the SaaS architecture defined in ADRs 001-012.

**Constraints from ADRs**:
- Financial correctness > availability (ADR-001, 007, 009)
- Zero data loss (RPO=0) via append-only log (ADR-007, 009)
- PostgreSQL + SQLAlchemy 2.x async + RLS (ADR-002, 003)
- Dual-pool async pipeline: Extraction + Materialization (ADR-010)
- Linearizable writes on Primary, eventual reads on Replica (ADR-005, 009)
- Circuit breakers, OTel, rate limiting (ADR-008)
- 5 bet origins, temporal data, 14 conferences, freebet logic (AGENTS.md)

**Non-goals for 4 weeks**:
- Frontend (site/extension/bot) — separate repos
- Billing/Stripe integration (M4)
- Chrome Web Store publishing (M3)
- Telegram bot (M4)
- Load testing (M4)
- Multi-region / sharding (M5)
- Cross-tenant analytics (M5)

---

## 4-Week Plan

### Week 1: Foundation — PostgreSQL + SQLAlchemy + RLS + CI/CD
**Target**: Schema migrated, models working, tenant isolation enforced, pipeline green.

| Day | Deliverable | Validation |
|-----|-------------|------------|
| 1-2 | New repo: `bancaemdia-api` (FastAPI, Poetry/pip-tools, Ruff, MyPy, Pytest) | `ruff check . && mypy . && pytest -n 8` passes |
| 2-3 | AWS RDS PostgreSQL 16 (dev: Single-AZ `db.t3.medium`; prod: Multi-AZ) + Secrets Manager for `DATABASE_URL` | `alembic upgrade head` creates all 28 tables + partitions |
| 3-4 | SQLAlchemy 2.x async models for all 28 tables (Appendix A ADR-002) | ORM round-trip: insert → select → update → delete works |
| 4-5 | **RLS policies** on all per-user tables: `current_setting('app.current_user_id')::int` | `tests/test_rls.py`: User A cannot read User B data |
| 5 | Alembic baseline at migration 028 + `pg_partman` for monthly partitions on `eventos`, `movimentos`, `mensagem_versoes`, `coletas_casa` | `conferir_numeros.py` passes on migrated schema |
| 5-7 | GitHub Actions: `test → build → staging-deploy` (ECS Fargate spot, 1 task) | Push to main → staging healthy in < 5 min |

**Key files to create**:
```
bancaemdia-api/
├── pyproject.toml              # Poetry, deps, tools config
├── alembic.ini
├── alembic/
│   ├── env.py                  # async, RDS URL from env
│   ├── script.py.mako
│   └── versions/               # 001_baseline_028.py (auto-generated from current 28)
├── src/bancaemdia/
│   ├── config.py               # Pydantic Settings (all env-driven)
│   ├── database.py             # async_engine, async_session, RLS middleware
│   ├── models/                 # SQLAlchemy models (one per table)
│   │   ├── __init__.py
│   │   ├── usuario.py
│   │   ├── aposta.py
│   │   ├── evento.py
│   │   ├── movimento.py
│   │   ├── coleta_casa.py
│   │   ├── canonical/          # casas, mercados, times, tipsters...
│   │   └── ...
│   ├── repositories/           # Data access (no business logic)
│   │   ├── aposta_repo.py
│   │   ├── evento_repo.py
│   │   ├── movimento_repo.py
│   │   └── ...
│   ├── domain/                 # Pure business logic (no DB, no HTTP)
│   │   ├── financeiro.py       # lucro = retorno - stake, freebet rules
│   │   ├── temporal.py         # unidades vigente_de/ate, contas_casa desde/ate
│   │   ├── conferencias.py     # 14 conferences (port from planilhador)
│   │   ├── materializar.py     # idempotent upsert + event emission
│   │   ├── pareador.py         # duvida_de_par, lock ordering
│   │   └── normalizacao.py     # 545→78→8 markets, casas canônicas
│   ├── api/                    # FastAPI routers (thin, delegate to domain)
│   │   ├── v1/
│   │   │   ├── apostas.py
│   │   │   ├── caixa.py
│   │   │   ├── coleta.py
│   │   │   ├── upload.py
│   │   │   ├── revisao.py
│   │   │   └── painel.py       # reads on replica
│   │   └── health.py
│   ├── workers/                # Celery tasks (extraction + materialization)
│   │   ├── celery_app.py
│   │   ├── extraction.py
│   │   └── materialization.py
│   ├── observability/          # OTel, structlog, metrics, circuit breakers
│   │   ├── logging.py
│   │   ├── metrics.py
│   │   ├── tracing.py
│   │   └── circuit_breaker.py
│   └── main.py                 # FastAPI app factory
├── tests/
│   ├── test_rls.py
│   ├── test_idempotency.py
│   ├── test_financeiro.py
│   ├── test_conferencias.py
│   ├── test_materializar.py
│   ├── test_pareador.py
│   └── test_replay.py
└── scripts/
    ├── conferir_numeros.py     # ported, validates 5 banks replay
    └── migrate_sqlite_to_pg.py # pgloader wrapper + validation
```

**Exit criteria Week 1**:
- [ ] `pytest -n 8` passes (all unit + integration tests)
- [ ] RLS verified: cross-tenant query returns 0 rows
- [ ] `conferir_numeros.py` passes on RDS staging
- [ ] CI/CD deploys to staging on push

---

### Week 2: Async Pipeline — Celery + Redis + Dual Pools + Extraction
**Target**: 200 bets processed in < 5 min P95, cache hit rate measured, cost/bet tracked.

| Day | Deliverable | Validation |
|-----|-------------|------------|
| 8-9 | Redis (ElastiCache `cache.t3.micro`) + Celery app with 2 queues: `extraction`, `materialization` | `celery -A workers.celery_app inspect ping` works |
| 9-10 | **Extraction workers**: OCR → Haiku → Sonnet (port from `planilhador/extracao/`) | 10 test images → structured JSON, conferences run |
| 10-11 | **Cache layer**: Redis `cache_key = (chat_id, message_id, versao_prompt)` TTL 30d | Cache hit rate gauge in Prometheus |
| 11-12 | **Rate limiting**: per-user (10/min) + global (100/min) token bucket on Anthropic | Burst test: 200 requests → queued, not rejected |
| 12-13 | **Circuit breaker**: `pybreaker` on Anthropic client (fail_max=5, reset=60s) | Simulated 5xx → breaker opens, metric exposed |
| 13-14 | **Materialization workers**: idempotent upsert, 1 tx/bet, emits `ApostaCriada` | Duplicate re-send → no duplicate rows, `atualizada_em` updated |
| 14 | **Batch metrics**: duration, throughput, cache hit, cost/bet, revisões | Grafana dashboard: 4 panels green |

**Key implementation**:
```python
# workers/extraction.py — chain pattern
@shared_task(bind=True, max_retries=3, queue="extraction")
def extract_bilhete(self, usuario_id, chat_id, message_id, versao_prompt, foto_bytes, midia_hash):
    # 1. Cache check
    # 2. Rate limit (per-user + global)
    # 3. Anthropic call with circuit breaker + tenacity retry
    # 4. Run 14 conferences
    # 5. If grave → create revisao_pendente (business DLQ), return
    # 6. Cache result → chain to materialization queue
    materializar_aposta.delay(usuario_id, resultado, midia_hash)
```

**Exit criteria Week 2**:
- [ ] 200 bets enqueued → processed in < 5 min (P95)
- [ ] Cache hit rate > 50% on replay test
- [ ] Cost/bet metric matches ~$0.0054 (ADR-001)
- [ ] `revisao_pendente` created for known-bad images
- [ ] Celery DLQ captures worker crashes

---

### Week 3: API Surface — REST + Webhooks + Auth + Observability
**Target**: All mutating endpoints linearizable on Primary; reads on Replica; full OTel stack.

| Day | Deliverable | Validation |
|-----|-------------|------------|
| 15-16 | **Auth**: JWT (RS256, JWKS), `usuario_id` in request state, middleware sets RLS session var | `tests/test_auth.py`: invalid token → 401; RLS var set |
| 16-17 | **Router**: Primary for `POST/PUT/PATCH/DELETE`, Replica for `GET /painel, /apostas, /export` | `tests/test_router.py`: writes hit primary, reads hit replica |
| 17-18 | **Read-after-write**: 5s Primary routing after write by same session | User creates bet → immediately visible in `/apostas/{id}` |
| 18-19 | **Endpoints**: `/upload` (export), `/coleta` (casa webhook), `/apostas` CRUD, `/caixa` CRUD, `/revisao` | OpenAPI spec generated; contract tests pass |
| 19-20 | **Observability**: OTel auto-instrument + custom spans, structlog JSON, Prometheus metrics, health/ready | `/health` + `/ready` < 2s; traces in Jaeger/X-Ray |
| 20-21 | **Rate limiting**: `slowapi` by `usuario_id` (`/coleta` 10/min, `/api` 100/min, `/upload` 1/5min) | Load test: 200 req/min per user → 429 after limit |
| 21 | **Alerting**: CloudWatch alarms on P99>1s, error>1%, lag>30s, queue>100, breaker open | Alert fires in < 5 min on injected failure |

**Key API contracts** (decoupled from frontend):
```yaml
# POST /api/v1/upload — Telegram export upload
request: multipart/form-data (file=zip)
response: 202 { job_id, estimated_bets, estimated_cost_usd }
# Webhook: POST /webhook/upload-complete { job_id, status, bets_processed }

# POST /api/v1/coleta — Extensão Chrome envia bilhete da casa
request: { casa, identidade, hash_conteudo, bruto_json, capturado_em }
response: 202 { coleta_id, status: "queued" }
# Idempotent: same (usuario_id, casa, identidade) + same hash → 200 no-op

# GET /api/v1/apostas?desde=&ate=&estado=&casa=&tipster= — List (replica)
# GET /api/v1/apostas/{chave} — Detail (primary if written <5s ago)
# PATCH /api/v1/apostas/{chave} — Edit (primary, FOR UPDATE)
# POST /api/v1/apostas/{chave}/resultado — Settle (primary, FOR UPDATE)

# POST /api/v1/caixa — Depósito/Saque/Transferência (primary)
# GET /api/v1/painel — Dashboard aggregates (replica, MV-backed)
```

**Exit criteria Week 3**:
- [ ] All endpoints return 2xx/4xx/5xx correctly, structured JSON errors
- [ ] `/ready` checks: PG writable, Anthropic HEAD ok, queue depth < 100
- [ ] Distributed trace: upload → extraction → materialization → painel visible
- [ ] Rate limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`) present

---

### Week 4: Hardening — Replay, Idempotency, Edge Cases, Docs
**Target**: Zero silent failures; `conferir_numeros.py` passes; runbooks written; deploy to prod-ready staging.

| Day | Deliverable | Validation |
|-----|-------------|------------|
| 22-23 | **Replay/Reprojeção CLI**: `reprocessar_usuario(usuario_id)`, `reler_todas(versao_prompt)` | Full replay on staging copy → `conferir_numeros.py` ✅ |
| 23-24 | **Idempotency test suite**: duplicate re-send for all 5 origins + coleta casa | `pytest tests/test_idempotency.py -v` all pass |
| 24-25 | **Edge cases**: freebet stake=0, meia-green/red, cashout, anulada, temporal queries | Known fixtures produce exact centavo values |
| 25-26 | **Failure injection**: kill worker mid-tx, PG primary failover, Anthropic 5xx, Redis down | No data loss; DLQ captures; auto-recovery |
| 26-27 | **Runbooks**: deploy, rollback, migration, incident response, scaling | `/docs/runbooks/` — 5 docs, < 2 pages each |
| 27-28 | **Load test (k6)**: 50 users, 200 bets/day, P95<1s API, zero errors | k6 script in repo; CI runs on PR |
| 28 | **Production staging deploy**: Multi-AZ RDS, 2 API tasks, 4 extraction workers, 8 materialization | Staging = prod mirror; `conferir_numeros.py` passes |

**Exit criteria Week 4 (Definition of Done)**:
- [ ] `pytest -n 8` — **all green** (unit + integration + idempotency + replay)
- [ ] `conferir_numeros.py` passes on staging (5 banks, 16k+ bets replay)
- [ ] k6 load test: 50 users concurrent, P95 API < 1s, 0% errors
- [ ] OTel traces show full request flow (upload → painel)
- [ ] Circuit breaker metrics visible in Grafana
- [ ] RLS leak test: automated in CI (`tests/test_rls.py`)
- [ ] Deploy to staging: `git push main` → healthy in < 10 min
- [ ] Rollback tested: bad deploy → rollback < 5 min
- [ ] All runbooks written and linked in `/docs/RUNBOOKS.md`

---

## Repository Structure (New Repo)

```
bancaemdia-api/                    # NEW REPOSITORY
├── .github/workflows/
│   ├── ci.yml                     # test → build → staging
│   └── cd.yml                     # promote staging → prod (manual)
├── pyproject.toml                 # Poetry, Python 3.12+, deps
├── Dockerfile                     # Multi-stage: builder → runtime (non-root)
├── docker-compose.yml             # Local dev: API + Redis + PG (Testcontainers for CI)
├── alembic.ini
├── alembic/
│   └── versions/001_baseline_028.py
├── src/bancaemdia/                # Package (see Week 1 structure)
├── tests/
│   ├── unit/                      # Pure domain logic (fast, no DB)
│   ├── integration/               # With Testcontainers PG + Redis
│   └── contract/                  # API schema validation
├── scripts/
│   ├── conferir_numeros.py
│   ├── migrate_sqlite_to_pg.py
│   └── seed_canonical.py          # casas, mercados, times, esportes
├── docs/
│   ├── ARCHITECTURE.md            # This ADR + diagrams
│   ├── API.md                     # OpenAPI spec + examples
│   ├── RUNBOOKS.md                # Index of runbooks
│   ├── runbooks/
│   │   ├── deploy.md
│   │   ├── rollback.md
│   │   ├── migration.md
│   │   ├── incident.md
│   │   └── scaling.md
│   └── DECISIONS.md               # Link to ADRs 001-012
├── k6/
│   └── load-test.js
└── Makefile                       # Shortcuts: make test, make lint, make migrate, make dev
```

---

## Key Decisions for 4-Week Scope

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Framework** | FastAPI + SQLAlchemy 2.x async | Type-safe, auto-OpenAPI, async native, team knows it |
| **ORM** | SQLAlchemy 2.x (not raw SQL) | Type safety, Alembic migrations, connection pooling, RLS middleware |
| **Multi-tenancy** | Shared schema + RLS | Single migration, single pool, cross-tenant analytics ready, AWS RDS native |
| **Async** | Celery + Redis Streams | Mature, dual-pool support, Flower monitoring, at-least-once + idempotency |
| **Partitions** | Native PG range (monthly) + `pg_partman` | No TimescaleDB dependency, partition pruning, easy maintenance |
| **Observability** | OpenTelemetry (auto + custom) + Prometheus/Grafana | Vendor-neutral, AWS X-Ray exporter available, single stack |
| **Auth** | JWT RS256 + JWKS | Stateless, works across API/workers/extension, standard |
| **Deployment** | ECS Fargate (spot for workers) + ALB | Serverless, auto-scale, AWS native, single dev operable |
| **Config** | Pydantic Settings (env only) | Zero hardcoded, secrets in AWS SM, validation at startup |
| **Testing** | Pytest + Testcontainers (PG/Redis) | Real DB in CI, parallel (`-n 8`), no mocks for integration |

---

## What's Explicitly NOT in 4 Weeks

| Deferred | When |
|----------|------|
| Frontend (site/extension/bot) | Separate repos, parallel tracks |
| Stripe/MP billing + customer portal | M4 (Week 11-12) |
| Telegram bot (canal parceiro) | M4 |
| Chrome Web Store publish | M3 |
| Feature flags admin UI | M3 |
| Load test 200 users / 10k bets/day | M4 (k6 script written Week 4, executed M4) |
| Multi-region / cross-region replica | M5 |
| Hash sharding by `usuario_id` | M5 |
| LGPD compliance (encryption, right to deletion) | M5 |
| Contract tests (Pact: Anthropic, Betano, Sofascore) | M4 |
| Admin UI for flags/revisão/canonical | M3 |

---

## Risk Mitigation (Top 5)

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| SQLite → PG migration corrupts data | Medium | Critical | `pgloader` + staging mirror + `conferir_numeros.py` validation + rollback plan |
| Celery/Redis complexity > 4 weeks | High | High | Spike Day 1-2 Week 2; fallback: RQ (simpler) if Celery blocks > 2 days |
| Anthropic breaking change | Medium | High | Circuit breaker + versioned prompts + Pact tests (M4) |
| RLS leak (User A sees User B) | Low | Critical | `tests/test_rls.py` in CI; mandatory code review checklist |
| Single dev operational burden | High | Medium | Runbooks Week 4; managed services (RDS, ElastiCache, ECS); alerting from Day 1 |

---

## Success Metrics (End of Week 4)

| Metric | Target | Measurement |
|--------|--------|-------------|
| **API P95 latency** | < 500ms | Grafana (Prometheus `http_request_duration_seconds`) |
| **Extraction P95 (200 bets)** | < 5 min | Celery Flower + custom metric |
| **Cache hit rate** | > 50% (replay) | `extraction_cache_hit_rate` gauge |
| **Cost/bet** | ~$0.0054 | `anthropic_cost_usd_total / bets_processed` |
| **Zero silent corruption** | 0 | `conferir_numeros.py` passes; `revisao_pendente` catches all grave |
| **RLS isolation** | 0 cross-tenant leaks | `tests/test_rls.py` in CI |
| **Deploy + rollback** | < 10 min / < 5 min | GitHub Actions timestamps |
| **Test suite** | 100% pass, < 10 min | `pytest -n 8` in CI |

---

## Next Steps

1. **Approve this scope** (this ADR)
2. **Create new repo** `bancaemdia-api` with Week 1 scaffold
3. **Provision AWS dev account**: RDS Single-AZ, ElastiCache, ECS cluster, Secrets Manager
4. **Start Week 1 Day 1**

---

## References

- ADR-001: Quality Attributes & SLAs
- ADR-002: Data Model & Query Strategy
- ADR-003: Storage Engine & Indexing
- ADR-005: Replication Strategy (Primary + Read Replica)
- ADR-007: Transaction Boundaries & Isolation
- ADR-008: Failure Modes, Timeouts, Observability
- ADR-009: Consistency Model
- ADR-010: Batch Pipeline Architecture
- ADR-011: Stream Processing & Async Architecture
- ADR-012: Evolvability & Extensibility
- HIGH_LEVEL_PLAN.md: Milestones M0-M5
- AGENTS.md: Domain rules (append-only, temporal, 14 conferences, 5 origins)
- `scripts/conferir_numeros.py`: Data integrity verification (5 banks replay)
- `planilhador/` — current implementation (reference for porting domain logic)