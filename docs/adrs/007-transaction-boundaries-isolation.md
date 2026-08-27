# ADR 007: Transaction Boundaries & Isolation — Read Committed, Pessimistic Locking, Per-Operation Transactions

## Status
Proposed

## Context

Current state: **SQLite** with `BEGIN IMMEDIATE` transactions via `planilhador/banco/conexao.py::transacao()`. Single-threaded, no concurrency issues.

Target: **PostgreSQL** with **SQLAlchemy 2.x async** sessions. Multi-tenant SaaS with concurrent users, background workers (planilhar, coleta casa), and web requests.

**Current Transaction Patterns (from code):**

| Operation | Tables | Current Atomicity |
|-----------|--------|-------------------|
| Ingestão export | `mensagens`, `mensagem_versoes`, `uploads`, `extracoes_cache` | Single transaction |
| Planilhar lote | `eventos`, `apostas`, `revisao_pendente` (per bet) | Single transaction per bet |
| Coleta casa | `coletas_casa`, `eventos`, `apostas`, pareador (may touch 2nd bet) | Single transaction |
| Marcar resultado | `eventos`, `apostas`, `movimentos` (if cashout) | Single transaction |
| Pareador dúvida | `apostas` (2 rows: new + existing) | Single transaction |

**Requirements from ADR-001:**
- Financial correctness > availability
- Zero data loss (append-only log)
- RTO < 1h, RPO = 0

## Decision

We adopt **`READ COMMITTED` isolation level** with **mandatory pessimistic locking (`SELECT ... FOR UPDATE ORDER BY id`)** on all financial mutations. **Transaction boundary = one business operation** (ingest, collect, settle, pair). Batch processing (`planilhar`) uses **one transaction per bet** with savepoint every 50. **Idempotency by design** via natural keys (PK/UNIQUE) on all external writes. Distributed transactions deferred (sharding = single-shard OLTP).

## Consequences

### Positive
- **Financial correctness**: Pessimistic locking prevents lost updates on `apostas`, `movimentos`, `contas_casa`
- **Performance**: `READ COMMITTED` is PG default; no serialization failures/retries
- **Isolation**: Per-operation transactions match business boundaries; failure in one bet doesn't roll back others
- **Concurrency**: Short locks (per-bet) allow high throughput; lock ordering (`ORDER BY id`) prevents deadlocks
- **Idempotency**: Natural keys enable safe retry/replay (`conferir_numeros.py` works automatically)
- **Simplicity**: No saga/2PC complexity at launch; sharding keeps OLTP single-shard

### Negative
- **Discipline required**: Developers must remember `FOR UPDATE` on mutations (enforced by code review + linter)
- **No snapshot isolation**: Non-repeatable reads possible (acceptable for OLTP; analytics on read replica)
- **Batch overhead**: One commit per bet vs single transaction (mitigated: connection pool + async)
- **Deadlock risk**: Pareador touches 2 bets → lock ordering mandatory

### Neutral
- **Optimistic locking**: Not used; `version` column adds complexity for marginal benefit
- **Serializable**: Rejected; 10-50% overhead + retry logic for rare anomalies
- **Distributed transactions**: Deferred to ADR-011/012; sharding by `usuario_id` ensures OLTP single-shard

## Options Considered

### Option A: READ COMMITTED + Pessimistic Locking — *Chosen*
**Pros:** Default PG, performant, explicit control, matches current mental model
**Cons:** Requires `FOR UPDATE` discipline

### Option B: REPEATABLE READ — *Rejected*
**Pros:** Snapshot isolation, no non-repeatable reads
**Cons:** Phantom reads still possible; serialization failures on write conflicts → retry logic needed

### Option C: SERIALIZABLE — *Rejected*
**Pros:** Strongest isolation
**Cons:** High abort rate under contention; complex retry logic; overkill for financial OLTP

### Option D: Optimistic Locking (version column) — *Rejected*
**Pros:** No locks, simple reads
**Cons:** Retry logic everywhere; silent failures if check forgotten; `FOR UPDATE` is simpler for our write patterns

### Option E: Single Transaction for Batch (planilhar) — *Rejected*
**Pros:** Atomic "all or nothing"
**Cons:** Locks held for minutes; deadlock city; rollback loses all progress; no partial visibility

## Compliance

- [ ] **Isolation Level**: `READ COMMITTED` (PG default; SQLAlchemy `session.begin(isolation_level="READ_COMMITTED")`)
- [ ] **Pessimistic Locking**: Mandatory `SELECT ... FOR UPDATE ORDER BY id` on all mutations of:
  - `apostas` (edit, settle, pair, select)
  - `movimentos` (insert + related `contas_casa` balance check)
  - `contas_casa` (create/update)
  - `unidades` (create/update)
