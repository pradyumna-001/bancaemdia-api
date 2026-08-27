# Technical Review — Planilhador de Apostas

**Preparado para:** Review com criador do projeto  
**Data:** 2026-08-24  
**Autor:** Senior Software Engineer (análise baseada em 12 ADRs + código atual)

---

## 1. Resumo Executivo

| Aspecto | Status Atual | Risco |
|---------|--------------|-------|
| **Produto** | Funciona (3.145 testes verdes, 49 users em produção) | ✅ Validado |
| **Arquitetura** | Monolito Python + SQLite, sincronismo total | 🔴 Crítico |
| **Segurança** | Chaves API em `.env`, sem rate limit, sem circuit breaker | 🔴 Crítico |
| **Escalabilidade** | SQLite single-file, sem pool, sem async | 🔴 Crítico |
| **Manutenibilidade** | `app.py` 275KB, `coleta.py` 159KB, 50+ docs dispersos | 🟡 Alto |
| **Observabilidade** | Zero (apenas `conferir_numeros.py`) | 🔴 Crítico |
| **Deploy** | `publicar.py` manual, sem rollback, sem CI/CD | 🔴 Crítico |

**Conclusão:** O produto **valida o mercado** (users reais, dinheiro real), mas a **base técnica não suporta crescimento**. Precisa refatoração fundamental antes de escalar.

---

## 2. Falhas Estruturais & Arquiteturais

### 2.1 Monolito Inchado (Single Process, Single Thread)

```
┌─────────────────────────────────────────────────────────────┐
│                    PROCESSO ÚNICO                           │
├─────────────────────────────────────────────────────────────┤
│  FastAPI (web)                                              │
│  ├── BackgroundTasks (extração IA, síncrono)               │
│  ├── BackgroundTasks (coleta casa, síncrono)               │
│  ├── BackgroundTasks (planilhar, síncrono)                 │
│  └── SQLite (conexão única, bloqueante)                    │
└─────────────────────────────────────────────────────────────┘
```

**Problemas:**
- Extração IA (segundos/bilhete) **bloqueia web workers**
- `planilhar.py` processa 10k bets → **horas de bloqueio**
- Um crash derruba **tudo** (web + workers + DB)
- Zero horizontal scaling

### 2.2 Arquivos "God Classes" (Violação SRP)

| Arquivo | Linhas | Responsabilidades | Deveria Ser |
|---------|--------|-------------------|-------------|
| `planilhador/web/app.py` | 5.381 | Rotas, auth, templates, static, middleware, business logic | **Múltiplos routers** |
| `planilhador/dominio/coleta.py` | ~4.000 | Ingestão, validação, fila revisão, importação, pareador | **Múltiplos módulos** |
| `planilhador/extracao/rodada.py` | ~1.000 | Orquestração extração, conferências, cache, escalonamento | **Serviços separados** |
| `planilhador/dominio/materializar.py` | ~1.200 | Releitura, escolha casa, criação aposta, eventos | **Múltiplos serviços** |

### 2.3 Acoplamento Temporal Espalhado (Violação DRY)

Regras de temporalidade (`vigente_de/ate`, `desde/ate`, `data_jogo`) implementadas em **7+ locais**:
- `unidades.py`, `contas_casa` (migração), `caixa.py`, `projecao.py`, `materializar.py`, `filtros.py`, `painel.py`

**Resultado:** Bugs silenciosos quando regra muda (já aconteceu 3x documentado no `ARMADILHAS.md`).

### 2.4 Dual Write Implícito (Inconsistência Garantida)

```
eventos (append-only)  ←──┐
                          ├──→ apostas (projeção)  ← DIVERGEM em produção
apostas (CRUD direto)  ←──┘
```

- `apostas` é **projeção** mas também recebe writes diretos
- `conferir_numeros.py` detecta divergência mas **não previne**
- Replay (`reconstruir()`) corrige mas **dado inconsistente existe no interim**

### 2.5 Configuração Espalhada & Secrets

- `config.py` + `.env` + `requirements.txt` + `requirements-dev.txt` + `pyproject.toml` + `config_do_servidor.py`
- Chave Anthropic em `.env` (commitado no histórico?)
- Tetos de custo duplicados em 2 locais (`cliente.py` + `lote_ia.py`)

---

## 3. Falhas de Segurança

