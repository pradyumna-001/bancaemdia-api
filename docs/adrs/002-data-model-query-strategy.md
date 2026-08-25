# ADR 002: Data Model & Query Strategy — Relational Model, SQLAlchemy ORM, Shared Schema with RLS

## Status
Proposed

## Context

The project currently uses **SQLite** with **raw SQL** (`repositorio.py`) and **28 migrations** (`planilhador/banco/migracoes/001_inicial.sql` → `028_duvida_de_par.sql`). The schema implements:

**Core Entities (28 migrations):**
- **Shared (Canal)**: `chats`, `mensagens`, `mensagem_versoes`, `midias`, `extracoes_cache`, `chamadas_ia`
- **Canonical**: `casas`, `esportes`, `competicoes`, `tipsters`, `times`, `mercados`, `apelidos` (normalização com confirmação humana)
- **Per-User**: `usuarios`, `bancas`, `contas_casa`, `unidades` (temporal: vigente_de/ate), `movimentos` (append-only caixa), `apostas` (projeção), `eventos` (append-only log), `revisao_pendente`
- **Coleta Casa (Etapa 12-bis)**: `coletas_casa` (raw JSON + dedup por `identidade`), `coleta_token` (segurança extensão)

**Key Design Decisions Already Implemented:**
- **Dinheiro em centavos (inteiro)** — nunca float/decimal (precisão exata)
- **STRICT tables** — SQLite rejeita tipo errado na hora
- **Append-only log** (`eventos`, `movimentos`, `mensagem_versoes`, `coletas_casa`) — gatilhos impedem UPDATE/DELETE
- **Temporal tables**: `unidades` (valor por período), `contas_casa` (desde/ate) — aposta de janeiro usa unidade de janeiro
- **Freebet modeling**: `stake_centavos = 0` + `valor_aposta_centavos` = valor facial → `lucro = retorno - stake` universal
- **5 origens de aposta**: `telegram`, `print`, `manual`, `planilha`, `casa` (CHECK constraint)
- **Seleção invertida**: `selecionada` default 1, usuário desmarca exceções
- **Dedup coleta casa**: UNIQUE `(usuario_id, casa, identidade)` + `hash_conteudo` para `open → settled`

**Trigger for this ADR:** Migration to **PostgreSQL** + **SQLAlchemy ORM** + **Multi-tenant SaaS** (50 users launch, growth after). Need explicit data model strategy before storage decision (ADR-003).

**Open Items (require confirmation with project creator):**
- Complete entity-relationship validation
- Near-real-time dashboard latency target (<30s acceptable per discussion, needs confirmation)
- Cross-tenant analytics requirements (affects RLS vs schema-per-tenant)

## Decision

We adopt a **relational data model on PostgreSQL** with **SQLAlchemy 2.x (async)** ORM, **shared schema with Row-Level Security (RLS)** for multi-tenancy, and **read replica + materialized views** for analytical queries (dashboard/painel).

## Consequences

### Positive
- **ACID guarantees** for financial data (movimentos + eventos + apostas atômicos)
- **Referential integrity** via FKs — dinheiro nunca órfão
- **SQLAlchemy 2.x async** — type-safe queries, migration tooling (Alembic), connection pooling
- **RLS** — single pool, single migration, tenant isolation enforced by DB
- **Materialized views** — dashboard aggregates refreshed every 5min, near-real-time without OLTP impact
- **JSONB** — `eventos.payload_json`, `coletas_casa.bruto_json` keep flexibility for raw payloads
- **Proven schema** — 28 migrations battle-tested, replay-verified (`conferir_numeros.py`)

### Negative
- **Write throughput** — PostgreSQL single-primary limits horizontal write scale (mitigated by async queue for extraction)
- **RLS complexity** — policies must be tested; leak = data exposure bug
- **Migration effort** — SQLite → PG requires data migration + index tuning + trigger port
- **ORM learning curve** — team (AI-assisted) must adopt SQLAlchemy patterns

### Neutral
- **Read models** — `apostas` remains materialized view (projection from `eventos`); additional MVs for painel
- **Temporal queries** — `unidades`, `contas_casa` use valid-time ranges; PG range types or `WHERE vigente_de <= X AND (vigente_ate IS NULL OR vigente_ate > X)`
- **Event store** — `eventos` append-only log stays; replay via `projecao.reconstruir()` unchanged

## Options Considered

### Option A: Stay SQLite + Raw SQL — *Rejected*
**Pros:** Zero migration effort; works for 50 users
**Cons:** Single-writer bottleneck; no RLS; no read replicas; no connection pooling; no managed backup/PITR on AWS

### Option B: PostgreSQL + Raw SQL — *Rejected*
**Pros:** PG features without ORM overhead
**Cons:** Manual query building error-prone; no type safety; Alembic still needed for migrations; connection pooling manual

### Option C: PostgreSQL + SQLAlchemy 2.x (async) + RLS — *Chosen*
**Pros:** Type-safe, async, Alembic migrations, connection pooling, RLS built-in, AWS RDS managed
**Cons:** Migration effort; ORM abstraction leak risks

