# Ser dono da infraestrutura: mapa completo de estudos

Este documento é o curriculum para você (proprietário) dominar, de ponta a ponta, tudo o que roda neste repositório — da requisição HTTP entrando na AWS até a linha gravada no PostgreSQL. Está organizado em **camadas**, da mais externa à mais interna. Para cada camada: o que é, por que existe aqui, a ponte com Python (que você já sabe), o arquivo deste repo que a implementa e como praticar.

**Regra de ouro:** você não precisa dominar tudo de uma vez. A Fase 0 (local) exige só as camadas 1, 9 e 10. A Fase 1 (Lightsail) acrescenta 2, 4 e 12. A Fase 2+ (ECS/RDS) traz o resto. Estude na ordem em que o dinheiro for gasto — nunca antes.

---

## 0. Fundamentos que valem para todas as camadas

**O que é:** DNS, HTTP/TLS, Linux básico (processos, portas, `curl`, logs), Git.

**Por que existe:** toda camada abaixo fala HTTP sobre TLS em algum host:porta. Se você não consegue responder "o que acontece entre digitar `api.bancaemdia.com` e o bytes chegarem no processo Python", o resto é mágico.

**Ponte Python:** você já fez isso com `httpx`/`requests` e `uvicorn`. DNS é um dicionário global distribuído (nome → IP); TLS é um canal criptografado; uvicorn é o servidor que escuta a porta.

**Praticar:**
- `curl -v https://api.bancaemdia.com/health` e ler cada linha (DNS → TCP → TLS → HTTP → resposta).
- `openssl s_client -connect api.bancaemdia.com:443 -servername api.bancaemdia.com` para ver o certificado.
- Rodar a stack com `docker compose up` e derrubar o Postgres de propósito: observar o erro, os logs, o `/ready` degradando.

---

## 1. Docker e docker-compose (Fase 0+)

**O que é:** cada serviço (API, PostgreSQL, Redis, workers) é um contêiner — um processo Linux isolado com seu próprio filesystem. Compose orquestra vários deles numa rede local.

**Por que existe aqui:** é a unidade de deploy em **todas** as fases (local, Lightsail, Fargate). "Mesma imagem em dev e produção" é uma garantia mantida da decisão de orçamento.

**Ponte Python:** imagem ≈ venv congelado + código; contâiner ≈ `python -m venv` rodando; compose ≈ um script que sobe todos os serviços com os env vars certos.

**Onde no repo:** [`docker-compose.yml`](../docker-compose.yml), [`Dockerfile`](../Dockerfile), [`.dockerignore`](../.dockerignore).

**Praticar:**
1. `docker compose up` → derrubar só o worker (`docker compose stop`) → ver a fila acumular no Redis.
2. Alterar código, `docker compose build api`, medir o tamanho das camadas (`docker history`).
3. Entender por que `.dockerignore` exclui `.env`: segredo nunca entra na imagem.

---

## 2. AWS Lightsail e computação simples (Fase 1)

**O que é:** VPS da AWS com preço fixo mensal. É "um computador Linux alugado" com IP estático opcional, snapshots e firewall próprio.