| Vulnerabilidade | Impacto | Status |
|-----------------|---------|--------|
| **Sem rate limiting** | DoS, abuso Anthropic ($), enumeração usuários | 🔴 Crítico |
| **Sem circuit breaker** | Falha cascata Anthropic → app todo para | 🔴 Crítico |
| **Sem timeouts HTTP** | Request trava worker indefinidamente | 🔴 Crítico |
| **SQLite sem WAL forçado** | Corrupção em crash (já aconteceu) | 🟡 Alto |
| **Extensão sem assinatura robusta** | Token no header, replay attack possível | 🟡 Alto |
| **Sem audit log de ações sensíveis** | Liquidação, exclusão, pareador sem rastro | 🟡 Alto |
| **CORS/Headers não configurados** | Clickjacking, CSRF em endpoints mutantes | 🟡 Alto |
| **Secrets em `.env` versionado?** | Vazamento chave Anthropic | ❓ Verificar |

---

## 4. Falhas de Manutenibilidade

### 4.1 Documentação Fragmentada (50+ arquivos .md)

```
Raiz: AGENTS.md, CLAUDE.md, ESTADO.md, ARMADILHAS.md, COLETA.md, SITE.md...
ETAPAS/: 8 arquivos (12bis, 12ter, 13, beta, extensao, login, oficina, redesenho)
PLANO_*: 25+ arquivos
AUDITORIA_*: 3 arquivos
```

**Problema:** Informação contraditória, desatualizada, impossível de navegar. `ESTADO.md` diz "não leia histórico", mas histórico é onde está a verdade.

### 4.2 Testes Lentos & Sem Isolamento

- 3.145 testes em **~600s** (paralelo -n 8)
- Sem subset rápido para dev (`pytest -n 0` = 20min+)
- Fixtures criam banco real → lento
- Testes de integração misturados com unitários

### 4.3 Deploy Manual & Arriscado

```bash
# publicar.py atual
git commit → chão (testes) → publicar.py → provas de vida
```
- Sem CI/CD
- Sem staging
- Rollback = `git revert` + novo deploy (15min+)
- `publicar.py` recusa deploy se `requirements.txt` muda (já travou deploy de segurança)

### 4.4 Onboarding Impossível

- `git clone` → `pip install` → **não roda** (precisa `.env`, banco, migrações, seed)
- Sem dados de teste realistas
- Sem documentação de arquitetura (até estes ADRs)

---

## 5. Falhas de Escalabilidade

| Componente | Limite Atual | Projetado (50 users) | Gap |
|------------|--------------|----------------------|-----|
| **SQLite** | 1 writer, ~100 ops/s | 10k bets/dia = 100 writes/s + reads | 🔴 1000x |
| **Extração IA** | Síncrono, 1 worker | 12k extrações/dia (cache miss) | 🔴 Serial |
| **Web workers** | 1 processo | 50 users concurrentes | 🔴 Single-thread |
| **Background jobs** | BackgroundTasks (memória) | Jobs longos, retry, scheduling | 🔴 Sem persistência |
| **Cache extração** | Arquivo local (`dados/`) | Compartilhado multi-worker | 🔴 Não funciona |
| **Rate limiting** | Zero | 50 users × 10 req/min | 🔴 Inexistente |
| **Observabilidade** | Zero | Debugging produção impossível | 🔴 Cego |

---

## 6. Proposta de Divisão: Backend / Frontend / Database

### 6.1 Arquitetura Alvo (Pós-ADRs)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AWS VPC                                        │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                 │
│  │   ALB        │    │   ALB        │    │   CloudFront │                 │
│  │  (API)       │    │  (Static)    │    │  (Frontend)  │                 │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘                 │
│         │                   │                   │                          │
│  ┌──────▼───────────────────▼───────────────────▼──────────┐             │
│  │                  ECS Fargate Services                    │             │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐       │             │
│  │  │  API    │ │Extract  │ │ Mater.  │ │ Backgr. │       │             │
│  │  │ Service │ │ Workers │ │ Workers │ │ Workers │       │             │
│  │  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘       │             │
│  └───────│───────────│───────────│───────────│────────────┘             │
│          │           │           │           │                          │
│  ┌───────▼───────────▼───────────▼───────────▼────────────┐             │
│  │                    DATA LAYER                            │             │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │             │
│  │  │  RDS PG     │  │  ElastiCache│  │  S3         │     │             │
│  │  │  (Primary)  │  │  Redis      │  │  (Exports,  │     │             │
│  │  │  + Replica  │  │  (Streams,  │  │   Photos,   │     │             │
│  │  │             │  │   Cache,    │  │   Excels)   │     │             │
│  │  │             │  │   Rate Lim) │  │             │     │             │
│  │  └─────────────┘  └─────────────┘  └─────────────┘     │             │
│  └────────────────────────────────────────────────────────┘             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Divisão de Código

