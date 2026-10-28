# Criação manual reconciliável — candidata da #202

Desbloqueia a frontend #29. PR diretamente contra `main` auditada em `b916f54331f14cf47a3800324bd61d8638043c06`. O merge e a integração pertencem ao administrador; não é declaração de tela pronta, versão integrada ou deploy.

## Proveniência e compatibilidade

O problema está no protocolo HTTP: o POST antigo gera `m:uuid4()` a cada envio. Os upserts com chave já conhecida da #36 não recuperam uma resposta 201 perdida. A rota antiga permanece compatível e sem uma garantia nova implícita de idempotência. A tela nova deve usar o protocolo abaixo.

Esta candidata implementa a capacidade sobre main, reutilizando **a mesma criação manual**, materialização/eventos, unidade vigente, atribuição e revisão de conta. A única mudança na fronteira compartilhada é permitir que o chamador do protocolo durável seja responsável pelo commit externo. Não há segundo motor financeiro/de atribuição. São herdadas pontualmente de #201 (`e830c436dbdeb763f7a7d09b1d849fdbb6652db7`) as otimizações ASGI de RLS/roteamento e dos caminhos de autenticação que já eram públicos/dispensados (sem mudar autorização, limites ou guardas), as consultas parametrizadas/cacheadas e menos leituras repetidas na coleta/validação de credencial, assinatura atual, matching e materialização (mantendo trava, usuário ativo, revogação e quota), a preservação de `Retry-After` explícito no limitador, a inicialização da réplica de teste com checkpoint rápido e o hook de mypy estrito sobre todo `src/` no ambiente completo do projeto, igual à CI. Isso não incorpora nem substitui as demais superfícies abertas.

A capacidade isolada não exige que #184/#187/#189/#193/#195/#197/#199/#201 sejam mescladas para funcionar. A árvore completa do consumidor **continua dependendo dessas candidatas**, preservadas no sandbox fornecido: `547a4d0cfce5308dfe71221a77ad6fd5eb6e47cd`, branch `codex/site-review-frontend-reconciliation`, 125 operações, SHA-256 do schema `09cf9fbb4b46a8dfc20fc4f4a3c97c3b88000612308cfb4e1e8fd4565901854a`. É descendente de testes de `27054591bf223d97151b06b0ab2632ccd20048d3`; código, migrations e schema são iguais entre esses pins, conforme o pedido do consumidor. Frontend #94 fixa essa árvore em `1428e9a5923dd622a3c54bdeada5f145ff5aa2c6`.

`m202manual2026` acrescenta a tabela de recibos a `j6main2026`, sem alterar IDs/parents de migrations publicadas. Na prova separada de composição, reconciliar os deltas **de código e migrations** sobre o pin completo, preservar o head conjunto e acrescentar um join local com a nova migration. Conservar todos os contratos e testes, inclusive as tabelas XLSX. Executar `python scripts/generate_openapi.py` nessa árvore real; o resultado esperado é 127 operações, preservando todas as 125 anteriores. Não substituir o schema completo pelo isolado de 91 operações, não unir JSON manualmente e não gerar tipos de duas versões. O manifesto entregue com a PR registra o commit/hash efetivamente ensaiado. Após merge administrativo, reconciliar o pin integrado e repetir as provas.

## Identidade, pedido e permissões

Antes do envio, a SPA gera um UUID e o registra na URL do próprio formulário. Corpo, descrição e valores financeiros ficam somente na memória do componente; tokens permanecem no servidor. A identidade pública não concede autorização. O namespace é `(usuário autenticado, UUID)`; outro usuário não enxerga a existência do recibo e pode usar o mesmo UUID para sua própria aposta.

```http
POST /api/v1/apostas-manuais/operacoes/ce1ad24e-e459-4809-b924-e916a86902e7
Content-Type: application/json
X-CSRF-Token: <prova atual da sessão>
Origin: <origem exata configurada>
```

```json
{
  "casa": "Betano",
  "data_aposta": "2026-10-15T14:00:00-03:00",
  "data_jogo": "2026-10-16T19:00:00-03:00",
  "odd": 2.0,
  "stake_unidades": 1.0,
  "evento": "Casa x Fora",
  "descricao": "Casa vence",
  "mercado_bruto": "Resultado final",
  "freebet": true,
  "comissao_centavos": "0"
}
```

`casa`, `odd` e `stake_unidades` são obrigatórios. `conta_casa_id`, quando informado, é **string decimal exata** de BIGINT positivo, por exemplo `"9007199254740995"`; precisa ser uma referência própria autorizada na casa. Comissão em centavos também é string exata, não negativa. Odds/unidades reutilizam a representação numérica do domínio existente; o frontend não converte unidades em dinheiro. `freebet` exige booleano e mantém face positiva na entrada; somente a API calcula custo próprio zero.