- [ ] **Lock Ordering**: Always `ORDER BY id` (or `usuario_id, id`) when locking multiple rows (pareador, batch)
- [ ] **Transaction Boundaries**:
  - [ ] Ingestão export: 1 tx (`mensagens` + `versoes` + `uploads` + `cache`)
  - [ ] Coleta casa: 1 tx (`coletas_casa` + `eventos` + `apostas` + pareador)
  - [ ] Marcar resultado: 1 tx (`eventos` + `apostas` + `movimentos`)
  - [ ] Pareador dúvida: 1 tx (both `apostas` rows)
  - [ ] Planilhar: 1 tx per bet; `SAVEPOINT` every 50; `COMMIT` each
- [ ] **Idempotency**: All external writes use natural keys:
  - [ ] Export: `UNIQUE (usuario_id, chat_id, message_id, ordem_na_mensagem)` on `apostas`
  - [ ] Print: `UNIQUE (usuario_id, midia_hash, ordem)` partial index
  - [ ] Manual: `UNIQUE (usuario_id, chave)` where `chave` = UUID
  - [ ] Planilha: `UNIQUE (usuario_id, linha_hash)` 
  - [ ] Coleta casa: `UNIQUE (usuario_id, casa, identidade)` + `hash_conteudo` for `open→settled`
  - [ ] Pattern: `INSERT ... ON CONFLICT (pk) DO UPDATE SET ... WHERE excluded.atualizada_em > table.atualizada_em`
- [ ] **Idempotency Tests**: `tests/test_idempotency.py` covering duplicate re-send for all 5 origins + coleta casa
- [ ] **Deadlock Monitoring**: `log_lock_waits = on` + CloudWatch alert on `deadlocks` metric
- [ ] **Code Review Checklist**: "Toda mutação financeira usa `FOR UPDATE ORDER BY id`?"

## Implementation Patterns

### SQLAlchemy Pessimistic Lock
```python
# In repository methods
async def get_aposta_for_update(session: AsyncSession, aposta_id: int) -> Aposta:
    result = await session.execute(
        select(Aposta).where(Aposta.id == aposta_id).with_for_update(of=Aposta, nowait=False)
    )
    return result.scalar_one()


# Pareador: lock both bets in consistent order
async def create_duvida_de_par(
    session: AsyncSession, aposta_nova_id: int, aposta_existente_id: int
):
    ids = sorted([aposta_nova_id, aposta_existente_id])
    result = await session.execute(
        select(Aposta)
        .where(Aposta.id.in_(ids))
        .with_for_update(of=Aposta, nowait=False)
        .order_by(Aposta.id)
    )
    apostas = result.scalars().all()
    # ... logic
```

### Planilhar Batch Transaction
```python
async def processar_lote(session: AsyncSession, apostas_brutas: list[ApostaBruta]):
    for i, bruta in enumerate(apostas_brutas):
        async with session.begin():  # 1 tx per bet
            await materializar_aposta(session, bruta)
        if i % 50 == 0:
            await session.commit()  # flush savepoint, release locks
```

### Idempotent Upsert Pattern
```python
# In repositorio.py
async def upsert_aposta(session: AsyncSession, aposta: Aposta) -> Aposta:
    stmt = (
        pg_insert(Aposta)
        .values(**aposta.to_dict())
        .on_conflict_do_update(
            index_elements=["usuario_id", "chat_id", "message_id", "ordem_na_mensagem"],
            set_={k: v for k, v in aposta.to_dict().items() if k not in IMMUTABLE_FIELDS},
            where=pg_insert(Aposta).excluded.atualizada_em > Aposta.atualizada_em,
        )
        .returning(Aposta)
    )
    result = await session.execute(stmt)
    return result.scalar_one()
```

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** Deadlock frequency > 1/day, or sharding implemented (ADR-006), or Serializable needed for audit
- **Related ADRs:** ADR-001 (QARs), ADR-002 (Data Model), ADR-003 (Storage), ADR-006 (Partitioning/Sharding), ADR-010 (Batch Pipeline), ADR-011 (Stream Processing)
- **Open Items:** Exact `IMMUTABLE_FIELDS` list for idempotent upsert; `nowait=False` vs `nowait=True` (timeout vs error)

## References

- DDIA Ch 7: Transactions (ACID, isolation levels, locking, deadlocks)
- PostgreSQL 16 Docs: `SELECT ... FOR UPDATE`, `READ COMMITTED`, `SAVEPOINT`
- SQLAlchemy 2.x Docs: `AsyncSession.begin()`, `with_for_update()`
- `planilhador/banco/conexao.py` — current transaction context manager
- `planilhador/dominio/materializar.py` — current materialization logic
- `planilhador/dominio/cruzamento.py` — pareador logic (touches 2 bets)
- `scripts/conferir_numeros.py` — replay validation (depends on idempotency)