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

## Process

1. **Create** new ADR from template (MADR elaborate format)
2. **Review** with stakeholders (async or sync)
3. **Accept** → merge to main, update this index
4. **Implement** → reference in PRs: "Implements ADR-XXX"
5. **Supersede** when changed: new ADR, mark old `Superseded by ADR-YYY`

## Template

See `.claude/skills/adr-writing/SKILL.md` for MADR elaborate template and conventions.