```
planilhador/
├── api/                    # FastAPI Service (HTTP layer only)
│   ├── routes/
│   │   ├── apostas.py      # GET/POST /api/v1/apostas
│   │   ├── coleta.py       # POST /coleta, GET /coleta/status
│   │   ├── painel.py       # GET /api/v1/painel/*
│   │   ├── banca.py        # GET/POST /api/v1/banca/*
│   │   ├── auth.py         # Login, session, tokens
│   │   └── health.py       # /health, /ready
│   ├── middleware/
│   │   ├── auth.py         # JWT/session validation
│   │   ├── rate_limit.py   # slowapi per usuario_id
│   │   ├── observability.py # OpenTelemetry, structlog
│   │   └── routing.py      # Primary/Replica routing
│   ├── dependencies.py     # DB sessions, Redis, config
│   └── main.py             # App factory
│
├── workers/                # Celery Workers (separate deploy)
│   ├── extraction/         # OCR → Haiku → Sonnet
│   │   ├── tasks.py
│   │   ├── anthropic_client.py (circuit breaker, rate limit)
│   │   ├── ocr_client.py
│   │   └── cache.py        # Redis cache
│   ├── materialization/    # Eventos + Apostas + Pareador
│   │   ├── tasks.py
│   │   ├── repositories.py # SQLAlchemy repos
│   │   ├── pareador.py
│   │   └── projector.py    # Trigger MV refresh
│   └── background/         # Email, webhooks, cleanup
│       ├── tasks.py
│       └── beat_schedule.py
│
├── domain/                 # Pure business logic (no IO)
│   ├── models/             # Pydantic/SQLAlchemy models
│   ├── services/           # Business rules
│   │   ├── betting.py      # Lucro, ROI, freebet, estados
│   │   ├── temporal.py     # Unidades, contas_casa (centralizado!)
│   │   ├── matching.py     # Pareador, cruzamento
│   │   └── validation.py   # 14 conferências
│   ├── events/             # Event definitions (Pydantic)
│   └── rules/              # Regras puras (testáveis unitariamente)
│
├── infrastructure/         # IO implementations
│   ├── database/
│   │   ├── connection.py   # SQLAlchemy async pool
│   │   ├── repositories.py # Repository implementations
│   │   ├── migrations/     # Alembic
│   │   └── models.py       # SQLAlchemy ORM models
│   ├── cache/
│   │   └── redis.py        # Redis client + streams
│   ├── storage/
│   │   └── s3.py           # Photos, exports, excels
│   └── external/
│       ├── anthropic.py    # Wrapper com circuit breaker
│       └── casas/          # Parsers por casa (extensível)
│
├── frontend/               # Static assets (servidos por CloudFront)
│   ├── templates/          # Jinja2 (server-rendered)
│   ├── static/
│   │   ├── css/            # paleta.css, estilo.css
│   │   ├── js/             # HTMX, alpine.js, extension bridge
│   │   └── images/
│   └── extension/          # Chrome extension (build separado)
│
├── shared/                 # Common utilities
│   ├── config.py           # Pydantic Settings (single source)
│   ├── logging.py          # structlog config
│   ├── metrics.py          # Prometheus metrics
│   └── exceptions.py       # Custom exceptions
│
└── tests/                  # Organizados por camada
    ├── unit/               # Domain services (fast, no IO)
    ├── integration/        # Repository + API (testcontainers)
    ├── contract/           # Pact tests (Anthropic, Betano)
    └── e2e/                # Critical paths (playwright)
```

### 6.3 Database Schema (PostgreSQL)

**Princípios:** RLS ativo, partitioning nativo, índices compostos, JSONB para payloads.

```sql
-- Tabelas particionadas (range monthly)
CREATE TABLE eventos (
    id BIGSERIAL,
    usuario_id INT NOT NULL,
    tipo VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    fonte VARCHAR(20) NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL,
    -- ...
) PARTITION BY RANGE (criada_em);

CREATE TABLE movimentos (
    id BIGSERIAL,
    usuario_id INT NOT NULL,
    conta_casa_id INT,
    tipo VARCHAR(20) NOT NULL,
    valor_centavos BIGINT NOT NULL,
    ocorrido_em TIMESTAMPTZ NOT NULL,
    -- ...
) PARTITION BY RANGE (ocorrido_em);

-- RLS Policies
ALTER TABLE apostas ENABLE ROW LEVEL SECURITY;
CREATE POLICY apostas_tenant ON apostas
    USING (usuario_id = current_setting('app.current_user_id')::int);

-- Índices compostos para partition pruning
CREATE INDEX idx_apostas_user_time ON apostas (usuario_id, criada_em);
CREATE INDEX idx_eventos_user_time ON eventos (usuario_id, criada_em);
```

