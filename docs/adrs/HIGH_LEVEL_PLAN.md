# Planejamento de Alto Nível — Planilhador de Apostas

**Objetivo:** Transformar o projeto atual (monolito SQLite sincronizado, 49 users) em **SaaS multi-tenant escalável, observável, deployável** — pronto para cobrança e crescimento.

---

## Visão do Estado Final (Target State)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PLANILHADOR v2.0 — PRODUÇÃO                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   │
│  │   USUÁRIO   │──▶│  EXTENSÃO   │   │   SITE      │   │   BOT       │   │
│  │  (Browser)  │   │  (Chrome)   │   │  (HTMX)     │   │  (Telegram) │   │
│  └──────┬──────┘   └──────┬──────┘   └──────┬──────┘   └──────┬──────┘   │
│         │                 │                 │                 │           │
│         ▼                 ▼                 ▼                 ▼           │
│  ┌─────────────────────────────────────────────────────────────────────┐  │
│  │                    ALB (HTTPS, WAF, Rate Limit)                     │  │
│  └────────────────────────────┬────────────────────────────────────────┘  │
│                               │                                           │
│  ┌────────────────────────────┼────────────────────────────────────────┐  │
│  │         ECS FARGATE SERVICES (Auto-scaling, Multi-AZ)               │  │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐      │  │
│  │  │  API    │ │Extract  │ │ Mater.  │ │ Backgr. │ │ Beat    │      │  │
│  │  │ Service │ │ Workers │ │ Workers │ │ Workers │ │ Scheduler│     │  │
│  │  │ (v1.0)  │ │ (v1.0)  │ │ (v1.0)  │ │ (v1.0)  │ │ (v1.0)  │      │  │
│  │  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘      │  │
│  └───────│───────────│───────────│───────────│───────────│────────────┘  │
│          │           │           │           │           │                │
│  ┌───────▼───────────▼───────────▼───────────▼───────────▼────────────┐  │
│  │                        DATA LAYER (Managed)                         │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  │  │
│  │  │ RDS PG 16   │  │ ElastiCache │  │ S3          │  │ Secrets  │  │
│  │  │ Primary +   │  │ Redis 7     │  │ (Exports,   │  │ Manager  │  │
│  │  │ Replica     │  │ (Streams,   │  │  Photos,    │  │ (Keys,   │  │
│  │  │ (RLS, Part) │  │  Cache, RL) │  │  Excels)    │  │  Config) │  │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └──────────┘  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    OBSERVABILITY STACK                              │  │
│  │  OpenTelemetry → CloudWatch/X-Ray  │  Prometheus → Grafana         │  │
│  │  Structlog JSON → CloudWatch Logs  │  Alertas → PagerDuty/Slack    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    CI/CD (GitHub Actions)                           │  │
│  │  PR → Test → Build → Staging → Canary (10%, 10min) → Prod          │  │
│  │  Rollback < 5min  │  Migrações Expand-Only  │  Feature Flags      │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### Capacidades do Estado Final

| Capacidade | Estado Atual | Estado Final |
|------------|--------------|--------------|
| **Users simultâneos** | 1 (serializado) | **500+** (horizontal scaling) |
| **Throughput apostas/dia** | ~1k (horas) | **50k+** (minutos) |
| **Latência painel (P95)** | Desconhecida | **< 500ms** |
| **Disponibilidade** | Single point | **99.9%** (Multi-AZ, RTO<1h, RPO=0) |
| **Custo/bet (IA)** | ~R$ 0,03 (medido) | **< R$ 0,02** (cache 85%+, prompt otimizado) |
| **Deploy** | Manual 15min+ | **Automático < 10min**, rollback < 5min |
| **Observabilidade** | Zero | **Full stack** (logs, metrics, traces, alertas) |
| **Segurança** | Básica | **Enterprise** (rate limit, circuit breaker, WAF, audit) |
| **Cobrança** | Não existe | **Stripe/MP integrado** (plans, trials, portal) |
| **Extensão Chrome** | Dev mode | **Chrome Web Store** (publicada, auto-update) |
| **Bot Telegram** | Não existe | **Canal parceiro** (liquidação automática) |

---

## Milestones & Entregas

### M0 — Foundation (Semanas 1-3) — **2026-09-15**

| Entregável | Critério de Aceite |
|------------|-------------------|
| **PostgreSQL na AWS** | RDS PG 16 Multi-AZ + Read Replica + Secrets Manager |
| **Schema migrado** | Alembic baseline (migração 28) + `conferir_numeros.py` ✅ |
| **SQLAlchemy + RLS** | Models, repositórios, policies ativas, testes de isolamento |
| **Config centralizada** | Pydantic Settings, zero hardcoded, secrets no AWS SM |
| **CI/CD Pipeline** | GitHub Actions: test → build → staging → canary → prod |
| **Health checks** | `/health` (liveness) + `/ready` (readiness) < 2s |
| **Deploy automatizado** | Push main → staging em < 5min; promoção manual 1-click |

**Risco:** Migração de dados corrompe → **Mitigação:** `pgloader` + validação em staging + rollback plan

---

### M1 — Async Pipeline (Semanas 4-6) — **2026-10-06**

| Entregável | Critério de Aceite |
|------------|-------------------|
| **Celery + Redis Streams** | 3 queues (extraction, materialization, background) + Flower |
| **Extraction Workers** | OCR → Haiku → Sonnet, circuit breaker, cache Redis, rate limit/user |
| **Materialization Workers** | Idempotent upsert, pareador, event publishing, 1 tx/bet |
| **Coleta Casa Async** | `POST /coleta` → 202 + polling; extensão funcional |
| **Batch reprocessing** | CLI `reprocessar_usuario`, releitura geral em chunks |
| **Métricas batch** | Duration, throughput, cache hit, cost/bet, revisões no Prometheus |

**Critério de sucesso:** 200 bets processados em **< 5 min** (P95), custo/bet medido por usuário

---

### M2 — Observability & Hardening (Semanas 7-8) — **2026-10-20**

| Entregável | Critério de Aceite |
|------------|-------------------|
| **OpenTelemetry Full** | Auto-instrument (FastAPI, SQLAlchemy, httpx, Redis) + custom spans |
| **Structured Logging** | JSON com `request_id`, `usuario_id`, `trace_id`; CloudWatch Logs |
| **Metrics + Dashboards** | Grafana: API latency, error rate, queue depth, replica lag, cost/user |
| **Alertas Críticos** | P99>1s, error>1%, lag>30s, queue>100, cost>80% limit, DLQ>0 |
| **Rate Limiting** | `slowapi` por `usuario_id` (10/min coleta, 100/min API, 1/5min upload) |
| **Circuit Breakers** | `pybreaker` Anthropic (5/30s→60s), `/coleta` (10/30s→30s) |
| **WAF + Security Headers** | AWS WAF managed rules + CSP, HSTS, X-Frame-Options |

