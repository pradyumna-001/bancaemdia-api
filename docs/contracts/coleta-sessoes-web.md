# Sessões de coleta para o proprietário web — #204 / frontend #34

## Operação e tipos

`GET /api/v1/coleta/installations` (`list_installations_api_v1_coleta_installations_get`)
continua sendo a listagem existente; não há endpoint ou credencial novo para a
SPA. Autenticação por cookie HttpOnly atual, ou Bearer do usuário conforme a
política de identidade. **READ_ONLY é suficiente**; não há 402 comercial nessa
leitura. HTTPS, CORS exata, sanitização e `Cache-Control: private, no-store` com
identidade web ativa permanecem. Sem identidade web ativa: 401, mesmo quando
`X-Coleta-Token` válido foi enviado. Não passar esse header pelo site.

`InstallationResponse` conserva os campos anteriores e acrescenta dois campos
sempre presentes: `estado_sessao` e `sessao_coleta`. O objeto não nulo usa
`OwnerCollectionSessionResponse`. UUIDs são referências opacas, não autorização.
O ID numérico legado da instalação é preservado; para identificar dispositivos
sem conversão numérica no cliente, conservar `instalacao_publica_id` UUID.

```http
GET /api/v1/coleta/installations
Cookie: __Host-bancaemdia_session=<cookie-gerenciado-pelo-navegador>
```

Recorte seguro de uma linha da resposta (os timestamps anteriores permanecem):

```json
{
  "id": 42,
  "instalacao_publica_id": "69a449c0-bca2-49c2-85d8-b8d8b86048ed",
  "nome_dispositivo": "Notebook sintético",
  "token_prefixo": "cti_exemplo",
  "criado_em": "2026-10-16T12:00:00Z",
  "pareado_em": "2026-10-16T12:00:00Z",
  "ultimo_uso_em": "2026-10-16T12:01:00Z",
  "rotacionado_em": null,
  "revogado_em": null,
  "expira_em": null,
  "estado_sessao": "aberta",
  "sessao_coleta": {
    "sessao_id": "1118e69c-72fd-48fc-bd67-1b0505c25eca",
    "selecao": "atual",
    "coletar_desde": "2026-08-01T00:00:00.123456Z",
    "criada_em": "2026-10-16T12:01:00Z",
    "encerrada_em": null
  }
}
```

Exemplo sintético: o prefixo é somente a pista já existente, jamais credencial.
Não há token completo/hash, corpo de captura, segredo de autenticação ou estado
de jobs nessa projeção.

## Seleção, nulidade e ordenação

| estado_sessao | sessao_coleta | selecao | encerrada_em | Significado |
| --- | --- | --- | --- | --- |
| `sem_sessao` | `null` | não há objeto | não há objeto | Nenhuma sessão persistida para essa instalação; não existe fronteira a anunciar. |
| `aberta` | objeto | `atual` | `null` | Sessão aberta no snapshot do banco, com a fronteira original. |
| `encerrada` | objeto | `ultima_encerrada` | timestamp | Não há aberta; é a última sessão histórica segundo a ordem publicada. |

Uma sessão aberta sempre precede as encerradas. Sem aberta, ordenar sessões por
`criada_em DESC, id interno DESC`; o segundo critério desempata timestamps iguais.
O ID interno da sessão não é divulgado. A restrição de banco de uma aberta por
instalação permanece. Fronteira não é relógio de último uso nem data de criação.
Datas são instantes ISO-8601 com fuso; a precisão original de microssegundos é
preservada. `criada_em` é criação da sessão, `criado_em` é criação do dispositivo.

A lista inclui **todas** as instalações próprias, inclusive revogadas/expiradas,
ordenadas por `id ASC`. Não há página, cursor, limite oculto ou truncamento. Uma
consulta SQL projeta todos os dispositivos e sua sessão selecionada, sem N+1,
filtrada por proprietário e protegida pelas RLS existentes. Um único statement
assegura estado/fronteira/seleção coerentes no mesmo snapshot. Outra pessoa tem
sua própria lista, sem sinalização de IDs existentes em outro tenant; a lista
vazia é `[]`. Rotação/revogação de ID alheio ou inexistente têm o mesmo 404.

## Estado verdadeiro e efeitos

Sessão aberta **não comprova online/conexão**. `ultimo_uso_em` registra o uso já
realizado pela instalação; o GET web não o altera. Validade da credencial e
estado da sessão são eixos independentes: rotação, expiração e revogação não
encerram uma sessão histórica automaticamente. Reconectar/reparear o mesmo
UUID próprio mantém a instalação e as sessões; retomar a sessão explícita mantém
`coletar_desde`. Não ampliar o corte nem criar transições na SPA.

