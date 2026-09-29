# Coleta v2 — contrato canônico da API

## Publicação e coexistência

- N = 2: POST /api/v1/coleta/batches.
- N-1 = 1: POST /coleta e POST /api/v1/coleta, com o envelope legado.
- GET /api/v1/coleta/contract publica versões, faixa de protocolo, hashes e depreciação.
- GET /api/v1/coleta/contract/schema publica o OpenAPI canônico; os mesmos bytes
  são reproduzidos em openapi/extension-collection-v2.json pela função de geração.
- openapi/extension-collection-release.json inclui SHA-256 do artefato e das
  definições, protocolos 1–2 e deprecation_date=null: nenhuma retirada anunciada.
- Mudanças incompatíveis exigem major novo, N/N-1 coexistentes, registro de
  compatibilidade, anúncio com pelo menos 90 dias e plano de rollback. A janela
  é uma política técnica deste contrato, não uma obrigação legal.

A CI regenera e compara os artefatos; os testes confrontam envelopes válidos e
inválidos com o JSON Schema publicado e os modelos do runtime. O snapshot geral
e o oasdiff continuam exigidos. O payload da casa é extensível; envelopes e
metadados são fechados. Versões desconhecidas e campos de identidade financeira
inventados pelo cliente são recusados.

## Sessões explícitas

Todas as operações de sessão/lote/job usam X-Coleta-Token, HTTPS e o primário.
POST /api/v1/coleta/sessions recebe coletar_desde ISO 8601 com fuso e, opcionalmente,
retomar_sessao_id. Sem UUID, abre uma sessão somente quando a instalação não tem
outra aberta. Com UUID, retoma a mesma sessão aberta e exige o mesmo corte.
GET /api/v1/coleta/sessions/{sessao_id} consulta o corte, e DELETE encerra a sessão.
A identidade e o corte também são imutáveis por trigger no PostgreSQL.

Outra instalação, mesmo do mesmo usuário, não pode usar ou consultar essa sessão.
Sessões encerradas não recebem itens novos; reenvios de eventos já recebidos
mantêm o ACK. Encerrar/revogar não apaga trabalho anteriormente aceito. Uma
instalação reconectada mantém seu ID e pode retomar explicitamente o UUID aberto.
Uma sessão nova nunca amplia implicitamente o corte da anterior.

## Envelope e segurança

CollectionBatch contém contrato=2, batch_id UUID, sessao_id UUID e 1–100 items.
Cada item contém client_event_id UUID estável, hostname exato, observado,
capturado_em com fuso, payload JSON sanitizado, content_hash SHA-256 e
conta_casa_ref opcional. O ACK devolve o ID e o hash do item a que se refere.

observado exige source=observed_response, transport=fetch/xhr, method=GET/POST,
path absoluto sem query/fragmento/espaços, status=200, content_type=application/json,
adapter_version e sanitization_version=1. Isso declara uma observação, jamais
autoriza uma ação automatizada na casa. A API não executa requisições à casa.

O teto é 1 MiB por corpo (também para streams sem Content-Length) e 128 KiB de
payload por item, com profundidade limitada. Chaves de sessão/credenciais, headers,
cookies, tokens, senhas, identificadores pessoais conhecidos e valores com
formato de token/JWT são recusados, não silenciosamente removidos. O produtor deve
sanitizar antes do envio. A validação de tamanho e credenciais complementa essa
obrigação; não identifica semanticamente todo dado arbitrário que uma casa possa
inventar em um campo desconhecido.

Somente envelopes seguros são persistidos. Rejeições guardam identidade/hash do
pedido e motivo estável, sem corpo. Nenhum payload é enviado ao broker ou incluído
em logs de processamento. Métricas usam somente contrato e resultado enumerado.

content_hash usa UTF-8 de JSON com chaves ordenadas, caracteres Unicode literais,
separadores vírgula/dois-pontos sem espaços e números JSON finitos (a implementação
de referência é domain/coleta_provenance.py). Não é hash do texto HTTP nem uma
afirmação de canonicalização RFC 8785. Clientes devem usar os vetores publicados
e o mesmo formato; a API verifica antes de aceitar. O payload sanitizado é
preservado como valores JSON, incluindo campos desconhecidos seguros, para replay.
A ordenação de chaves e a formatação do JSON HTTP não são parte do conteúdo.

## Recebimento, retries e processamento