Datas ISO sem offset usam America/Sao_Paulo e são normalizadas para um instante UTC antes da identidade do corpo. `data_aposta` omitida/null é resolvida **somente na primeira criação** pelo relógio do servidor; a unidade vigente é a dessa data. O instante do jogo decide a conta quando não há referência humana explícita; data de colocação não substitui jogo ausente. Ausência/ambiguidade cria revisão e aviso, sem escolher a primeira conta. A exceção multicontas conserva a conta explícita própria da casa, inclusive histórica/encerrada/sem titular quando o serviço existente permite. Titular, casa, conta e banca são distintos.

POST inicial e replay explícito exigem **FULL_WRITE atual**, inclusive após esperar uma trava. Cookie HttpOnly exige Origin exata e CSRF atual. GET usa **READ_ONLY**, autenticado no primário, e não exige nova assinatura. Todas as respostas dessa superfície são `private, no-store` e variam por Cookie/Authorization.

## Confirmação original e leitura atual

Criação inicial retorna 201; repetição explícita do mesmo UUID/corpo normalizado retorna 200 e **o mesmo recibo**; GET também retorna 200:

```http
GET /api/v1/apostas-manuais/operacoes/ce1ad24e-e459-4809-b924-e916a86902e7
```

```json
{
  "intencao_id": "ce1ad24e-e459-4809-b924-e916a86902e7",
  "estado": "confirmada",
  "aposta_chave": "m:2d135b187c0144119bf0157423a0ee44",
  "detalhe_url": "/api/v1/apostas/m:2d135b187c0144119bf0157423a0ee44",
  "confirmado_em": "2026-10-15T17:00:00Z",
  "recuperavel_ate": "2027-01-13T17:00:00Z",
  "confirmacao_original": {
    "data_aposta": "2026-10-15T17:00:00Z",
    "data_jogo": "2026-10-16T22:00:00Z",
    "conta_casa_id": null,
    "criterio_conta": "data_jogo",
    "valor_unidade_centavos": "10000",
    "stake_centavos": "0",
    "valor_aposta_centavos": "10000",
    "comissao_centavos": "0",
    "freebet": true
  },
  "avisos": [{"codigo":"conta_ausente","revisao_id":"42","encaminhamento":"revisar_conta"}]
}
```

Exemplo ilustrativo com unidade padrão e sem conta resolvida; as referências não são dados de produção. `avisos` pode ser vazio; o outro código publicado é `conta_ambigua`. A referência da revisão é autorizada e original, não promessa de que ela ainda esteja aberta.

O recibo comprova **a primeira criação**, não é a projeção financeira atual. Data padrão, unidade, conta, comissão e fatos usados nessa criação ficam congelados. Uma correção/apagamento posterior não modifica o recibo nem permite recriar a aposta. Só após confirmação autoritativa a SPA navega à chave retornada e usa o detalhe existente para a leitura atual; esse detalhe pode estar alterado, apagado ou indisponível. Uma exclusão física posterior também não apaga o recibo: não há FK em cascata da aposta para a intenção.

## Máquina de recuperação e retenção

Não há fila nem estado assíncrono fictício: a operação é síncrona e só existe recibo confirmado depois do commit. Durante uma transação ainda não confirmada, GET pode retornar 404. Esse resultado não cancela o POST em voo e não prova rollback.

| Resultado | Garantia e ação do consumidor |
|---|---|
| POST 201 / POST explícito 200 / GET 200 | Confirmação original do UUID. Navegar à chave retornada e ler o estado atual separadamente. |
| `manual_intention_not_found`, 404 | Não há recibo visível no snapshot consultado; recurso alheio é indistinguível. Não comprova rollback. Reler após falha/resposta perdida. |
| `manual_receipt_expired`, 410 | Janela de recuperação de 90 dias acabou. A identidade continua ocupada; não reenviar com UUID novo para tentar recriar a mesma intenção. Encaminhar a suporte/conferência humana. |
| `manual_intention_conflict`, 409 | UUID já vinculado a outro corpo normalizado. Sem nova criação; recuperar a confirmação original quando disponível. |
| `manual_creation_busy`, 409 + Retry-After: 1 | Trava/transação concorrente; nenhuma repetição automática. Consultar o UUID antes de um gesto explícito. |
| `manual_validation_invalid`, `manual_account_invalid`, 422 | `campo` seguro para o formulário; `efeito=nenhuma_gravacao_neste_pedido`. Não significa que outra tentativa do mesmo UUID não tenha confirmado. Preservar valores e ajustar o formulário. |
| 401 | Reautenticar, mantendo o UUID de recuperação. Não repetir criação automaticamente. |
| 402 `account_read_only` | Preservar sessão/formulário; GET de recibo continua permitido. Pagamento não dispara replay. |
| 403 | Corrigir Origin/CSRF/sessão atual; não repetir automaticamente. |
| 413 / 429 `rate_limited` | Respeitar limite/Retry-After e preservar o formulário. Consultar o UUID se houve tentativa anterior de resultado desconhecido. |
| `manual_creation_unavailable`, 503 + Retry-After: 5; autenticação 503; demais 5xx/timeouts | Resultado desconhecido. Consultar o UUID, sem concluir rollback nem repetir mutation automaticamente. |