Esse GET não abre/retoma/encerra sessão, não autentica como extensão, não ingere
capturas, não registra uso da extensão e não cria eventos/apostas/revisões.
A projeção pode mudar quando a extensão efetuar uma operação posterior; não é
comprovante de uma mutation cujo resultado o site desconheça. ACK de submissão
permanece distinto de resultado financeiro materializado nos contratos v2.
Nenhum histórico novo de jobs foi acrescentado.

## Permissões, controles e falhas

| Operação | Permissão e autenticação |
| --- | --- |
| GET installations | Leitura própria READ_ONLY, cookie/Bearer web; sem CSRF para GET. |
| POST pairing-codes | Exceção de controle existente, inclusive READ_ONLY; cookie exige Origin exata e CSRF. |
| POST installations/{id}/rotate | Exceção de controle existente, somente própria e não revogada; cookie exige Origin/CSRF. |
| DELETE installations/{id} | Exceção de controle existente, somente própria; cookie exige Origin/CSRF. |
| pairing-exchange | Código descartável entregue à extensão; não é autenticação web. |
| sessions/batches/jobs/status/catalogo | Credencial da instalação; poderes não ampliados por esta entrega. Ingestão continua sujeita ao acesso de escrita existente. |

401 exige reautenticação; 402 das escritas de negócio/ingestão é bloqueio
comercial, sem destruir sessão/formulário. 403 nos controles por cookie exige
corrigir Origin/CSRF. Queries arbitrárias e transporte HTTP são recusados com
400 sem ecoar dados. 422 dos controles mantém validação sanitizada existente.
429 publica `Retry-After` e corpo seguro `error=rate_limited` no limitador geral;
quotas próprias de pareamento conservam seu contrato. Falha SQL na autenticação
do proprietário ou projeção retorna 503, `detail=Collection management
temporarily unavailable`, `Retry-After: 5`; identidade indisponível também pode
retornar o 503 de identidade com `X-Auth-Error`. Erros inesperados conservam o
500 sanitizado. Não imprimir nem interpretar SQL/stack/diagnóstico bruto na UI.

Código e eventual nova credencial são transitórios e somente em memória, fora
de URL, query cache, storage e telemetria. Limpar ao sair da tela, expirar quando
aplicável, trocar identidade e fazer logout. Rotação devolve a credencial **uma
única vez** e invalida a antiga atomicamente. Se a resposta se perder, a leitura
não recupera esse segredo nem prova qual tentativa ocorreu: orientar novo
pareamento/ação humana conforme o runbook, sem replay automático. Nenhuma
mutation recebe retry automático. Consultar a lista pode informar o estado
atual, nunca comprovar a execução de um pedido específico desconhecido.

Não há catálogo web de domínios/versões/downloads publicado por esta entrega.
`GET /api/v1/coleta/catalogo` autentica instalação e não deve receber a sessão
web como substituto. O frontend omite anúncios de compatibilidade/permissões/
downloads que não tenham contrato. Leitores, captura, outbox e ACK ficam na
extensão/API; não são pré-requisitos artificiais desta leitura.

## Provas e proveniência

`tests/integration/coleta/test_owner_sessions.py`: **26 aceites obrigatórios**
HTTP com cookie real, JWT interno assinado, serviço de identidade real em role
restrita separada, PostgreSQL 16 e role API sem superuser/BYPASSRLS. Os dados da
sessão de autenticação são fixtures criptografadas reais; não há substituto do
autenticador, RLS ou domínio. Fixture injeta somente configuração e os engines
descartáveis nos provedores de conexão, preservando o processamento real.
O inventário fixo `scripts/owner-sessions-required.json` e o checker
`scripts/check_owner_sessions_junit.py` recusam skips/falhas/omissões/duplicatas.
Integram o job obrigatório existente de coleta antes de Docker Build.

```sh
pytest tests/integration/coleta/ tests/integration/test_coleta_db.py tests/security/test_coleta_tokens.py -n 0 --junitxml=pairing-results.xml
python scripts/check_review_acceptance.py pairing-results.xml
python scripts/check_owner_sessions_junit.py pairing-results.xml
pytest tests/contract/test_owner_collection_sessions.py tests/contract/test_openapi.py -n 0
python scripts/generate_openapi.py --check
ruff check .
ruff format --check .
mypy src/ --strict --ignore-missing-imports
```