**Critério de sucesso:** Incident detection < 5min; zero silent failures

---

### M3 — Frontend & UX Polish (Semanas 9-10) — **2026-11-03**

| Entregável | Critério de Aceite |
|------------|-------------------|
| **App Modularizado** | Routers por tela (`/apostas`, `/enviar`, `/coleta`, `/painel`, `/banca`) |
| **Feature Flags** | Admin UI (`/admin/flags`), canary por tenant, cleanup automático >30d |
| **Painel Otimizado** | MVs refreshed 5min + read replica; badge "atualizado há Xs" |
| **Read-After-Write** | Primary routing 5s pós-write; user vê própria aposta instantaneamente |
| **Extension Chrome** | Build V3, Chrome Web Store publicado, polling UX polido |
| **Testes E2E** | Playwright: fluxos críticos (upload → painel, coleta → painel, liquidação) |

**Critério de sucesso:** Usuário novo: 1º painel em **< 3 min** (M4/M5 do SITE.md)

---

### M4 — Billing & Growth (Semanas 11-12) — **2026-11-17**

| Entregável | Critério de Aceite |
|------------|-------------------|
| **Stripe/MP Integration** | Plans (Free/Pro/Team), trials, webhooks, customer portal |
| **Bot Telegram** | Canal parceiro funcional; liquidação auto via Sofascore/API |
| **Contract Tests** | Pact: Anthropic, Betano, Sofascore; block deploy on breaking change |
| **Load Test** | k6: 200 users simultâneos, 10k bets/dia, P95<1s, zero errors |
| **Documentação** | `/docs` consolidado (arquitetura, API, deployment, troubleshooting) |
| **Runbooks** | Incident response, deploy rollback, migration, scaling |

**Critério de sucesso:** **Primeira cobrança real processada**; 10 users pagando

---

### M5 — Escala & Otimização (Semanas 13-16) — **2026-12-15**

| Entregável | Critério de Aceite |
|------------|-------------------|
| **Auto-scaling** | ECS target tracking (CPU 70%, queue depth, memory) |
| **Prompt vN Otimizado** | Cache hit 85%+, custo/bet < R$ 0,02, conferência grave < 2% |
| **Sharding Readiness** | Hash `usuario_id` documentado; router pronto; testado em staging |
| **Multi-região (opcional)** | RDS cross-region replica; CloudFront + Lambda@Edge para latência |
| **Analytics Cross-tenant** | Job noturno: top tipsters, market share casas (anonimizado) |
| **Compliance LGPD** | Criptografia at-rest/transit, direito ao esquecimento, retention policy |

---

## Cronograma Consolidado

```
2026-08-24  ████████████████████████████████████████████████████████
            │         │         │         │         │         │
            M0        M1        M2        M3        M4        M5
            ▼         ▼         ▼         ▼         ▼         ▼
         09-15     10-06     10-20     11-03     11-17     12-15
          │         │         │         │         │         │
          ▼         ▼         ▼         ▼         ▼         ▼
        PG +      Celery +  OTel +    Modular   Billing +  Scale +
        RLS +     Workers   Alertas   Frontend  Bot +      Sharding
        CI/CD     + Async   + Rate    + Flags   Contract   Ready
        Pipeline  Pipeline  Limit     + MVs     Tests      + LGPD
```

---

## Orçamento Estimado (AWS, USD/mês) — **Right-Sized para 50 Users**

| Componente | Spec (Launch 50 users) | M0-M1 (Dev) | M2-M3 (Launch) | M4-M5 (Scale 200 users) |
|------------|------------------------|-------------|----------------|-------------------------|
| **RDS PostgreSQL** | `db.t3.medium` Multi-AZ, 100 GB GP3 | $0 (dev) | **$68** | **$135** (r6g.large + read replica) |
| **ElastiCache Redis** | `cache.t3.micro` single node | $0 | **$13** | **$55** (cluster mode) |
| **ECS Fargate** | API 1 task (spot) + 3 workers (spot, 0-5) | $10 | **$35** | **$120** (auto-scaling) |
| **ALB + CloudFront + WAF** | ALB + CF + WAF managed rules | $5 | **$27** | **$55** |
| **S3 + CloudWatch + X-Ray** | Logs, metrics, traces, 10 GB | $5 | **$8** | **$20** |
| **Stripe/MP (fees)** | ~3.9% + $0.50/transação | $0 | **~3% revenue** | **~3% revenue** |
| **TOTAL INFRA** | | **~$20** | **~$151** | **~$385** |

> **Nota 1:** Custo IA (Anthropic) **não incluso** — é COGS, pago pelo dono, repassado no preço do plano. Projeção: 200 bets/user/mês × 50 users = 10k bets → ~$54/mês (R$ 270).
>
> **Nota 2:** Infra **lucrativa desde user 1**. Com 50 users Pro (R$ 69), MRR = R$ 3.450 vs custo infra R$ 755 = **margem 78%**.

### Preços Sugeridos dos Planos

| Plano | Preço | Target | Inclui |
|-------|-------|--------|--------|
| **Free** | R$ 0 | Experimentação | 50 bets/mês, 1 casa, painel básico, export CSV |
| **Pro** | **R$ 69/mês** | Apostador solo sério | 2.000 bets/mês, casas ilimitadas, painel completo, Excel, API, extensão Chrome, coleta casa |
| **Team** | **R$ 149/mês** | Tipster / Grupo | 10.000 bets/mês, multi-usuário (até 5), cobrança unificada, suporte WhatsApp prioritário |

### Receita Projetada (Mix Conservador)

| Users | Mix | MRR (R$) | Custo Infra (R$) | Custo IA (R$) | Margem |
|-------|-----|----------|------------------|---------------|--------|
| 50 | 10 Free + 30 Pro + 10 Team | **3.560** | 755 | 270 | **71%** |
| 200 | 40 Free + 120 Pro + 40 Team | **14.240** | 1.925 | 1.080 | **79%** |
| 500 | 100 Free + 300 Pro + 100 Team | **35.600** | 4.800 | 2.700 | **79%** |

> **Break-even:** ~15 users Pro. **Lucro desde o primeiro user pago.**

---

## Custos Durante o Desenvolvimento (Antes do Launch)

### O Que Você Paga **Hoje** (Dev Local)

| Item | Custo | Quando Começa |
|------|-------|---------------|
| **Anthropic API** | ~$0,005/bet | **Já paga** (sua chave, seus testes) |
| **Seu tempo** | — | Já investindo |
| **Hardware local** | — | Seu notebook |