---

## 7. Visão Consolidada das Decisões (ADRs 001-012)

| Área | Decisão Chave | ADR |
|------|---------------|-----|
| **Qualidade** | RPO=0, RTO<1h, P95<500ms, custo/bet medido | 001 |
| **Dados** | Relacional PG, SQLAlchemy async, RLS multi-tenant | 002 |
| **Storage** | PG nativo, range partitioning mensal, read replica async | 003 |
| **Evolução** | JSON + versionamento implícito, API `/v1/`, Pydantic | 004 |
| **Replicação** | Single-leader, async replica, read-after-write 5s | 005 |
| **Particionamento** | Range time (PG nativo), sharding futuro hash `usuario_id` | 006 |
| **Transações** | READ COMMITTED + `FOR UPDATE`, tx por operação negócio | 007 |
| **Resiliência** | Circuit breaker (pybreaker), timeouts centralizados, OTel | 008 |
| **Consistência** | Linearizable writes (Primary), eventual reads (Replica ≤30s) | 009 |
| **Batch** | Dual pool (Extraction + Materialization), at-least-once + idempotency | 010 |
| **Stream** | Redis Streams, 202 polling, Celery unificado, dual DLQ | 011 |
| **Evolução** | Feature flags, expand-only migrations, blue-green, contract tests | 012 |

---

## 8. Perguntas para o Criador do Projeto

### 8.1 Produto & Negócio (Decisões de Escopo)

| # | Pergunta | Por que Importa |
|---|----------|-----------------|
| **P1** | **Qual o maior dor hoje?** (performance extração? UX painel? bugs dinheiro? deploy? onboarding?) | Define prioridade de refatoração |
| **P2** | **Público-alvo imediato:** Tipsters com canal parceiro? Usuários solo? Ambos? | Define features: bot canal vs coleta casa |
| **P3** | **Cobrança:** Stripe? Mercado Pago? Próprio? Quando ativar? | Define infra billing, webhooks, trials |
| **P4** | **Beta atual (bancaemdia.com.br):** O que funciona bem? O que usuários reclamam? | Valida UX vs refatoração técnica |
| **P5** | **Meta de usuários 3 meses:** 50? 200? 1000? | Dimensiona infra (RDS class, workers) |

### 8.2 Decisões Técnicas Pendentes (ADRs Marcados "Confirmar")

| # | Pergunta | ADR | Opções |
|---|----------|-----|--------|
| **P6** | **Latência painel:** "Near-real-time ≤30s aceitável" confirmado? | 002, 009 | Sim / Precisa <5s (forte) |
| **P7** | **Queries cross-tenant:** Precisa "top 10 tipsters globais", market share casas? | 002 | Sim (shared schema sem RLS analytics) / Não (RLS estrito) |
| **P8** | **Releitura geral:** Ainda adiada? Quer auditoria limpa antes? | ESTADO.md | Sim (aguardar) / Não (fazer agora) |
| **P9** | **Mercado 62% (nomes com ` - `):** Aceita ficar 62% até portão A reabrir? | ESTADO.md | Sim / Não (reabre portão A) |
| **P10** | **88 msgs importadas não planilhadas:** Adicionar aviso no painel? | ESTADO.md | Sim (badge) / Não (manual) |

### 8.3 Arquitetura & Divisão

| # | Pergunta | Impacto |
|---|----------|---------|
| **P11** | **Frontend:** Manter Jinja2 + HTMX (server-rendered) ou migrar para SPA (React/Vue) + API? | Custo dev, UX, SEO |
| **P12** | **Extensão Chrome:** Publicar na Chrome Web Store agora? Precisa review Google (semanas)? | Time-to-market coleta casa |
| **P13** | **Bot Telegram (canal parceiro):** Prioridade alta? Substitui export para tipsters parceiros? | Diferencial #4, liquidação auto |
| **P14** | **Liquidação automática:** Sofascore API? Outra fonte? Custo? | Remove dor manual "marcar resultado" |
| **P15** | **Multi-região:** Users fora BR? Latência <200ms necessário? | Define single vs multi-AZ/region |

