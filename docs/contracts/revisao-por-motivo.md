# Revisão por motivo — contrato candidato da #200

Esta candidata desbloqueia o frontend #28. O merge e a reconciliação administrativa pertencem ao administrador. Não declara a tela, o lançamento ou a integração em main concluídos.

## Base e proveniência

PR independente contra `main`, base auditada `b916f54331f14cf47a3800324bd61d8638043c06`. Código herdado real, sem merge administrativo e sem união de schemas:

- #199, `a67920f1aa6f77de1fd34c6b142c3e611f584502`: revisão de apostas no PostgreSQL para todos os escritores, Resultados/recibos, projeção, aceites obrigatórios e otimizações compartilhadas. Sua proveniência parcial inclui #187/#191/#193.
- #195, `6db8964d11c471b47d15c08bd5d46d864623bb1e`: materializador/resolver de Print sem aposta, mídia privada, intake/worker e seus aceites; herda #193 e otimizações de #191. As diferenças compartilhadas com #199 foram reconciliadas no código.
- #193, `71f1a1acba17f2b99e8dcd55c25c672c98f43967`: fotos/proveniência, herdada por #195. Não precisa de #197/XLSX para resolver uma revisão, mas esse contrato deve ser preservado na árvore completa do consumidor.

As migrations publicadas de Prints e Resultados conservam seus IDs; `r200review2026` reúne ambas e acrescenta metadados de evidência e recibos de revisão. A candidata inclui esses endpoints herdados de forma executável. Não empilha uma PR sobre outra.

O sandbox do frontend `b37a3f88b922b471d25d5f1c92c84b336cd2c9c0` tem **120 operações**, schema SHA-256 `9da4116cf357df8c1b933c575156c067d8df86b3a216d234d810214a27a02ecb`, reunindo #184/#187/#189/#193/#195/#197/#199. Esta candidata gera seu próprio **OpenAPI inteiro** em `tests/contract/schemas/openapi.json` a partir do aplicativo. O consumidor deve reconciliar os deltas de código/migrations/testes/documentação desta candidata sobre sua árvore real, manter os contratos anteriores e executar novamente o gerador e os aceites. **Não substituir o schema de 120 operações pelo schema isolado desta PR; não fazer concatenação/união manual de JSON.** O administrador integra as candidatas em main; o sandbox continua sendo apenas ensaio.

## Leitura e ações

`GET /api/v1/revisao-operacional/fila` e `GET /api/v1/revisao-operacional/revisoes/{id}` usam snapshot do primário e retornam `ReviewPage`/`ReviewView`. IDs e novos valores monetários inteiros são strings exatas; retorno desconhecido permanece null. `stake_centavos` é custo próprio calculado no servidor; leitura legada sem stake conhecida retorna stake/valores null e exige preenchimento humano antes de aprovar; `valor_aposta_centavos` preserva o valor de face de freebet. Fonte contextual não deve ser somada pelo consumidor.

| Família | Ações publicadas quando elegível | Efeito/encaminhamento |
|---|---|---|
| `leitura` | CORRIGIR, DESCARTAR | Correção existente; descarte exclui a aposta da apuração, com histórico preservado. |
| `conta` | CORRIGIR, se há candidato autorizado | Escolha humana obrigatória. Sem candidatos: cadastrar conta na casa ou informar casa e reler. |
| `par_legado` | MESMA, DISTINTAS, RECUSAR_VINCULO | MESMA adota o par retido na consolidação durável; as outras recusam o vínculo e restauram a dica retida como aposta distinta. |
| `consolidacao` | MESMA, DISTINTAS, RECUSAR_VINCULO | MESMA mantém Casa como fato financeiro e Telegram como contexto. Recusa registra associação rejeitada e preserva os dois fatos originais. |
| `print_incompleto` | CORRIGIR, DESCARTAR | Usa o resolver/materializador de #195: só correção válida cria aposta; descarte fecha sem criar aposta de valor zero. |
| `nao_suportada` | nenhuma | Encaminhamento `suporte`, sem interpretar JSON interno ou adivinhar ação. |
| Qualquer resolvida/vínculo indisponível | nenhuma | Reler/reconciliar; não presumir elegibilidade. |

As ações efetivas vêm de `acoes`, e não apenas da tabela. Pares não oferecem descarte como substituto de recusa: excluir uma aposta é diferente de rejeitar sua associação. Para esses fluxos o site envia o ID da revisão, nunca deriva IDs persistidos/orientação de uma chave.

`fontes` contém somente dados legíveis/autorizados e papéis `casa`/`telegram`/`aposta`. `par.tipster_opcoes` publica a escolha por fonte com rótulo e tipster. `escolha_tipster_obrigatoria` vale para conflito de identidade **ou de nome**. A escolha modifica o contexto do fato consolidado e mantém a evidência original das duas fontes.