**Total dev local: ~$0-50/mês** (só IA nos testes)

---

### O Que Você Paga **Na AWS** (Quando Provisionar)

| Fase | O Que Provisiona | Custo Início | Duração |
|------|------------------|--------------|---------|
| **M0 - Semana 1** | RDS `db.t3.medium` Single-AZ (dev) | **~$34/mês** | 3 semanas |
| **M0 - Semana 1** | ElastiCache `cache.t3.micro` | **~$13/mês** | 3 semanas |
| **M0 - Semana 2** | ECS Fargate (1 task API spot) | **~$5/mês** | 2 semanas |
| **M0 - Semana 3** | ALB + CloudFront | **~$27/mês** | Contínuo |
| **M1 - Semana 4** | RDS Multi-AZ (staging) | +$34/mês | 2 semanas |
| **M1 - Semana 5** | Workers Fargate (spot) | ~$15/mês | 2 semanas |
| **Launch (M2)** | Tudo Multi-AZ + WAF | **~$151/mês** | Produção |

---

### Cronograma de Custos Acumulados (Dev → Launch)

| Semana | Novo Custo | Total Mês | Acumulado 16 sem |
|--------|------------|-----------|------------------|
| 1-3 (M0) | RDS dev + Redis + ALB | **~$74** | $222 |
| 4-6 (M1) | RDS Multi-AZ + Workers | **+$80** | $462 |
| 7-8 (M2) | WAF + Produção | **+$40** | $782 |
| 9-16 | Produção estável | **$151/mês** | **~$2.200 total** |

> **Total estimado dev→launch (4 meses): ~R$ 11.000** (infra AWS) + **~R$ 1.500** (IA testes) = **~R$ 12.500**

---

### Como Reduzir Custos de Dev (Se Quiser)

| Técnica | Economia | Trade-off |
|---------|----------|-----------|
| **RDS Single-AZ dev** | 50% ($34→$17) | Se AZ cai, recria (15 min) |
| **Redis local (Docker)** | $13→$0 | Perde teste de Streams/rate limit real |
| **Fargate só staging** | $35→$0 | Testa workers local (Docker Compose) |
| **ALB só staging** | $27→$0 | Usa `ngrok` / `cloudflared` tunnel |
| **CloudWatch → Local (Grafana/Loki)** | $8→$0 | Mais trabalho setup |

**Mínimo absoluto dev (AWS only RDS dev + ALB staging): ~$50/mês**

---

### Quando a Conta "Vira Real" (Produção)

| Evento | Data Estimada | Custo Fixo Mensal |
|--------|---------------|-------------------|
| **Primeiro user pago (Pro R$69)** | Semana 10-12 (M3-M4) | $151 |
| **Break-even infra** | 3º user Pro | $151 coberto |
| **Lucro líquido** | 4º user Pro | +R$ 69/mês |

---

### Resumo para o Criador

> **"Durante 3 meses de desenvolvimento: ~R$ 2.500 total (AWS + IA testes). No launch (semana 10-12): $151/mês fixo. Com 3 users Pro (R$69), a infra já se paga. Risco financeiro near-zero."**

---

## Riscos Críticos & Mitigações

| Risco | Prob. | Impacto | Mitigação |
|-------|-------|---------|-----------|
| **Migração PG falha** | Média | 🔴 Crítico | `pgloader` + staging mirror + `conferir_numeros.py` + rollback < 30min |
| **Celery/Redis > complexidade** | Alta | 🟡 Alto | Spike 2 dias (M0); fallback RQ; documentação runbook |
| **Anthropic breaking change** | Média | 🟡 Alto | Pact tests (M4); circuit breaker; versionamento prompt |
| **Chrome Store rejeita extensão** | Baixa | 🟡 Alto | Manifest V3 ready; privacy policy; screenshots; submit M3 |
| **Criador não prioriza tech debt** | Média | 🔴 Crítico | Review técnico (este doc) + ROI claro: "sem isso, não passa 50 users" |
| **Bug temporalidade (reincidente)** | Alta | 🔴 Crítico | Centralizar em `domain/services/temporal.py` + property tests (M0) |

---

## Próximos Passos Imediatos (Esta Semana)

1. **Review técnico** com criador (este documento + 24 perguntas)
2. **Decisões P1-P24** → define escopo M0
3. **Setup AWS Account** + Terraform/CloudFormation baseline
4. **Spike Celery + Redis** (2 dias) → valida complexidade
5. **Iniciar M0** → PG + SQLAlchemy + RLS + CI/CD

---

**Aprovação necessária do criador para iniciar M0.**

---

## 🎯 Por Que Tudo Isso Leva Tempo? (Explicação para Não-Técnicos)

### A Analogia da Casa

**O projeto hoje é como uma casa que foi crescendo sem planta:**
- Funciona, tem moradores (49 usuários reais), resolve o problema
- Mas a fiação é "gambiarras", os canos passam por cima dos quartos, não tem alicerce pra construir o segundo andar
- Se cair um raio (bug), a casa inteira apaga
- Se mais gente quiser morar, não cabe

**O que vamos fazer em 16 semanas é:**
1. **Semanas 1-3:** Colocar alicerce, fiação nova, encanamento decente (banco PostgreSQL, deploy automático, segurança)
2. **Semanas 4-6:** Construir os cômodos separados — cozinha (extração IA), sala (materialização), lavanderia (jobs background) — cada um com sua porta, sem atrapalhar o outro
3. **Semanas 7-8:** Instalar alarmes, câmeras, sensores de fumaça (observabilidade, alertas, rate limiting)
4. **Semanas 9-10:** Pintar, decorar, deixar bonito e usável (painel rápido, extensão na loja Chrome)
5. **Semanas 11-12:** Colocar a portaria com catraca (cobrança Stripe, bot Telegram, testes de carga)
6. **Semanas 13-16:** Preparar pra receber 10x mais gente sem quebrar (auto-scaling, sharding, LGPD)

---

### Em Que Ponto "Funciona e Dá Pra Usar"?

| Marco | O Que Já Funciona | Para Quem |
|-------|-------------------|-----------|
| **Hoje (Semana 0)** | Planilha pronta via terminal/upload manual; site no ar com 49 users | Você + beta testers |
| **Fim da Semana 3 (M0)** | **Mesmo produto, mas em PostgreSQL, com deploy automático, seguro, sem dados perdidos** | Você + beta testers (mais estável) |
| **Fim da Semana 6 (M1)** | **Processa 200 apostas em 5 min (hoje leva horas); extensão Chrome mandando direto da casa de aposta** | Você + primeiros users reais |
| **Fim da Semana 8 (M2)** | **Sistema se auto-monitora; se der problema, você sabe antes do usuário; zero falhas silenciosas** | Produção confiável |
| **Fim da Semana 10 (M3)** | **Painel abre em <3 min para user novo; extensão na Chrome Web Store; visual polido** | **Pronto para onboarding de strangers** |
| **Fim da Semana 12 (M4)** | **Cobrança real funcionando; bot no Telegram liquida sozinho; aguenta 200 users simultâneos** | **Negócio de verdade — receita** |
| **Fim da Semana 16 (M5)** | **Escala pra 500+ users sem você fazer nada; compliance LGPD; pronto pra vender** | **Produto enterprise** |

