# ADR 001: Quality Attributes & SLAs — Define Reliability, Scalability, Maintainability Targets

## Status
Proposed

## Context

The project is a **multi-tenant SaaS** (bet tracking spreadsheet) currently running on a single server with SQLite, serving 49 real users (16,327 bets). We are planning for **50 users at launch** with growth after.

**Current state:**
- Single server, SQLite file database
- No formal SLAs, RTO/RPO defined
- Deploy via `publicar.py` (manual, no rollback automation)
- Observability: only `scripts/conferir_numeros.py` validates 5 banks replay
- Anthropic API calls synchronous in batch (`planilhar.py`)
- Extension breaks when betting sites change layout

**Trigger:** Architectural redesign to support SaaS scale, reliability, and team maintainability per DDIA Ch 1.

**Constraints:**
- **Zero data loss** — financial data is immutable, append-only log (`eventos`, `movimentos`)
- **Financial correctness** > availability (better to reject write than corrupt ledger)
- **Single developer** (AI-assisted) — operational simplicity matters
- **AWS target** for hosting
- **Existing rules** (AGENTS.md): append-only log, temporal data, 14 conferences, 5 bet origins

## Decision

We define explicit **Quality Attribute Requirements (QARs)** and **Service Level Objectives (SLOs)** as the foundation for all subsequent architectural decisions.

## Consequences

### Positive
- Every subsequent ADR traces back to measurable targets
- Trade-offs made explicit (e.g., consistency vs availability for financial writes)
- Enables capacity planning and cost estimation
- Compliance checklist drives implementation verification

### Negative
- Upfront effort to instrument/monitor before features
- May constrain technology choices (e.g., SQLite write throughput)
- Requires discipline to maintain SLO dashboards

### Neutral
- Migration from SQLite → PostgreSQL driven by write concurrency SLO
- Async extraction pipeline driven by latency SLO for batch imports
- Multi-AZ deployment driven by RTO target

## Options Considered

### Option A: No explicit SLOs (Status Quo) — *Rejected*
**Pros:** Zero upfront work
**Cons:** Cannot measure reliability; no basis for architecture choices; "it works on my machine" scales poorly

### Option B: Industry-standard SaaS SLOs (99.9% uptime, RTO<1h, RPO=0) — *Chosen*
**Pros:** Meets "zero data loss" + reasonable availability for 50→500 users; aligns with AWS managed services
**Cons:** Requires PostgreSQL (not SQLite), async pipeline, observability stack

### Option C: Aggressive SLOs (99.99%, RTO<15min, multi-region) — *Rejected*
**Pros:** Maximum reliability
**Cons:** Cost/complexity unjustified for launch scale; single dev cannot operate

## Compliance

- [ ] **RPO = 0** for financial data: implemented via PostgreSQL + WAL + point-in-time recovery (PITR)
- [ ] **RTO < 1 hour**: documented runbook + automated restore test (quarterly)
- [ ] **Write availability > 99.9%** for bet ingestion: async queue + idempotent consumers
- [ ] **Read latency P95 < 500ms** for dashboard (painel): read replicas + caching
- [ ] **Extraction latency P95 < 5min** for 200-bet batch: async worker pool + progress API
- [ ] **Zero silent data corruption**: all mutations via append-only log; `conferir_numeros.py` runs in CI
- [ ] **Observability**: structured JSON logs, metrics (Prometheus), alerts (PagerDuty/CloudWatch), tracing (X-Ray)
- [ ] **Deploy**: CI/CD pipeline (GitHub Actions) → staging → production with automated rollback on error rate spike
- [ ] **Backup**: Daily automated PITR + cross-region snapshot; restore tested monthly

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** User count > 200, or any P0 incident, or migration to multi-region
- **Related ADRs:** Supersedes implicit assumptions in current `publicar.py` and `conferir_numeros.py`
- **Migration path:** SQLite → PostgreSQL (ADR-003), sync → async extraction (ADR-010/011)

## References

- DDIA Ch 1: Reliable, Scalable, Maintainable Systems
- AGENTS.md §0 (token diet), §7 (append-only log), §14 (money security)
- AWS Well-Architected Framework: Reliability & Operational Excellence pillars
- `scripts/conferir_numeros.py` — current data integrity verification
- `planilhador/banco/conexao.py` — current connection management (single-threaded SQLite)