O ACK por item tem ack=accepted/duplicate/rejected, reason, retryable e job_id.
O servidor responde somente após o commit da inbox e do ACK.

- accepted / durably_received: captura persistida e aguardando processamento.
- duplicate / content_already_received: conteúdo idêntico já admitido na mesma
  sessão/host/conta. Não afirma que o processamento anterior terminou.
- rejected: motivo permanente, como unsafe_payload, unsupported_exact_host,
  content_hash_mismatch, item_too_large ou session_closed.
- Mesmo (instalação, client_event_id) e mesmo envelope/sessão: ACK original,
  inclusive depois de processar, fechar sessão ou perder a resposta do servidor.
- Reutilizar esse ID com outro conteúdo ou sessão: event_id_conflict, sem alterar
  a entrega original. batch_id pode mudar em retry parcial; a identidade é por item.

Cada novo item admitido tem job próprio. GET /api/v1/coleta/jobs/{job_id} retorna
pending ou terminal: materialized, updated, duplicate, ignored_before_boundary,
needs_review ou failed, com reason estável e aposta_chave quando aplicável.
O ACK de submissão é imutável; o status do job evolui separadamente.

Um envelope estruturalmente inválido recebe 422 sem ACK; 401 indica credencial
inválida, 429 inclui Retry-After, 413 indica excesso de corpo e 5xx permite repetir
IDs idênticos. Limites por credencial reutilizam COLETA_RATE_LIMIT e o backend de
rate limit existente. Itens semanticamente recusados não derrubam seus vizinhos.
retryable=false no ACK significa que o recebimento está decidido; pending é
responsabilidade do servidor, não pedido para reenviar conteúdo novo.

O worker busca uma inbox PostgreSQL durável, com lock por entrega e por bilhete;
eventos, aposta, metadados de versão e estado terminal são commitados juntos.
Morte antes do commit devolve o trabalho ao próximo poll. Beat a cada 10 segundos
recupera inclusive entregas cujo aviso opcional ao broker se perdeu. Falhas de
banco não consomem a entrega. Falhas inesperadas repetidas terminam em failed
após três tentativas, com diagnóstico seguro; dados inválidos seguem para revisão.

## Fronteira temporal e identidade financeira

capturado_em, batch_id e ordem de chegada são diagnósticos. O corte usa a criação/
colocação do bilhete da fonte; horário de jogo não transforma bilhete antigo em novo.
Sem relógio de origem confiável, o resultado é needs_review; nunca se usa o
horário da captura como substituto.

Betano usa placedAt e settledAt; outros leitores conhecidos têm campos de
colocação mapeados explicitamente. Ausência de campo medido (incluindo formatos
sem hora de colocação conhecida) ou leitor indisponível resulta em revisão.
Mudanças de lifecycle sem uma revisão de origem comprovadamente posterior
também exigem revisão. Suporte a envelopes não é declaração de cobertura completa
de leitores nem autenticação da veracidade dos dados da casa.

A identidade de negócio vem do parser: (usuário, casa, identidade do bilhete).
Mesmo conteúdo canônico é no-op; revisão antiga não regride o estado. Conteúdo
diferente com o mesmo relógio de origem exige revisão. Reabrir uma aposta liquidada
exige revisão explícita. Uma tarefa v1 atrasada também respeita a versão v2
persistida e não desfaz sua liquidação.

conta_casa_ref é validada por usuário, casa e intervalo [desde, ate) no horário de
colocação. Uma conta histórica inativa só é válida dentro de um intervalo encerrado.
Ausência de referência não escolhe a primeira conta ativa: needs_review. Referência
alheia, inválida, fora do intervalo ou conflitante com uma aposta existente não
atribui dinheiro. Esta entrega não implementa gestão de titulares nem habilita
concorrência de contas como feature de produto.

## Matriz de compatibilidade

| Caso | v1 | v2 |
| --- | --- | --- |
| Instalação/HTTPS | Credencial #107 | Mesma identidade |
| Envelope | contrato=1, casa, apostas | Sessão e eventos UUID |
| Recibo | Contagens legadas de ingestão | ACK durável por item + job |
| Corpo sensível | Rejeitado antes de persistir | Rejeitado antes de persistir |
| Horário/conta | Sem ampliar garantias históricas | Corte e conta explícitos |
| Retorno a cliente v1 | Rotas mantidas | Revisão v2 impede regressão por tarefa antiga |
