# ADR 012: Evolvability & Extensibility — Feature Flags, Schema Migration, Deploy Safety

## Status
Proposed

## Context

Current state: **Monolithic FastAPI + Celery** with 11 ADRs defining architecture. System evolves: new betting houses, new prompt versions, new markets, new APIs.

**DDIA Ch 12 themes**: Evolvability, schema migration, deployment, operational simplicity.

**Key evolution vectors for this project:**
| Vector | Frequency | Risk |
|--------|-----------|------|
| **Nova casa de apostas** | ~1/mês | Extensão + parser + coleta + pareador |
| **Nova versão de prompt** | ~1/semana | Extração muda → reprocessamento |
| **Novo mercado/tipo aposta** | ~1/quinzena | Normalização + conferências + liquidação |
| **Mudança API Anthropic** | ~1/trimestre | Cliente + modelos + custos |
| **Mudança site casa** | Imprevisível | Extensão quebra → coleta para |

---

## Decision

We adopt **feature flags** for gradual rollouts, **expand-only schema migrations** with backward compatibility, **blue-green deploy** via AWS, **contract testing** for external APIs, and **observability-driven development** (metrics before features).

---

## Consequences

### Positive
- **Safe rollouts**: Feature flags = kill switch per tenant/percentage
- **Zero-downtime deploys**: Blue-green + expand-only migrations
- **External API resilience**: Contract tests catch breaking changes early
- **Debuggability**: Metrics/logs/traces before code = faster MTTR

### Negative
- **Feature flag debt**: Flags accumulate; cleanup process needed
- **Migration discipline**: Expand-only requires more steps (add → migrate → remove)
- **Contract test maintenance**: One test per external API endpoint

### Neutral
- **Monolith first**: Modular monolith (clear boundaries) → extract services later if needed
- **Schema registry**: Deferred (ADR-004); Pydantic + SQL migrations sufficient

---

## Feature Flags

### Implementation
```python
# config/flags.py
from functools import lru_cache


class FeatureFlags:
    # Rollout: percentual de usuários (0-100)
    NOVA_CASA_BET365 = "nova_casa_bet365"  # 0 → 10 → 50 → 100
    PROMPT_V3_EXTRACAO = "prompt_v3_extracao"  # canary por tenant
    MERCADO_JOGADOR_FALTAS = "mercado_jogador_faltas"
    LIQUIDACAO_AUTOMATICA = "liquidacao_automatica"

    @classmethod
    @lru_cache(maxsize=128)
    def is_enabled(cls, flag: str, usuario_id: int = None) -> bool:
        if usuario_id:
            # Verifica override por usuário (canary)
            override = redis.get(f"flag:{flag}:user:{usuario_id}")
            if override is not None:
                return override == "1"

        # Percentual global
        pct = int(redis.get(f"flag:{flag}:pct") or "0")
        if pct >= 100:
            return True
        if pct <= 0:
            return False
        # Deterministic hash por usuario_id
        return (hash(f"{flag}:{usuario_id}") % 100) < pct


# Uso no código
if FeatureFlags.is_enabled("prompt_v3_extracao", usuario_id):
    resultado = await extrair_com_prompt_v3(foto)
else:
    resultado = await extrair_com_prompt_v2(foto)
```

### Governança
- [ ] Flags em `config/flags.py` (single source of truth)
- [ ] Admin UI: `/admin/flags` (CRUD + percentual + user override)
- [ ] Limpeza: Flag > 100% por 30 dias = remover (code review checklist)
- [ ] Métrica: `feature_flag_evaluation{flag,result}` (Counter)

---

## Schema Migrations — Expand-Only Pattern

### Princípio
**Nunca remover coluna/tabela em migração única**. Sempre:
1. **Expand**: Add nova coluna/tabela (nullable, default)
2. **Migrate**: Backfill dados (async job, não na migração)
3. **Switch**: Código usa nova coluna
4. **Contract** (opcional, depois): Remove coluna velha