`conta` distingue casa, conta e titular, informa `instante`/`criterio`, `status` atual e `status_na_abertura` quando o produtor registrou o diagnóstico. O padrão usa **dia/instante do jogo**, nunca a data de colocação. A exceção multicontas é uma referência humana explícita. Cada candidato foi autorizado/revalidado pelo serviço existente; `coincide_no_dia_do_jogo` usa o mesmo resolvedor de vigências, sem segundo motor. Conta histórica/encerrada/inativa/sem titular pode ser escolhida explicitamente quando o domínio permite; a confirmação valida usuário e casa, não escolhe a primeira. O PostgreSQL impede criar novas vigências sobrepostas; uma revisão de ambiguidade histórica mantém o motivo de abertura, enquanto a leitura/POST reavaliam a matriz atual.

Fotos são opcionais. `foto_url` aponta à rota autenticada de mesma origem `/api/v1/revisao-operacional/revisoes/{id}/foto`, com autorização pela revisão e proveniência existente. Hash não dá acesso. Não há URL assinada nem credenciais. Foto ausente, expirada, de outra pessoa ou inválida: 404 seguro. MIME real JPEG/PNG/GIF/WebP, até 20 MiB, 40 milhões de pixels decodificados e 256 frames. `private, no-store`, `nosniff` e variação por Cookie/Authorization, inclusive em falhas.

## Idade, população e consumo

`desde` inclusivo e `ate` exclusivo filtram **RevisaoPendente.criado_em**; ISO sem offset é America/Sao_Paulo. O filtro de idade é explicitamente um intervalo de abertura, não um parâmetro relativo ignorado nem filtragem local de uma página. O frontend pode oferecer datas de corte, sempre enviadas ao servidor. `observado_em` registra o relógio do servidor.

`familia` filtra antes da paginação/contagem. `total_filtrado` e `por_familia_filtrado` são do mesmo recorte; `total_global` identifica separadamente todas as revisões abertas. Ordem `criado_em,id`, página 1–100 itens. Depois de cada resolução, **reler página 1 com os mesmos filtros**: incrementar um offset numa população que encolheu pode pular itens. O antigo `/api/v1/revisao/stats` é global e não herda filtros da lista antiga. A documentação de `desde/ate` das rotas antigas foi corrigida sem mudar seus predicados/fuso.

## Escrita, CAS e recuperação

Persistir um UUID da intenção antes do POST. Enviar exatamente a precondição lida, a ação permitida e as escolhas humanas:

```http
GET /api/v1/revisao-operacional/revisoes/42
```

```json
{"id":"42","familia":"consolidacao","motivo":"fontes_a_consolidar","estado":"aberta","precondicao":"<64 caracteres opacos>","acoes":["MESMA","DISTINTAS","RECUSAR_VINCULO"],"encaminhamento":"resolver_par","par":{"escolha_tipster_obrigatoria":true,"tipster_opcoes":[{"fonte":"casa","rotulo":"Tipster da casa","tipster":"Tipster Casa","tipster_id":null},{"fonte":"telegram","rotulo":"Tipster do Telegram","tipster":"Tipster Telegram","tipster_id":null}]}}
```

O exemplo de leitura acima é um **trecho ilustrativo**, não a resposta completa. O schema gerado é autoritativo.

```http
POST /api/v1/revisao-operacional/operacoes/2054dce7-13c1-4c94-a1c5-5b7a4bf108c1
Content-Type: application/json
X-CSRF-Token: <prova da sessão mantida em memória>
```

```json
{"revisao_id":"42","precondicao":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","acao":"MESMA","tipster_choice":"telegram"}
```

Para conta, preferir `{"revisao_id":"42","precondicao":"<token lido>","acao":"CORRIGIR","conta_casa_id":"9007199254740995"}`. A string é convertida em BIGINT somente no backend. `aposta_corrigida` reutiliza `CorrecaoDaRevisao`, cujas propriedades estão no OpenAPI; o formulário antigo permanece disponível. Não preencher `revisao_grave`/campos internos.

A transação SERIALIZABLE no primário usa as travas de usuário/pareador, fonte e linhas do domínio; exige FULL_WRITE atual e compara a precondição antes dos efeitos. O fingerprint é privado, vinculado ao usuário/revisão e cobre versões PostgreSQL das apostas de #199, revisões, candidatos/concorrentes, vínculos, contas, vigências, titulares, bancas e estado de Print, além do vocabulário utilizado. Mudanças nominais/ABA nas linhas também mudam suas versões. A invalidação é conservadora: outra alteração da mesma pessoa pode exigir releitura. Nenhum snapshot/correção fornecido pelo cliente substitui a evidência atual. Revalida fontes/orientação/versão do candidato, vínculo vigente, atribuição e decisão nos serviços existentes. Eventos, fatos, recusa/consolidação, fechamento e recibo concordam no mesmo commit; recusa/falha reverte tudo.

