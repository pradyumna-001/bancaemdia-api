# ADR 003: Storage Engine & Indexing Strategy — PostgreSQL, Native Partitioning, Async Read Replica

## Status
Proposed

## Context

Current state: **SQLite** (B-tree, row-oriented, single file, WAL mode via `planilhador/banco/conexao.py`). 28 migrations, ~16k bets in production, 49 users.

Target: **PostgreSQL on AWS RDS** for multi-tenant SaaS launch (50 users, ~10k bets/day projected).

**Workload Profile:**
- **OLTP Write**: Batch import (export Telegram/planilha) → `eventos` + `movimentos` + `apostas` projection; Coleta casa stream → `coletas_casa` + `eventos`; Manual edits → `eventos` (append-only)
- **OLTP Read**: Single-bet lookup, revisão pendente, coleta dedup check
- **OLAP Read**: Painel dashboard (ROI, lucro, saldo por casa/tipster/mercado/tempo), Excel export (denormalized projection)

**Key Tables Growth:**
| Table | Type | Growth | Retention |
|-------|------|--------|-----------|
| `eventos` | Append-only log | ~1 row/bet + corrections | Forever (audit) |
| `movimentos` | Append-only caixa | ~2-5 rows/user/month | Forever |
| `mensagem_versoes` | Append-only | ~1 row/message edit | Forever |
| `coletas_casa` | Raw JSON + dedup | ~1 row/bet from casa | Forever |
| `apostas` | Materialized view | ~1 row/bet | Rebuildable |
| `chamadas_ia` | Audit log | ~1 row/API call | Forever |

## Decision

We use **standard PostgreSQL (AWS RDS)** with **native range partitioning** on time-series tables, **B-tree + partial + BRIN + GIN indexes** as needed, **async read replica** for analytics, **SQLAlchemy connection pool** (no PgBouncer at launch), **AWS RDS automated backup + cross-region snapshots** for PITR, and **materialized views refreshed every 5min** for dashboard.

## Consequences

### Positive
- **Operational simplicity**: Single managed service (RDS), no TimescaleDB/ClickHouse expertise needed
- **Cost-effective**: RDS pricing predictable; read replica ~50% of primary
- **Partitioning native**: `PARTITION BY RANGE (criada_em)` on `eventos`, `movimentos`, `mensagem_versoes`, `coletas_casa` — automatic partition pruning, easy maintenance (detach old partitions)
- **PITR + Cross-region**: RPO=0 achieved via WAL archiving; restore tested monthly
- **GIN on JSONB**: Enables queries like `payload_json @> '{"tipo": "RESULTADO_REGISTRADO"}'` for replay filtering
- **BRIN indexes**: 10-100x smaller than B-tree for append-only time columns; ideal for partition-level scans
- **Materialized views**: Near-real-time dashboard (<30s lag) without OLTP impact

### Negative
- **Partitioning setup effort**: DDL changes, migration script complexity, ORM mapping (SQLAlchemy + `postgresql_partitioned` or raw DDL)
- **Async replica lag**: Dashboard may show stale data (<30s); users must understand "near-real-time"
- **BRIN limitations**: Only effective on correlated data (time-ordered inserts); ineffective if backfill occurs
- **No PgBouncer at launch**: Connection spikes during batch import may saturate pool; monitoring required

### Neutral
- **Compression**: PG TOAST handles large JSONB automatically; `pglz`/`lz4`/`zstd` available later if storage cost becomes issue
- **Columnar**: Deferred to post-launch (ClickHouse/TimescaleDB) if MVs + read replica insufficient
- **Connection pool**: SQLAlchemy async pool (`pool_size=10, max_overflow=20`) sufficient for 50 users; PgBouncer added when `max_overflow` exhausted consistently

## Options Considered

### Option A: PostgreSQL Standard (No Partitioning) — *Rejected*
**Pros:** Simplest migration
**Cons:** `eventos`/`movimentos` grow unbounded; index bloat; vacuum/analyze slow; no partition pruning for time-range queries

### Option B: TimescaleDB (Managed or Self-hosted) — *Rejected for Launch*
**Pros:** Hypertables, continuous aggregates, native compression, retention policies
**Cons:** Extra dependency; managed TimescaleDB cost premium; self-hosted ops burden; continuous aggregates duplicate MV logic