### Exemplo: Renomear `apostas.odd` → `odd_atual`
```sql
-- Migração 1: Expand
ALTER TABLE apostas ADD COLUMN odd_atual REAL;
-- Backfill (job assíncrono, não na migração)
UPDATE apostas SET odd_atual = odd WHERE odd_atual IS NULL;

-- Migração 2: Switch (código já lê odd_atual)
-- CREATE INDEX idx_apostas_odd_atual ON apostas (odd_atual);

-- Migração 3 (futuro): Contract
-- ALTER TABLE apostas DROP COLUMN odd;
```

### Regras
- [ ] **Nunca** `DROP COLUMN` / `DROP TABLE` em migração que deploya código
- [ ] **Sempre** `nullable` + `default` em `ADD COLUMN`
- [ ] **Backfill** = job separado (Celery), não `UPDATE` na migração (locks)
- [ ] **Índices** = `CONCURRENTLY` (PG) ou `CREATE INDEX` fora de transação
- [ ] **Alembic** = baseline na migração 28 atual; novas migrações seguem padrão

---

## Deploy — Blue-Green na AWS

### Arquitetura
```
                    ┌─────────────┐
                    │  ALB (AWS)  │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    ┌──────────┐     ┌──────────┐     ┌──────────┐
    │  Blue    │     │  Green   │     │  Canary  │
    │  (v1)    │     │  (v2)    │     │  (10%)   │
    └──────────┘     └──────────┘     └──────────┘
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                    ┌─────────────┐
                    │  RDS PG     │
                    │  (shared)   │
                    └─────────────┘
```

### Pipeline (GitHub Actions)
```yaml
# .github/workflows/deploy.yml
deploy:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    
    - name: Build & Test
      run: |
        docker build -t bancaemdia:${{ github.sha }} .
        docker run --rm bancaemdia:${{ github.sha }} pytest -n 8
    
    - name: Deploy to Green (staging)
      run: |
        aws ecs update-service --cluster prod --service green \
          --task-definition bancaemdia:${{ github.sha }}
    
    - name: Smoke Tests
      run: |
        sleep 30
        curl -f https://green.bancaemdia.com/health
        pytest tests/test_site_esqueleto.py -v
    
    - name: Canary (10% traffic)
      run: |
        aws alb modify-listener --listener-arn $ALB_LISTENER \
          --default-actions Type=forward,TargetGroupArn=$GREEN_TG,Weight=10 \
          Type=forward,TargetGroupArn=$BLUE_TG,Weight=90
    
    - name: Monitor (10min)
      run: |
        # Custom action: watch CloudWatch error rate, latency
        ./scripts/monitor_canary.sh 600
    
    - name: Promote or Rollback
      run: |
        if [ $CANARY_OK = true ]; then
          aws alb modify-listener --listener-arn $ALB_LISTENER \
            --default-actions Type=forward,TargetGroupArn=$GREEN_TG,Weight=100
          # Swap Blue/Green labels
        else
          aws alb modify-listener --listener-arn $ALB_LISTENER \
            --default-actions Type=forward,TargetGroupArn=$BLUE_TG,Weight=100
        fi
```

### Requisitos
- [ ] **Health checks**: `/health` (liveness) + `/ready` (readiness) < 2s
- [ ] **Migration first**: Deploy migração (expand-only) → espera → deploy código
- [ ] **Rollback < 5min**: ALB weight switch instantâneo
- [ ] **Database compat**: Código v1 e v2 rodam simultaneamente no mesmo schema (expand-only)

---

## Contract Testing — APIs Externas

### Alvos
| API | Contrato | Frequência Teste |
|-----|----------|------------------|
| **Anthropic** | Request/Response schema | Daily (CI) + on deploy |
| **Betano (coleta)** | JSON response schema | Daily (cron) + on extensão update |
| **Sofascore (liquidação)** | Event schema | Daily |
| **Email (SES/SendGrid)** | Template variables | On deploy |