**Por que existe aqui:** decisão de orçamento (PR #128 → `docs/decisions/aws-initial-budget.md`): Fase 1 cabe em US$ 24/mês numa instância 4 GB, em vez do Terraform de US$ 1.641/mês.

**Ponte Python:** é o `ssh` no servidor onde você rodaria `docker compose up -d` — como hospedar o finAgent numa máquina qualquer, só que com disco/backup gerenciados.

**Praticar:**
1. Criar instância Lightsail, anexar IP estático, apontar um DNS (subdomínio de teste).
2. Instalar Docker, clonar o repo, subir com compose, expor 443 via Caddy (ver camada 4).
3. Criar snapshot, **restaurar um snapshot** — o snapshot que nunca foi restaurado não é backup, é decoração.

---

## 3. PostgreSQL: o coração do sistema

**O que é:** banco relacional. Aqui ele faz três papéis: dados, **Row-Level Security** (RLS) e fila de locks (`FOR UPDATE`).

**Por que existe aqui:** RLS é o mecanismo central de isolamento multi-tenant — cada query só vê linhas do `app.current_user_id` da sessão. Os triggers de `audit_log` e `require_active_tenant` (migração `011`) rodam dentro dele.

**Ponte Python:** RLS ≈ um `Depends(get_current_user)` aplicado *dentro* do banco, em toda query — não dá pra esquecer no código. Trigger ≈ um signal/hook do Django/SQLAlchemy executado no servidor.

**Onde no repo:** [`src/bancaemdia/db/`](../src/bancaemdia/db/), migrações em [`alembic/versions/`](../alembic/versions/) (especialmente `011_audit_log.py`), testes em [`tests/integration/test_auth_rls.py`](../tests/integration/test_auth_rls.py).

**Praticar:**
1. Conectar com `psql`, `SET app.current_user_id = '1'`, consultar `apostas`; depois trocar para `'2'` e ver zero linhas.
2. Tentar `DELETE FROM audit_log` e ver o trigger `append-only` rejeitar.
3. Entender `EXPLAIN ANALYZE` numa query lenta do painel.
4. Backup e restore: `pg_dump` → dropar tabela → restaurar.

---

## 4. HTTPS na borda: Caddy (Fase 1) → ALB (Fase 2+)

**O que é:** terminador TLS — o processo que recebe HTTPS e passa HTTP limpo para a API. Certificados via Let's Encrypt.

**Por que existe:** na Fase 1 é Caddy dentro da instância (grátis, automático); na Fase 2+ vira o ALB da AWS (US$ 16+/mês) com WAF. O Terraform do ALB já existe, congelado.

**Ponte Python:** Caddy ≈ um proxy reverso na frente do uvicorn, como nginx/certbot automatizado.

**Onde no repo:** [`infra/terraform/modules/`](../infra/terraform/modules/) (ALB, listener TLS 1.2+), headers de segurança em [`src/bancaemdia/security/http.py`](../src/bancaemdia/security/http.py).

**Praticar:**
1. Subir Caddy com um `Caddyfile` de 3 linhas e ver o cadeado verde.
2. Ler os headers de segurança com `curl -I` (CSP, HSTS, X-Frame-Options) — o middleware os grava; o proxy só entrega.

---

## 5. Redis e Celery: o pipeline assíncrono

**O que é:** Redis é o broker (fila) + cache; Celery é o executor de tarefas em processos separados (extraction, materialization, beat).

**Por que existe aqui:** um print de aposta demora segundos na IA — não pode segurar a requisição HTTP. A Fila desacopla: a API agenda, o worker processa, a materialização grava.

**Ponte Python:** Celery ≈ `asyncio.create_task` que sobrevive a restart e distribui entre máquinas. Redis ≈ um dicionário com TTL + listas usadas como fila.

**Onde no repo:** [`src/bancaemdia/workers/`](../src/bancaemdia/workers/), [`src/bancaemdia/workers/celery_app.py`](../src/bancaemdia/workers/celery_app.py), cache de extração em [`src/bancaemdia/extracao/`](../src/bancaemdia/extracao/).

**Praticar:**
1. Enviar upload, `docker compose logs -f`, seguir a tarefa da fila até a aposta gravada.
2. Matar o worker no meio de uma tarefa (`docker kill`) e ver o reenfileiramento (acks tardios — configurado por causa do Spot, camada 8).
3. `redis-cli LLEN` nas filas para ver profundidade.

---

## 6. Autenticação: JWT RS256 + JWKS

**O que é:** o emissor de identidade (a definir) assina tokens com chave privada RSA; a API **só verifica** com a chave pública publicada em JWKS.

**Por que existe aqui:** stateless — nenhuma sessão no banco. A troca python-jose → PyJWT (PR #130) removeu uma dependência vulnerável e foi feita com checagem explícita de `alg` e `kid` (previne alg-confusion).

**Ponte Python:** JWT ≈ um dict JSON assinado; RS256 ≈ assinatura criptográfica assimétrica (como verificar assinatura com `cryptography`, que você já viu nos testes). JWKS ≈ um endpoint público com a "chave pública do Porteiro".

**Onde no repo:** [`src/bancaemdia/auth/jwt.py`](../src/bancaemdia/auth/jwt.py), [`src/bancaemdia/auth/middleware.py`](../src/bancaemdia/auth/middleware.py).

**Praticar:**
1. Gerar par RSA, assinar um token "na mão" com PyJWT, chamar a API com e sem expiração válida.
2. Forjar um token com `alg: none` e ver o 401.
3. Entender o fluxo de rotação de `kid` (documentado em [`docs/SECURITY.md`](SECURITY.md), item 4).

---

## 7. Migrações com Alembic (expand-only)

**O que é:** versionamento de schema — cada arquivo em `alembic/versions/` é uma migração com `upgrade()`/`downgrade()`.

**Por que existe aqui:** o runbook de migração exige **expand-only** (adicionar, nunca destruir no mesmo deploy) para permitir rollback. `conferir_numeros.py` valida integridade depois.

**Ponte Python:** Alembic ≈ `manage.py migrate` do Django; expand-only ≈ nunca quebrar contrato de API — primeiro adiciona, depois (outro deploy) remove.

**Praticar:**
1. Criar uma migração que adiciona coluna nullable, aplicar, fazer downgrade.
2. Ler `011_audit_log.py` linha por linha — é o exemplo mais completo do repo (tabela + RLS + 2 funções trigger).

---

## 8. AWS gerenciado: RDS, ElastiCache, ECS/Fargate, S3, Secrets Manager, IAM (Fase 2+)

**O que é:** as versões gerenciadas da AWS do que você roda em Docker na Fase 1. Fargate = contêineres sem servidor; RDS = PostgreSQL gerenciado com failover; Secrets Manager = cofre de segredos; IAM = quem pode fazer o quê.

**Por que existe:** alta disponibilidade e escala quando a receita justificar (gatilho da decisão: custo real > R$ 150/mês por 2 meses ou exigência de SLA).

**Ponte Python:** ECS service ≈ `docker compose up` que a AWS mantém vivo; Secrets ≈ variáveis de `settings.py` vindas de um cofre; IAM ≈ permissões de `Depends` no nível da infra.

**Onde no repo:** [`infra/terraform/`](../infra/terraform/) — **leia como documentação, não aplique** (`TF_APPLY_ENABLED` desabilitado por decisão).

**Praticar (na Fase 2, com billing alarm ligado):**
1. Subir um RDS single-AZ barato em conta sandbox, conectar a API local nele.
2. Entender o aviso de 2 minutos do Fargate Spot e por que o `stopTimeout: 120` existe (módulo ECS).
3. Política de least privilege: escrever uma policy IAM que permite só `s3:GetObject` num bucket.

---

## 9. Observabilidade: logs, métricas, traces

**O que é:** três pilares — logs estruturados (JSON com `request_id`), métricas (Prometheus, `/metrics`), traces (OpenTelemetry). Alertas saem das métricas.

**Por que existe:** sem isso você opera às cegas. Toda decisão de incidente começa em "qual trace/request_id?".

**Ponte Python:** structlog ≈ logging com `extra={}` tipado; Prometheus ≈ contadores/gauges num dict global exposto via HTTP; OTel ≈ middleware que carrega um contexto por request (como `contextvars`, que o projeto já usa pra tenant).

**Onde no repo:** [`src/bancaemdia/observability/`](../src/bancaemdia/observability/), alertas em [`infra/terraform/modules/alerting/`](../infra/terraform/modules/alerting/).

**Praticar:**
1. Rodar a stack, gerar erro proposital, achar o `request_id` no log e segui-lo.
2. `curl localhost:8000/metrics | grep http_requests` antes/depois de um burst.
3. Subir Prometheus + Grafana locais no compose e montar 1 painel com latência p95.

---

## 10. Testes: pytest, contrato OpenAPI, idempotência, chaos, k6

**O que é:** camadas de garantia — unitários, integração (PostgreSQL real), contrato (o OpenAPI commitado **é** o contrato), idempotência (mesma requisição 2× = 1 efeito), chaos (derruba dependências), carga (k6).

**Por que existe:** a assinatura da issue #43 exige pip-audit/bandit na CI; a #36 exige zero duplicação; a #38 exige sobreviver a falhas. CI verde é pré-requisito de qualquer merge (PR shape do workflow).

**Ponte Python:** você já conhece pytest — a novidade é docker nos testes de integração e k6 (JavaScript) para carga.

**Praticar:**
1. Quebrar um schema de resposta de propósito e ver o contrato falhar.
2. Rodar `pytest tests/idempotency -v` e ler as 15 combinações (5 origens × 3 cenários).
3. Rodar `k6 run k6/load-test.js` contra o compose local.

---

## 11. CI/CD: GitHub Actions, SBOM, canary

**O que é:** pipeline que roda lint/format/mypy/security/test/contract/docker a cada PR (`ci.yml`) e o pipeline de deploy com canary (`cd.yml`, Fase 2+).

**Por que existe:** "nunca commitar em main; tudo passa por PR com CI verde" — é o contrato do workflow do repo.

**Praticar:**
1. Abrir um PR com um erro de digitação e ver o CI bloquear.
2. Ler `ci.yml` e nomear cada job; adicionar um step inofensivo (ex.: `python --version`) e ver rodar.
3. Na Fase 2: ler `cd.yml` e os runbooks ([`docs/runbooks/`](runbooks/)) antes do primeiro deploy real.

---

## 12. Custos e billing da AWS

**O que é:** AWS Budgets (alerta em US$ 50 — decisão registrada), Cost Explorer, entender os drivers: horas de instância, NAT (caro!), armazenamento, tráfego.

**Por que existe:** o teto do projeto é financeiro (R$ 200/mês). A conta Free Plan tem US$ 100 de créditos **totais**, não mensais — já documentado na decisão.

**Praticar:**
1. Criar o alerta de Budgets **antes** de ligar qualquer recurso.
2. Todo domingo: abrir Cost Explorer e explicar cada centavo da semana. Se não consegue explicar um item, desligue-o até entender.

---

## 13. LGPD e privacidade

**O que é:** direitos do titular (acesso, eliminação), minimização de dados, base legal para retenção.

**Por que existe aqui:** `GET/DELETE /api/v1/usuario/me(...)` (PR #130) implementam exportação e anonimização; dados brutos do Telegram ainda **não têm dono** (409 na exclusão assistida) — pendência aberta que exige decisão sua.

**Onde no repo:** [`docs/SECURITY.md`](SECURITY.md) (seção "Antes de operar em produção"), [`src/bancaemdia/api/v1/usuario.py`](../src/bancaemdia/api/v1/usuario.py).

**Praticar:**
1. Rodar a exportação de um usuário de teste e ler o JSON inteiro — você consegue explicar cada campo ao titular?
2. Simular a exclusão com e sem dados de Telegram e observar o 409.

---

## 14. Segurança prática

**O que é:** headers de segurança, limites de corpo por endpoint, TLS 1.2+ em tudo, segredos fora de código/env/git, varredura contínua (pip-audit, bandit, GitGuardian).

**Onde no repo:** [`src/bancaemdia/security/http.py`](../src/bancaemdia/security/http.py), `.dockerignore`, `docs/SECURITY.md`.

**Checklist do dono (revisar a cada release):**
- [ ] Nenhum segredo em git (`git log -p | grep -i "sk-"` deve retornar vazio).
- [ ] Rotação de chave de IA trimestral executada (runbook em `docs/SECURITY.md`, item 3).
- [ ] Rotação anual de JWT keys (item 4).
- [ ] `pip-audit` e `bandit` verdes na CI.
- [ ] Restore de backup testado no trimestre.

---

## Plano de estudo sugerido (ordem)

| Semana | Camadas | Entrega de prova |
|---|---|---|
| 1–2 | 0, 1, 5 | Stack local de pé de cabeça, explicando cada serviço de cor |
| 3–4 | 3, 7 | RLS demonstrado ao vivo; uma migração escrita por você |
| 5–6 | 6, 10 | Um token assinado na mão; CI verde num PR seu |
| 7–8 | 2, 4, 12 | Fase 1 no ar com domínio real e Budgets ligado |
| 9+ | 8, 9, 11 | Só quando os gatilhos da Fase 2 chegarem |

A cada camada, se travar mais de 1 hora: pare, anote a dúvida e traga — explicar é exatamente o que este processo de aprendizado prevê.
