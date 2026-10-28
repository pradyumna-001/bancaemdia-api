# Troca temporal: confirmação e recuperação — #206

Entrega direta à main para frontend #53 (consumidor PR #99, `07263a02d4cf4de578bc1333785650477e2336c5`). Revisão/merge pelo administrador. A composição de consumo sobre `86c82c9cd4e9cbb296a6e7991c06a8f3e5f3c7a7` é sandbox, sem deploy ou autorização de produção. O PR publica os commits imutáveis e hashes completos das duas árvores.

## Pedido e consequência

`preview_troca_api_v1_titulares_trocas_preview_post` e `aplicar_troca_api_v1_titulares_trocas_post` continuam recebendo `TrocaContaEntrada`, retornando `TrocaContaSaida`, com `Idempotency-Key` obrigatória. Não existe caminho novo que permita omitir a precondição: a aplicação exige a prévia persistida daquele usuário/chave/corpo, ainda válida e com evidência igual. Não há hash ou contagem confiada ao navegador.

Exemplo sintético do corpo dos dois POSTs:

```json
{"casa_id":7,"conta_origem_id":10,"conta_destino_id":11,"efetiva_em":"2026-10-01T15:00:00-03:00","estado_origem":"LIMITADA"}
```

Escolher explicitamente `DISPONIVEL`, `LIMITADA` ou `ENCERRADA`. O destino continua sujeito à matriz/mesma casa/proprietário; não há conta simultânea, seleção automática, data futura ou sobreposição permitida. O servidor normaliza o instante com fuso; T pertence ao destino, T menos um microssegundo pertence à origem. Não usar data de cadastro da aposta como substituto da data do jogo.

`apostas_afetadas_ids` são apostas existentes atribuídas à origem, com jogo em/depois de T e sem referência explícita no histórico. São itens para **revisão de atribuição retrospectiva**, não linhas migradas pela troca. O frontend apresenta os IDs e oferece o fluxo existente de revisão/correção individual, sujeito às políticas do servidor. Invalidar/refazer leituras pertinentes após sucesso. Não disparar correção em lote nem recalcular dinheiro. Aposta anterior ainda aberta continua na origem; multicontas/referência explícita e ausência de relógio do jogo seguem o motor existente. Nenhuma aposta, evento financeiro ou valor é modificado pela troca.

## Evidência e atomicidade

A prévia salva no JSON existente um SHA-256 privado da representação canônica versão 1. A API pública não expõe esse hash. Inclui contas origem/destino (identidade, titular, apelido, estado, atividade, desde/até), nomes/arquivamento dos titulares envolvidos, todas as vigências daquela casa (identidade, conta, início/fim/origem), e pares ordenados ID/instante do jogo das apostas para revisão. O pedido normalizado tem digest separado.

Inclusão/remoção de item elegível, correção da data mesmo permanecendo depois de T, explicitação da referência, troca de identidade/estado/apelido/titular/vigência conflitam. Uma alteração de resultado, stake, odd ou metadado sem efeito nesses elementos não conflita: esses fatos não fazem parte da confirmação nem são alterados por ela. A evidência compara o estado vigente no momento da decisão, não um contador de toda mudança histórica; alteração revertida ao mesmo contexto não exige conflito.

Ordem: advisory transacional `pareador:{usuario}` → usuário → contas em ID crescente → uso atual. Nenhuma espera por trava de aposta é introduzida depois das contas. Ingestão/materialização, correção HTTP, atribuição explícita, replay e consolidação já entram pelo advisory antes das chaves/apostas/contas; os escritores de titular/conta/ativação/arquivamento e exclusão passam a tomá-lo antes do usuário. Os FKs e travas das linhas preservam a ordenação das inserções. O worker mantém o protocolo de matching e o caller controla commit/rollback. SQL administrativo fora desses serviços exige a mesma ordem; não é protocolo público de escrita. Não há tabela, migração ou alteração do grafo Alembic nesta entrega.