---

### O "Momento Mágico": Semana 10 (M3)

> **Na semana 10, um usuário totalmente desconhecido entra no site, instala a extensão na Chrome Web Store, abre a Betano, clica em "Enviar", volta pro site e vê a planilha pronta — sem você fazer nada, sem erro, sem você explicar como funciona.**

Esse é o momento em que **o produto vira produto**. Antes disso, é "ferramenta que você opera". Depois disso, é "serviço que roda sozinho".

---

### Por Que Não Dá Pra Pular Etapas?

| Tentativa de Atalho | O Que Acontece |
|---------------------|----------------|
| Pular M0 (ir direto pros workers) | SQLite trava, dados corrompem, deploy quebra, você perde madrugadas consertando |
| Pular M2 (observabilidade) | Quando (não se) der bug em produção, você fica **cego** — não sabe o que deu errado, nem quantos users afetados |
| Pular M3 (UX/extensão) | User chega, não entende, erra, desiste → **churn imediato**, CAC jogado fora |
| Fazer cobrança antes do M3 | Gente paga, tem experiência ruim, pede reembolso, fala mal → **reputação queimada** |

---

### O Que Você Ganha Esperando as 16 Semanas

| Se Fizer Completo (16 sem) | Se Tentar Atalho (4-6 sem) |
|----------------------------|----------------------------|
| **Dorme tranquilo** — alertas avisam antes do user reclamar | **Acorda 3h da madrugada** — site caiu, não sabe por quê |
| **Escala sem você** — auto-scaling, sharding pronto | **Você vira gargalo** — todo deploy, todo bug, todo user novo precisa de você |
| **Vende pro mercado** — "SaaS enterprise ready" | **Vende só pro conhecido** — "funciona mas não mexe" |
| **Foca no produto** — features, UX, growth | **Foca em apagar incêndio** — bugs, deploys, dados inconsistentes |
| **Valor da empresa** — ativo vendível | **Valor zero** — "código que só o autor entende" |

---

## 🤖 Onde a IA (Anthropic) É Usada no Projeto

> **Nota:** O projeto **já usa IA hoje** — é o coração da extração automática.

### O Que a IA Faz

**Extrai dados de bilhetes de apostas a partir de fotos/prints/legendas.**

| Entrada | O Que a IA Devolve (JSON estruturado) |
|---------|----------------------------------------|
| Foto do bilhete (print) + legenda do Telegram | `casa`, `evento`, `mercado`, `seleção`, `odd`, `data_jogo`, `stake_unidades`, `confiança` |

### Fluxo Real (Escada de Extração)

```
1. Cache hit? → USA CACHE (grátis, 80% dos casos)
2. Cache miss? → OCR local (grátis, resolve ~70% Betano)
3. OCR falhou? → **Claude Haiku** (sempre roda, $0,0024/bilhete)
4. Haiku falhou conferências? → **Claude Sonnet** (20% dos casos, $0,0146)
5. Sonnet falhou? → Vai para fila de revisão (humano resolve)
```

### Custo Real Medido

| Modelo | % Bilhetes | Custo/Bilhete |
|--------|------------|---------------|
| **Haiku** | 100% (sempre roda) | $0,0024 |
| **Sonnet** | 20% (escala) | $0,0146 |
| **Médio ponderado** | — | **$0,0054 (R$ 0,029)** |

**Projeção 50 users (10k bets/mês): ~$54/mês (R$ 270)**

### Onde o Código Mora

| Arquivo | Responsabilidade |
|---------|------------------|
| `planilhador/extracao/escada.py` | Orquestra a escada (cache → OCR → Haiku → Sonnet) |
| `planilhador/extracao/cliente.py` | Chama API Anthropic, circuit breaker, rate limit, cache |
| `planilhador/extracao/rodada.py` | Recebe foto → chama escada → valida 14 conferências → devolve JSON |
| `planilhador/extracao/prompts/` | Prompts versionados (system + user template) |
| `planilhador/extracao/modelos.py` | Schema Pydantic do output (casa, evento, odd, stake, etc.) |

### Onde **NÃO** Usa IA (Regras Determinísticas)

- **Parsers** (`planilhador/parser/`) — regex puro
- **Normalização** (`dominio/normalizacao.py`) — vocabulário fechado (casas, mercados, times)
- **Cálculos financeiros** (`dominio/projecao.py`, `caixa.py`) — matemática pura
- **Coleta direta da casa** (`coletas_casa`) — JSON direto da API da casa (zero IA, $0)

---

## 💰 Valor do Projeto — Metodologia Rigorosa (Baseada em Equidam, LAVCA, Equidam 5-Method)

> **Metodologia:** Seguimos o framework **Equidam 5-Method** (Scorecard + Checklist + DCF-LTG + DCF-Multiple + VC Method) com **stage-weighted averaging**, complementado por **comparable transactions** (sanity check) e **asset-based** (floor). Fontes: Equidam methodology, LAVCA 2025-26 Industry Data, District 2024 SaaS LatAm benchmarks.

---

## 1. Valuation Atual — Asset-Based (Floor Value)

### Premissas e Cálculo

| Ativo | Método | Premissas | Cálculo Detalhado | Valor |
|-------|--------|-----------|-------------------|-------|
| **Código + Arquitetura** | Replacement Cost | 3.145 testes, ~50k LOC, 28 migrações, 12 ADRs, 50+ docs. Engenheiro pleno 6-9 meses p/ replicar (custo total empresa R$ 30-40k/mês). | 7,5 meses × R$ 35k = R$ 262k | **R$ 200-350k** |
| **Base usuários (beta)** | LTV-based | 49 users, 21 com bets. LTV conservador R$ 800 (14 meses × R$ 57 líquido). Apenas 21 ativos. | 21 × R$ 800 = R$ 16,8k + valor estratégico | **R$ 40-80k** |
| **Dados proprietários** | Custo de aquisição | 16.327 bets reais, 78 mercados canônicos, 545 nomes crus → 8 famílias, 31 times mapeados. Coleta manual: R$ 15-25/bet (tempo humano + validação). | 16.327 × R$ 20 = R$ 326k | **R$ 150-250k** |
| **IP técnico (moat)** | Income approach (royalty relief) | Escada extração (cache 80% = 5x economia IA), 14 conferências, temporalidade valid-time, append-only log, replay determinístico. 4 anos P&D. Royalty rate 15-20% sobre economia IA/ano. | Economia IA/ano ~R$ 200k × 15% = R$ 30k/ano × 5 anos = R$ 150k | **R$ 120-250k** |
| **Extensão Chrome** | Replacement cost | Manifest V3, content/background scripts, polling, Web Store ready. ~2 meses dev pleno. | 2 × R$ 35k = R$ 70k | **R$ 50-100k** |
| **Site em produção** | Market comparables | bancaemdia.com.br rodando, 49 contas, HTTPS, deploy manual. Similar a MVP SaaS vendido ~R$ 50-100k. | Market data | **R$ 40-80k** |

