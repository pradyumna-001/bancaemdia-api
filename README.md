# Bancaemdia API

Backend API for the Bet Spreadsheet SaaS — transforms betting slips (photos, Telegram exports, direct house collection) into auditable financial spreadsheets with ROI, profit, and balance tracking per house/tipster/market.

**Stack**: FastAPI + Celery + PostgreSQL (RDS) + Redis (ElastiCache) on AWS ECS Fargate

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        AWS VPC                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐                  │
│  │   ALB    │  │  ALB     │  │  CloudFront  │                  │
│  │  (API)   │  │ (Static) │  │  (Frontend)  │                  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘                  │
│       │             │               │                           │
│  ┌────▼─────────────▼───────────────▼────────┐                 │
│  │           ECS Fargate Services            │                 │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐     │                 │
│  │  │  API    │ │Extract  │ │ Mater.  │ ... │                 │
│  │  │ Service │ │ Workers │ │ Workers │     │                 │
│  │  └────┬────┘ └────┬────┘ └────┬────┘     │                 │
│  └───────│───────────│───────────│──────────┘                 │
│          │           │           │                            │
│  ┌───────▼───────────▼───────────▼────────┐                  │
│  │              DATA LAYER                 │                  │
│  │  ┌────────────┐ ┌──────────┐ ┌───────┐ │                  │
│  │  │ RDS PG 16  │ │ ElastiCache│ │  S3   │ │                  │
│  │  │ Primary +  │ │  Redis 7 │ │(Files)│ │                  │
│  │  │  Replica   │ │ (Streams)│ └───────┘ │                  │
│  │  └────────────┘ └──────────┘           │                  │
│  └────────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4-Week Implementation Plan

