# Teste de carga de staging

`k6 run k6/load-test.js` executa os quatro perfis em sequência (55 minutos). `LOAD_PROFILE=steady|spike|coleta|painel` executa um perfil isolado. Os limites em `config.js` fazem o k6 sair com erro quando p95 ≥ 1 s, p99 ≥ 2 s, falhas HTTP ≥ 1%, checks ≤ 99%, profundidade da fila de extração ≥ 100, atraso da réplica ≥ 30 s ou telemetria estiver ausente. O relatório HTML e o resumo JSON são gravados no diretório de execução.

| Perfil | Carga | Fluxo |
| --- | --- | --- |
| steady | 50 VUs; 5 min subida, 20 min estável, 5 min descida | Um upload por VU, consulta de status e painel a cada 10 s |
| spike | 200 VUs; 2 min subida, 6 min estável, 2 min descida | Mesmo fluxo de upload, com pico de usuários |
| coleta | 100 VUs por 5 min | Um POST `/coleta` por VU a cada 6 s, com aposta sintética distinta |
| painel | 100 VUs por 10 min | GET `/api/v1/painel` a cada 10 s |

O fluxo de upload usa `fixtures/telegram-small.zip`, um export sintético com uma foto. O mesmo arquivo é compartilhado entre usuários; o cache de extração pode servir a imagem após a primeira leitura. Assim, o teste mede upload, filas, autenticação, status e leitura do painel, mas **não** equivale a 200 imagens inéditas por dia nem mede 200 chamadas independentes à IA. A meta de 200 apostas/dia da issue é uma referência de capacidade, não uma taxa reproduzida por esta amostra. Não há endpoints HTTP de login/logout nesta API: os tokens são pré-provisionados e cada VU usa seu próprio token Bearer.

## Preparação

1. Use um ambiente de staging isolado, com no mínimo 250 usuários e tokens JWT de teste válidos por mais de 55 minutos e 100 tokens de coleta. Os primeiros 50 JWTs são do steady state; os 200 seguintes são do spike, evitando deduplicação por usuário entre perfis. Evite dados ou credenciais de produção. Os arrays JSON são passados por `JWT_TOKENS_JSON` e `COLETA_TOKENS_JSON`, respectivamente. Se os JWTs excederem o limite de tamanho de um segredo do GitHub, divida a lista em até cinco arrays `JWT_TOKENS_JSON_1` a `_5`, mantendo a ordem.
2. Defina `BASE_URL` como origem HTTPS do staging. `METRICS_URL` pode apontar para o endpoint Prometheus da API acessível ao executor; por padrão usa `${BASE_URL}/metrics`. Este endpoint precisa expor `celery_queue_depth{queue="extraction"}` e `pg_replication_lag_seconds{role="replica"}`. A ausência de qualquer série falha o teste.
3. Para o perfil coleta, configure `COLETA_IP_RATE_LIMIT` do staging para ao menos `1200/minute`, pois o limite padrão de `60/minute` por IP rejeita o gerador único. Mantenha `COLETA_RATE_LIMIT=10/minute` por token. Aumente a capacidade somente no staging e isole os usuários do teste; respostas 429 fazem os checks falharem.
4. Execute `k6 run k6/load-test.js`. Para um perfil isolado, defina `LOAD_PROFILE` antes do comando. HTTP sem TLS só é aceito para `localhost`/`127.0.0.1` com `ALLOW_HTTP_LOCAL=1`.

O workflow `load-test.yml` valida a sintaxe em todos os PRs. Para PRs do próprio repositório, executa o teste completo e barra o PR por limiares quando `STAGING_BASE_URL` está configurada como variável do repositório. PRs de forks executam somente a validação sem segredos. O agendamento semanal e a execução manual exigem `STAGING_BASE_URL`, `STAGING_METRICS_URL` opcional, os segredos `STAGING_JWT_TOKENS_JSON` (ou shards `_1` a `_5`) e `STAGING_COLETA_TOKENS_JSON` no ambiente `staging`; falham explicitamente se faltarem. O workflow envia os relatórios como artefatos mesmo se o teste falhar.

O job de validação também roda `k6/smoke.js` contra `k6/tests/mock_server.py` por 12 segundos com `K6_LOCAL_SMOKE=1`, cobrindo os quatro cenários e a interpretação das métricas, sem gerar carga externa. Essa variável só encurta os intervalos para o smoke local e não é definida no teste de staging.
