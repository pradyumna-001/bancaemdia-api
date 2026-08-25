# ADR 005: Replication Strategy — Single-Leader, Async Read Replica, Read-After-Write to Primary

## Status
Proposed

## Context

Current state: **SQLite** (no replication). Target: **PostgreSQL on AWS RDS** with Multi-AZ for HA.

**ADR-001 Requirements:**
- RPO = 0 (zero data loss) → PITR via WAL
- RTO < 1 hour → automated failover
- Read latency P95 < 500ms for dashboard

**ADR-003 Decision:** Async read replica (same region) for analytics/dashboard; lag alert <30s.

**Workload:**
- Primary: All writes (OLTP: eventos, movimentos, apostas, coletas_casa)
- Read Replica: Dashboard queries (materialized views + ad-hoc), Excel export, cross-tenant analytics (future)

## Decision

We use **single-leader replication** with **AWS RDS Multi-AZ (synchronous standby for HA)** + **1 async read replica (same region)** for read scaling. **Read-after-write consistency** achieved by routing reads to primary for 5 seconds post-write. **Automatic failover** for Multi-AZ standby; read replica promotion manual. **Replication slots** configured to prevent WAL removal during replica lag. **Cross-region** via automated snapshots (DR); cross-region read replica deferred. **Logical replication** (CDC) deferred to future.

## Consequences

### Positive
- **RPO=0**: Synchronous Multi-AZ standby ensures no committed transaction lost
- **RTO<1h**: RDS automatic failover (~60-120s typical); no manual intervention
- **Read scaling**: Dashboard queries offloaded to replica; primary handles writes
- **Read-after-write**: 5s primary routing guarantees user sees own writes immediately
- **Operational simplicity**: Fully managed by RDS; no self-managed replication
- **Replication slots**: Prevent primary from discarding WAL needed by replica

### Negative
- **Async replica lag**: Dashboard may show stale data (<30s); mitigated by primary routing for recent writes
- **Single read replica**: Write throughput limited to primary; horizontal read scaling requires more replicas later
- **Manual replica promotion**: If primary fails, read replica not automatically promoted (separate from Multi-AZ standby)
- **Cross-region latency**: Users outside deployment region see higher latency; deferred

### Neutral
- **Logical replication (CDC)**: `wal_level = logical` enabled for future; no consumers yet
- **Multi-leader/leaderless**: Not needed; single-write-leader matches financial data consistency requirements

## Options Considered

### Option A: Single-Leader + Async Read Replica (Chosen)
**Pros:** Simple, managed, matches workload (write-heavy OLTP, read-heavy OLAP)
**Cons:** Replica lag; single write leader

### Option B: Multi-Leader (Multi-Master) — *Rejected*
**Pros:** Write scaling across regions
**Cons:** Conflict resolution complexity; financial data requires strong consistency; overkill for launch

### Option C: Leaderless (DynamoDB-style) — *Rejected*
**Pros:** High availability, multi-region writes
**Cons:** Eventual consistency conflicts with financial correctness; no SQL/relational model

### Option D: Synchronous Read Replica — *Rejected*
**Pros:** Zero lag reads
**Cons:** Write latency penalty; defeats read scaling purpose; RDS doesn't support synchronous read replicas

### Option E: No Read Replica (All Reads on Primary) — *Rejected*
**Pros:** Simplest
**Cons:** Dashboard queries compete with writes; violates ADR-001 latency SLO

## Compliance

- [ ] **RDS Multi-AZ**: Enabled (synchronous standby in different AZ)
- [ ] **Read Replica**: 1 async replica in same region; instance class matching primary
- [ ] **Replication Slot**: Physical slot created on primary for read replica (`pg_create_physical_replication_slot`)
- [ ] **Slot Monitoring**: CloudWatch alert if `pg_replication_slot` lag > 1GB or replica lag > 30s
- [ ] **Read-After-Write Routing**: Middleware/router directs reads to primary for 5s after any write by same session (cookie/session-based)
- [ ] **Failover**: RDS automatic failover enabled; runbook documents manual read replica promotion if needed
- [ ] **Cross-Region DR**: Automated snapshots (35 days) + daily cross-region snapshot copy; monthly restore test
- [ ] **Logical Replication**: `wal_level = logical` set; no publications/subscriptions yet (future CDC)
- [ ] **Connection Pooling**: SQLAlchemy pool routes to primary for writes; read replica for SELECT (via separate pool or router)

## Read-After-Write Implementation Detail

```python
# Middleware (FastAPI) - pseudo-code
@app.middleware("http")
async def read_after_write_routing(request: Request, call_next):
    # Track last write timestamp per session
    last_write = request.session.get("last_write_ts", 0)
    now = time.time()
    
    # Route to primary if: write in last 5s OR explicit header OR mutation method
    use_primary = (
        request.method in ("POST", "PUT", "PATCH", "DELETE") or
        (now - last_write) < 5 or
        request.headers.get("X-Read-Primary") == "true"
    )
    
    request.state.db_pool = primary_pool if use_primary else replica_pool
    
    response = await call_next(request)
    
    # Update last_write on mutations
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        request.session["last_write_ts"] = now
    
    return response
```

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** Read replica lag >60s sustained, need for cross-region reads, CDC/kafka pipeline, write throughput saturation
- **Related ADRs:** ADR-001 (QARs), ADR-003 (Storage), ADR-008 (Failure Modes), ADR-011 (Stream Processing)
- **Open Items:** Exact read-replica instance class; session affinity implementation; replica promotion runbook

## References

- DDIA Ch 5: Replication (leader-based, multi-leader, leaderless, lag, failover)
- AWS RDS User Guide: Multi-AZ, Read Replicas, Replication Slots, Failover
- `planilhador/banco/conexao.py` — current connection management (to be replaced with pooled primary/replica)