### Implementação (Pact ou Custom)
```python
# tests/contract/test_anthropic.py
import pytest
from pact import Consumer, Provider

pact = Consumer("bancaemdia-extracao").has_pact_with(
    Provider("anthropic-api"), host_name="api.anthropic.com", port=443
)


def test_extracao_request_response():
    expected_request = {
        "model": "claude-3-haiku-20240307",
        "messages": [{"role": "user", "content": Like("[IMAGE]...")}],
        "max_tokens": 4096,
    }
    expected_response = {
        "content": [{"type": "text", "text": Like('{"odd": 1.95, ...}')}],
        "usage": {"input_tokens": Like(100), "output_tokens": Like(50)},
    }

    (
        pact
        .given("valid API key")
        .upon_receiving("extraction request")
        .with_request("POST", "/v1/messages", body=expected_request)
        .will_respond_with(200, body=expected_response)
    )

    with pact:
        resultado = await chamar_anthropic(foto_bytes)
        assert "odd" in resultado
```

### Requisitos
- [ ] Pact broker (ou arquivo local) para versionamento
- [ ] CI roda contract tests **antes** de deploy
- [ ] Falha no contract = bloqueia deploy (breaking change detectado)

---

## Observability-Driven Development

### Princípio
**Métrica antes do código**. Para toda feature nova:
1. Definir **SLI/SLO** (latência, erro, throughput)
2. Criar **dashboard** (Grafana/CloudWatch)
3. Criar **alertas** (pager)
4. **Só então** implementar

### Checklist por Feature
```markdown
## Feature: Nova Casa X

### SLIs
- [ ] Latência coleta casa X: P95 < 5s
- [ ] Taxa sucesso coleta: > 99%
- [ ] Custo extração casa X: < $0.01/bet

### Dashboard
- [ ] Painel "Coleta Casa X" no Grafana
- [ ] Variáveis: casa, usuario_id, status

### Alertas
- [ ] `coleta_sucesso_rate{casa="X"} < 0.95` por 5min → PagerDuty
- [ ] `coleta_latencia_p95{casa="X"} > 10s` por 5min → Slack

### Logs
- [ ] Structured log: `casa`, `identidade`, `status`, `latencia_ms`
- [ ] Correlation ID: `job_id` propagado

### Testes
- [ ] Contract test (Betano API)
- [ ] Integration test: upload → extração → materialização → painel
- [ ] Chaos test: API casa X down → circuit breaker abre → revisao_pendente
```

---

## Modular Monolith → Future Extraction

### Boundaries (já implícitos no código)
| Módulo | Responsabilidade | Futuro Serviço? |
|--------|------------------|-----------------|
| `extracao/` | IA + OCR + cache | **Sim** (stateless, CPU-intensive) |
| `coleta/` | Parsers casas + pareador | **Talvez** (complexo, stateful) |
| `projecao/` | Replay + agregações | **Não** (core financeiro) |
| `web/` | API + HTMX | **Não** (frontend) |

### Critérios de Extração
- [ ] Escala independente (extracao = CPU, projecao = IO)
- [ ] Deploy independente (prompt vN não precisa deploy projecao)
- [ ] Equipe dedicada (solo dev = não extrair)

---

## Compliance

- [ ] **Feature Flags**: `config/flags.py` + Admin UI + métrica `feature_flag_evaluation`
- [ ] **Migrations**: Expand-only pattern; backfill via Celery job; `DROP` só após 30 dias
- [ ] **Deploy**: Blue-Green via ALB; canary 10% 10min; rollback < 5min; health checks
- [ ] **Contract Tests**: Anthropic + Betano + Sofascore; Pact broker; block deploy on failure
- [ ] **Observability**: Dashboard + alertas antes do código; structured logs com correlation ID
- [ ] **Modular Boundaries**: `extracao/` isolado (futuro serviço); interfaces tipadas

## Notes

- **Review date:** 2026-11-01 (post-launch)
- **Trigger for revisit:** First service extraction, feature flag debt > 20, migration rollback needed, contract test false positive
- **Related ADRs:** All previous (this ADR governs evolution of all)

## References

- DDIA Ch 12: The Future of Data Systems (evolvability, schema migration, deployment)
- "Building Evolutionary Architectures" (Ford, Parsons, Kua)
- AWS Blue/Green Deploy: https://docs.aws.amazon.com/whitepapers/latest/blue-green-deployments/
- Pact: https://docs.pact.io/
- `planilhador/banco/migracoes/` — current migrations (28, expand-only pattern already used)
- `scripts/publicar.py` — current deploy script (to be replaced by GitHub Actions)