O recibo `ReviewReceipt` retorna UUID, revisão, família, ação, referência da aposta (nullable), instante, prazo e número real de eventos. É imutável e protegido por FORCE RLS e FK de revisão do mesmo usuário. Disponível por **90 dias** por `GET /api/v1/revisao-operacional/operacoes/{UUID}`; após esse prazo: 410. O registro mínimo permanece para impedir reutilização silenciosa do UUID; excluído somente no apagamento autorizado da conta. Downgrade com recibos existentes recusa perda de auditoria.

Após timeout/503/resposta perdida, consultar o GET com o UUID conhecido. **Não repetir POST automaticamente.** 404/410 não provam rollback; a revisão pode ter sido resolvida por outro escritor e isso não comprova qual intenção venceu. Repetição **explícita** do mesmo UUID/payload retorna o mesmo recibo dentro de 90 dias, sem acrescentar eventos/dinheiro; UUID com outro payload retorna conflito. O acesso de escrita atual também é exigido no replay explícito. Após 402, preservar sessão/leitura/formulário; não fazer replay após pagamento. 401 requer reautenticação; cookie exige CSRF/Origin atuais, tokens permanecem no servidor.

| Código | HTTP | Conduta |
|---|---|---|
| `review_not_found`, `review_photo_not_found`, `review_intention_not_found` | 404 | Ausência/recurso alheio indistinguíveis; recibo desconhecido não prova rollback. |
| `review_already_resolved` | 409 | Reler estado, sem atribuir a ação a uma intenção desconhecida. |
| `review_evidence_changed` | 409 | Descartar decisão antiga, preservar formulário e reler evidência. |
| `review_busy` | 409, Retry-After: 1 | Reconciliar primeiro; não repetir escrita automaticamente. |
| `review_link_incompatible` | 409 | Fontes/vínculo recusados pelo domínio; nenhum efeito parcial. |
| `review_intention_conflict` | 409 | Mesmo UUID usado com payload diferente. |
| `review_tipster_required`, `review_tipster_invalid` | 422 | Ajustar `tipster_choice` sem editar fonte para eliminar conflito. |
| `review_account_required`, `review_account_invalid` | 422 | Ajustar escolha humana de `conta_casa_id`. |
| `review_correction_invalid`, `review_validation_invalid` | 422 | Campo seguro em `campo`, sem input/msg/ctx ou dump interno. |
| `review_unsupported`, `review_action_not_allowed` | 422 | Respeitar encaminhamento/ações publicados. |
| `review_projection_conflict` | 409 | Linha e histórico financeiros divergem; não sobrescrever fatos a partir de log incompleto. |
| `review_history_incomplete`, `review_source_missing` | 409 | Histórico/fonte insuficiente; encaminhar para suporte. |
| `review_receipt_expired` | 410 | Prazo de recuperação terminado; não presumir rollback. |
| `review_unavailable` | 503, Retry-After: 5 | Infra indisponível; consultar intenção antes de nova ação. |
| `account_read_only` | 402 | Manter sessão, dados e formulário; sem replay após pagamento. |

As rotas antigas permanecem compatíveis. O resolver antigo recebeu `tipster_choice` opcional para MESMA e o encaminha ao domínio. Para a tela nova, usar a superfície operacional que exige CAS e publica recuperação; a rota antiga não passa a oferecer essa garantia retroativamente.

## Aceites obrigatórios

`pytest tests/integration/test_reviews_db.py -n 0 --tb=short --no-cov --junitxml=reviews-acceptance.xml` com `TEST_DATABASE_URL` apontando a PostgreSQL 16 real; `python scripts/check_reviews_junit.py reviews-acceptance.xml` valida o inventário congelado completo, sem omissões, duplicatas, skips ou falhas. Job `reviews-db` é pré-requisito do Docker Build. Preserva também os gates Resultados (70), Prints (35), fotos (42), matching/consolidação, contas/billing, contratos, identidade, integridade, leitores, replay/16k e carga de 55 minutos com réplica real.

Valida caso criado pelo servidor de tipsters conflitantes e ambas escolhas com uma apuração; correção/foto; conta ausente/ambiguidade de abertura/histórica/encerrada/sem titular; par legado mesma/distinta; matching mesma/distinta/recusa; Print sem aposta corrigido/descartado; motivo desconhecido; filtros/contagens/fila vazia e consumo; CAS por escritores antigos/intermediários/candidato/concorrente/vínculo/dicionário; corrida, idempotência e resposta realmente perdida após commit; rollback provocado no PostgreSQL antes do recibo; 401/402, cookie/CSRF/revogação; outro usuário/RLS; MIME/tamanho/corrupção; dinheiro exato/ID >2^53/freebet/null; recibo imutável/expirado. Financeiro/consolidação não são mocks. A extração externa de IA em testes de Print é sintética; o materializador e a decisão financeira são reais.

Demais comandos: `python scripts/generate_openapi.py --check`, `ruff check .`, `ruff format --check .`, `mypy src/ --strict --ignore-missing-imports`, suíte geral e todos os jobs aplicáveis no SHA final do GitHub. O hash definitivo é dos bytes LF versionados do schema completo no SHA entregue, e não de um arquivo de trabalho com CRLF.