`TEST_DATABASE_URL` deve apontar PostgreSQL 16 descartável, ou Docker disponível;
a ausência de banco não autoriza skip dos aceites. Inclui dois usuários e dois
dispositivos; sessões abertas/encerradas/ausentes/históricas com empate; RLS;
retomada concorrente/leitura, reconexão/fronteira; rotação/revogação/expiração;
cookie/CSRF/Origin/READ_ONLY; leitura sem efeitos; 105 dispositivos completos
com uma única projeção; erros 429/503 reais e ausência de segredos em resposta/
logs. As regressões existentes preservam ACK/materialização/pareamento.

PR diretamente sobre main `b916f54331f14cf47a3800324bd61d8638043c06`, sem nova
migration: usa tabelas/RLS/restrições já integradas #107/#108. Reproduz pontualmente
de #203 `b2bb0dfc66e26621d41c064f4690932938b877f5` (origem #201) os ajustes de
runtime já validados na carga integral: ASGI RLS/router e bypass das rotas já
públicas; construção cacheada de SQL parametrizado, nunca dados/permissões;
reuso da aposta resolvida no matching; preservação de Retry-After; checkpoint
rápido da réplica real; mypy estrito no ambiente do projeto e pré-aquecimento do
OpenAPI antes do loader cronometrado. Mantém todos os limiares/gates. Não importa
a feature manual nem as migrations das dependências na candidata isolada.

O pré-teste de coleta de cinco minutos expôs P95 de 1.031 ms acima do teto
inalterado de 1.000 ms. Esta entrega também remove a redefinição redundante do
contexto RLS após authenticate (já presente no pin ec46748) e projeta somente
o ID da aposta no lookup de coleta/matching, preservando filtro de tenant,
RLS e o lookup completo para os demais consumidores. A configuração do
contexto permanece dentro do autenticador; nenhum dado/permissão é cacheado.

A composição separada parte do pin real completo
`ec46748b00ce97ae3c3cac032f03cc691f7f8908` (127 operações, SHA-256
`797c735e0fa6d63f46c3eb1635e569e434154dfc02fb17d27c940ff339cfcf96`), preservando
#184/#187/#189/#193/#195/#197/#199/#201/#203 e o head conjunto `u202sandbox2026`,
inclusive tabelas/inventários XLSX. A listagem evolui no mesmo caminho: a árvore
completa continua com 127 operações. O commit/hash final da composição e suas
provas são publicados no PR. Nenhum JSON é unido manualmente.

## Handoff ao frontend

No repositório da API, buscar a branch de reconciliação indicada no PR e criar
um worktree no **commit completo imutável** publicado; executar o gerador pelo
app (`python scripts/generate_openapi.py --check`). No frontend, atualizar
`config/api-contract.json` em revisão, conservando seus demais campos e usando
`repository=pradyumna-001/bancaemdia-api`, `commit=<composição imutável>`,
`schemaPath=tests/contract/schemas/openapi.json` e `sha256=<hash completo>`.

```sh
pnpm install --frozen-lockfile
pnpm gen-types
pnpm lint
pnpm typecheck
pnpm test
pnpm exec playwright install --with-deps chromium firefox webkit
```

Gerar uma única vez pelo schema completo verificado; não misturar tipos de main
isolada e sandbox. Operação/tipos finais estão no início desta spec. Implementar
`tests/e2e/coleta.spec.ts` na #34 com a jornada real sobre a composição, duas
identidades e dois dispositivos, cookie e CSRF reais, HTTPS/proxy de mesma
origem conforme o harness de identidade existente. Confirmar que a configuração
do ensaio aponta ao sandbox real e não a handlers mockados. Então executar:

```sh
pnpm exec playwright test tests/e2e/coleta.spec.ts --project=chromium-mobile --project=chromium-desktop --project=firefox-mobile --project=firefox-desktop --project=webkit-mobile --project=webkit-desktop
```

Os projetos existentes usam **390×844** e **1440×900**. Provar carregamento/vazio/
erro/READ_ONLY; sessão sem/aberta/encerrada; fronteira após retomar/reparear;
revogar/rotacionar só um dispositivo; ausência de indicação online; 401 distinto
de 402; 429/503 sem replay; código/credencial fora de cache/URL/storage/telemetria
e apagados na saída/troca/logout; teclado/foco e reflow de 320px. Registrar
relatórios e capturas nos seis projetos, com todos os checks verdes no HEAD
do frontend. Estas provas de browser pertencem à implementação frontend;
não foram declaradas executadas por este PR backend.

Sandbox é ensaio, não versão integrada nem deploy. O PR frontend referencia a
dependência backend e bloqueia publicação até merge administrativo, atualização
do pin integrado e repetição das provas. Não provisionar serviços nem ativar
produção para executar esta emergência.