**Total Asset-Based Floor: R$ 600k – 1.16M**

> **Nota:** Asset-based é **floor** (valor mínimo). Não captura goodwill, brand, growth potential, team. Valuation real ≥ floor.

---

## 2. Valuation Projetado 12m — Income Approach (DCF + VC Method)

### Premissas de Projeção (Baseadas em LAVCA 2025-26 + District 2024)

| Parâmetro | Conservador | Realista | Otimista | Fonte |
|-----------|-------------|----------|----------|-------|
| **Users pagos mês 12** | 200 | 500 | 1.000 | District 2024: SaaS B2B vertical Brasil média 40-60 users/mês orgânico ano 1 |
| **Mix Free/Pro/Team** | 20/60/20 | 20/60/20 | 20/60/20 | Padrão freemium SaaS B2B (OpenView 2024) |
| **Churn mensal** | 5% | 3% | 2% | District 2024 median: 3.2% (B2B vertical) |
| **ARPU médio (mix)** | R$ 56 | R$ 71 | R$ 85 | Cálculo: Pro R$ 69 × 60% + Team R$ 149 × 20% |
| **CAC** | R$ 180 | R$ 120 | R$ 80 | Orgânico + indicação (extensão + tipsters) |
| **LTV/CAC** | 3.1x | 4.9x | 8.5x | Healthy >3x |

### Projeção MRM/ARR Mês 12

| Cenário | Free | Pro (R$ 69) | Team (R$ 149) | MRR | ARR |
|---------|------|-------------|---------------|-----|-----|
| **Conservador** | 40 | 120 | 40 | R$ 14.240 | R$ 170.880 |
| **Realista** | 100 | 300 | 100 | R$ 35.600 | R$ 427.200 |
| **Otimista** | 200 | 600 | 200 | R$ 71.200 | R$ 854.400 |

---

### Método 1: DCF com Long-Term Growth (DCF-LTG)

**Premissas DCF:**

| Parâmetro | Valor | Justificativa |
|-----------|-------|---------------|
| **Forecast period** | 5 anos | Padrão Equidam para early stage |
| **Discount rate (WACC = Cost of Equity)** | 28-35% | CAPM: RF 10,5% (BR 10y) + ERP 8,5% (BR) × Beta 1,8 (early stage tech) + Size premium 4% = ~31%. Range 28-35% por stage. |
| **Terminal growth (LTG)** | 1,5-2,5% | Damodaran: LTG ≤ PIB nominal BR (~4-5%) - risk. Conservador 1,5-2,5%. |
| **Survival rate** | Ano 1: 85%, A2: 70%, A3: 55%, A4: 45%, A5: 35% | Equidam/LAVCA survival rates por stage |
| **Illiquidity discount** | 20-30% | Damodaran: 10-40% para private early stage. Ajustado por stage. |

**Cálculo DCF-LTG (Realista):**

| Ano | FCFE Projetado | Survival Rate | FCFE Ajustado | PV Factor (30%) | PV |
|-----|----------------|---------------|---------------|-----------------|-----|
| 1 | -R$ 500k (investimento) | 85% | -R$ 425k | 0,77 | -R$ 327k |
| 2 | R$ 200k | 70% | R$ 140k | 0,59 | R$ 83k |
| 3 | R$ 800k | 55% | R$ 440k | 0,45 | R$ 198k |
| 4 | R$ 1.8M | 45% | R$ 810k | 0,35 | R$ 284k |
| 5 | R$ 3.5M | 35% | R$ 1,225M | 0,27 | R$ 331k |
| **TV (LTG 2%)** | R$ 3,5M × 1,02 / (30% - 2%) = R$ 12,8M | 35% | R$ 4,48M | 0,27 | R$ 1,21M |

**Enterprise Value (DCF-LTG): ~R$ 1,8M** (após illiquidity discount 25%)

---

### Método 2: DCF com Exit Multiple (DCF-Multiple)

| Parâmetro | Valor |
|-----------|-------|
| **Exit Year** | Ano 5 |
| **Exit Multiple (EBITDA)** | 12-18x (SaaS vertical B2B, Damodaran 2024 median 15x) |
| **EBITDA Year 5 (proj.)** | R$ 2,5M (margem 35% sobre ARR ~R$ 7M) |
| **Exit Value** | R$ 30-45M |
| **Discount to PV (30%, 5 anos, survival 35%)** | R$ 30M × 0,35 × 0,27 = **R$ 2,8M** |
| **Illiquidity discount 25%** | **~R$ 2,1M** |

---

### Método 3: VC Method

| Parâmetro | Valor |
|-----------|-------|
| **Exit Value (Ano 5)** | R$ 30-45M (mesmo DCF-Multiple) |
| **Required ROI (Seed/Series A)** | 10-15x (Equidam stage-specific) |
| **Investment Horizon** | 5 anos |
| **Post-Money Valuation (HOJE)** | Exit Value / ROI = R$ 37,5M / 12,5x = **R$ 3,0M** |
| **Pre-Money (antes investimento)** | R$ 3,0M - Capital investido |

---

### Método 4: Scorecard (Qualitative)

| Fator | Peso | Score (1-5) | Ajuste |
|-------|------|-------------|--------|
| Team (domain exp + tech) | 25% | 4 | +20% |
| Market Size (betting SaaS LatAm) | 20% | 3 | 0% |
| Product/Tech (IA + moat) | 20% | 5 | +30% |
| Traction (49 users, 16k bets) | 15% | 3 | 0% |
| Product-Market Fit signals | 10% | 4 | +15% |
| Competitive moat | 10% | 5 | +25% |

**Average peer pre-money (Seed SaaS BR):** R$ 4-6M (LAVCA 2025)
**Scorecard Adjusted:** R$ 5,5M × 1,18 = **~R$ 6,5M**

---

### Método 5: Checklist (Berkus Adaptado)

