# Revisão da parte 4: coleta, catálogo e infraestrutura dos readers

Esta entrega reúne os apontamentos dos PRs #164 (#108), #171 (#113) e
#172 (#114) em uma árvore de produto com base `main`. Publica os exemplos sintéticos da #114 por autorização do titular em 06/10/2026, para revisão no PR; não declara aprovação do administrador nem suporte real de casa. O matching/consolidação pertence à parte 5.

## Composição e ordem administrativa

Base conferida: `8d5aea6bf4c9c99581910a8b1a2ed8c19edc65b8`.
Dependências intrínsecas incluídas e testadas, sem overlays de CI:

| Origem | SHA | Motivo |
| --- | --- | --- |
| #163, parte 1 | `85f4a3941ccb01ecf4af3b3823b402888146e458` | Identidade e ciclo das instalações, escopo das credenciais e RLS |
| #177, parte 2 | `8d727d2fd1acb941552b49d0d60829cf3df9c3ce` | Resolver compartilhado por horário do jogo, multicontas e bloqueio de escrita do billing |
| #164 | `9210cef7e1b64664ac8ad347afa69ad02c056cef` | Envelope v2, sessões, inbox durável e ACK |
| #171 | `e61f9426d9a90755bf8e949d6e9f8bad26396230` | Fontes separadas e catálogo técnico assinado |
| #172 | `bcd53895238a2b5ed38aafbc9217de4da5b907ca` | Harness independente, quarentena e gate humano |

O #164 foi mesclado em uma branch intermediária e posteriormente revertido
para isolar a entrega da #107. Sua implementação é reaplicada nesta composição;
isso não significa que ela já esteja em main. Administrador deve revisar/mesclar
#163 e #177 antes desta entrega, ou coordenar sua integração. Todos os PRs têm
base main; os diffs podem compartilhar os pré-requisitos até esses merges.
Não mesclar os PRs históricos de catálogo/readers separadamente.

`h4review2026` une as pontas publicadas `f107main2026`, `c108v22026`,
`h2review2026`, `c113catalog2026` e `c114reader2026`, preservando seus IDs e
guards. Upgrade/downgrade vazio e recusa de downgrade com dados são exercitados
em PostgreSQL. Rollback com histórico segue os runbooks de cada contrato;
nunca apagar histórico para permitir downgrade.

## Respostas aos apontamentos

