# ADR 006: Partitioning & Sharding Strategy — Native PG Range Partitioning, Future Hash Sharding by usuario_id

## Status
Proposed

## Context

**ADR-003 Decision**: Native PostgreSQL range partitioning on `criada_em` (monthly) for append-only time-series tables: `eventos`, `movimentos`, `mensagem_versoes`, `coletas_casa`.

**ADR-002 Decision**: Shared schema + RLS for multi-tenancy (logical isolation, not physical sharding).

**Scale at Launch**: 50 users, ~10k bets/day → ~3M bets/year. PostgreSQL handles 100M+ rows in partitioned tables comfortably.

**Trigger**: Need explicit partitioning/sharding strategy before migration to PG.

## Decision

We implement **native PG range partitioning (monthly)** on time-series tables at launch via `pg_partman`. **Physical sharding** (multiple primaries) deferred with a **numeric trigger**: write latency P99 > 500ms sustained for 15 minutes. When triggered, sharding key = **hash of `usuario_id`**. All analytical queries **must include temporal filter** for partition pruning. Archive policy: retain 5 years in PG, then detach to S3/Parquet.

## Consequences

### Positive
- **Partition pruning**: Time-filtered queries (all dashboard queries) scan only relevant monthly partitions
- **pg_partman automation**: Zero ongoing ops for partition creation/maintenance
- **Sharding trigger defined**: Measurable, actionable, prevents premature optimization
- **Hash `usuario_id` sharding**: Natural tenant isolation, matches RLS model, 95% queries single-tenant
- **5-year retention**: Negligible storage cost (~2.5 GB at 500 users), simplicity over premature archive

### Negative
- **No global indexes**: Queries without time filter scan all partitions (mitigated: all dashboard queries include time filter)
- **Hotspot on current partition**: All writes go to active month (acceptable for launch scale)
- **pg_partman dependency**: One more extension; minimal risk, well-maintained

### Neutral
- **Secondary indexes**: Local per partition (PG limitation); composite `(usuario_id, criada_em)` on each partition
- **Cross-tenant analytics**: Future scatter-gather job across shards (batch, not real-time)
- **Archive implementation**: Deferred to year 5; `pg_partman` supports detach + custom script

## Options Considered

### Option A: No Partitioning (Single Tables) — *Rejected*
**Pros:** Simplest migration
**Cons:** Index bloat, vacuum slowdown, no partition pruning, unlimited table growth

### Option B: Native PG Range Partitioning + pg_partman — *Chosen for Launch*
**Pros:** Native, automated, partition pruning, managed by RDS
**Cons:** Local indexes only; current-month hotspot

### Option C: Hash Partitioning by `usuario_id` — *Rejected for Launch*
**Pros:** Even write distribution
**Cons:** No partition pruning for time-range queries (dashboard); analytics harder

### Option D: Physical Sharding at Launch — *Rejected*
**Pros:** Horizontal write scale
**Cons:** Operational complexity; cross-shard queries; overkill for 50 users

### Option E: TimescaleDB Hypertables — *Rejected (ADR-003)*
**Pros:** Automatic partitioning, compression, continuous aggregates
**Cons:** Extra dependency; native PG partitioning sufficient

## Compliance

- [ ] **Range Partitioning**: Monthly on `criada_em` for `eventos`, `movimentos`, `mensagem_versoes`, `coletas_casa`
- [ ] **pg_partman**: Installed via migration; `partman.create_parent()` + `run_maintenance()` cron (daily)
- [ ] **Partition Retention**: 3 future partitions pre-created; detach policy after 5 years (configurable)
- [ ] **Indexes**: Local B-tree on PK/FK; composite `(usuario_id, criada_em)` on each partition; BRIN on `criada_em` per partition; GIN on JSONB columns
- [ ] **Query Pattern Enforcement**: All dashboard/analytics queries **must** include `criada_em`/`ocorrido_em` filter (code review + linter rule)
- [ ] **Sharding Trigger**: Monitor `write_latency_p99_ms` (CloudWatch + app metric); alert if >500ms for 15min; runbook defines sharding execution
- [ ] **Sharding Key**: Hash(`usuario_id`) — documented for future implementation
- [ ] **Archive Policy**: Detach partitions >5 years → export Parquet → S3 → Athena/Trino (implement when triggered)
- [ ] **Monitoring**: Partition count, size per partition, pruning effectiveness (`EXPLAIN ANALYZE`)

## Sharding Execution Runbook (Future)

```markdown
# Sharding Execution (when trigger fires)

## Pre-requisites
- Trigger: write_latency_p99 > 500ms for 15min sustained
- Decision recorded in ADR-XXX (supersedes this section)
- Downtime window scheduled (or blue-green)

## Steps
1. Provision N new RDS primaries (shards)
2. Implement router: hash(usuario_id) % N → shard
3. Migrate data: COPY per usuario_id batch (or pg_dump/restore + delete other tenants)
4. Update connection pool: per-shard pools + router
5. Update RLS: optional (router enforces)
6. Validate: conferir_numeros.py on each shard + cross-shard
7. Cutover: DNS / connection string switch
8. Decommission old primary

## Rollback
- Revert router to single primary
- Replication slot kept for 24h post-cutover
```

## Notes

- **Review date:** 2026-11-01 (post-launch) or when sharding trigger fires
- **Trigger for revisit:** Sharding trigger fires, or partition count > 60 (5 years), or cross-tenant analytics needed
- **Related ADRs:** ADR-002 (Data Model), ADR-003 (Storage), ADR-005 (Replication), ADR-012 (Evolvability)
- **Open Items:** Exact shard count formula (start with 2? 4?); router implementation (application-level vs proxy)

## References

- DDIA Ch 6: Partitioning (key range, hash, composite, secondary indexes, rebalancing)
- PostgreSQL 16 Docs: Declarative Partitioning, `pg_partman`
- `pg_partman` GitHub: https://github.com/pgpartman/pg_partman
- `planilhador/banco/migracoes/` — current schema (source for partition DDL)