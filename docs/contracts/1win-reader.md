# Leitor 1win — fonte interpretada e dependências de materialização

Revisão: 08/10/2026. Relacionado à [#115](https://github.com/pradyumna-001/bancaemdia-api/issues/115).
Esta entrega implementa a leitura da projeção candidata e sua retenção transacional;
**não conclui a #115 nem habilita suporte financeiro/produção**. O lote escolhido
pelo titular é 1win; os seis leitores herdados continuam disponíveis para replay.

## Contrato e origem

Base desta implementação: `main` API `b916f54331f14cf47a3800324bd61d8638043c06`.
Dependência externa: [extensão PR #33](https://github.com/pradyumna-001/bancaemdia-extension-client/pull/33),
HEAD `0fcf6c4fcbd5de6289eb7eef67b26f90f28c9871`, e seu
[handoff único](https://github.com/pradyumna-001/bancaemdia-extension-client/blob/0fcf6c4fcbd5de6289eb7eef67b26f90f28c9871/docs/1win-reader-handoff-115.md).
O PR API nasce diretamente de main; não importa commits ou depende de branches
intermediárias. #111–#114 já estão integradas via #181. #184 não é dependência.

| Item | Valor exato |
| --- | --- |
| Reader | `1win_history_candidate`, `0.1.0` |
| Página escolhida | `1win.com` |
| Resposta final observada | `api-gateway.top-parser.com` |
| Fonte | `/bets/history/get-many`, Fetch ou XHR, resposta observada |
| Schema v1 | `1win-history-fields-v1`; objeto plano por aposta; adapter `own-fields-v1` |
| Schema candidato v2 | `1win-history-game-v2`; um `{bet, selections}` por aposta; adapter `own-game-v2` |
| Envelope interno | `ReaderEnvelope`, versão 1, JSON textual, SHA-256 dos bytes UTF-8 |

`OneWinReader.inspect` valida novamente envelope/hash, JSON sem chaves duplicadas,
campos mínimos e dados sensíveis, origem e versão. Não recebe a resposta completa
`result.items`: a projeção por item é explícita. Não aceita host da página como host
final, detalhe `/bets/history/get`, wildcard, mirror, marca equivalente ou schema
desconhecido. A v1 continua distinta e indica `game_fields_not_projected`.

Strings e números preservam seus tipos. Numbers são lidos diretamente do texto
JSON com `Decimal`; bool, float Python, string monetária e não finito são recusados.
O limite de 128 dígitos por número e magnitude de 10^15 é de leitura defensiva;
nenhum arredondamento ou conversão para centavos ocorre nesta observação.
O schema candidato aceita moeda ISO de três letras, sem assumir BRL, escala ou
fórmula de liquidação. `cf` exige o intervalo/precisão aceitos pelo contrato comum.
Opcionais podem estar ausentes; somente `match.startAt` admite null, que é retido
como falta de horário. Campos extras, valores nulos indevidos e texto vazio falham.

## Semântica comprovada e limites

O enum público mapeia 0=pendente, 1=perda, 2=ganho, 3=reembolso, 4=vendida/cashout.
São estados do código público, **não estados reais comprovados pelo corpus**.
Um enum desconhecido é drift. `isHalfReturn` é preservado como flag, sem inventar
estado parcial ou calcular retorno. Valores de bônus/freebet são preservados
separadamente, sem somar, subtrair ou tratá-los como stake debitado.

`bet.id` define identidade dentro do namespace marca/host; a persistência existente
adiciona o escopo do usuário autenticado. Mudança de conteúdo muda o hash dos
bytes, preservando identidade. IDs numéricos e textuais das seleções têm namespaces
`n:` e `s:` distintos. Seleção repetida pelo mesmo match/odd é drift.
Isso detecta repetição e alteração; **não prova ordenação de versões atrasadas**.

O horário do jogo vem exclusivamente de `selections[].match.startAt`, em segundos
Unix. Aceita número JSON exato ou string numérica decimal candidata, até resolução
de microssegundo; rejeita data textual, formato local, bool, zero, negativos ou overflow.
`game_at` é o menor início em UTC, independente da ordem das seleções.
Descrição exige nomes dos competidores e seleção; não inventa evento/mercado.
`createdAt` só admite ISO com fuso como colocação. Não substitui horário de jogo
nem relógio de revisão. **Conta será resolvida pelo jogo; somente referência
explícita e validada de multicontas poderá prevalecer. `wallet` não escolhe conta.**
Esta observação não atribui conta, stake, retorno nem `source_updated_at`.

Fontes públicas pinadas, obtidas em 06–07/10 e conferidas por hash nesta entrega:

| Asset da casa | SHA-256 |
| --- | --- |
| [history-CxC0I4Sf-C765cAb0.js](https://1win.com/resources/v1/app/assets/history-CxC0I4Sf-C765cAb0.js) | `82a1a86977a75a068fcaa7a9d3e602ec9d07b04f4c56608d89aaf1a833a19c8c` |
| [useModalSettings-DQKnEpz0-D-d4NpVg.js](https://1win.com/resources/v1/app/assets/useModalSettings-DQKnEpz0-D-d4NpVg.js) | `2a49159856fef8454f57aca0942069466473e149c1b62da345829ca071bb8b76` |
| [betting-CDFLfyB7.js](https://1win.com/resources/v1/app/assets/betting-CDFLfyB7.js) | `0c0d13f457b26f6c0db6826e533ced95629f3f429e72eb02944f3af2cc42a8da` |

Os offsets e funções estão na
[auditoria publicada da extensão](https://github.com/pradyumna-001/bancaemdia-extension-client/blob/0fcf6c4fcbd5de6289eb7eef67b26f90f28c9871/docs/research-evidence/1win-public-semantics-2026-10-08.json).
A atualização online dos assets em 08/10 falhou no DNS na pesquisa da extensão;
esta conferência offline não afirma que os bundles ainda sejam os atuais.
O corpus real aprovado (`c5c0d0e6ffc85fe8c25363e3e16dd52a5eb41be747c401e391f12c225409c94c`)
mantém valores neutralizados; não comprova relações financeiras nem horários.

## Retenção e persistência

`OneWinReader.parse` executa a interpretação e então retorna erro estável
`incomplete_payload:financial_evidence_pending`. Assim não produz `CanonicalBet`
sem stake/retorno/revisão comprovados. `candidate_registry()` é uma registry
explícita para autoria e quarentena, **fora de `DEFAULT_REGISTRY`, rotas e workers**.
Não ativa perfil cliente, catálogo ou capacidade de produção.

A fronteira existente `parse_or_quarantine` persiste rejeições sob RLS do usuário
autenticado, na transação do chamador. Replay idêntico e concorrente converge numa
única evidência. Entradas privadas não guardam corpo nem valores nos logs.
Falha deste reader não desabilita outro reader registrado. Não cria `Aposta` ou
`Evento`; não chama materialização legada para contornar a retenção.

## Dependências para concluir #115

| Dependência | Responsável e próximo passo |
| --- | --- |
| Delta de seleções/horários e relações numéricas reais | Extensão/titular: captura passiva mínima da lista, sanitização coerente, revisão específica da candidata v2; detalhe somente se necessário. Não repetir export estrutural zerado. |
| Stake real, `profitAmount` bruto versus lucro, unidade/precisão e moeda, bônus/freebet/parciais/cashout | API interpreta com o delta acima e escreve oráculos independentes. Não inferir retorno por stake × odd. Estados ausentes limitam cobertura. |
| Ausência de relógio de revisão | API: contrato explícito de conflito/versão, ou campo autoritativo comprovado. Captura/ingestão não vira `source_updated_at`. |
| Ponte lossless, host final e versão | API #108/#115 + extensão #17: evoluir wire de modo coordenado, preservando bytes/decimais. `contrato: 2` usa payload estruturado/hash canônico e host da página; não é este envelope. Reserializar numbers JS não prova preservação. |
| Pacote financeiro completo | API prepara fora do Git; `pradyumna-001` revisa e atesta o digest específico conforme [contrato de leitores](backend-readers.md). O rascunho negativo da extensão não é corpus completo/aprovado. |
| Materialização e resolução de conta | API após as provas: adaptar ao modelo financeiro, conta pelo jogo/multicontas validada, acceptance PostgreSQL e replay sem duplicação/regressão. |
| Promoção de suporte/E2E | Registry somente após evidência real revisada e goldens; catálogo e #117 após perfil cliente/release e E2E contra origem HTTPS controlada aprovada. |

Não há alteração silenciosa de coleta-v2, fixture real nova no Git, aprovação
humana presumida ou fechamento automático de #115 por este trabalho.

## Verificação obrigatória

`tests/coleta/test_one_win.py` usa entradas transitórias de algoritmo, sem alegação
de captura real. Cobre fonte/schema exatos, enum, Decimal, identidade e alteração,
horário UTC, múltipla, ausência/invalidade, privacidade e isolamento.
`tests/coleta/one_win_quarantine_acceptance.py` exige PostgreSQL e 14 casos sem
skips: estados/replay, dados ausentes, v1, privacidade, RLS, concorrência e isolamento.
CI executa esses casos junto da acceptance herdada, sem alterar seus gates.
Fixture gate, lint/formato/tipagem, cobertura, segurança e checks gerais continuam
obrigatórios no HEAD final. Tests verdes comprovam esta fronteira de leitura e
retenção; não substituem as dependências financeiras acima.

## Latência da coleta na base main

A primeira execução de CI (`f279094`, run `37781374367`) falhou no preflight de
100 usuários: p95 da API 1462,46 ms e p99 2081,91 ms, com zero falhas HTTP.
Os limites continuam p95 < 1000 ms, p99 < 2000 ms e erros < 1%; o teste de 55
minutos permanece obrigatório. A interpretação candidata não é ativada nesse
fluxo. A falha revelou custo no caminho herdado da base main.

Esta branch inclui independentemente as otimizações de coleta também presentes
no #184; não importa seus commits, filtros, modelos ou migrations. SQL de forma
fixa passa a reutilizar construção/compilação com parâmetros por execução em
credencial, coleta, aposta e snapshot de conciliação. IDs/valores de usuários
não são cacheados. Formas dinâmicas e replay com timestamp explícito continuam
no caminho genérico com comparação contra escrita atrasada.

Autenticação valida o proprietário ativo, expiração e revogação no UPDATE que
registra uso e mantém o lock da credencial até o commit; rotação/revogação seguem
aguardando a admissão em curso. A rota reaproveita o escopo RLS já instalado pela
autenticação e o ID de aposta já consultado. Conciliação sem candidatos preserva
o flush e evita consultas de conjuntos vazios.

Regressões PostgreSQL verificam reutilização da mesma chave entre usuários com
valores distintos, atualização sem perder conta e snapshot sem alterar pares
ou revisões existentes. Verificação de desempenho é repetida no novo HEAD;
aprovação do #184 ou resultados antigos não substituem essa execução.