Antes da primeira aplicação, a evidência corrente é comparada sob essas mesmas travas. Divergência resulta em `409 {"detail":"swap_preview_stale"}` antes de qualquer escrita da troca. Não há fechamento/abertura parcial, alteração de estado, recibo ou evento APPLY. Contexto já inválido pode responder `409 swap_invalid`, igualmente sem efeitos da troca.

## Identidade, renovação, retenção e resposta perdida

1. Antes de enviar, guardar localmente **usuário + chave aleatória única + corpo exato normalizado pela UI**. Não reutilizar chave entre intenções/usuários. Não guardar cookie/tokens na persistência da intenção.
2. POST `/api/v1/titulares/trocas/preview` com essa chave; mostrar consequência e pedir confirmação humana. PREVIEW grava recibo/auditoria e exige FULL_WRITE/CSRF.
3. POST `/api/v1/titulares/trocas` com a mesma chave/corpo somente após confirmação. Nunca repetir automaticamente mutações por timeout, 429 ou 503.
4. Com resposta perdida, recarga ou reinício, GET `/api/v1/titulares/trocas/intencao`, enviando a chave no header `Idempotency-Key`. Operação `obter_intencao_troca_api_v1_titulares_trocas_intencao_get`, tipo `TrocaIntencaoSaida`. É leitura do proprietário no primário, permitida em READ_ONLY.
5. `APPLIED` com `recibo` comprova commit daquela intenção, inclusive após alterações posteriores. O recibo é exatamente o original; não revalidar evidência de uma aplicação concluída. `PREVIEW`/`EXPIRED` têm `recibo:null` e prévia original. Não comprovam rollback ou ausência de operação ainda em voo. `404 swap_intention_unknown` também não comprova rollback; pode significar outra identidade, registro removido ou pedido ainda em voo. GET de matriz/histórico jamais substitui essa recuperação.
6. Com acesso de escrita, o usuário pode **reenviar explicitamente a mesma intenção confirmada**, mesma chave/corpo, após consultar o estado desconhecido. A API serializa com eventual pedido em voo e retorna o recibo existente ou aplica uma vez se a prévia continuar válida. Não criar nova chave para tentar resolver resultado desconhecido. Se a assinatura expirou depois do commit, usar GET: POST responde 402 mesmo sendo replay.
7. Para `swap_preview_stale`/`swap_preview_expired`, quando não houver resultado desconhecido pendente, criar **nova chave**, obter nova prévia, apresentar novamente as consequências e exigir nova confirmação. Repetir PREVIEW antiga retorna sempre a resposta antiga, nunca renova evidência ou prazo. Uma intenção aplicada não é reaproveitada.

Prévia pode ser confirmada por **24 horas** a partir do horário PostgreSQL persistido (`criado_em`); fronteira inclusiva de expiração. Depois, primeira aplicação retorna `410 swap_preview_expired`. Prévia/recibo/identidades são retidos sem prazo automático enquanto a conta existir, sujeitos à exclusão privada já existente. Não existe coletor que apague essas intenções por TTL, nem cache em memória para decisão. Reinício/recarga não renova prazo. Recibos APPLY continuam recuperáveis/repetíveis depois das 24h. Prévia anterior ao patch, sem evidência privada, exige renovação com nova chave; APPLY antigo continua retornando seu recibo original.

## Acesso e falhas

Cookie HttpOnly, HTTPS, Origin e X-CSRF-Token existentes; CORS permite `Idempotency-Key`. As duas mutações exigem FULL_WRITE; leitura de intenção permite READ_ONLY e valida usuário ativo/RLS. 401 é sessão ausente/inválida; 402 é escrita sem acesso. Chave não é credencial: outro proprietário recebe a mesma resposta que chave inexistente. Não colocar chave/corpo/token em URL ou logs. Respostas privadas não são cacheáveis quando sessão web está habilitada.

409 `swap_intention_conflict` indica chave usada com outro corpo; não trocar corpo e reaproveitar chave. 422 `swap_invalid_request` não ecoa input/msg/ctx. 429 inclui Retry-After; 503 `swap_temporarily_unavailable` inclui Retry-After 5 e não expõe SQL/stack. Ambos mantêm possível resultado desconhecido; consultar a identidade e aguardar o intervalo antes de nova ação permitida. 410 não prova rollback. Nada autoriza retry automático.

