# Contrato de filtros do site — issue #183

Esta implementação atende à [emergência #183](https://github.com/pradyumna-001/bancaemdia-api/issues/183), vinculada às issues frontend [#17](https://github.com/pradyumna-001/bancaemdia-frontend/issues/17), [#50](https://github.com/pradyumna-001/bancaemdia-frontend/issues/50) e ao [PR frontend #74](https://github.com/pradyumna-001/bancaemdia-frontend/pull/74). PR aberto não representa contrato integrado: o frontend deve promover seu pin somente depois do merge e da validação da versão correspondente.

## Estado revalidado e reutilização

Base independente: `main` em `df5d58043562ca42aa1d422d81f3e08d3e07da6d`; branch `codex/site-filter-contracts-20261007`. A main avançou com o merge do #178 durante o trabalho; esta branch foi reconciliada com a nova base integrada. Nenhum PR aberto é ancestral desta branch.

| Capacidade antes desta entrega | Estado na main / referência | Lacuna atendida pela #183 |
| --- | --- | --- |
| Titulares, contas, intervalos de uso e atribuição pelo jogo | Integrada na main | Reutilizados; não há segunda entidade de conta/titular |
| Banca e `apostas.banca_id` | Integrada na main; campo sem escrita manual disponível | Filtro pelo vínculo gravado; correção explícita validada e replayável |
| Painel, métricas e XLSX materializados (#30/#105) | Integrados; ampliações de análises no #181 | Reutiliza regras exatas de razão, colunas e gerador XLSX; visão filtrada consulta fatos vivos |
| Catálogo técnico para instalação (#113) | Implementado no #179, HEAD `9ce5c284ca09549792a752623bc194f4c951523d`; fechado sem merge durante o trabalho | Não atende à sessão do site; nova fachada autenticada para referências existentes |
| Somente apagadas, seletores do site, grupo e agregados equivalentes | Planejados com aceite explícito frontend #17/#50/#74; contratos backend ausentes | Implementados nesta entrega independente |
| JSON/OpenAPI e migrações executáveis da base | Reparos antes no #181 agora integrados pelo #178; main atual tem JSON válido e head `j3main2026` | Geração oficial, migração dos grupos sobre esse head e gate estrito contra a base integrada |

Reutilização restrita do [#181](https://github.com/pradyumna-001/bancaemdia-api/pull/181), HEAD `81bfa9b77cceba76f2e174ba6c343d2c50b356ca`: fixtures `isolated_database`/`isolated_banco` para a prova real de replay/refresh/conferência, adaptada aos recursos integrados desta base. Os reparos de `j168base2026`, transporte ASGI de identidade, proteção de credenciais, handler comercial, OpenAPI e YAML inicialmente reutilizados já entraram na main pelo #178 e não são reapresentados no diff. Os auxiliares DDL já existiam no commit integrado `bd055417459f796fed960b5b37efb33a9744419f`. Os inventários são atualizados para a árvore executável desta entrega, sem incorporar recursos de outras branches.

O [#178](https://github.com/pradyumna-001/bancaemdia-api/pull/178) foi mesclado e compõe a base. [#179](https://github.com/pradyumna-001/bancaemdia-api/pull/179) e [#180](https://github.com/pradyumna-001/bancaemdia-api/pull/180) (`2b4d6f41ba2597a6497d4bdb0b407d08b3009c24`) foram fechados sem merge em 07/10; seu código não está integrado. O #181 permanece aberto e contém sobreposições: antes de integrar as entregas, reconciliar migrações, metadados, middleware, CI e gerar/revalidar o contrato do HEAD resultante. Em particular, caso o fato financeiro consolidado presente no #180/#181 seja integrado, sua elegibilidade canônica deve alcançar a seleção compartilhada. Esta entrega usa as projeções/regras já integradas, não declara a consolidação futura disponível nem duplica sua implementação.

## Recursos e matriz de filtros

| Recurso da visão filtrada | Contrato |
| --- | --- |
| Lista | `GET /api/v1/apostas` → `data` + `pagination` |
| Resumo e grupos | `GET /api/v1/painel/filtrado` → `resumo`, `por_casa`, `por_tipster`, `por_mercado` |
| Gráficos | `GET /api/v1/painel/filtrado/metricas` → `labels`, `datasets`, `total_periodo`, granularidade `dia` |
| XLSX agregado | `GET /api/v1/painel/filtrado/export` → binário; seis abas existentes |
| Opções | `GET /api/v1/filtros/{dimensao}` → `dimensao`, `data`, `pagination` |

Todos os quatro recursos da visão filtrada aplicam integralmente a matriz abaixo, usando `repositories/selecao_apostas.py`, antes de paginar, contar ou agregar. Filtros simultâneos são uma interseção.

| Dimensão | Parâmetro | Lista | Resumo | Gráficos | XLSX |
| --- | --- | --- | --- | --- | --- |
| Casa | `casa_id` | Sim | Sim | Sim | Sim |
| Tipster | `tipster_id` | Sim | Sim | Sim | Sim |
| Mercado | `mercado_id` | Sim | Sim | Sim | Sim |
| Competição | `competicao_id` | Sim | Sim | Sim | Sim |
| Estado | `estado` | Sim | Sim | Sim | Sim |
| Origem | `origem` | Sim | Sim | Sim | Sim |
| Revisão | `revisao_grave` | Sim | Sim | Sim | Sim |
| Titular | `titular_id` | Sim | Sim | Sim | Sim |
| Conta | `conta_casa_id` | Sim | Sim | Sim | Sim |
| Grupo | `grupo_id` | Sim | Sim | Sim | Sim |
| Banca | `banca_id` | Sim | Sim | Sim | Sim |
| Apagadas | `visibilidade` / legado `incluir_apagadas` | Sim | Sim | Sim | Sim |
| Datas | `periodo` **ou** `desde`/`ate` | Sim | Sim | Sim | Sim |

A lista conserva `page` (mínimo 1, padrão 1) e `page_size` (1–100, padrão 50). Página vazia mantém o total correto por uma única instrução SQL com a mesma CTE para total e página. Os três agregados filtrados não são paginados. Os endpoints legados `/api/v1/painel`, `/api/v1/painel/metricas` e `/api/v1/painel/export` mantêm seus parâmetros, MVs, granularidades e padrão de período `30d`; os adapters do site devem escolher os recursos `/filtrado` para a matriz completa, sem enviar filtros novos aos recursos legados.

### Visibilidade e erros

- Omitir ambos → `ativas`: `selecionada=true`.
- `visibilidade=apagadas` → exclusivamente `selecionada=false`.
- `visibilidade=todas` → ambos os conjuntos.
- Legado `incluir_apagadas=false` → ativas; `true` → todas.
- Enviar os dois só é permitido quando concordam (`ativas`+false, `todas`+true). Qualquer combinação conflitante retorna 422; para somente apagadas, omitir o legado.

ID sintaticamente inválido, não positivo ou acima de `9223372036854775807` → 422. ID válido inexistente ou privado de outro usuário → seleção vazia e total 0, sem retirada do filtro e sem distinguir existência. Estado/origem desconhecidos mantêm a comparação exata e produzem conjunto vazio; origem vazia/com controles retorna 422. Visibilidade inválida ou intervalos conflitantes/invertidos retornam 422. Não autenticado → 401. As permissões comerciais atuais permanecem: leitura/exportação continuam disponíveis quando a escrita retorna 402. Nenhum GET grava fatos, restaura apostas ou exige token de extensão.

### Datas e atribuição

Seleção: **`data_aposta`**, limite `desde` inclusivo e `ate` exclusivo. Aceita ISO 8601 com offset; entradas antigas sem offset continuam interpretadas explicitamente como UTC. Dias da URL do frontend devem ser convertidos para meia-noite civil de `America/Sao_Paulo`: o fim inclusivo escolhido no calendário vira a meia-noite do dia seguinte, exclusiva.

`periodo` aceita `7d`, `30d`, `90d`, `1y`, `all`, com as janelas civis existentes do domínio e fim na meia-noite do próximo dia de São Paulo. `all` não tem limite inicial, mas mantém esse limite final. Qualquer `periodo` junto com `desde` ou `ate` retorna 422. Sem nenhum parâmetro temporal, inclui todo o histórico, inclusive `data_aposta=null`; qualquer intervalo com limite exclui datas ausentes. Não existe período padrão que recorte silenciosamente um intervalo explícito.

Os gráficos sempre agrupam por dia civil de São Paulo, inclusive em `all` e em intervalos livres; dias com fatos aparecem ordenados, sem preencher dias inventados. Registros sem data aparecem em um bucket final `labels=null` quando não há limite temporal. `total_periodo` é calculado no servidor, incluindo esse bucket.

A atribuição de **conta/titular** continua usando o instante do jogo nos fluxos existentes. Multicontas com referência explícita preserva a conta que efetivamente fez a aposta. O filtro consulta a conta gravada e seu titular; não recalcula essa atribuição usando `data_aposta`, captura ou resultado.

### Grupos e bancas

Não havia entidade de grupo de apostas nesta base. O mínimo de domínio são `grupos_aposta` privados e `apostas_grupos`, relação explícita por `(usuario_id, aposta_id, grupo_id)`, com FKs compostas que impedem vincular outra pessoa. Grupos não são buckets analíticos nem associações deduzidas de nomes/tipsters. A hierarquia grupo→tipster mencionada em outras partes da frontend #50 continua sendo uma lacuna separada.

Comandos com autenticação/permissão de escrita vigentes:

```http
POST /api/v1/grupos
{"nome":"Apostas acompanhadas","arquivado":false}

PATCH /api/v1/grupos/12
{"nome":"Apostas acompanhadas","arquivado":true}

PUT /api/v1/apostas/{chave}/grupos
{"grupo_ids":["12","34"]}
```

PUT substitui os vínculos; lista vazia remove-os. Até 100 IDs, sem duplicatas, todos próprios e ativos; rejeição integral com 422 se algum estiver indisponível. Grupo estrangeiro/inexistente no PATCH e aposta estrangeira/inexistente no PUT retornam 404. Locks e auditoria por triggers acompanham a transação. Grupo arquivado preserva vínculos e continua filtrável/resolúvel historicamente, mas não recebe novas associações. Exclusão/restauração da aposta preserva seu ID e esses vínculos. Não existe reatribuição automática temporal de grupos; históricos sem vínculo continuam sem grupo. O filtro usa `EXISTS`, evitando multiplicar fatos quando uma aposta tem vários grupos.

`banca_id` usa **`Aposta.banca_id` gravado**, distinto da banca atual em `ContaCasa.banca_id`. Correção manual pela rota PATCH existente aceita banca própria ou null, persiste evento e projeção e é recuperável no replay. Não se inventa banca para registros antigos sem atribuição, nem se infere por nome ou por conta atual. Banca, grupo, titular, conta e casa são dimensões distintas.

## Opções autenticadas e IDs exatos

`dimensao`: `casas`, `tipsters`, `mercados`, `competicoes`, `origens`, `grupos`, `bancas`, `titulares`, `contas`.

Casas/tipsters/mercados/competições são referências globais compartilhadas, acessíveis à sessão autenticada; grupos/bancas/titulares/contas são estritamente privados. Reutiliza os modelos/repos de referência atuais. A fachada não altera nem substitui as rotas CRUD de titulares/contas existentes. `origens` provém de `models.aposta.ORIGENS`: `telegram`, `telegram_bot`, `print`, `manual`, `planilha`, `casa`; não é inferido de exemplos da extensão.

```json
{"dimensao":"grupos","data":[{"id":"12","nome":"Apostas acompanhadas","ativa":true}],"pagination":{"page":1,"page_size":50,"total":1}}
```

Parâmetros: `q` (até 160 caracteres, substring literal sem distinção de maiúsculas), `page` (mínimo 1), `page_size` (1–100, padrão 50), `incluir_inativas` (padrão false), `id` (valor canônico exato). `id` resolve diretamente uma opção, inclusive arquivada/inativa, mesmo fora da primeira página. Não combinar `id` com `q` ou `page!=1` (422). Casa `ativa=false`, titular/grupo arquivado e conta inativa/encerrada não aparecem por padrão, mas são retornados por `id` ou `incluir_inativas=true`. Entidades sem estado de ativação são sempre ativas.

Vazio é 200 com `data=[]`, `total=0`; indisponibilidade do banco é 503 com erro, nunca catálogo vazio fabricado. ID privado estrangeiro e inexistente têm a mesma resposta vazia. Busca usa parâmetros SQL vinculados; caracteres como `%`, `_` e aspas são literais, não SQL nem wildcard.

IDs numéricos de opções e grupos agregados são **strings decimais**, 1 até BIGINT máximo. `origens.id` é o texto do domínio aceito em `origem`. As queries de dimensão publicam integer|string|null para compatibilidade e para clientes gerados poderem preservar texto decimal. Enviar o texto sem `Number`, `parseInt` ou conversão por float; testado com `9007199254740993`. Nomes servem à apresentação; nunca usar slug/hostname/id de outra entidade como substituto. IDs dos comandos novos de associação são também strings.

## Financeiro, exportação e frescor

Contagem base inclui todas as apostas selecionadas. Pendentes e anuladas são contadas, mas não entram em giro, base ROI, retorno e lucro. Revisão grave aparece na contagem/revisão e não entra nas somas financeiras. Casos GREEN/RED, MEIO_GREEN/MEIO_RED e CASHOUT usam os valores canônicos já materializados; não há nova fórmula para obter retorno da odd.

Giro é stake desembolsada. Freebet tem stake zero e seu valor facial integra a base de ROI, conforme o domínio existente. Lucro é retorno menos stake. Razões são formatadas exatamente no servidor pelas funções atuais do Painel. Resultado liquidado sem retorno faz `retorno_centavos`, `lucro_centavos` e `roi` ficarem null no grupo/total afetado, com `resultados_desconhecidos>0`; giro/base conhecidos não são apagados. Um subconjunto financeiro vazio possui somas zero efetivamente conhecidas; isso não transforma retorno desconhecido em zero.

Mantém o gerador XLSX, as seis abas (`Resumo`, `Por casa`, `Por tipster`, `Por mercado`, `Por periodo`, `Evolucao`) e seus cabeçalhos atuais. As primeiras cinco reconciliam contagens/valores com a seleção; a última mostra contribuição e lucro acumulado por dia, propagando desconhecido para o acumulado. IDs agregados são texto. Valores desconhecidos ficam em branco. Saldo bancário não é deduzido do lucro da seleção: campos de saldo e banca da evolução ficam vazios, com `saldo_escopo=nao_aplicavel_a_selecao_filtrada`. O saldo all-time continua no Painel materializado legado. A exportação é de agregados, sem exigir aba de IDs de apostas.

Esta visão consulta `apostas` ao vivo e não utiliza/refresca MVs. Agregados da mesma resposta usam um snapshot `REPEATABLE READ`, com `snapshot_em` do PostgreSQL, `respondido_em` UTC, `fonte_dados=apostas_ao_vivo`, critério e fuso explícitos. Requests separados podem observar commits diferentes. Réplica segue o roteamento/lag vigente; `fresh=true` nos três agregados e `X-Read-Replica:false` levam ao primário. Refetch refaz a consulta; não promete refresh de MV.

Custo proporcional à seleção/histórico, pois o banco agrupa os fatos vivos por consulta; resumo executa uma consulta total e três agrupamentos. Gráficos carregam os dias selecionados; XLSX percorre os agrupamentos em streaming e usa arquivo temporário removido após envio. Reutiliza índices de usuário/data e das dimensões já existentes, além da unicidade `(usuario_id,id)` de aposta, índices de grupo/usuário e `(usuario_id,grupo_id,aposta_id)` da nova relação. Não há cache compartilhado de resultados privados. GETs retornam `Cache-Control: private, no-store`, `Vary: Authorization, Cookie`; transporte hospedado acrescenta `Origin` quando aplicável.

## Contrato e verificação

Fonte gerada: `tests/contract/schemas/openapi.json`; referência humana gerada: `docs/API.md`. Uma única aplicação registra todas as rotas desta árvore:

```bash
python scripts/generate_openapi.py
python scripts/generate_openapi.py --check
pytest -n 0 tests/integration/test_site_filters_db.py --junitxml=site-filter-results.xml
python scripts/check_site_filter_results.py site-filter-results.xml
```

O checker exige os 45 cenários específicos completos, sem failure/error/skip. A suíte usa FastAPI real, PostgreSQL 16, papel de aplicação, RLS, dois usuários, conjuntos/contagens pré-declarados e valores financeiros independentes. Cobre toda a matriz, sete páginas, página vazia, múltiplos grupos, limites de São Paulo, desconhecidos, BIGINT exato, entidades históricas, comandos/auditoria, banco explícito/replay e exclusão/restauração. Um timeout real do PostgreSQL comprova 503 distinto de catálogo vazio. O mock de JWKS somente substitui a rede; assinatura e verificação são reais.

A jornada obrigatória `tests/identity/journey.py` usa issuer OIDC, e-mail, navegador, cookie HttpOnly e PostgreSQL reais, acessando os nove catálogos e os três agregados filtrados com a mesma sessão do site. Executa na CI com serviços descartáveis, junto às invariantes de identidade existentes, sem skips. Não depende de ativação de produção. Gates anteriores continuam: Ruff, mypy, regressão/cobertura ≥80%, migrations/RLS, segurança, build e OpenAPI/oasdiff `fail-on: WARN`.

Baseline de compatibilidade é o SHA imutável da base do PR. O reparo de sintaxe/referências recuperado do #181 e agora integrado na main aceita somente o hash exato do artefato antigo corrompido; restaura as três definições do pai integrado imutável `00ae1e0b1d5d89224ce5028447767a23f527ad6e`. A base atual válida é usada diretamente. Nenhum schema novo deste PR vira baseline, e o diff estrito continua cobrindo todas as operações. SHA-256 do OpenAPI gerado: `7c0514fbc079cd07e7d7db5073512bbb6015c3b5da2bbdf4380f44b731d0878b`. O SHA final do PR e links dos checks serão registrados no relatório de entrega após a CI final.

## Próximos passos exatos do frontend

1. Revisar/mesclar este PR backend e reconciliar os PRs abertos que sobrepõem sua base. Validar migrations (`alembic upgrade head`) e a versão executada, incluindo permissões de aplicação nas novas tabelas pelo processo de implantação vigente.
2. Confirmar o commit **integrado** que contém esta implementação e os checks verdes; obter os bytes de `tests/contract/schemas/openapi.json` nesse commit e seu SHA-256. Se o merge modificar o artefato, usar o novo hash, não o do HEAD revisado.
3. Em `config/api-contract.json` do frontend, manter `repository=pradyumna-001/bancaemdia-api` e `schemaPath=tests/contract/schemas/openapi.json`, atualizar `commit` completo e `sha256` validados; executar `pnpm gen-types`. Isso gera `src/api/schema.d.ts` e `src/api/operations.generated.ts` da mesma fonte verificada. Não editar os tipos gerados manualmente.
4. Atualizar adapters de lista e Painel/métricas/XLSX para os quatro recursos da matriz. Mapear a URL apagadas para `visibilidade=apagadas` omitindo o bool legado; usar os catálogos e `id` para resolver pílulas históricas. Preservar IDs como strings decimais nos params e datas com offset; não fazer cálculos financeiros no cliente.
5. Tratar null/desconhecido e bucket sem data explicitamente, usar `total_periodo` do servidor, exibir erro 503 separado de opções vazias e manter diferenças do Painel legado documentadas. Manter as demais lacunas da #50 abertas.
6. Executar `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build` e os testes Playwright/integração real do frontend #74 (`pnpm test:e2e`), cobrindo sessão, URL, pílulas, histórico, somente apagadas e reconciliação. Só concluir a #17 após integração/CI frontend verdes; esta entrega backend não fecha #17/#50.
