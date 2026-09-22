# Architecture Decision Records (ADRs)

| # | Title | Status | Date |
|---|-------|--------|------|
| 001 | Quality Attributes & SLAs — Define Reliability, Scalability, Maintainability Targets | Proposed | 2026-08-24 |
| 002 | Data Model & Query Strategy — Relational Model, SQLAlchemy ORM, Shared Schema with RLS | Proposed | 2026-08-24 |
| 003 | Storage Engine & Indexing Strategy — PostgreSQL, Native Partitioning, Async Read Replica | Proposed | 2026-08-24 |
| 004 | Schema Evolution & Serialization — JSON, Implicit Versioning, Path-Based API Versioning | Proposed | 2026-08-24 |
| 005 | Replication Strategy — Single-Leader, Async Read Replica, Read-After-Write to Primary | Proposed | 2026-08-24 |
| 006 | Partitioning & Sharding Strategy — Native PG Range Partitioning, Future Hash Sharding by usuario_id | Proposed | 2026-08-24 |
| 007 | Transaction Boundaries & Isolation — Read Committed, Pessimistic Locking, Per-Operation Transactions | Proposed | 2026-08-24 |
| 008 | Failure Modes, Timeouts, Retries & Observability — Circuit Breakers, OpenTelemetry, Rate Limiting | Proposed | 2026-08-24 |
| 009 | Consistency Model — Linearizable for Writes, Eventual for Reads | Proposed | 2026-08-24 |
| 010 | Batch Pipeline Architecture — Dual Worker Pools, At-Least-Once + Idempotency, Shared Pool | Proposed | 2026-08-24 |
| 011 | Stream Processing & Async Architecture — Redis Streams, 202 Polling, Celery Unified | Proposed | 2026-08-24 |
| 012 | Evolvability & Extensibility — Feature Flags, Schema Migration, Deploy Safety | Proposed | 2026-08-24 |

---

## Planning Roadmaps

| # | Title | Status | Date |
|---|-------|--------|------|
| 013 | 4-Week Backend Scope | Existing | 2026-08-25 |
| 014 | Week 1 — Foundation Issues | Existing | 2026-08-25 |
| 015 | Week 2 — Async Pipeline Issues | Existing | 2026-08-25 |
| 016 | Week 3 — API Surface Issues | Existing | 2026-08-25 |
| 017 | Week 4 — Hardening & Deploy Issues | Existing | 2026-08-25 |
| 018 | Expansion Roadmap — Decisions Before New GitHub Issues | Proposed in PR | 2026-09-21 |
| 019 | Week 5 — Billing & Account Ownership Issues | Proposed in PR | 2026-09-21 |
| 020 | Week 6 — Telegram, Calculators & Analytics Issues | Proposed in PR | 2026-09-21 |
| 021 | Week 7 — Collection Integrity & Extension Contract Issues | Proposed in PR | 2026-09-21 |
| 022 | Week 8 — Bookmaker Coverage Issues | Proposed in PR | 2026-09-21 |
| 023 | Week 9 — Expansion Validation Issue | Proposed in PR | 2026-09-21 |
| 024 | Extension Client — Centralized Responsibility Map | Proposed in PR | 2026-09-21 |

The Week 5–9 issue bodies and the extension responsibility map are review artifacts. Issues are created in the central GitHub tracker only after the administrator merges the proposal PR; ADR 024 creates no separate client backlog.

---

## Process

1. **Create** new ADR from template (MADR elaborate format)
2. **Review** with stakeholders (async or sync)
3. **Accept** → merge to main, update this index
4. **Implement** → reference in PRs: "Implements ADR-XXX"
5. **Supersede** when changed: new ADR, mark old `Superseded by ADR-YYY`

## Template

Use ADRs 001–012 as the decision-record convention and ADRs 014–017 as the established milestone/issue-body convention. New planning documents must preserve the repository's `Labels`, `Size`, `Files`, `Tasks`, and `Acceptance` structure.