### Option D: Schema-per-tenant (PostgreSQL) — *Rejected*
**Pros:** Strong isolation; custom schemas per tenant
**Cons:** N× migrations; N× connection pools; cross-tenant analytics impossible; operational burden >50 tenants

### Option E: NoSQL (DynamoDB/Firestore) — *Rejected*
**Pros:** Horizontal scale
**Cons:** No ACID transactions for money; no joins for painel; temporal queries hard; team has no NoSQL ops experience

## Compliance

- [ ] **Entity model** matches current 28 migrations (see Appendix A)
- [ ] **SQLAlchemy 2.x async** models defined for all tables
- [ ] **Alembic** configured; baseline = current 28 migrations
- [ ] **RLS policies** created: `CREATE POLICY` on all per-user tables using `current_setting('app.current_user_id')::int`
- [ ] **Connection pool** (asyncpg via SQLAlchemy): `pool_size=10`, `max_overflow=20`, `pool_pre_ping=True`
- [ ] **Read replica** configured on AWS RDS; materialized views refreshed via `pg_cron` every 5min
- [ ] **JSONB** columns for `eventos.payload_json`, `coletas_casa.bruto_json`, `extracoes_cache.resultado_json`
- [ ] **Indexes** ported from SQLite (see Appendix B) + PG-specific (partial indexes for `revisao_grave`, `duvida_de_par`)
- [ ] **Append-only triggers** ported to PG: `BEFORE UPDATE/DELETE ON eventos ... RAISE EXCEPTION`
- [ ] **CHECK constraints** ported: `freebet = 0 OR stake_centavos = 0`, `stake_centavos >= 0`, enums via `CHECK`
- [ ] **Cross-tenant analytics**: decision documented — if needed later, add `analytics` role with `BYPASSRLS`
- [ ] **Dashboard latency**: materialized views refreshed ≤5min; P95 < 30s confirmed with creator
- [ ] **Migration rehearsal**: `pgloader` or custom script tested on staging copy; `conferir_numeros.py` passes post-migration

## Appendix A: Entity-Relationship Summary (to validate with creator)

```
Usuario 1──< Banca
Usuario 1──< ContaCasa >──1 Casa
Usuario 1──< Unidade (temporal: vigente_de/ate)
Usuario 1──< Movimento (append-only caixa)
Usuario 1──< Aposta (projeção, reconstrói de Eventos)
Usuario 1──< Evento (append-only log, fonte: export|ia|manual|liquidacao|planilha|casa)
Usuario 1──< RevisaoPendente
Usuario 1──< ColetaCasa (raw JSON, dedup: UNIQUE(usuario_id, casa, identidade))
Usuario 1──< ColetaToken (1 vivo por usuario)

Aposta >──1 Tipster (via apelidos confirmados)
Aposta >──1 ContaCasa (onde dinheiro entrou/saiu)
Aposta >──1 Time (casa/fora, via apelidos)
Aposta >──1 Mercado (familia: GOLS|CARTOES|ESCANTEIOS|RESULTADO|HANDICAP|JOGADOR|AMBAS_MARCAM|OUTRO)
Aposta >──1 Competicao >──1 Esporte

Evento >──1 Aposta (via aposta_chave estável, não id)
Movimento >──1 ContaCasa (NULL = movimento banca)

ColetaCasa: identidade = id do bilhete na casa; hash_conteudo detecta open→settled
```

## Appendix B: Key Indexes (from migrations)

| Table | Indexes |
|-------|---------|
| `apostas` | `usuario_id, criada_em`; `usuario_id, estado`; `banca_id, criada_em`; `usuario_id, origem`; `chat_id, message_id`; `mercado_id`; `time_casa_id`; `usuario_id, competicao_id`; `usuario_id, data_aposta`; `usuario_id, duplicada_de`; `usuario_id, revisao_grave` (partial); `usuario_id, chave` (unique partial) |
| `eventos` | `usuario_id, criado_em`; `usuario_id, aposta_chave, id` |
| `movimentos` | `usuario_id, ocorrido_em`; `conta_casa_id, ocorrido_em` |
| `coletas_casa` | `usuario_id, casa`; `usuario_id, casa, identidade` (unique) |
| `mensagens` | `data`; `autor_bruto`; `midia_hash` |
| `extracoes_cache` | PK `(chat_id, message_id, versao_prompt)` |

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** User count > 200, or RLS leak incident, or analytics cross-tenant requirement
- **Related ADRs:** ADR-001 (QARs), ADR-003 (Storage Engine), ADR-007 (Transactions), ADR-010 (Batch Pipeline)
- **Migration path:** SQLite → PG via `pgloader` + manual fixups; run `conferir_numeros.py` on both pre/post; baseline Alembic at migration 028

## References

- DDIA Ch 2: Data Models & Query Languages
- DDIA Ch 6: Partitioning (multi-tenancy strategies)
- `planilhador/banco/migracoes/` — 28 migrations (source of truth)
- `planilhador/dominio/projecao.py` — replay logic (`reconstruir()`)
- `scripts/conferir_numeros.py` — data integrity verification (5 banks replay)
- AGENTS.md §5 (modelo financeiro), §6-bis (3 camadas nome), §7 (append-only log)
- COLETA.md §3 (contrato envio), §5 (dedup), §6 (pareador)