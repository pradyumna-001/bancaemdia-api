# ADR 004: Schema Evolution & Serialization — JSON, Implicit Versioning, Path-Based API Versioning

## Status
Proposed

## Context

Current state: **JSON everywhere** (stdlib `json` module). Schema evolution handled implicitly:

| Layer | Format | Versioning Mechanism |
|-------|--------|---------------------|
| `eventos.payload_json` | JSON (TEXT→JSONB) | `versao_prompt` in `extracoes_cache`; new event types via SQL CHECK migration |
| `coletas_casa.bruto_json` | JSON (raw from casa) | Immutable — stored byte-for-byte; `contrato: 1` in payload |
| IA Request/Response | Pydantic (`extracao/modelos.py`) → JSON | `versao_prompt` in cache; models evolve with prompt changes |
| Web API (HTMX + JSON) | HTML fragments + JSON | Path-based (`/api/v1/...`) — *decided* |
| Extensão ↔ Site | JSON | `contrato: 1` in body — *evolution TBD* |
| Export Telegram | JSON (Telegram Desktop) | Parser in `planilhador/export/leitor.py` |
| Excel Export | openpyxl → .xlsx | Fixed columns in `exportacao/colunas.py` |

**Trigger:** Migration to PostgreSQL + SQLAlchemy + multi-tenant SaaS. Need explicit evolution strategy before schema changes accumulate.

**Key Constraint:** "Vou mudar completamente o jeito de criar prompts e interagir com llm via api" — IA interaction layer will be redesigned. Current `versao_prompt` + cache strategy may need replacement.

**Open Items (require confirmation with project creator):**
- Backward compatibility policy for `eventos` consumers (replay, projection)
- Extensão contrato evolution strategy (rolling deploy, breaking vs additive)
- Future microservices serialization (Avro/Protobuf vs JSON)

## Decision

We adopt **JSON with implicit versioning** for all current layers, **path-based API versioning** (`/api/v1/`), **SQL migrations + defensive code** for schema evolution, and **stdlib `json`** for serialization (upgrade to `orjson` only if profiling proves bottleneck). Event schema registry deferred; IA interaction redesign will define new versioning.

## Consequences

### Positive
- **Zero new dependencies** for serialization; stdlib `json` sufficient for launch scale
- **Path-based API versioning** — simple, visible, cache-friendly (CDN), works with HTMX
- **Implicit event versioning** — `versao_prompt` + `tipo` CHECK constraint + defensive `.get()` in projection matches current working pattern
- **Raw JSON preservation** — `coletas_casa.bruto_json`, `mensagens.bruto_json`, `extracoes_cache.resultado_json` enable future replay/reprocessing
- **Pydantic for IA boundary** — type safety at API boundary; models in `extracao/modelos.py` versioned with prompt

### Negative
- **No formal schema registry** — breaking changes detected only at runtime (tests/consumer failure)
- **Backward compatibility burden on consumers** — projection/replay must handle missing/extra fields gracefully
- **Extensão contrato evolution undefined** — risk of breaking extension updates without coordination
- **IA interaction redesign pending** — current `versao_prompt` strategy may be replaced entirely

### Neutral
- **Future microservices** — Avro/Protobuf evaluated when service boundaries solidify (ADR-012)
- **Database migrations** — Alembic handles relational schema; event payload evolution = migration + code compat
- **Excel/Telegram formats** — external, not controlled; parsers adapt as needed

## Options Considered

### Option A: Formal Schema Registry (Confluent/Apicurio) — *Rejected for Launch*
**Pros:** Enforced compatibility, documentation, code generation
**Cons:** Operational overhead; overkill for single-producer (our code) + single-consumer (projection/replay) event log

### Option B: JSON + Implicit Versioning (Current) — *Chosen for Launch*
**Pros:** Zero ops, matches existing working pattern, `versao_prompt` already tracks IA model evolution
**Cons:** Discipline required; breaking changes silent until consumer fails

### Option C: Avro/Protobuf for Events — *Rejected for Launch*
**Pros:** Schema evolution guarantees, compact, code gen
**Cons:** Extra tooling; JSONB in PG already indexed/queryable; no polyglot consumers yet

### Option D: Header-based API Versioning — *Rejected*
**Pros:** Cleaner URLs
**Cons:** Harder to debug, cache, test with curl/browser; HTMX works naturally with path versioning

## Compliance

- [ ] **Serialization**: stdlib `json` for all layers; `orjson` added only if profiling shows >5% CPU in JSON encode/decode
- [ ] **API Versioning**: All new endpoints under `/api/v1/`; legacy paths redirected or deprecated
- [ ] **Event Payload Evolution**:
  - [ ] New `eventos.tipo` values added via migration (ALTER CHECK) — *never remove*
  - [ ] New fields in `payload_json` are optional; projection uses `.get(field, default)`
  - [ ] `versao_prompt` remains in `extracoes_cache` and `chamadas_ia` for IA cost tracking
- [ ] **Extensão Contrato**: Document evolution strategy before v2 (see Open Items)
- [ ] **Pydantic Models**: `extracao/modelos.py` source of truth for IA I/O; versioned with prompt changes
- [ ] **Database Migrations**: Alembic for relational schema; baseline = current 28 migrations
- [ ] **Backward Compatibility**: Projection (`projecao.reconstruir()`) and replay scripts handle missing fields; integration tests cover old event formats
- [ ] **Raw JSON Preservation**: Never strip fields from `bruto_json`, `payload_json`, `resultado_json` — only add
- [ ] **Documentation**: Event types documented in `docs/events/` (auto-generated from Pydantic + SQL CHECK)

## Open Items (Confirm with Creator)

1. **Backward Compatibility Policy**: How long must projection support old event formats? (Forever? Major version?)
2. **Extensão Contrato Evolution**: 
   - Additive only (`contrato: 1` → `contrato: 1` with new optional fields)?
   - Breaking (`contrato: 2`) requires extension update + site supports both during rollout?
   - Version negotiation header?
3. **IA Interaction Redesign**: New prompt/LLM architecture will replace `versao_prompt` — define new versioning before implementation.

## Notes

- **Review date:** 2026-11-01 (post-launch) or when IA interaction redesign lands
- **Trigger for revisit:** IA interaction redesign complete, or first microservice extracted, or schema registry needed
- **Related ADRs:** ADR-002 (Data Model), ADR-003 (Storage), ADR-010 (Batch Pipeline), ADR-011 (Stream Processing), ADR-012 (Evolvability)
- **Migration Path:** SQLite JSON TEXT → PG JSONB (transparent); Alembic manages relational changes; event payload evolves in place

## References

- DDIA Ch 4: Encoding & Evolution (JSON, Avro, Protobuf, schema registry)
- `planilhador/extracao/modelos.py` — Pydantic models for IA I/O
- `planilhador/extracao/cache.py` — `versao_prompt` usage
- `planilhador/banco/migracoes/018_coleta_direta_das_casas.sql` — `contrato` in `coletas_casa`
- `planilhador/export/leitor.py` — Telegram export parser
- `planilhador/exportacao/colunas.py` — Excel column definitions
- `extensao/envio.js` — Extension payload construction (`contrato: 1`)