# Prints web — entrada, reconciliação e resultado por arquivo

Entrega candidata de backend #194 para [frontend #25](https://github.com/pradyumna-001/bancaemdia-frontend/issues/25) / #50, conforme a decisão do titular que substitui referências históricas a consentimento nos documentos frontend ADR-019, docs/backlog/025.md e docs/API-CONTRACTS.md. Merge e publicação produtiva ficam com o administrador. Nenhuma alteração no frontend ou na extensão. Não há estimativa, preço de processamento, autorização de gasto ou etapa de consentimento na API/jornada de prints.

## Operações

| Operação | Corpo | Acesso |
| --- | --- | --- |
| POST `/api/v1/prints/intencoes/{intent_id}/itens/{item_id}` | multipart, exatamente um `file` | FULL_WRITE |
| GET no mesmo caminho | nenhum | FULL_WRITE e READ_ONLY |
| GET `/api/v1/prints/intencoes/{intent_id}` | nenhum | FULL_WRITE e READ_ONLY |
| POST caminho do item + `/retry` | JSON `retry_id` UUID e `expected_attempt` inteiro 1–3 | FULL_WRITE |
| POST `/api/v1/prints/revisoes/{revisao_id}/resolver` | `acao=CORRIGIR|DESCARTAR` e `aposta_corrigida` opcional | FULL_WRITE |

Nas quatro operações de intenção/item os parâmetros são UUID; na resolução, `revisao_id` é o inteiro positivo de `review_ids`. Sem query params, chat/message_id, nome de storage ou credencial de bot. `intent_id` e `item_id` são UUIDs escolhidos pela SPA e persistidos **antes** de enviar. A seleção múltipla envia vários itens, cada um com um POST independente. O nome do arquivo não é armazenado/usado como caminho. Cookie HttpOnly e `credentials: 'same-origin'` (ou `include` na origem exata autorizada), Origin e `X-CSRF-Token` em memória para mutations. O transporte de sessão existente verifica CSRF. Bearer permanece somente no modo legado explicitamente habilitado pelo servidor.

202 significa admissão durável, **não** criação de aposta. O commit da admissão precede a publicação na fila. Broker indisponível pode produzir 503 depois desse commit; GET com os UUIDs originais encontra o estado. A inbox PostgreSQL e o tick Celery a cada 10 segundos recuperam a publicação após falha de transporte. A fila leva apenas IDs de usuário/recurso, nunca bytes/base64. Não há cancelamento de processamento publicado; abortar fetch/observação, refresh ou logout não cancela nem autoriza replay.

## Identidade e recuperação

Escopo da identidade: usuário autenticado + intenção + item. Reenviar **exatamente** os mesmos bytes nessa identidade não altera os dados aceitos nem cria fatos/eventos novos; bytes incompatíveis são recusados com 409. Não usar novo UUID para tentar resolver timeout. Mutations não recebem retry automático. Após perda de resposta, consultar o GET do item; após perda de resposta de retry, comparar `last_retry_id` e `attempt` com a ação persistida. 404 significa ausência/inacessibilidade/estado expirado, sem autorizar reenvio automático. Uma nova tentativa de entrada sem admissão comprovada exige ação explícita da pessoa, preservando a identidade quando ainda válida.

SHA-256 **dos bytes reais** decide dedupe entre arquivos de um mesmo usuário, inclusive entre intenções e envios concorrentes. O hash do navegador é auxiliar e não entra no contrato de autoridade. Outro usuário recebe seu próprio processamento, sem acesso cruzado. Recorte, recompactação ou alteração visual mudam os bytes: não há garantia de reconhecer semanticamente todo bilhete modificado. O pareamento/materializador existentes continuam decidindo fatos; o navegador não decide nem soma finanças.

Aliases duplicados acompanham o mesmo estado/resultados canônicos. Um alias de processamento falho pode solicitar a mesma tentativa explícita; versões e lock por usuário impedem duas tentativas concorrentes de criarem um novo processamento. Sucesso, item aceito/processando, erro não retryable, três tentativas consumidas ou bytes expirados recusam retry com 409. O último `retry_id` aceito com a mesma `expected_attempt` é idempotente e retorna o estado atual. Identidades mais antigas/versões incompatíveis retornam 409, sem refazer sucessos.

