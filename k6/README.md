# Teste de carga de staging

`python k6/run.py` executa o k6 com os quatro perfis em sequência (55 minutos). `LOAD_PROFILE=steady|spike|coleta|painel` executa um perfil isolado. Os limites em `config.js` fazem o k6 sair com erro quando p95 ≥ 1 s, p99 ≥ 2 s, falhas HTTP ≥ 1%, checks ≤ 99%, profundidade da fila de extração ≥ 100, atraso da réplica ≥ 30 s ou telemetria estiver ausente. O relatório HTML e o resumo JSON são gravados no diretório de execução.

O launcher mantém um alocador de credenciais somente em loopback, durante o processo k6.
Cada VU usa seu `idInTest` suportado para obter uma credencial estável e exclusiva por
cenário, mesmo quando os IDs são esparsos ou os VUs são reutilizados. Steady usa os 50
primeiros JWTs e spike os 200 seguintes. O campo antigo `idInScenario` não existe na API
de execução do k6 e enviava `Bearer undefined`. O alocador nunca emite tokens nem muda
a autenticação do backend; apenas distribui os tokens fornecidos. Não grava logs de
acesso. Além dos limiares globais, limiares iguais exclusivos do tráfego da API impedem
que as poucas requisições locais de alocação diluam as métricas de desempenho/falha.
O servidor local aceita o início simultâneo dos 100 VUs de coleta/painel. A coleta mantém
a cadência de seis segundos e, ao consumir as dez requisições da janela, aguarda o
`X-RateLimit-Reset` informado pela API. Isso evita antecipar a renovação por variação de
fila/recebimento no servidor. Respostas 429 continuam reprovando o teste.
As consultas síncronas do SlowAPI/Redis, inclusive a leitura da janela para os cabeçalhos
de sucesso/429, rodam no thread pool do backend. O loop ASGI continua atendendo outros
pedidos enquanto o armazenamento aguarda I/O, preservando quotas, fallback e cabeçalhos.
Nos filhos prefork, a materialização de coleta reutiliza um loop e uma conexão PostgreSQL
por processo, com pre-ping, em vez de abrir uma conexão por captura. Cada chamada recebe
um contexto novo; tarefas pendentes são canceladas ao terminar e shutdown fecha engine/loop.
Fora dos filhos prefork, chamadas diretas, CLI/eager e outros pools mantêm `asyncio.run` e
`NullPool`. Upload e extração mantêm seus fluxos. Testes com PostgreSQL real conferem que
a conexão é reutilizada sem conservar o tenant da transação anterior.
Coleta e upload acompanham o processamento no PostgreSQL e publicam suas tarefas sem
assinar resultados Celery não consumidos; retries e o errback de upload permanecem ativos.

| Perfil | Carga | Fluxo |
| --- | --- | --- |
| steady | 50 VUs; 5 min subida, 20 min estável, 5 min descida | Um upload por VU, consulta de status e painel a cada 10 s |
| spike | 200 VUs; 2 min subida, 6 min estável, 2 min descida | Mesmo fluxo de upload, com pico de usuários |
| coleta | 100 VUs por 5 min | Um POST `/coleta` por VU a cada 6 s, com aposta sintética distinta |
| painel | 100 VUs por 10 min | GET `/api/v1/painel` a cada 10 s |

O fluxo de upload usa `fixtures/telegram-small.zip`, um export sintético com uma foto. O mesmo arquivo é compartilhado entre usuários; o cache de extração pode servir a imagem após a primeira leitura. Assim, o teste mede upload, filas, autenticação, status e leitura do painel, mas **não** equivale a 200 imagens inéditas por dia nem mede 200 chamadas independentes à IA. A meta de 200 apostas/dia da issue é uma referência de capacidade, não uma taxa reproduzida por esta amostra. Não há endpoints HTTP de login/logout nesta API: os tokens são pré-provisionados e cada VU usa seu próprio token Bearer.

## Preparação

### Fase 0: ambiente descartável automático na CI