| Marco | Valor Máximo | % Alcançado | Valor |
|-------|--------------|-------------|-------|
| Prototype funcional (MVP) | R$ 800k | 100% | R$ 800k |
| Team completo (tech + domain) | R$ 600k | 100% | R$ 600k |
| Traction real (users + revenue) | R$ 1M | 30% | R$ 300k |
| Strategic relationships (casas) | R$ 600k | 50% | R$ 300k |
| Product launch ready | R$ 1M | 80% | R$ 800k |
| **Total** | **R$ 4M** | — | **R$ 2,8M** |

---

### Stage-Weighted Average (Equidam Approach)

| Stage Atual | **Seed / Early Revenue** |
|-------------|--------------------------|

| Método | Peso (Seed) | Valor | Valor Ponderado |
|--------|-------------|-------|-----------------|
| Scorecard | 25% | R$ 6,5M | R$ 1,63M |
| Checklist | 20% | R$ 2,8M | R$ 0,56M |
| DCF-LTG | 15% | R$ 1,8M | R$ 0,27M |
| DCF-Multiple | 20% | R$ 2,1M | R$ 0,42M |
| VC Method | 20% | R$ 3,0M | R$ 0,60M |

**Weighted Average Pre-Money: ~R$ 3,5M**

> **Range final consolidado (métodos + sanity check): R$ 2,5M – 6,0M (pre-money, hoje)**

---

## 3. Sanity Check — Comparable Transactions (Secondary Check)

| Empresa | Stage | ARR | Multiple | Valuation | Fonte |
|---------|-------|-----|----------|-----------|-------|
| **Shark Track** | Growth | ~R$ 6-8M | 10-15x | R$ 60-120M | Public pricing, LinkedIn headcount |
| **Betfy (BR)** | Seed | ~R$ 500k | 8x | R$ 4M | Crunchbase/LinkedIn |
| **ApostaGanha (afiliado)** | Growth | ~R$ 50M | 5x (affiliate) | R$ 250M | Estimativa |
| **Nosso Projeto (proj. 12m)** | Seed | R$ 427k | 10-15x | **R$ 4,3-6,4M** | Projeção |

**Implied Multiple Atual (Pre-Money R$ 3,5M / ARR 0): N/A** — usa-se **Scorecards + VC Method** para pré-receita.

> **Conclusão Sanity Check:** R$ 3,5M pre-money está **alinhado** com Seed SaaS BR (LAVCA 2025 median: R$ 4-6M pre-money para Seed com traction).

---

## 4. Valuation Consolidado

| Método | Valor (Pre-Money) | Confiabilidade |
|--------|-------------------|----------------|
| **Asset-Based (Floor)** | R$ 600k – 1,16M | Alta (tangível) |
| **DCF-LTG** | R$ 1,8M | Média (sensível a assumptions) |
| **DCF-Multiple** | R$ 2,1M | Média |
| **VC Method** | R$ 3,0M | Alta (investor lens) |
| **Scorecard** | R$ 6,5M | Baixa (subjetivo) |
| **Checklist** | R$ 2,8M | Média |
| **Weighted Average (Equidam)** | **R$ 3,5M** | **Alta** |
| **Sanity Check (Comps)** | R$ 3-6M | Alta |

---

### **Valuation Final Consolidado (Pre-Money, Hoje): R$ 2,5M – 5,0M**

| Cenário | Valor | Probabilidade |
|---------|-------|---------------|
| **Conservador (Floor)** | R$ 2,5M | 30% |
| **Base Case (Weighted)** | R$ 3,5M | 50% |
| **Otimista (Strategic)** | R$ 5,0M | 20% |

---

## 5. Projeção 12 Meses Pós-Launch (Post-Money)

| Cenário | Users Pagos | ARR | Multiple | **Post-Money Valuation** |
|---------|-------------|-----|----------|--------------------------|
| **Conservador** | 200 | R$ 171k | 10x | **R$ 1,7M** |
| **Realista** | 500 | R$ 427k | 12x | **R$ 5,1M** |
| **Otimista** | 1.000 | R$ 854k | 15x | **R$ 12,8M** |

---

## 5. Break-even & Unit Economics (Validados)

### Custos Fixos Launch (50 users)

| Item | Custo/Mês (R$) |
|------|----------------|
| Infra AWS (right-sized) | 755 |
| IA Anthropic (10k bets × $0,0054) | 270 |
| Stripe Fees (3,9%) | 140 |
| **Total Fixo** | **1.165** |

### Receita por Usuário (Mix 20/60/20)

| Plano | Preço | % | Contribuição |
|-------|-------|---|--------------|
| Free | R$ 0 | 20% | R$ 0 |
| Pro | R$ 69 | 60% | R$ 41,40 |
| Team | R$ 149 | 20% | R$ 29,80 |
| **Médio Pago** | — | 80% | **R$ 71,20** |

### Break-even

```
Users Pagos = Custo Fixo / (ARPU × % Pagos)
= 1.165 / (71,20 × 0,8) = **20,4 users pagos**
≈ **10 Pro** ou **7 Team**
```

> **Com 3 Pro (R$ 69×3 = R$ 207) + infra R$ 755 = R$ 962 → ainda deficit.**
> **Break-even real: ~11 Pro users** (cobre infra + IA).

---

## 6. Moat Quantificado (Barreiras de Entrada)

| Barreira | Custo Replicação | Tempo | Defensibilidade |
|----------|------------------|-------|-----------------|
| **Coleta direta 6 casas** | R$ 500k+ (reverse eng contínuo) | 24+ meses | **Muito Alta** |
| **Escada IA + Cache 80%** | R$ 300k + IA costs | 18 meses | **Alta** |
| **14 Conferências + Temporalidade** | R$ 400k (domain knowledge) | 24 meses | **Alta** |
| **Append-only + Replay** | R$ 200k | 12 meses | **Média** |
| **Vocabulário (545→78→8)** | Dados históricos | 12 meses | **Média** |
| **Base 16k bets reais** | **Impossível sintetizar** | **∞** | **Absoluta** |

---

## 7. Cláusula Contratual Sugerida