Worker usa lease de dez minutos, token de posse e materialização de todos os cupons do item na mesma transação com resultado final. Leitura extraída é checkpoint privado antes da gravação; reentrega depois da conclusão não altera fatos. Recuperar lease vencido conta uma tentativa, também limitada a três, e reutiliza o checkpoint quando disponível. Quotas internas são transacionais, por usuário; `PRINT_DAILY_ATTEMPT_LIMIT=200` por dia UTC, positivo, inclui admissão e retries/recovery e é conservador ao reabrir um item de dia anterior. A extração também mantém os limites globais/por usuário do backend. Essas decisões são internas, sem valores monetários ou aprovação no cliente.

## Estados publicados

Item: `status=accepted|processing|completed|failed`. `outcome=null` antes do terminal; no terminal `materialized|review|no_bet|duplicate|failed`. `results=null` quando desconhecido; `[]` significa leitura concluída sem aposta. Cada resultado tem `type=materialized|review`, `bet_key` nullable e `review_ids` reais. Duplicado conserva os resultados canônicos e expõe `duplicate=true`. Não inferir criação de aposta por contagem: seguir `outcome` e os resultados explícitos. `progress_percent=null` enquanto não há progresso mensurável; 100 indica processamento terminal, inclusive falha, não sucesso financeiro.

Intenção agrega **os itens efetivamente recebidos**, com `state=processing|complete|partial|failed`, `received_items` e `terminal_items`. Não há manifesto de seleção nem operação de selagem: `complete` prova que todos os itens recebidos até a consulta terminaram, não que todos os arquivos locais foram enviados/aceitos. A SPA mantém o estado de transporte por identidade e mostra individualmente 400/413/422 de arquivos recusados; nunca declara toda a seleção enviada a partir da contagem aceita. O GET pode mudar quando outro item é recebido na mesma intenção.

Stake/odd desconhecidas ficam null na Revisão; não nasce aposta financeira com zero inventado. GET de Revisão mostra campos preservados e foto privada. Resolver `CORRIGIR` exige valores válidos e usa o materializador existente uma vez; `DESCARTAR` fecha a revisão sem criar aposta, com `aposta=null` na resposta publicada. A rota própria `POST /api/v1/prints/revisoes/{revisao_id}/resolver` admite esse null quando não há aposta financeira, sem ampliar a resposta da resolução padrão já publicada. Usar esta rota para os resultados de Revisão com `bet_key=null`; apostas já materializadas continuam usando a resolução padrão. Dados completos usam conferências, revisão de conta pelo **dia do jogo**, pareamento e materialização existentes; origem `print`, chat/message_id nulos.

## Limites e erros

20 itens / 100 MiB somados por intenção, contabilizando cada alias recebido; 20 MiB por arquivo. JPEG/PNG/GIF/WebP, MIME real e MIME declarado compatíveis, decodificação real, 40 milhões de pixels somados e até 256 frames. Arquivo vazio/truncado/corrompido, SVG/HTML ou MIME divergente: 422. Exatamente um arquivo por multipart; extras são recusados, nunca ignorados silenciosamente. Corpo HTTP limitado também sem Content-Length; a margem multipart é 64 KiB.

| HTTP | Significado |
| --- | --- |
| 400 | multipart malformado ou múltiplos campos/arquivos |
| 401 | sessão ausente/inválida/expirada/revogada; não é billing |
| 402 | account_read_only, nova admissão/retry recusado; GET permanece disponível |
| 403 | Origin/CSRF inválido no modo cookie |
| 404 | intenção/item/foto inexistente, alheio ou estado expirado, sem revelar existência |
| 409 | identidade/bytes incompatíveis, retry ineligível ou versão antiga |
| 410 | identidade conhecida expirada; não substitui tombstone nem duplica fatos |
| 413 | bytes/quantidade acima do teto |
| 422 | imagem/pedido/correção inválidos |
| 429 | quota, respeitar Retry-After em segundos |
| 503 | fila/banco/dependência indisponível, Retry-After 5; consultar identidade antes de ação |