| Week | Milestone | Focus |
|------|-----------|-------|
| **1** | [Foundation](https://github.com/pradyumna-001/bancaemdia-api/milestone/1) | PostgreSQL + SQLAlchemy 2.x + RLS + CI/CD (local Docker) |
| **2** | [Async Pipeline](https://github.com/pradyumna-001/bancaemdia-api/milestone/2) | Celery + Redis dual-pool: extraction (OCR→Haiku→Sonnet) + materialization |
| **3** | [API Surface](https://github.com/pradyumna-001/bancaemdia-api/milestone/3) | JWT auth, Primary/Replica router, REST endpoints, OTel observability, rate limiting |
| **4** | [Hardening & Deploy](https://github.com/pradyumna-001/bancaemdia-api/milestone/4) | Replay CLI, idempotency tests, chaos testing, runbooks, k6, Terraform, CD pipeline |

**Total**: 4 milestones, **44 issues** — [View all](https://github.com/pradyumna-001/bancaemdia-api/issues)

---

## Architecture Decision Records (ADRs)

All architectural decisions are documented in [`docs/adrs/`](docs/adrs/).

### Core ADRs (001–012)

| ADR | Title | Description |
|-----|-------|-------------|
| [001](docs/adrs/001-quality-attributes-slas.md) | Quality Attributes & SLAs | RPO=0, RTO<1h, P95<500ms, cost/bet tracking |
| [002](docs/adrs/002-data-model-query-strategy.md) | Data Model & Query Strategy | Relational PG, SQLAlchemy async, RLS multi-tenant |
| [003](docs/adrs/003-storage-engine-indexing.md) | Storage Engine & Indexing | PG native, range partitioning, async read replica |
| [004](docs/adrs/004-schema-evolution-serialization.md) | Schema Evolution & Serialization | JSON + implicit versioning, `/v1/` API, Pydantic |
| [005](docs/adrs/005-replication-strategy.md) | Replication Strategy | Single-leader, async replica, read-after-write 5s |
| [006](docs/adrs/006-partitioning-sharding.md) | Partitioning & Sharding | Range time (PG native), future hash sharding by `usuario_id` |
| [007](docs/adrs/007-transaction-boundaries-isolation.md) | Transaction Boundaries & Isolation | READ COMMITTED + `FOR UPDATE`, per-business-op transactions |
| [008](docs/adrs/008-failure-modes-timeouts-observability.md) | Failure Modes, Timeouts, Observability | Circuit breakers, centralized timeouts, OpenTelemetry |
| [009](docs/adrs/009-consistency-model.md) | Consistency Model | Linearizable writes (Primary), eventual reads (Replica ≤30s) |
| [010](docs/adrs/010-batch-pipeline-architecture.md) | Batch Pipeline Architecture | Dual pool (extraction + materialization), at-least-once + idempotency |
| [011](docs/adrs/011-stream-processing-async-architecture.md) | Stream Processing & Async Architecture | Redis Streams, 202 polling, unified Celery, dual DLQ |
| [012](docs/adrs/012-evolvability-extensibility.md) | Evolvability & Extensibility | Feature flags, expand-only migrations, blue-green, contract tests |

### Planning ADRs (013–017)

| ADR | Title | Description |
|-----|-------|-------------|
| [013](docs/adrs/013-4-week-backend-scope.md) | 4-Week Backend Scope | Detailed 4-week implementation scope |
| [014](docs/adrs/014-week1-milestones-issues.md) | Week 1 Milestones & Issues | 13 issues for Foundation |
| [015](docs/adrs/015-week2-milestones-issues.md) | Week 2 Milestones & Issues | 10 issues for Async Pipeline |
| [016](docs/adrs/016-week3-milestones-issues.md) | Week 3 Milestones & Issues | 11 issues for API Surface |
| [017](docs/adrs/017-week4-milestones-issues.md) | Week 4 Milestones & Issues | 10 issues for Hardening & Deploy |

### Reference Documents

- [HIGH_LEVEL_PLAN.md](docs/adrs/HIGH_LEVEL_PLAN.md) — 16-week roadmap (M0–M5)
- [TECHNICAL_REVIEW.md](docs/adrs/TECHNICAL_REVIEW.md) — Technical review of current codebase

---

## Quick Start (Local Development)

### Prerequisites
- Docker + Docker Compose
- Python 3.12+ (for local development without Docker)
- `gh` CLI (for GitHub Actions)

### Start Local Environment
```bash
git clone https://github.com/pradyumna-001/bancaemdia-api
cd bancaemdia-api

# Copy environment template
cp .env.example .env
# Edit .env with your keys (ANTHROPIC_API_KEY, etc.)

# Start PostgreSQL + Redis + API
docker compose up -d

# Run migrations
docker compose exec api alembic upgrade head

# Seed canonical data (houses, markets, teams, sports)
docker compose exec api make seed

# Run tests
docker compose exec api make test
```

### Useful Commands (via Makefile)
```bash
make up           # Start containers
make down         # Stop containers
make logs         # API logs
make test         # Run tests (pytest -n 8)
make lint         # Ruff check + format
make typecheck    # MyPy
make migrate      # Alembic upgrade head
make seed         # Seed canonical data
make shell        # Shell into API container
```

### Local Access Points
- **API**: http://localhost:8000
- **Swagger Docs**: http://localhost:8000/docs
- **Health**: http://localhost:8000/health
- **Readiness**: http://localhost:8000/ready
- **Metrics**: http://localhost:8000/metrics
- **Flower (Celery)**: http://localhost:5555
- **PostgreSQL**: `localhost:5432` (user: `bancaemdia`, pass from `.env`)
- **Redis**: `localhost:6379`

---

## Project Structure

```
bancaemdia-api/
├── .github/workflows/          # CI/CD pipelines
│   ├── ci.yml                  # Test → Build
│   ├── cd.yml                  # Deploy Staging → Canary → Prod
│   └── load-test.yml           # k6 load test
├── infra/terraform/            # Infrastructure as Code
│   ├── modules/                # Reusable modules
│   └── environments/           # staging/, production/
├── k6/                         # Load tests
│   ├── load-test.js
│   └── scenarios/
├── src/bancaemdia/             # Application code
│   ├── api/v1/                 # FastAPI routers
│   │   ├── apostas.py
│   │   ├── caixa.py
│   │   ├── coleta.py
│   │   ├── painel.py
│   │   ├── revisao.py
│   │   ├── upload.py
│   │   └── health.py
│   ├── auth/                   # JWT RS256 + JWKS
│   ├── cache/                  # Redis cache (extraction)
│   ├── cli/                    # CLIs (replay, reprocess, etc.)
│   ├── domain/                 # Pure business logic (no I/O)
│   │   ├── financeiro.py       # Profit, ROI, freebet, states
│   │   ├── temporal.py         # Units, house accounts (centralized!)
│   │   ├── conferencias.py     # 14 validation conferences
│   │   ├── materializar.py     # Idempotent upsert + events
│   │   ├── pareador.py         # Matching + pairing
│   │   └── normalizacao.py     # Houses, markets, teams
│   ├── infrastructure/         # I/O implementations
│   │   ├── database/           # SQLAlchemy + repositories
│   │   ├── cache/              # Redis client
│   │   ├── storage/            # S3
│   │   └── external/           # Anthropic, house parsers
│   ├── middleware/             # ASGI middlewares
│   │   ├── auth.py
│   │   ├── rate_limit.py
│   │   ├── router.py           # Primary/Replica routing
│   │   └── rls.py              # RLS session variable
│   ├── observability/          # OTel, structlog, metrics, health
│   ├── resilience/             # Circuit breakers, retries
│   ├── rate_limit/             # Token bucket for Anthropic
│   ├── workers/                # Celery tasks
│   │   ├── celery_app.py
│   │   ├── extraction.py
│   │   └── materialization.py
│   ├── config.py               # Pydantic Settings
│   ├── database.py             # Async engines + sessions
│   └── main.py                 # App factory
├── tests/                      # Tests organized by layer
│   ├── unit/                   # Domain services (fast, no I/O)
│   ├── integration/            # Repository + API (testcontainers)
│   ├── contract/               # Pact tests (Anthropic, Betano)
│   ├── idempotency/            # Exhaustive idempotency tests
│   ├── edge_cases/             # Financial/temporal edge cases
│   └── chaos/                  # Failure injection tests
├── scripts/                    # Operational scripts
│   ├── conferir_numeros.py     # Integrity validation (5 banks)
│   ├── migrate_sqlite_to_pg.py
│   ├── seed_canonical.py
│   └── final_validation.py
├── docs/                       # Documentation
│   ├── adrs/                   # Architecture Decision Records
│   ├── runbooks/               # Operational runbooks
│   └── API.md                  # API documentation (auto-generated)
├── Dockerfile                  # Multi-stage build
├── docker-compose.yml          # Local dev stack
├── pyproject.toml              # Poetry + tool config
├── alembic.ini                 # Database migrations
└── Makefile                    # Development shortcuts
```

---

## Key Technical Decisions

| Area | Decision | Rationale |
|------|----------|-----------|
| **Framework** | FastAPI + SQLAlchemy 2.x async | Type-safe, auto-OpenAPI, native async |
| **Database** | PostgreSQL 16 (RDS) + RLS | ACID, native multi-tenant, managed |
| **Partitioning** | Native range (monthly) + `pg_partman` | Zero extra cost, partition pruning |
| **Async** | Celery + Redis Streams | Mature, dual-pool, Flower monitoring |
| **Cache** | Redis (extraction) | 80%+ hit rate = 100x IA cost reduction |
| **Observability** | OpenTelemetry + Prometheus + Grafana | Vendor-neutral, auto-instrumentation |
| **Auth** | JWT RS256 + JWKS | Stateless, industry standard |
| **Deploy** | ECS Fargate (spot workers) + ALB | Serverless, auto-scale, cost-effective |
| **Config** | Pydantic Settings (env only) | Zero hardcoded, startup validation |
| **Testing** | Pytest + Testcontainers | Real DB in CI, parallel (`-n 8`) |

---

## Financial Model (Core Domain)

Immutable rules (ported from original domain):

```python
# Single formula for all cases
profit = return_amount - stake

# State → Return mapping
GREEN       → stake * odd
RED         → 0
ANULADA     → stake
MEIO_GREEN  → stake/2 * odd + stake/2
MEIO_RED    → stake/2
CASHOUT     → user-provided value

# Freebet: stake_centavos = 0, valor_aposta_centavos = face value
# ROI: freebet enters via face value in denominator
```

**Centralized temporality** (`domain/temporal.py`):
- `unidades`: `vigente_de` / `vigente_ate` — January bet uses January unit
- `contas_casa`: `desde` / `ate` — opening account today doesn't own yesterday's bet

---

## Testing & Quality

```bash
# Local (with Docker)
make test              # All tests (parallel -n 8)
make test-unit         # Unit tests only (fast)
make test-integration  # With Testcontainers PG/Redis
make test-contract     # Pact tests
make lint              # Ruff
make typecheck         # MyPy

# CI (GitHub Actions)
# Push → lint + typecheck + test + docker build
# PR   → same + contract tests
```

**Targets**:
- Coverage > 80% on domain
- Test suite < 10 min (parallel)
- Zero flaky tests

---

## Observability

| Component | Tool | Endpoint/Port |
|-----------|------|---------------|
| **Logs** | structlog (JSON) → CloudWatch | stdout |
| **Metrics** | Prometheus `/metrics` | :8000/metrics |
| **Traces** | OpenTelemetry → OTLP | Jaeger/Tempo/X-Ray |
| **Dashboards** | Grafana | Provisioned via Terraform |
| **Alerts** | CloudWatch → SNS → Slack/PagerDuty | See ADR-008 |
| **Health** | `/health` (liveness) + `/ready` (readiness) | :8000/health |

---

## Security & Compliance

- **Secrets**: AWS Secrets Manager (zero in code/env)
- **Rate Limiting**: `slowapi` by `usuario_id` (10/min coleta, 100/min API, 1/5min upload)
- **Circuit Breaker**: `pybreaker` on Anthropic (fail_max=5, reset=60s)
- **Headers**: CSP, HSTS, X-Frame-Options, X-Content-Type-Options
- **RLS**: Row-Level Security on all per-user tables
- **LGPD (prep)**: Encryption at-rest/transit, right to deletion, data export

---

## Deploy

### Staging (auto on push to main)
```bash
git push origin main
# → GitHub Actions: test → build → deploy staging (ECS Fargate spot)
# → Health check /ready
# → Smoke tests
# → conferir_numeros.py on staging DB
```

### Production (manual, canary)
```bash
# Via GitHub Actions UI or CLI
gh workflow run cd.yml -f sha=<commit> -f environment=production

# Pipeline:
# 1. Build + push GHCR
# 2. Deploy Green (staging) → smoke tests
# 3. Canary 10% (10 min monitoring)
# 4. Promote 10% → 50% → 100% (30 min total)
# 5. Rollback < 5 min at any point
```

### Rollback
```bash
gh workflow run cd.yml -f sha=<previous_sha> -f environment=production
# Target: < 5 min decision → healthy
```

---

## Documentation

| Document | Location |
|----------|----------|
| **API Reference** | `/docs/API.md` (auto-generated from OpenAPI) |
| **Runbooks** | `docs/runbooks/` (deploy, rollback, migration, incident, scaling) |
| **ADRs** | `docs/adrs/` (all architectural decisions) |
| **Roadmap** | `docs/adrs/HIGH_LEVEL_PLAN.md` (M0–M5) |

---

## Contributing

1. **Branch**: `feature/<issue-number>-<short-description>`
2. **Commits**: Conventional (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`)
3. **PR**: Passes CI (lint + typecheck + test + contract)
4. **Review**: 1 approval + CI green
5. **Merge**: Squash + merge to main

### Code Style
- **Python**: Ruff (format + lint), MyPy (strict)
- **SQL**: Alembic migrations (expand-only pattern)
- **Commits**: Conventional Commits 1.0
- **ADRs**: MADR elaborate format for new decisions

---

## License

Proprietary — All rights reserved.

---

## Links

- **Repository**: https://github.com/pradyumna-001/bancaemdia-api
- **Issues**: https://github.com/pradyumna-001/bancaemdia-api/issues
- **Milestones**: https://github.com/pradyumna-001/bancaemdia-api/milestones
- **Actions**: https://github.com/pradyumna-001/bancaemdia-api/actions
- **Discussions**: https://github.com/pradyumna-001/bancaemdia-api/discussions