### Option C: PostgreSQL + Native Partitioning — *Chosen*
**Pros:** Native, no extra cost, partition pruning, `pg_partman` can automate partition maintenance
**Cons:** Manual partition creation (or `pg_partman`); ORM mapping complexity

### Option D: ClickHouse for Analytics — *Rejected for Launch*
**Pros:** Best OLAP performance
**Cons:** Separate stack; data sync complexity; overkill for 50 users

## Compliance

- [ ] **Primary**: AWS RDS PostgreSQL 16+ (multi-AZ for HA)
- [ ] **Partitioning**: Native range partitioning on `criada_em` (monthly) for `eventos`, `movimentos`, `mensagem_versoes`, `coletas_casa`
- [ ] **Partition Maintenance**: `pg_partman` or custom cron to create future partitions (3 months ahead), detach partitions >2 years (archive to S3/Parquet)
- [ ] **Indexes**:
  - [ ] B-tree: PKs, FKs, high-cardinality lookups (`usuario_id`, `chat_id, message_id`)
  - [ ] Partial: `revisao_grave`, `duvida_de_par`, `chave` (where not null)
  - [ ] BRIN: `criada_em`/`ocorrido_em` on partitioned tables (per partition)
  - [ ] GIN: `eventos.payload_json`, `coletas_casa.bruto_json`, `extracoes_cache.resultado_json`
  - [ ] Composite: `(usuario_id, criada_em)` for user-scoped time queries
- [ ] **Read Replica**: Async, single replica in same region; lag monitored via CloudWatch `ReplicaLag`; alert if >30s
- [ ] **Connection Pool**: SQLAlchemy async pool (`pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=300`); PgBouncer deferred
- [ ] **Backup/PITR**: AWS RDS automated backups (35 days retention) + cross-region snapshot (daily); monthly restore test to staging
- [ ] **Materialized Views**: For painel aggregates (ROI por casa/tipster/mercado, saldo por casa, lucro acumulado); refreshed via `pg_cron` every 5min; `CONCURRENTLY` to avoid locks
- [ ] **Monitoring**: CloudWatch (CPU, connections, replica lag, storage), `pg_stat_statements` for slow queries, structured logs
- [ ] **Migration Rehearsal**: `pgloader` SQLite→PG on staging copy; validate row counts, `conferir_numeros.py` passes, indexes created

## Partitioning Detail (MVP)

```sql
-- Example: eventos partitioned by month
CREATE TABLE eventos (
    id           BIGSERIAL,
    usuario_id   INT NOT NULL,
    tipo         TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    fonte        TEXT NOT NULL,
    confianca    REAL,
    chat_id      BIGINT,
    message_id   BIGINT,
    criado_em    TIMESTAMPTZ NOT NULL,
    aposta_chave TEXT,
    PRIMARY KEY (id, criado_em)  -- partition key must be in PK
) PARTITION BY RANGE (criada_em);

-- Monthly partitions (created via pg_partman or migration)
CREATE TABLE eventos_2026_08 PARTITION OF eventos
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE eventos_2026_09 PARTITION OF eventos
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
-- ... etc

-- BRIN index per partition (auto-created by pg_partman or manual)
CREATE INDEX idx_eventos_criada_em_brin ON eventos_2026_08 USING BRIN (criada_em);

-- GIN on JSONB (non-partitioned, or per partition)
CREATE INDEX idx_eventos_payload_gin ON eventos USING GIN (payload_json);
```

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** Table size >100GB, replica lag >60s sustained, PgBouncer needed, columnar analytics required
- **Related ADRs:** ADR-001 (QARs), ADR-002 (Data Model), ADR-005 (Replication), ADR-010 (Batch Pipeline)
- **Open Items (confirm with creator):**
  - Partition retention policy (2 years? 5 years? forever?)
  - Archive format for detached partitions (Parquet on S3?)
  - Exact MV definitions for painel (query patterns to optimize)

## References

- DDIA Ch 3: Storage & Retrieval (B-trees, LSM, column-oriented)
- DDIA Ch 6: Partitioning
- PostgreSQL 16 Docs: Partitioning, BRIN, GIN, `pg_partman`
- AWS RDS User Guide: Read Replicas, Automated Backups, Cross-Region Snapshots
- `planilhador/banco/migracoes/` — current schema source of truth
- `scripts/conferir_numeros.py` — migration validation