### 8.4 Operação & Equipe

| # | Pergunta | Impacto |
|---|----------|---------|
| **P16** | **Equipe:** Só você (IA-assisted)? Pretende contratar? | Complexidade aceitável vs "bus factor" |
| **P17** | **Orçamento infra/mês:** $50? $200? $500? | RDS class, ElastiCache, workers, monitoring |
| **P18** | **On-call:** Quem acorda 3am se site cai? PagerDuty? | Define SLO realistas |
| **P19** | **Compliance:** LGPD? Dados sensíveis (CPF, bancários)? | Criptografia, retenção, direito ao esquecimento |
| **P20** | **Migração dados atuais:** 49 users, 16k apostas → PG. Janela de downtime aceitável? | Planeja cutover |

### 8.5 Priorização (Forçar Escolha)

| # | Tradeoff | Sua Escolha |
|---|----------|-------------|
| **P21** | **Refatorar `app.py`/`coleta.py` AGORA** vs **Features novas (bot, liquidação, cobrança)** | Técnico / Produto |
| **P22** | **Observabilidade completa (OTel, dashboards, alertas) AGORA** vs **Deploy pipeline (CI/CD, blue-green) AGORA** | Visibilidade / Velocidade deploy |
| **P23** | **SQLite → PG migration AGORA** vs **Async workers (Celery) AGORA** | Fundação / Throughput |
| **P24** | **Testes: subset rápido (<2min) para dev** vs **Cobertura total mantida** | Velocidade dev / Confiança |

---

## 9. Próximos Passos Sugeridos (Se Alinhado)

### Sprint 0 - Foundation (2-3 semanas)
1. **PG Migration**: SQLite → RDS PG + Alembic baseline (migração 28)
2. **SQLAlchemy + RLS**: Models, repositórios, policies
3. **Config Centralizada**: Pydantic Settings, secrets no AWS Secrets Manager
4. **CI/CD**: GitHub Actions → test → build → staging → canary → prod

### Sprint 1 - Async Pipeline (2-3 semanas)
1. **Celery + Redis**: Broker, 3 queues, Flower monitoring
2. **Extraction Workers**: OCR → Haiku → Sonnet, circuit breaker, cache Redis
3. **Materialization Workers**: Idempotent upsert, pareador, event publishing
4. **Coleta Casa Async**: 202 + polling, Redis Streams

### Sprint 2 - Observability & Safety (1-2 semanas)
1. **OpenTelemetry**: Auto-instrument + custom spans
2. **Structlog + CloudWatch**: JSON logs, correlation IDs
3. **Prometheus + Grafana**: Dashboards + alertas (P99, error rate, queue depth, cost)
4. **Rate Limiting + Circuit Breakers**: slowapi, pybreaker

### Sprint 3 - Frontend & UX (2 semanas)
1. **Modularizar `app.py`**: Routers por tela, dependency injection
2. **Feature Flags**: Admin UI, canary por tenant
3. **Painel Otimizado**: MVs + read replica, badge "atualizado há Xs"
4. **Extension Polish**: Chrome Web Store build, polling UX

### Sprint 4 - Billing & Growth (2 semanas)
1. **Stripe/MP Integration**: Plans, webhooks, portal cliente
2. **Bot Telegram**: Canal parceiro, liquidação auto
3. **Contract Tests**: Anthropic, Betano, Sofascore
4. **Load Test**: k6/Gatling → valida 50→200 users

---

## 10. Riscos & Mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|-------|---------------|---------|-----------|
| **Migração PG corrompe dados** | Média | 🔴 Crítico | `pgloader` + `conferir_numeros.py` em staging; rollback plan |
| **Celery/Redis complexidade > esperado** | Alta | 🟡 Alto | Spike 2 dias; fallback: RQ se Celery falhar |
| **Anthropic API muda (breaking)** | Média | 🟡 Alto | Contract tests (Pact) + circuit breaker + versionamento prompt |
| **Extensão rejeitada Chrome Store** | Baixa | 🟡 Alto | Preparar manifest V3, privacy policy, screenshots antecipado |
| **Criador não aprova refatoração técnica** | Média | 🔴 Crítico | Mostrar ROI: "sem isso, não passa de 50 users" + métricas |
| **Bug silencioso em temporalidade** | Alta (já houve 3) | 🔴 Crítico | Centralizar em `domain/services/temporal.py` + property tests |

---

**Fim do Documento de Review**  
**Próxima ação:** Agendar 2h com criador, apresentar seções 1-8, decidir P1-P24.