Nenhum identificador novo numérico ou campo monetário foi adicionado: a intenção usa string opaca do cliente, validada por usuário/chave/corpo no servidor. IDs legados preservam representação existente; frontend deve respeitar o domínio exato já contratado, sem inventar conversão de dinheiro.

## Provas e retomada do site

Reprodução antes do patch: HTTP com JWT RSA verificado e PostgreSQL restrito em main `b916f54331f14cf47a3800324bd61d8638043c06`, mesma implementação do pin completo. Prévia `[A]`, inserção B, APPLY respondeu 200 aplicada `[A,B]`. O teste de regressão falhou como esperado antes da correção. O PR vincula o inventário obrigatório congelado e os resultados no SHA final.

`tests/integration/test_troca_confirmacao_db.py` é obrigatório sem skips; `scripts/switch-confirmation-required.json` congela todos os casos, validados por `scripts/check_switch_confirmation_junit.py`. CI mantém as suítes de contas/financeiro/materialização/atribuição, RLS, segurança, OpenAPI, migrations, Docker e carga integral. Correções de runtime/performance herdadas de #205 (`0f9b2aec8fa9b47ada6ce18d8382766a22e5469a`) estão declaradas no PR; o endpoint de coleta de #205 não é incorporado na PR desta emergência. Na composição, permanece integralmente junto às #184/#187/#189/#193/#195/#197/#199/#201/#203/#205.

No frontend, atualizar `config/api-contract.json` com **um único** commit/hash/path da composição publicados no PR. Gerar pelo aplicativo inteiro, nunca concatenar JSON ou selecionar operações de árvores diferentes:

```sh
pnpm install --frozen-lockfile
pnpm gen-types
pnpm lint
pnpm typecheck
pnpm test
pnpm exec playwright install --with-deps chromium firefox webkit
pnpm exec playwright test tests/e2e/troca-conta.spec.ts --project=chromium-mobile --project=chromium-desktop --project=firefox-mobile --project=firefox-desktop --project=webkit-mobile --project=webkit-desktop
```

O spec será implementado no repositório do site. Os seis projetos devem manter 390×844 e 1440×900. Com API/PG/identidade reais da composição, provar: seleção elegível, picker acessível com fuso explícito, estado final escolhido, prévia e confirmação, entrada/correção concorrente recusada sem efeitos, nova prévia e nova confirmação, perda de resposta/recarga recuperando a mesma intenção, READ_ONLY após commit, 401/402/409/410/422/429/503, teclado/foco e ausência de cálculo financeiro. Guardar traces/screenshots por navegador/viewport; mocks de rede não provam o contrato. A API não afirma ter executado essa jornada de tela. Publicação depende de integração/revalidação administrativa; sandbox disponível não significa contrato em produção.


## Revisão do administrador — 20/10/2026

- Fluxo conferido: `preview_troca` passa `aplicar=False`; `aplicar_troca` passa `aplicar=True`; `_switch` encaminha o mesmo booleano ao serviço real. Não foi alterada uma decisão de negócio que já estava correta. Os 34 aceites HTTP/PostgreSQL verificam a prévia sem troca parcial, aplicação concorrente única, replay, evidência alterada e limites temporais.
- Recuperação usa o header `Idempotency-Key`, primário e proprietário autenticado. GET não aplica nem renova intenção e permanece disponível em READ_ONLY. PREVIEW/EXPIRED/404 não autorizam concluir rollback ou retry automático.
- Validações de vigência, origem, destino e evidência continuam antes da troca; alteração do jogo é observada pelo aceite HTTP real. A conta permanece regida pelo jogo, com exceção multicontas explícita.
- Regressões de contextvars ASGI, reset em exceções, RLS, autenticação e roteamento são executadas; standby real e todos os gates continuam exigíveis no HEAD final.
- PR diretamente sobre main. Dependências de integração #193/#195/#199 permanecem declaradas; depois de aceitas, reconciliar com a main efetiva e repetir CI, sem presumir merge ou aprovação.