Erros de domínio usam `{ "detail": "mensagem segura" }`; quota genérica mantém o envelope rate_limited existente; erro de envelope multipart no limite global mantém a resposta HTTP 413 da infraestrutura. Sem nomes internos, bytes, armazenamento, segredo, tokens ou custos nas respostas/logs. `error_code` terminal é `unavailable|account_read_only|media_expired|invalid_reading`, com `retryable` calculado pelo servidor. Respeitar 401/402 separadamente, deadlines/cancelamento e não usar cache persistente da sessão.

## Foto, privacidade e retenção

Bytes cifrados em tabela privada forced RLS, com chave derivada separadamente do segredo do backend; nenhum blob global por hash. GETs de estado são private/no-store, Vary Authorization/Cookie e nosniff. Revisão recebe a referência exata de item/proveniência; foto da aposta usa a capacidade herdada de #193, GET `/api/v1/apostas/{chave}/foto`, mesma origem, sem URL assinada/storage ref. Apenas o dono da aposta com resultado canônico correspondente acessa; hash conhecido não autoriza. Preservado após resolução e exclusão/restauração lógica, sujeito à retenção.

Bytes: sete dias **da primeira aceitação canônica**, sem extensão por duplicação/retry. GET retorna ausência imediatamente após o prazo, mesmo antes da remoção física. Tick remove bytes expirados e checkpoints/resultados sem intenção ativa, sem alterar apostas/eventos/Caixa/consolidações. Estado de reconciliação por intenção: 90 dias; uma intenção nova pode acompanhar resultado canônico ainda retido, sem restaurar foto expirada. Tombstones mínimos de UUID/hash/estado ficam até exclusão da conta, garantindo que expiração não habilite duplicação financeira. Os resultados não incluem bytes/base64. Excluir a conta remove as tabelas por cascade; nenhum dado dessa entrada é enviado ao frontend ou extensão fora das leituras autorizadas.

## Dependências e prova

Branch diretamente sobre main b916f54331f14cf47a3800324bd61d8638043c06. Reaproveita fonte #193 `71f1a1acba17f2b99e8dcd55c25c672c98f43967`, incluindo foto/provas e infraestrutura de desempenho já declarada nela da #191 `552c4ffe3924df3eac2caa50ea4f5df4c553b816`. Arquivos de código/testes copiados de modo explícito, sem merge/cherry-pick e sem copiar schemas gerados. Código próprio: entrada, estado/dedupe/inbox privados, retry, leases/checkpoint, extensão da revisão e da proveniência para mídia web, migração, contrato e gate. Os candidatos precisam de reconciliação administrativa de overlaps antes de publicação conjunta; não exige merge da #193 para testar esta árvore standalone. Nenhum schema foi unido manualmente.

```sh
pip install -e '.[dev]'
# PostgreSQL 16 e Redis 7 descartáveis; roles reais NOBYPASSRLS preparadas pelas fixtures
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/disposable \
TEST_REDIS_URL=redis://localhost:6379/0 \
pytest -n 0 tests/integration/test_web_print_db.py --no-cov --junitxml=web-print-results.xml
python scripts/check_web_print_results.py web-print-results.xml
python scripts/generate_openapi.py --check
```

Entradas sintéticas, extração por adaptador isolado sem chamada paga; worker Celery real em processo separado e Redis real. O gate dedicado exige o inventário inteiro, sem falhas/skips/duplicações. As 42 provas de foto de #193 continuam obrigatórias. Suíte/segurança/cobertura/identidade/billing/integridade/build/carga existentes mantidos no HEAD final. Exemplos tipados em `docs/contracts/examples/prints-web.json`; hash/SHA integral e links da entrega ficam na PR. Esta capacidade desbloqueia o contrato da #25, não implementa/fecha a tela do frontend. Não é deploy, contratação ou provisionamento de produção.
