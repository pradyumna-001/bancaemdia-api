# ADR 009: Consistency Model — Linearizable for Writes, Eventual for Reads

## Status
Proposed

## Context

Current state: **SQLite** (single-threaded, strong consistency by default). Target: **PostgreSQL Primary + Async Read Replica** (ADR-005) with multi-tenant SaaS workload.

**Requirements (ADR-001):**
- Financial correctness > availability
- Zero data loss (RPO=0)
- Dashboard latency P95 < 500ms (near-real-time acceptable)

**Key Insight**: Not all operations need the same consistency. Financial writes need linearizability; dashboard reads can tolerate <30s staleness.

## Decision

We adopt **per-operation consistency models**:
- **Linearizable (strong)**: All financial mutations on Primary
- **Eventual (<30s)**: Dashboard, analytics, exports on Read Replica
- **Sequential**: Event log replay (ordered by `id`)

No consensus protocol implementation needed (RDS manages Raft internally).

## Consequences

### Positive
- **Financial safety**: Linearizable writes prevent lost updates, double-spends, inconsistent state
- **Performance**: Reads scale on replica; no synchronous replication penalty
- **Simplicity**: No custom consensus, no distributed transactions at launch
- **Clear contract**: Developers know which operations need Primary vs Replica

### Negative
- **Stale reads**: Dashboard may show data <30s old (mitigated: read-after-write routing to Primary for 5s, ADR-005)
- **Application complexity**: Router must direct mutations to Primary, reads to Replica (with exceptions)

### Neutral
- **RDS Multi-AZ**: Synchronous standby provides durability (RPO=0) but not read scaling
- **Event log**: `eventos` ordered by `id` provides sequential consistency for replay

## Consistency Model per Operation

| Operation | Model | Target | Implementation |
|-----------|-------|--------|----------------|
| Create bet (manual/import/coleta) | **Linearizable** | Primary | `FOR UPDATE` + single tx |
| Mark result (cashout/green/red/anulada) | **Linearizable** | Primary | `FOR UPDATE` + single tx |
| Caixa (depósito/saque/transferência) | **Linearizable** | Primary | `FOR UPDATE` + single tx |
| Pareador (duvida_de_par, parceira_chave) | **Linearizable** | Primary | Lock ordering `ORDER BY id` |
| Update bet (odds, stake, selection) | **Linearizable** | Primary | `FOR UPDATE` |
| Dashboard / Painel | **Eventual (<30s)** | Replica | Materialized views refreshed 5min |
| Excel Export | **Eventual (<30s)** | Replica | Query on replica |
| Extraction (IA async) | **Eventual** | Queue + Primary | Idempotent worker, writes to Primary |
| Coleta casa (webhook) | **Eventual** | Primary | Idempotent ingest, single tx |
| Replay / Reprojeção | **Sequential** | Primary | `eventos` ordered by `id` |
| Cross-shard (future) | **Eventual** | Async job | Saga pattern (ADR-011) |

## Read-After-Write Consistency

Per ADR-005: **Route to Primary for 5s after any write by same session**.
- Implements "session consistency" (stronger than eventual, weaker than linearizable)
- Guarantees user sees own writes immediately
- Dashboard badge: "Atualizado há X segundos" (based on replica lag metric)

## Options Considered

### Option A: Strong Consistency Everywhere (Sync Replica) — *Rejected*
**Pros:** Simple mental model
**Cons:** Write latency penalty; defeats read scaling; RDS doesn't support sync read replicas

### Option B: Eventual Consistency Everywhere — *Rejected*
**Pros:** Maximum performance
**Cons:** Financial data corruption risk; violates ADR-001

### Option C: Per-Operation Model (Chosen) — *Chosen*
**Pros:** Right tool for each job; financial safety + read performance
**Cons:** Router logic required; developer discipline

## Compliance

- [ ] **Router**: All mutating endpoints (`POST`, `PUT`, `PATCH`, `DELETE`) → Primary pool
- [ ] **Router**: `GET /painel`, `GET /api/v1/apostas` (list), `GET /export` → Replica pool
- [ ] **Router**: `GET /api/v1/apostas/{id}` after write by same session → Primary (5s window)
- [ ] **Materialized Views**: Refreshed every 5min via `pg_cron`; `CONCURRENTLY` to avoid locks
- [ ] **Replica Lag Monitoring**: CloudWatch alert if `ReplicaLag > 30s` for 5min
- [ ] **Dashboard Badge**: Shows `now() - pg_last_xact_replay_timestamp()` as "atualizado há Xs"
- [ ] **Event Log Ordering**: `eventos.id` (BIGSERIAL) = total order for replay; never reorder
- [ ] **No Distributed Consensus**: RDS manages; sharding = single-shard OLTP (ADR-006)

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** Replica lag >60s sustained, financial anomaly detected, sharding implemented
- **Related ADRs:** ADR-001 (QARs), ADR-003 (Storage), ADR-005 (Replication), ADR-007 (Transactions), ADR-008 (Failure Modes), ADR-010 (Batch), ADR-011 (Stream)

## References

- DDIA Ch 9: Consistency & Consensus (linearizability, sequential, eventual, consensus)
- ADR-005: Read-after-write routing implementation
- `planilhador/banco/conexao.py` — connection pooling (to be split primary/replica)
- `planilhador/dominio/projecao.py` — replay logic (sequential consistency)