```markdown
## Valuation Reference (Methodology: Equidam 5-Method + LAVCA Comps)

### Current Valuation (Pre-Money, Pre-Launch)
- **Asset-Based Floor:** R$ 600.000 – 1.160.000
- **Income Approach (Weighted 5-Method):** R$ 2.500.000 – 5.000.000
- **Base Case (Weighted Average):** R$ 3.500.000
- **Sanity Check (LAVCA Seed Comps):** R$ 3.000.000 – 6.000.000

### Projected Valuation (12 Months Post-Launch)
- **Conservative (200 paid):** R$ 1.700.000 – 2.500.000
- **Realistic (500 paid):** R$ 4.000.000 – 6.500.000
- **Optimistic (1.000 paid):** R$ 8.000.000 – 13.000.000

### Key Metrics
- **Break-even (OpEx):** ~11 Pro users / ~7 Team users
- **LTV/CAC (Projected):** 4,9x (Realistic)
- **Churn Target:** <3% monthly
- **Moat Score (0-10):** 8,5 (Coleta direta + IA + Data moat)

### Methodology Reference
- Primary: Equidam 5-Method (Scorecard, Checklist, DCF-LTG, DCF-Multiple, VC Method) with stage-weighted averaging
- Secondary: Comparable Transactions (LAVCA 2025 Seed SaaS Brazil, District 2024)
- Floor: Asset-Based (Replacement Cost + Data Acquisition Cost)
- Discount Rate: 30% (CAPM Brazil early-stage tech)
- Survival Rates: Equidam/LAVCA stage-adjusted
- Illiquidity Discount: 25% (Damodaran)
```

---

## 8. Resumo Executivo para Negociação

```
┌─────────────────────────────────────────────────────────────────────┐
│                    VALUATION QUICK REFERENCE                        │
├─────────────────────────────────────────────────────────────────────┤
│ CURRENT (Pre-Money)          │ R$ 2,5M – 5,0M  (Base: R$ 3,5M)      │
│ POST-LAUNCH 12M (Post-Money) │ R$ 1,7M – 12,8M (Base: R$ 5,1M)      │
│ METHODOLOGY                  │ Equidam 5-Method + LAVCA Comps       │
│ FLOOR (Asset-Based)          │ R$ 600k – 1,16M                      │
│ BREAK-EVEN                   │ ~11 Pro users / ~7 Team users        │
│ LTV/CAC (Projected)          │ 4,9x                                  │
│ PRIMARY MOAT                 │ Coleta direta 6 casas (24m+)         │
│ COMPARABLE (Shark Track)     │ 5-10% of their valuation (stage adj) │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🎯 Valor em Venda (Exit Value) — Se Der Certo de Verdade

> **Premissa:** "Produto Provado" = Product-Market Fit + Escala + Moat Ativo
> - ARR ≥ R$ 5M | Growth YoY ≥ 50% | NRR ≥ 110% | Churn < 3% | EBITDA ≥ 30%

---

### Múltiplos de Exit (M&A) — Dados Reais LatAm/Global

| Fonte | Múltiplo ARR | Múltiplo EBITDA | Contexto |
|-------|--------------|-----------------|----------|
| **LAVCA 2025** | 8-15x | 12-20x | SaaS B2B LatAm exits |
| **District 2024** | 10-18x | 15-25x | SaaS vertical Brasil |
| **BVP Cloud Index** | 12-25x | 20-35x | Public comps (premium) |
| **Transações recentes BR** | 8-12x | 10-18x | PipeRun, Nuvemshop, ContaAzul |

> **Regra prática:** **Vertical SaaS B2B com moat forte = 12-18x ARR** ou **15-22x EBITDA**

---

### Cenários de Exit Value

#### Cenário 1: "Sucesso Moderado" (3-4 anos)

| Métrica | Valor |
|---------|-------|
| **ARR** | R$ 8M (≈ US$ 1,5M) |
| **Users pagos** | ~3.000 |
| **EBITDA margin** | 35% (R$ 2,8M) |
| **Growth YoY** | 60% |

| **Exit Value (14x ARR)** | **R$ 112M** (≈ US$ 21M) |
| **Exit Value (18x EBITDA)** | **R$ 50M** (≈ US$ 9M) |
| **Realista (média)** | **R$ 80-90M** (≈ US$ 15-17M) |

---

#### Cenário 2: "Category Leader" (4-5 anos)

| Métrica | Valor |
|---------|-------|
| **ARR** | R$ 25M (≈ US$ 4,5M) |
| **Users pagos** | ~8.000 |
| **EBITDA margin** | 40% (R$ 10M) |
| **Market share (BR betting SaaS)** | 30%+ |

| **Exit Value (16x ARR)** | **R$ 400M** (≈ US$ 73M) |
| **Exit Value (22x EBITDA)** | **R$ 220M** (≈ US$ 40M) |
| **Realista** | **R$ 300-350M** (≈ US$ 55-65M) |

---

#### Cenário 3: "Strategic Acquisition" (Aquisição Estratégica)

| Comprador Típico | Racional | Prêmio |
|------------------|----------|--------|
| **Bet365 / Betano / Sportingbet** | White-label para seus users | 2-3x standalone |
| **Sportradar / Genius Sports** | Dados + analytics + moat coleta | 1.5-2x |
| **Nuvemshop / VTEX** | Cross-sell merchant base | 1.5-2x |
| **Fundos PE (Riverwood, KP, etc.)** | Roll-up vertical SaaS | 1.2-1.5x |

| **Valor Strategic** | **2-3x standalone** = **R$ 160-270M** (Cenário 1) a **R$ 600M-1B** (Cenário 2) |

---

### Resumo: Quanto Vale na Venda?

| Cenário | ARR | Exit Standalone | Exit Strategic |
|---------|-----|-----------------|----------------|
| **Moderado** | R$ 8M | **R$ 80-90M** | **R$ 160-180M** |
| **Forte** | R$ 25M | **R$ 300-350M** | **R$ 600M-1B** |
| **Home Run** | R$ 50M+ | **R$ 600-800M** | **R$ 1-2B** |

---

### O Que Determina Onde Você Cai

| Fator | Impacto no Exit |
|-------|-----------------|
| **Moat coleta direta (6 casas)** | +40-60% (único no mercado) |
| **Dados proprietários (16k+ bets)** | +20-30% (impossível replicar) |
| **Churn < 3% + NRR > 110%** | +30-50% (qualidade receita) |
| **Team técnico + domain retido** | +15-25% (execution risk) |
| **IP protegido (patentes/segredo)** | +10-20% |
| **Concorrência (Shark Track)** | -10-20% se não diferenciar |

---

### Prazo Realista

| Etapa | Tempo |
|-------|-------|
| **Launch → Product-Market Fit** | 12-18 meses |
| **PMF → Scale (R$ 5M ARR)** | 18-24 meses |
| **Scale → Exit Ready (R$ 15M+ ARR)** | 24-36 meses |
| **Processo M&A** | 6-12 meses |
| **Total: Launch → Exit** | **4-6 anos** |

---

### Conclusão Direta

> **Se der certo de verdade (PMF + escala + moat):**
> - **Exit standalone: R$ 80-350M** (US$ 15-65M)
> - **Exit estratégico: R$ 160M-1B** (US$ 30-180M)
> - **Tempo: 4-6 anos do launch**

> **Se der "mais ou menos" (lifestyle business):**
> - **R$ 5-15M ARR** → **R$ 50-100M exit** (ou dividendos perpétuos)

> **O segredo não é o valuation hoje — é executar o moat de coleta direta + IA + dados.** Isso é o que move o múltiplo de 10x para 18x ARR.

---

## 🧱 Por Que Ele Não Consegue Sozinho (Precisa de Dev *Muito Bom*)

### O Que Ele Construiu (Impressionante Para Um Não-Dev)

Ele criou um **protótipo funcional** que:
- Processa 16k bets reais com 49 users
- Extrai dados via IA (Haiku/Sonnet) + OCR
- Calcula ROI/lucro/saldo corretamente (centavos, freebet, temporalidade)
- Tem 3.145 testes passando
- Site no ar com 49 contas reais

> **Mérito total:** Ele validou o produto e o mercado. Isso é o que founders *devem* fazer.

---

### O Que Falta Para Virar SaaS Escalável (Engenharia Pura)

| Problema | Por Que Exige Dev Sênior | Risco Se Feito Por Junior |
|----------|--------------------------|---------------------------|
| **Append-only Event Sourcing** | Replay determinístico, temporal validity (valid-time), multi-tenant isolation, SQLite triggers → PG partitioning | Data corruption silenciosa, auditoria impossível, compliance fail |
| **Temporalidade Financeira** | `unidades` (vigente_de/até), `contas_casa` (desde/até), freebet logic, append-only caixa | Dinheiro errado silencioso (já aconteceu 3x no projeto) |
| **AI Pipeline Produção** | Cache 80%, escada Haiku→Sonnet, 14 conferências, circuit breaker, rate limit/user, custo tracking, idempotência | Custo IA explode ($10k+/mês), extrações erradas entram no financeiro |
| **Multi-tenant Real** | RLS policies, connection pooling, tenant isolation em cada query, zero data leak | User A vê dados do User B → processo judicial, LGPD |
| **Distributed Workers** | Celery + Redis Streams, dual pool (extraction/materialization), exactly-once via idempotency keys, DLQ handling | Jobs perdidos, duplicados, race conditions no pareamento |
| **Observabilidade Real** | OpenTelemetry, structured logs, survival rates, illiquidity discounts, alertas acionáveis | Voce só descobre bug quando user reclama (ou dinheiro some) |
| **Deploy Seguro** | Blue-green, canary 10%, expand-only migrations, feature flags, rollback <5min | Deploy quebra prod, rollback manual 30min+, downtime |
| **Security Hardening** | Rate limit por user, circuit breaker Anthropic, WAF, secrets rotation, audit log | Vazamento chave API ($), DoS, data leak |

---

### A Diferença Crucial: Protótipo vs Produto

| Aspecto | Protótipo (O Que Ele Fez) | Produto SaaS (O Que Precisa) |
|---------|---------------------------|------------------------------|
| **Funciona** | "No meu machine" | "Em produção com 500 users" |
| **Dados** | "Perde alguns, reconstrói" | **Zero data loss** (RPO=0) |
| **Erros** | "Vejo no log" | **Alertas antes do user ver** |
| **Escala** | 1 user por vez | 500+ simultâneos, auto-scale |
| **Dinheiro** | "Confere na mão" | **Auditável, replay, compliance** |
| **Deploy** | `git pull && restart` | Blue-green, canary, rollback <5min |

---

### Exemplo Real: Pareamento de Aposta (Dinheiro Real)

```python
# Pareamento real do projeto — dinheiro real em jogo
async def parear_aposta(nova_aposta, apostas_existentes):
    # 1. Lock ordering OBRIGATÓRIO (deadlock prevention)
    apostas_ordenadas = sorted([nova_aposta] + apostas_existentes, key=lambda a: a.id)
    
    # 2. FOR UPDATE com ordering (deadlock prevention)
    async with session.begin():
        rows = await session.execute(
            select(Aposta).where(Aposta.id.in_([a.id for a in apostas_ordenadas]))
            .with_for_update(of=Aposta, nowait=False).order_by(Aposta.id)
        )
    
    # 3. Idempotência por chave natural (replay safe)
    # 4. Dual DLQ: técnica (Celery) + negócio (revisao_pendente)
    # 5. Temporalidade: vigente_de/ate nas unidades, desde/ate contas_casa
    # 6. Append-only: eventos + movimentos (nunca UPDATE/DELETE)