Um **gesto humano explícito**, dentro da janela e com FULL_WRITE atual, pode reenviar **o mesmo UUID e corpo original**, se o cliente ainda os tiver em memória. O servidor serializa a identidade: ou recupera o commit anterior, ou cria uma única aposta se nenhuma criação confirmou. Nunca inventar UUID novo como recuperação. Após recarga sem o corpo original, usar GET; não reconstruir o corpo por semelhança de listagem/totais. Um formulário corrigido após 422 pode ser submetido por gesto explícito; se outra tentativa já confirmou aquele UUID, corpo diferente conflita.

Normalização v1: casa canônica do vocabulário, números parseados pelo contrato, datas convertidas ao mesmo instante UTC, opcionais omitidos/null equivalentes e comissão omitida/null equivalente a `"0"`. Textos permanecem exatos; valores resolvidos no cadastro/relógio não entram no fingerprint do pedido. **UUIDs diferentes com conteúdo igual criam duas apostas legítimas.**

O recibo é consultável por 90 dias a partir de `confirmado_em`. Depois retorna 410, inclusive em replay do corpo original. Identidade, fingerprint e confirmação permanecem como auditoria imutável até apagamento autorizado da conta; não há job que libere UUID antigo. A expiração é de acesso de recuperação, não declaração de purga automática do registro. RLS obrigatório/FORCE, guarda FULL_WRITE no banco e trigger imutável protegem os recibos. Somente o fluxo autorizado de apagamento de dados do mesmo usuário pode removê-los. Downgrade com recibos presentes recusa perda de identidades.

## Atomicidade e aceites

A transação READ COMMITTED no primário usa a mesma trava de usuário/pareador do escritor atual. Pedidos simultâneos veem o recibo confirmado após a trava; a chave composta `(usuario_id,UUID)` também impede dois recibos. Aposta, evento, revisão de atribuição e recibo são confirmados juntos. Um erro de flush antes do commit reverte todos eles. Nenhuma unidade/data/conta é recalculada ao recuperar/repetir. Métrica de criação só é incrementada após commit novo, nunca em replay.

Provas obrigatórias reais estão em `tests/integration/test_manual_creation_db.py`: perda da resposta depois do commit; replay sequencial, oito concorrentes e corpos concorrentes divergentes; duas intenções legítimas iguais; dois processos reais sucessivos da API; expiração e identidade retida; erro PostgreSQL antes do commit; freebet/face/custo/comissão/unidade/fuso; conta temporal, histórica, multicontas, ausência e ambiguidade legada; UUIDs/BIGINTs; erros seguros; 401/402/CSRF/Origin/revogação/RLS; limite/Retry-After; correção/apagamento posterior e compatibilidade antiga. A ambiguidade histórica é preparada em transação de teste revertida, sem retirar as restrições de produção. Não são mocks financeiros nem upsert isolado.

```sh
pytest tests/integration/test_manual_creation_db.py -n 0 --tb=short --junitxml=manual-creation-results.xml
python scripts/check_manual_creation_junit.py manual-creation-results.xml
pytest tests/contract/test_manual_creation.py -n 0
python scripts/generate_openapi.py --check
ruff check .
ruff format --check .
mypy src/ --strict --ignore-missing-imports
```

`TEST_DATABASE_URL` deve apontar a PostgreSQL 16 real. O checker congela o inventário completo e recusa falhas, erros, skips, duplicatas ou omissões. O job `manual-creation-db` é pré-requisito de Docker Build; permanecem todos os demais gates, cobertura mínima e carga de 55 minutos com réplica real. A PR só é entregue fora de rascunho após todos os checks aplicáveis verdes no SHA final. Browser do formulário pertence ao frontend ao consumir o contrato.


## Revisão do administrador — 20/10/2026

- Conta temporal continua exclusivamente pelo jogo; ausência não usa a data da aposta como fallback. Multicontas explícita conserva a exceção autorizada. Os campos opcionais documentam isso também no OpenAPI.
- Data da aposta omitida/null é resolvida pelo servidor somente na primeira criação; replay e recuperação conservam o recibo. O teste real de defaults cobre essa política já existente.
- Erros inesperados do criador recebem 503 estruturado, sem diagnóstico privado. Rollback remove fatos ainda não confirmados; a resposta continua declarando resultado desconhecido para que falha de resposta/commit nunca seja interpretada como autorização de replay automático. Novo aceite HTTP executa o criador real antes de interromper e verifica ausência de fatos parciais e recuperação pelo UUID.
- FULL_WRITE é verificado antes da trava para recusar READ_ONLY sem disputar o escritor; a verificação após a espera permanece para impedir permissão obsoleta. 402 não é convertido em 503.
- Inventário obrigatório passa a 45 casos. Regressões ASGI de contexto, autenticação e roteamento e a prova de standby real continuam exigíveis. Nenhum gate ou limite foi relaxado.