| PR / apontamento | Comportamento e evidência executável |
| --- | --- |
| #164: identidade/idempotência cliente e servidor | `client_event_id`, hash e identidade canônica persistidos; reenvio, perda da resposta HTTP após commit, reinício do worker e instalações concorrentes preservam ACK e uma materialização. `tests/integration/coleta/test_contract_v2.py` |
| #164: timeout/retry e limite | Inbox PostgreSQL sobrevivente ao broker; polling/retry e três falhas técnicas produzem estado durável `failed`. Não se afirma uma DLQ externa inexistente. Billing somente leitura pausa em `pending`, preserva envelope/ACK e não consome tentativas técnicas. `test_review_part4.py` |
| #164: revisão antiga não regride finanças | Watermark de revisão da fonte, hash canônico, conflito de mesma versão, atualização sem mudança financeira e ordenação v1/v2 testados. Reabrir liquidação exige revisão. Consolidação Casa × Telegram pertence à parte 5; não é comprovada por estes testes. |
| #164: N/N-1 e mudança incompatível | Artefatos gerados, hashes e compatibilidade em `coleta-v2.md`, `extension-contract-release.md` e `scripts/generate_collection_contract.py --check`; OpenAPI passa o gate de breaking changes contra main. |
| #164: concorrência real | Testes HTTP, transações PostgreSQL/RLS, duas instalações, retry após commit e locks sobre bilhete; nenhum ACK de aceitação promete processamento já concluído. |
| #171: ordem de integração | #163 e #177 incluídos nos SHAs acima; catálogo e autenticação são testados juntos sobre o código final, sem aplicar patches em `/tmp`. |
| #171: domínio e vínculo da assinatura | O catálogo assina versão, ambiente, hosts exatos, suporte e limites técnicos. É global por ambiente e não inclui `issuer`, `sub` ou instalação. A autorização de cada GET/304 valida a instalação pareada e seu usuário antes do cache. O teste `test_global_signed_catalog_is_authenticated_per_installation_without_tenant_identity` prova dois tenants, revogação e separação. Assinar identidade no catálogo criaria uma projeção personalizada incompatível com esse contrato; não foi adotado. |
| #171: comunicação ao usuário | Legalidade/fonte/data de consulta ficam separadas do suporte técnico; catálogo não concede permissão de navegador nem endosso legal. Runbook especifica os campos e limites que o cliente deve apresentar. UI/extensão não são alteradas neste backend. Snapshots regulatórios datados não são tratados como consulta atual. |
| #171: E2E real | Parear, obter publicação assinada, ETag, rotacionar, revogar e rejeitar token/ambiente/versão/host inválidos via API real e PostgreSQL. Suíte de 41 casos de catálogo/instalação sem skips. |
| #172: schema desconhecido | Registry fechado por casa/host/schema/endpoint/canal/versão; casos incompatíveis viram quarentena sem fallback default ou mutação financeira. `tests/coleta/`, `reader_quarantine_acceptance.py` |
| #172: golden independente | Oracle por bytes canônicos, hash e versão. Capturas reais exigem aprovação humana antes do Git. Para o pacote sintético exato da #114, o titular autorizou publicação para review em 06/10/2026; digest fixo, sem aprovação administrativa inferida. |
| #172: ausência de matching | Reader produz resultado normalizado ou quarentena. Matching #109, consolidação #110 e ligação ao pipeline #115 permanecem fronteiras explícitas; teste de reader não comprova consolidação. |
| #172: formatos mistos/regressão | Harness cobre versão/canal/host incompatíveis, moeda, opcionais, múltiplas e segurança; 16 aceites de quarentena PostgreSQL/RLS/rollback/concorrência, além da regressão herdada. O conjunto de produção ainda está vazio enquanto falta aprovação das fixtures. |

## Conta pelo jogo e multicontas

Materialização v1/v2 e a entrada de compatibilidade do repositório usam o
resolver da parte 2 com `comeca_em`/`data_jogo`. Sem horário do jogo ou com
usos ambíguos, não se escolhe conta atual, menor ID ou data de colocação.
Uma referência explícita válida mantém a conta que efetivamente apostou,
inclusive fora do período do default; referência inválida nunca faz fallback.
O v2 passa a marca de referência explícita separadamente do resultado derivado.

`test_review_part4.py` prova aposta colocada antes da troca e jogo depois,
default pela conta nova, multiconta pela antiga, ausência de jogo, evento de
auditoria e ACK/retry. A ativação de billing usa um banco descartável próprio
para não interferir com testes paralelos. A data legada de colocação conserva
seu significado para outros consumidores, mas não determina esta atribuição.

## Pacote sintético da #114 publicado para revisão

Em 06/10/2026 o titular autorizou publicar os 11 exemplos/33 arquivos no PR, substituindo a espera de aprovação anterior ao Git para esse pacote exato. Digest: `4df5308bfc391564e6e2cb0e6ace49e29f7194a640f5c74ce53aec55353de601`. A revisão administrativa continua pendente; `reviewer` e `reference` permanecem null e o status é `publication_authorized`.

O gate aceita somente esse digest e a proveniência sintética do exemplo; continua verificando os hashes originais, dados sensíveis, inventário, versões e goldens. Os testes publicados comprovam estados, valores, replay e atualizações. O relatório distingue um conjunto publicado para revisão de zero conjuntos aprovados, e registra zero leitores de produção. Capturas reais e outros pacotes seguem o gate de aprovação humana anterior. Detalhes: [contrato](../contracts/backend-readers.md).

## Gates de entrega

Ruff, formato, mypy, auditoria de dependências/código, suíte com cobertura
mínima de 80%, contratos/OpenAPI/breaking changes e Docker permanecem
obrigatórios. Jobs dedicados exercitam contas/billing, coleta/instalações,
catálogo e readers no código final. Aceites obrigatórios rejeitam ausência,
falha, erro e skip; a regressão herdada permite apenas os cenários de réplica
não configurada e o placeholder histórico do painel. O gate de segurança/autorização continua
obrigatório; a exceção sintética exata não promove leitor de produção.
