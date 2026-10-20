# Resultados: fila, intenção e desfazer

Entrega da API para [frontend #27](https://github.com/pradyumna-001/bancaemdia-frontend/issues/27) e [#50](https://github.com/pradyumna-001/bancaemdia-frontend/issues/50), rastreada na [API #198](https://github.com/pradyumna-001/bancaemdia-api/issues/198). A publicação de um PR não significa integração nem deploy. Merge e reconciliação de outras candidatas pertencem ao administrador.

## Referência única

Branch `codex/resultados-seguros-20261013`, base `main` (`b916f54331f14cf47a3800324bd61d8638043c06`). O documento **inteiro**, com 94 operações, é `tests/contract/schemas/openapi.json`, gerado pelo aplicativo com `python scripts/generate_openapi.py`. `docs/API.md` é gerado da mesma fonte. O SHA final, hash dos bytes versionados, PR e checks são publicados na entrega; nunca usar uma concatenação de schemas.

Código próprio: domínio/serviço/router de resultados, tabela de operações, revisão opaca no banco, evento de reversão, migração `r198result2026` filha de `j6main2026`, provas e documentação. O CRUD e o resultado legados mantêm seus corpos e comportamentos públicos; sua escrita também invalida a revisão do site.

Código herdado declarado, incluído como conteúdo sobre main, sem empilhar branches:

- #187, `186be7482437eb5dd4b70914843dc6c5f90395f1`: `ApostaContextoRepo` e leitura em lote de históricos do `EventoRepo`. A fila reutiliza a projeção autorizada de conta/titular/banca e os textos canônicos, sem exigir que #187 seja mesclado para testar esta candidata. Não importa o endpoint de listagem desse PR.
- #191, `552c4ffe3924df3eac2caa50ea4f5df4c553b816`: funções comuns de Coleta, acesso comercial, locks, contexto ASGI, diagnóstico de carga e benchmark de 16 mil apostas; #193, `71f1a1acba17f2b99e8dcd55c25c672c98f43967`: repositório de apostas e roteamento comum. Incluem cache da estrutura SQL, nunca de autorização/revisão ou dados pessoais. A preservação de `Retry-After` explícito e o roteamento primário dos resultados são próprios desta entrega. Não importa Hipotética nem foto.
- Não depende dos contratos de #184/#185/#189/#195/#197, nem reproduz o sandbox frontend `003d3a0782cca071eaa0db4e8ce66fed2811a7ac`. Ao integrar candidatas relacionadas, reconciliar arquivos comuns e as migrações na árvore real; não aplicar cópias divergentes ou gerar schema composto manualmente.

## Consulta e elegibilidade

| Operação | Resposta / finalidade |
|---|---|
| `GET /api/v1/resultados/fila` | `QueuePage`: itens `ResultBet`, `next_cursor`, `total_elegivel`, `observado_em`, `fronteira_iniciada_em`, `frescor`, `populacao` |
| `GET /api/v1/resultados/apostas/{chave}` | Estado atual, revisão, campos financeiros e decisão de elegibilidade; consulta após conflito |
| `GET /api/v1/resultados/operacoes/{intention_id}` | Recibo confirmado da intenção, inclusive resposta perdida; é um retrato da confirmação, não o estado atual |

Todos os GETs são autenticados, RLS, permitidos em READ_ONLY e usam snapshot coerente do **primário**. Não escrevem nem atualizam MV. Respostas e erros são `Cache-Control: private, no-store`, `Vary: Cookie, Authorization`; não há hash de mídia, credencial ou dados de outra pessoa. O resumo desta operação é exclusivamente a **contagem elegível** fornecida pelo servidor; não há lucro/ROI agregado para o browser reconstruir a partir das páginas. O lucro por aposta é calculado na API e permanece null se o retorno for desconhecido.

A fila contém apenas `PENDENTE`, selecionadas/ativas, com chave, conta atribuída, sem revisão grave e na população financeira canônica. Fonte Telegram/print contextual de uma consolidação ativa não é financeira e sai da fila; a linha Casa consolidada continua elegível. Conta histórica encerrada ou titular arquivado não invalida sua atribuição já persistida. Consulta não escolhe conta novamente. A conta padrão foi atribuída pelo **dia/instante do jogo**; multicontas explicitamente referenciada preserva a conta que fez a bet.

O detalhe publica `elegivel` e `motivo_inelegivel`: `deleted`, `severe_review`, `contextual_source`, `account_unassigned`, `already_settled`, nessa precedência. Itens liquidados não entram na fila, mas podem receber uma correção intencional protegida após consulta atual. Apagadas, graves, contextuais e não atribuídas não podem receber resultado ou reversão neste fluxo. Resolver a revisão e consultar novamente não reaproveita uma intenção antiga.

### Ordem e paginação

Ordem global crescente por `COALESCE(data_jogo, criada_em)`, desempate por identidade interna monotônica. `antiguidade` e `criterio_antiguidade` explicitam o campo usado. `data_aposta` é independente e nunca define esta fila. Datas são instantes `timestamptz`, normalizados pelo domínio; timestamps de entrada sem fuso no CRUD usam `America/Sao_Paulo`. Filtros da fila exigem ISO 8601 **com fuso**. Comparação usa instantes; `Z` e `-03:00` podem representar um empate. Fallback `criada_em` não é null. A identidade interna de desempate fica no cursor criptografado, sem necessidade de interpretação pelo site.

`page_size`: 1–100, padrão 50. `cursor`: opaco, criptografado/autenticado, vinculado ao usuário e filtros; 30 minutos desde a primeira página, sem renovar a validade a cada página. A primeira consulta fixa o maior ID existente. Páginas seguintes procuram estritamente depois da última tupla de antiguidade/ID, sem offset e sem teto de 10.000. Liquidar os itens já vistos não faz a próxima página pular apostas.

Cada página é um novo snapshot do primário: `total_elegivel` conta **toda a população ainda elegível** da fronteira e filtros naquele snapshot, inclusive posições anteriores ao cursor. A contagem pode diminuir com liquidações e aumentar com restaurações dentro da fronteira. Novas apostas ficam para uma nova fila, mesmo que sua data seja antiga. Alteração de data/filtro/atribuição pode mover uma aposta para antes do cursor ou fazê-la reaparecer depois: após alteração externa, reiniciar a fila e deduplicar por chave; não é um snapshot congelado de toda a sessão. Revalidar a revisão em cada escrita. Um cursor ausente significa nova fila, não retomar offset.

Filtros isolados/combinados: `desde` inclusivo e `ate` exclusivo sobre a mesma **antiguidade**, `casa_id`, `titular_id`, `conta_casa_id`, `banca_id`, `tipster_id`, `mercado_id`, `competicao_id`, `time_casa_id`, `time_fora_id`, `origem`, `freebet`. IDs positivos até BIGINT; opções de conta/titular/banca continuam vindo dos catálogos/CRUDs autenticados. Intervalo invertido, data sem fuso, tamanho inválido, cursor de outro usuário/filtro e **qualquer parâmetro extra** recebem 422. Não admite `page`, `estado`, `incluir_apagadas`, `somente_apagadas`, `revisao_grave`, grupos, múltiplos IDs ou pesquisa textual. O escopo é uma fila ativa de pendentes; não substitui o relatório/listagem abrangente da #184.

## Um gesto, uma intenção

`POST /api/v1/resultados/operacoes/{intention_id}` exige UUID gerado **antes** do envio. Corpo `ResultInput`:

```json
{"chave":"m:synthetic","revisao_esperada":"00000000-0000-4000-8000-000000000001","estado":"GREEN"}
```

GREEN/RED exigem apenas estado e referência; não há confirmação adicional. ANULADA, MEIO_GREEN e MEIO_RED seguem o motor financeiro existente. CASHOUT exige `cashout_valor_centavos` ou `retorno_centavos`, valor efetivamente pago; se ambos forem informados, precisam coincidir. CASHOUT não admite comissão. `retorno_centavos` explícito substitui o cálculo; `comissao_centavos` ausente/null preserva a comissão vigente. PENDENTE é permitido como nova intenção de correção, sem retorno pago, e não é o protocolo de desfazer. Campos extras, booleanos/fracionários monetários, negativos, overflow BIGINT ou retorno calculado negativo são 422 sem efeitos.

Valores monetários são inteiros em centavos no JSON, sem conversão em reais nem cálculo no browser. Consumidor deve preservar inteiros exatos ao ler/escrever, inclusive valores além de `Number.MAX_SAFE_INTEGER` (parser/serialização sem perda, ou recusa explícita de valores não representáveis na interface). Freebet mantém valor de face, custo próprio zero e as fórmulas canônicas; odd desconhecida pode produzir retorno/lucro null. Não substituir null por zero.

`ResultReceipt`: UUID, `tipo`, aposta confirmada com **nova revisão**, `operacao_desfeita`, `confirmado_em`, `desfazer_ate`, `consultar_ate`, `eventos_gravados=1`. A aposta inclui retorno informado versus calculado, comissão e contexto autorizado. MV/painel pode continuar com frescor eventual conforme seu próprio contrato; não usar o recibo para inventar um resumo consolidado.

### Concorrência entre escritores

`revisao` é UUID opaco criado no banco, não timestamp nem hash de mídia. Gatilho de UPDATE da aposta invalida em toda escrita relevante, mesmo quando o estado nominal não muda ou um valor muda e volta ao anterior. Inserção de evento da aposta (incluindo partições), alteração da consolidação de ambas as fontes e criação/resolução de revisão invalidam também. Como revisões podem ter identidade contextual incompleta, alterações em `revisao_pendente` invalidam conservadoramente as apostas do dono. Outros usuários não são afetados.

Mutation usa o primário, lock de usuário do pareador, advisory lock da aposta e `FOR UPDATE`. Compara a revisão **dentro dessa transação** antes de registrar fatos. Coleta, Telegram, importações, CRUD/resultados legados, correções financeiras, exclusão/restauração, revisão e consolidação já passam pelo banco protegido; não dependem de adotar o corpo do site. Escrita por SQL/repositório sem atualizar o histórico também invalida; uma projeção financeira divergente do histórico recebe `result_projection_conflict`, para reconciliação no backend, sem inventar pré-imagem.

Duas intenções distintas com a mesma revisão: uma vence, outra recebe `result_revision_conflict`, sem evento, movimento, saldo, projeção ou histórico adicional. Replay da intenção já concluída é reconhecido **antes** de comparar a revisão que ela mesma alterou, após revalidar acesso comercial. Nenhum endpoint legado enfraquece a proteção do endpoint novo.

## Desfazer exato e condicional

`POST /api/v1/resultados/operacoes/{nova_intention_id}/desfazer`:

```json
{"operacao_id":"00000000-0000-4000-8000-000000000002","revisao_esperada":"00000000-0000-4000-8000-000000000003"}
```

Só o mesmo usuário dono/autor da operação de resultado pode revertê-la. `revisao_esperada` deve ser exatamente a produzida pela operação alvo e continuar atual no primário. Validade: 24 horas da confirmação, sem extensão por consultas/replays. Qualquer escrita intermediária, apagamento/restauração ou consolidação gera conflito mesmo que o estado volte a GREEN ou ao mesmo valor. Operação de reversão não pode ser alvo de outra reversão neste protocolo.

Pré-imagem é guardada pelo servidor na mesma transação do resultado: estado, retorno, retorno_informado e comissão, com **presença de chave** preservada. O desfazer restaura ausente/null/valor, cashout pelo estado/retorno registrado e retorno calculado versus informado. Não restaura dados enviados pelo browser nem sobrescreve conta/odd/stake modificadas depois; estas alterações invalidam a revisão antes da reversão.

Histórico permanece íntegro. `RESULTADO_DESFEITO` registra intenção, operação alvo, ator e restauração financeira. O fold/replay canônico conhece e valida o evento; materialização e saldos seguem o mesmo modelo dos resultados existentes (não existe movimento de caixa extra por liquidar uma aposta). Idempotência da mesma intenção de desfazer retorna seu recibo; outro UUID tentando desfazer de novo recebe conflito. Conta excluída remove operação/pré-imagem pela política de apagamento, sem permitir recuperação de outro usuário.

## Recuperação, validade e erros

Intenções concluídas e pré-imagens ficam em tabela privada, RLS forçada e recibos imutáveis, até exclusão da conta. Consulta/replay do recibo expira em **90 dias**, mesmo que a identidade permaneça guardada para impedir duplicação. Não há janitor, billing, serviço ou infraestrutura nova. Migração recusa downgrade quando existem operações ou eventos de reversão; rollback com dados exige preservação/reconciliação administrativa. Upgrade/downgrade vazio é testado.

UUID identifica uma intenção, não autoriza. Mesmo UUID e payload normalizado: mesmo recibo, sem fato adicional. Mesmo UUID e payload/tipo diferente: `result_intention_conflict`. Após timeout/503, logout ou refresh, não repetir POST automaticamente. Manter a identidade e consultar GET quando houver sessão válida; recibo 200 resolve a confirmação perdida. GET 404 não prova rollback (pode haver requisição ainda em curso); 410 também não autoriza nova tentativa automática. Repetição explícita do **mesmo** UUID/payload enquanto válido é segura pelo contrato e exige FULL_WRITE; criar outra intenção só após reconciliação e uma nova decisão humana.

| HTTP | Semântica |
|---|---|
| 401 | Sessão/credencial ausente, inválida ou revogada; distinto de acesso comercial |
| 402 | `account_read_only`; preservar sessão, consulta, exportação e trabalho; registrar/desfazer/replay de POST continuam recusados |
| 403 | Prova CSRF/Origin ausente ou inválida no transporte por cookie |
| 404 | `result_not_found` ou `result_intention_not_found`, sempre restrito ao dono |
| 409 | `result_revision_conflict`, `result_intention_conflict`, `result_projection_conflict`; consultar detalhe/recibo e pedir nova intenção quando aplicável |
| 409 | `result_busy` é trava transitória; `Retry-After: 1`, não afirmar que a revisão está antiga |
| 410 | `result_undo_expired` ou `result_receipt_expired`; não autoriza replay automático |
| 422 | `result_not_eligible`, `result_invalid`, `result_cursor_invalid` ou validação estruturada; corrigir entrada/escopo |
| 429 | `error=rate_limited`, `retry_after` e `Retry-After`; respeitar quota sem perder intenção |
| 503 | `result_unavailable` ou indisponibilidade de autenticação; `Retry-After: 5`; resultado de POST pode ser desconhecido |

Cookies HttpOnly, Origin exata e `X-CSRF-Token` continuam obrigatórios conforme identidade existente. FULL_WRITE é revalidado no endpoint e pelos guards do banco. Exemplos são sintéticos; não registrar corpo financeiro, cookies, revisão privada ou credenciais nos logs/telemetria do consumidor.

## Provas e consumo

`tests/integration/test_results_db.py` exige 72 casos reais de HTTP/PostgreSQL: mais de 10 mil itens, paginação com remoções, desempates/null/filtros, 20 gestos e liquidações, estados/freebet/comissão/retorno explícito/null, duas sessões e escritores intermediários, pré-imagem exata/replay, perda de resposta, RLS/CSRF/401/402, consolidação, conta temporal/multicontas, apagamento, indisponibilidade e migrações. `scripts/check_results_junit.py` congela os nomes, recusa skips, falhas, omissões e duplicações; job obrigatório `Results queue and conditional undo (PostgreSQL)` bloqueia Docker/release. Gates existentes continuam exigidos, incluindo contrato, tipos, segurança, 16 mil apostas/replay e carga de 55 minutos.

Passos para o frontend, sem alterar esta API nem fechar #27/#50 pela entrega backend:

1. Fixar o SHA final e SHA-256 publicados em `config/api-contract.json` (`repository`, `commit`, `schemaPath`, `sha256`), com disponibilidade **candidate/sandbox**, sem publicar produção. Baixar os bytes dessa referência inteira; verificar hash antes de gerar qualquer tipo.
2. Na referência frontend #91, executar `node scripts/gen-types.mjs <caminho-local-do-openapi-inteiro>` (ou sem argumento para a referência pinada). Esse script verifica hash e produz `src/api/schema.d.ts`, `operations.generated.ts` e `site-read.generated.ts` juntos. O gerador atual conhece paginação por page/headers: complementar sua extração a partir de `x-bancaemdia-resultados` para cursor, matriz comercial e identidade de intenção; não classificar estas mutations para replay automático porque têm UUID. Nunca somar/concatenar documentos.
3. Na candidata backend isolada: instalar `.[dev]`, configurar banco PostgreSQL 16 descartável, `alembic upgrade head`, emissor/credenciais de teste reais e iniciar a aplicação. Rodar os 72 casos com `TEST_DATABASE_URL` apontando **apenas** para esse banco, `pytest tests/integration/test_results_db.py -n 0 --no-cov --junitxml=results-acceptance.xml`, seguido de `python scripts/check_results_junit.py results-acceptance.xml`. Consumidor deve provar seus fluxos com a API/emissor/banco reais, não apenas mocks.
4. No site: obter uma página, gerar UUID antes de cada gesto, enviar revisão daquele item, aceitar finanças/contagem da API, conservar próximo cursor quando itens saírem. Desfazer cria UUID próprio e usa operação/revisão do recibo. Foco, alvo do próximo gesto e otimismo são apresentação; duplo toque não muda UUID/alvo. Limpar dados privados de memória no logout e reconciliar intenções conhecidas após novo login, sem POST automático.
5. O documento independente de 94 operações não contém todos os contratos do sandbox frontend de 115. Para testar o site completo antes do merge, reconciliar **código real** em uma única árvore de sandbox autorizada, migrar o banco de prova e gerar o documento inteiro pelo app dessa árvore; fixar seu único commit/hash e disponibilidade `sandbox_reconciliation`. Não usar o sucesso isolado como prova de deploy integrado.
6. Após merges administrativos: reconciliar esta branch e todos os pontos comuns/migrações, gerar OpenAPI da árvore integrada, atualizar pin único e repetir tipos/políticas, contrato/drift, aceites backend e consumidor real. Confirmar ancestralidade/disponibilidade integrada antes de publicar. Não inferir integração de dependências só porque esta CI passou.


## Revisão administrativa de 20/10/2026

- O detalhe lê aposta, revisão e elegibilidade financeira em uma consulta própria
  filtrada pelo dono e chave. A revisão continua fora do registro ORM legado por
  decisão de compatibilidade; `ResultBet` é uma resposta, não a linha carregada.
  O aceite mede duas consultas à tabela de apostas: projeção completa e contexto
  de exibição autorizado, sem SELECT separado de revisão/elegibilidade.
- Elegibilidade depende também de consolidações, não só da linha da aposta. GET
  reutiliza a condição do mesmo snapshot; escritas mantêm a revalidação financeira
  fresca sob os locks. Passar uma revisão conhecida não usa o atalho da fila e
  não dispensa checar apagada, revisão grave, conta ausente ou fonte contextual.
- Não se adiciona índice de `result_revision`: ela é comparada depois de localizar
  a linha pelo dono/chave/ID já indexados. O WHEN do trigger compara OLD e NEW,
  sem busca nessa coluna; indexá-la acrescentaria custo a toda invalidação.
- `retorno_informado` registra se o histórico forneceu retorno explicitamente,
  incluindo zero; retorno calculado pode ser conhecido sem essa marca. Ausência
  legada continua null, sem inventar presença para alterar a pré-imagem do undo.
  Os estados/freebet/undo agora verificam a marca e incluem cashout explícito zero.
- A validação exige regressões de middleware e sete provas com standby/RLS real,
  além dos 72 aceites obrigatórios de resultados. Atribuição pelo jogo e referência
  explícita de multicontas permanecem intactas.