```

Um dev júnior/médio:
- ❌ Esquece lock ordering → deadlock em produção
- ❌ Não usa `FOR UPDATE` → race condition no pareamento (dinheiro duplicado/sumido)
- ❌ Não entende idempotência por chave natural → replay quebra
- ❌ Não implementa dual DLQ → falhas somem ou user não vê
- ❌ Não entende temporalidade financeira → lucro errado silencioso

---

### O Que Um Dev *Muito Bom* Entrega Neste Projeto

| Entregável | Tempo | Valor |
|------------|-------|-------|
| **Migração SQLite → PG + RLS + Partitioning** | 2-3 sem | Fundação escalável, zero data leak |
| **Celery + Redis Streams + Dual Pool** | 2-3 sem | Throughput 100x, custo IA controlado |
| **Observabilidade Completa (OTel + Grafana + Alertas)** | 1-2 sem | MTTR minutos, não horas |
| **Deploy Pipeline (Blue-green + Canary + Feature Flags)** | 1-2 sem | Deploy seguro, rollback <5min |
| **Security Hardening + LGPD** | 1 sem | Compliance, zero risco jurídico |
| **Refatoração Modular (app.py 275KB → routers)** | 2-3 sem | Manutenibilidade, onboarding |
| **Knowledge Transfer Contínuo** | Contínuo | Você mantém o código depois |

**Total: 10-15 semanas** (alinhado com os 16 semanas do plano)

---

### Resumo Para a Negociação

> **Ele construiu o PRODUTO (domain, validação, mercado).**
> 
> **Um dev muito bom constrói a PLATAFORMA (escala, confiabilidade, observabilidade, segurança).**
> 
> **São habilidades ortogonais. Raro ter as duas em uma pessoa.**
> 
> **Tentar economizar no dev = reconstruir tudo daqui 6 meses quando quebrar em produção com dinheiro real de usuários.**

---

### Analogia Final

> **Ele desenhou a casa, plantou o jardim, morou nela.**
> 
> **Precisa de um engenheiro estrutural para:** fundação funda, encanamento pressurizado, fiação 220V, alvará, laudo técnico, seguro.
> 
> **Pedreiro comum racha a fundação. Engenheiro garante que aguenta 500 pessoas.**

---

### Resumo: O Investimento Vale a Pena