Sem `STAGING_BASE_URL`, o job `staging` cria seu próprio ambiente no runner Linux do
GitHub Actions. Roda o backend do commit em teste, PostgreSQL 16 com streaming replica
real, Redis, até quatro workers HTTP conforme as CPUs do runner (keep-alive de 30 segundos
para os intervalos de 6/10 s), dois processos Celery de extração e um de materialização,
para dividir as CPUs com API/banco/gerador, e refresh das
materialized views. Cria 250 usuários sintéticos e 100 tokens de coleta, assina JWTs RS256
com chave efêmera e serve o JWKS local. Não é o `mock_server.py` do smoke.

O pool mantém 30 conexões por engine (`DB_POOL_SIZE=30`, `DB_POOL_MAX_OVERFLOW=0`),
mesmo máximo de 30 dos padrões 10+20, evitando reconexão do overflow a cada rajada.
`DB_POOL_PREWARM=true` autentica as conexões de primário/réplica antes de concluir o startup.
A opção é desligada por padrão nos demais ambientes. Falha de conexão impede prontidão
e fecha os pools. A prova mede o backend pronto; não mede o tempo de arranque.
`DB_POOL_RECYCLE_SECONDS=7200` cobre o job de até 90 minutos, sem recriar conexões
saudáveis durante a carga; `pool_pre_ping` continua ativo. O padrão fora deste staging
permanece em 300 segundos. CPUs, workers e configurações do pool ficam na evidência.

Os quatro perfis completos mantêm 55 minutos e todos os limiares originais. `K6_LOCAL_SMOKE`
é recusada neste job. A verificação posterior exige uploads concluídos, apostas Telegram
persistidas, apostas Casa materializadas, streaming ativo e RLS sem acesso entre tenants.
Um preflight autenticado de 30 segundos usa a API real antes da carga; falha rapidamente
se houver problemas de credenciais. Ele não substitui os quatro perfis completos.
Para o perfil completo, a Fase 0 também executa primeiro cinco minutos de coleta com 100
VUs, os mesmos limiares e verificação de materialização. Isso detecta problemas de início
simultâneo, quota e pipeline antes dos 55 minutos. As coletas dessa etapa permanecem no
banco e suas contagens são incluídas na verificação final, além das coletas da carga completa.
Seus relatórios próprios são publicados como `k6-coleta-preflight-*`.
Os relatórios incluem `k6-staging-evidence.json` com SHA, contagens e fronteiras da prova.
O mesmo cluster também executa `tests/integration/test_router_db.py` num banco separado,
com `TEST_REPLICA_DATABASE_URL` real. O job exige JUnit sem skips/falhas para roteamento,
RLS, leitura após gravação e pausa/retomada do replay. Esses testes não limpam os dados
da carga. Os skips históricos desse módulo na suíte principal ficam comprovados por
este job dedicado. `k6-router-test-results.xml` é publicado junto aos relatórios.

O arquivo sintético já versionado `fixtures/telegram-small.zip` tem sua leitura sintética
pré-carregada no cache real de extração. A IA aponta para uma porta local sem serviço;
qualquer cache miss falha, sem chamada à IA paga. Isto mede o percurso com cache aquecido,
não qualidade/latência da IA nem imagens inéditas. Não usa as fixtures pendentes de revisão
da #114. HTTP é restrito ao loopback por `ALLOW_HTTP_LOCAL=1`, já suportado pelo k6.

Senhas, JWTs e chaves HMAC são aleatórios, mascarados e escritos apenas em diretório privado
do runner, fora do checkout. A API e os workers usam papel PostgreSQL sem SUPERUSER ou
BYPASSRLS. Migração, seed e refresh usam a credencial administrativa só deste banco novo.
O script recusa execução fora do GitHub Actions Linux e destinos fora de `RUNNER_TEMP` ou
dentro do checkout. Cleanup remove somente containers/rede etiquetados por esta execução;
Os resumos excluem `setup_data`, que contém tokens. Nenhuma credencial ou log bruto é
enviado como artefato.

O staging descartável usa HTTPS real na API e no webhook interno. Um certificado
localhost com SAN IP/DNS e chave privada efêmera fica em `RUNNER_TEMP`; `SSL_CERT_FILE`
configura a confiança local de Python/k6, sem desativar a validação TLS. O teste de
segurança aceita o certificado confiável e recusa uma conexão sem essa confiança.
Os resumos incluem contagem/tamanho de auditoria por tipo de recurso, sem IDs ou valores.

Isso corresponde à Fase 0 da [decisão AWS](../docs/decisions/aws-initial-budget.md).
Não provisiona AWS nem substitui prova de TLS, capacidade/custos da Lightsail, backup,
restauração ou sign-off de lançamento (#41/#44). Os recursos do ambiente terminam ao final
de cada job. Após o merge desta configuração, PRs do repositório, agendamento semanal e
execução manual funcionam sem cadastro de URL ou segredos externos. Forks continuam sem
acesso ao job de staging.

### Staging externo, quando provisionado

1. Use um ambiente de staging isolado, com no mínimo 250 usuários e tokens JWT de teste válidos por mais de 55 minutos e 100 tokens de coleta. Os primeiros 50 JWTs são do steady state; os 200 seguintes são do spike, evitando deduplicação por usuário entre perfis. Evite dados ou credenciais de produção. Os arrays JSON são passados por `JWT_TOKENS_JSON` e `COLETA_TOKENS_JSON`, respectivamente. Se os JWTs excederem o limite de tamanho de um segredo do GitHub, divida a lista em até cinco arrays `JWT_TOKENS_JSON_1` a `_5`, mantendo a ordem.
2. Defina `BASE_URL` como origem HTTPS do staging. `METRICS_URL` pode apontar para o endpoint Prometheus da API acessível ao executor; por padrão usa `${BASE_URL}/metrics`. Este endpoint precisa expor `celery_queue_depth{queue="extraction"}` e `pg_replication_lag_seconds{role="replica"}`. A ausência de qualquer série falha o teste.
3. Para o perfil coleta, configure `COLETA_IP_RATE_LIMIT` do staging para ao menos `1200/minute`, pois o limite padrão de `60/minute` por IP rejeita o gerador único. Mantenha `COLETA_RATE_LIMIT=10/minute` por token. Aumente a capacidade somente no staging e isole os usuários do teste; respostas 429 fazem os checks falharem.
4. Execute `python k6/run.py` (Python 3.12 e k6 no PATH). Para um perfil isolado, defina `LOAD_PROFILE` antes do comando. HTTP sem TLS só é aceito para `localhost`/`127.0.0.1` com `ALLOW_HTTP_LOCAL=1`. A chamada direta `k6 run k6/load-test.js` requer `K6_CREDENTIALS_URL` de um launcher ativo; prefira o comando Python para iniciar/encerrar esse alocador automaticamente.

O workflow `load-test.yml` valida a sintaxe em todos os PRs. PRs do próprio repositório executam o teste completo e são barrados pelos limiares. Sem URL externa, usam a Fase 0 acima. Para usar staging externo, configure `STAGING_BASE_URL` como variável do **repositório**, `STAGING_METRICS_URL` opcional, os segredos `STAGING_JWT_TOKENS_JSON` (ou shards `_1` a `_5`) e `STAGING_COLETA_TOKENS_JSON` no ambiente `staging`. URL externa configurada com tokens ausentes falha explicitamente, sem fallback para outro ambiente. PRs de forks executam somente a validação sem segredos. O workflow envia os relatórios como artefatos mesmo se o teste falhar.

O job de validação também roda `k6/smoke.js` contra `k6/tests/mock_server.py` por 12 segundos com `K6_LOCAL_SMOKE=1`, cobrindo os quatro cenários e a interpretação das métricas, sem gerar carga externa. Essa variável só encurta os intervalos para o smoke local e não é definida no teste de staging.
