# Hipotética histórica — frontend #21 / #50

Prioridade emergencial solicitada pelo proprietário em 11/10/2026. Backend
[#190](https://github.com/pradyumna-001/bancaemdia-api/issues/190) desbloqueia o
contrato de [frontend #21](https://github.com/pradyumna-001/bancaemdia-frontend/issues/21),
parcela de [#50](https://github.com/pradyumna-001/bancaemdia-frontend/issues/50).
Pergunta: **“E se eu tivesse seguido exatamente a stake indicada pelo tipster?”**

Esta candidata nasce diretamente de main
`b916f54331f14cf47a3800324bd61d8638043c06`. Incorpora a dependência funcional
[#184](https://github.com/pradyumna-001/bancaemdia-api/pull/184), SHA
`9b42a3579518a84d348d272afbaeff88f9119825`, sem merge ou PR empilhada.
[#189](https://github.com/pradyumna-001/bancaemdia-api/pull/189), SHA
`22d5b77f946c58dda4f0eeb64db201ee5c63c009`, foi auditada: só a limpeza da fixture
compartilhada, as anotações estritas dos testes herdados, o hook mypy com dependências
instaladas e os reparos portáveis do runner k6 são reutilizados. Nenhuma asserção,
threshold ou skip é relaxado. Seu diário/grupos/cursors não são dependências desta
rota. Não inclui #187 ou suporte USDT #185. Após integrar essas PRs, reconciliar
contratos e moedas explicitamente; nunca somar moedas ou compor schemas.

## HTTP, parâmetros e segurança

`GET /api/v1/painel/hipotetica`, autenticado por sessão HttpOnly ou Bearer vigente.
Operação comercial **hipotetica_historica**, registrada explicitamente em
`domain/access.py`: permitida em FULL_WRITE e READ_ONLY, independente do verbo
HTTP. Usuário precisa estar ativo; sessão não concede assinatura. Não há escrita,
gasto de IA, estimativa de custo ou fluxo de autorização de gasto.

Usa os [filtros canônicos](site-filters.md): `periodo`, `desde`, `ate`,
`casa_id`, `tipster_id`, `mercado_id`, `competicao_id`, `titular_id`,
`conta_casa_id`, `grupo_id`, `banca_id`, `estado`, `origem`, `revisao_grave`,
`visibilidade`, `incluir_apagadas`. Interseção de todos os parâmetros.
`fresh=true` escolhe primário; padrão segue replica/recent-write e
`X-Read-Replica:false`. Snapshot REPEATABLE READ compartilhado com autenticação,
seleção, links, evidência e timestamp da transação. Não lê/refresca MVs.

Padrão: todo o histórico ativo, sem período implícito. Períodos nomeados seguem
o calendário civil de America/Sao_Paulo; `desde` inclusivo, `ate` exclusivo,
ISO com offset (legado sem offset interpretado UTC). Datas ausentes só entram
sem limites. Período não combina com limites. Critério de seleção é
**data_aposta**, como a lista e o Painel filtrado; isso não muda a atribuição de
conta pelo dia do jogo, nem a exceção multicontas que usa a conta executora.
Resposta inclui filtro normalizado com IDs strings, limites, período, fuso,
fonte e instantes de snapshot/resposta. Não interpretar respondido_em como
refresh, nem prometer que réplica está instantaneamente atualizada.

Máximo **10.000 registros da população**. Busca 10.001 e rejeita o excedente
inteiro com 422; nunca trunca ou calcula sobre uma página. `page`, `page_size`,
stake livre, percentuais, ordenação, moeda e parâmetros desconhecidos/repetidos
são rejeitados. Estado/origem precisam existir no vocabulário. SQL usa binds,
índices por usuário/chave, timeouts vigentes e limite por usuário do middleware.

IDs privados (conta, titular, banca, grupo) precisam pertencer ao usuário;
inexistente/estrangeiro dão o mesmo 404 `filtro_nao_encontrado`. IDs de casa,
tipster, mercado e competição são catálogos públicos compartilhados e precisam
existir. RLS e predicados de dono protegem apostas, eventos e as duas pontas do
vínculo; FKs compostas impedem vínculo entre usuários. Nenhuma conta/titular é
inferida pelo nome da casa. Filtra a conta materializada, incluindo histórico;
grupo é vínculo explícito por aposta, sem multiplicar fatos por participação.

Todos os resultados e erros são `private, no-store`, Vary Authorization/Cookie.
401 identidade inválida; 404 filtro privado inacessível ou ID inexistente;
422 validação/conflito/população excedida; 429 limite vigente, respeitar Retry-After;
503 dependência/timeout, nunca equivalente a vazio; 500 inesperado sem detalhe
privado. Modelos de erro oficiais no OpenAPI. Não há 402 por READ_ONLY nesta operação.

## População e evidência

A seleção é a mesma da lista/Painel **filtrado**, antes de paginação.
Ativas = selecionadas e financeiras canônicas. Apagadas/todas preservam fontes
para auditoria: nesses recortes explicitamente pedidos, as apostas reais
apagadas podem contribuir como no Painel filtrado. Isso não restaura apostas
no resultado principal. Dica com consolidação active nunca contribui dinheiro,
mesmo visível em todas/apagadas; `fora_do_financeiro` informa a contagem.
Pendentes, anuladas e revisão grave não entram nas métricas liquidadas.
Duplicidade diagnosticada não é deduplicação fuzzy: usa os fatos materializados
e selecionados existentes, sem criar nova decisão. Revisão grave exclui dinheiro.
Registros sem tipster/conta/grupo/banca continuam no recorte geral, sem atribuição
presumida; filtro dessas dimensões exige sua referência explícita.

Só `ApostaConsolidacao.estado=active`, com casa e dica Telegram/bot/imagem do mesmo usuário,
é evidência confirmada. `unlinked`, `rejected`, parceira_chave solta e candidatos
não confirmados não mudam a aposta. Dúvida não cria par; registra limitação.
Cada casa pode ter uma ponta active, e cada dica uma casa active (índices únicos).
Evidência ausente/mais de uma ponta é defensivamente indisponível.

Busca a dica separadamente, sem aplicar os filtros de origem, data, visibilidade
ou estado do cenário real à evidência. Ex.: `origem=casa` em setembro ainda usa
a dica Telegram vinculada de outro dia, mesmo não selecionada. Usa os centavos
materializados **atuais** da dica vinculada; não inventa valor em unidades,
stake histórica ausente ou câmbio. A odd e o estado da dica são ignorados:
continuam valendo a odd e o resultado **reais da casa**.

## Dinheiro, estados e limites financeiros

Regra `stake_dica_confirmada_odd_resultado_reais_v1`. Núcleo compartilhado de
`financeiro.retorno_liquidado/lucro_exato` e `painel.MetricasPainel`/ROI.
Dinheiro e diferenças são strings decimais inteiras em centavos de BRL;
agregados Python não limitados a BIGINT individual. Nada usa float para a
comparação. Odd física legada é lida como texto SQL e convertida em Decimal,
sem reinterpretar os bits como precisão histórica que não foi armazenada.
Odd finita de 1 até 1.000.000; aritmética de liquidação Decimal com 80 dígitos.
**ROUND_HALF_EVEN por etapa**: primeiro produto stake×odd para centavos, depois
metade quando cabível. ROI e win_rate são frações string com seis casas via
formatação canônica; ROI sem base é null. Diferenças = hipotético menos real.

| Estado | Regra padrão de retorno | Participação |
|---|---|---|
| GREEN | produto arredondado; freebet subtrai face | giro/retorno/lucro liquidados |
| RED | 0 | custo vira prejuízo; freebet custo 0 |
| MEIO_GREEN | metade arredondada do produto + stake devolvida; freebet não devolve face | liquidados |
| MEIO_RED | metade arredondada da stake; freebet 0 | liquidados |
| ANULADA | devolução da stake; freebet 0 | fora do giro/lucro, mantida na população |
| PENDENTE | desconhecido, nunca 0 | exposição em campos próprios, não lucro |
| CASHOUT | valor factual negociado | stake diferente: indisponível, sem proporcionalidade |

Para stake diferente só aplica cálculo seguro quando há evento de criação,
sem comissão não zero/malformada, bônus/taxa/retorno fixo sinalizados ou retorno
informado manualmente/planilha. Eventos são consultados apenas como flags e
última comissão, não payloads privados na resposta. A liquidação real precisa
coincidir exatamente com a regra padrão na stake original: discrepância, valor
desconhecido, odd ausente/inválida, stake real zero ou dica sem stake positiva
impedem o cálculo. Stake da dica freebet também é insuficiente para sugerir gasto.
Freebet real é suportada quando padrão comprovado: muda **face**, custo financeiro
permanece 0; face entra no denominador ROI. Não escala prêmio fixo, comissão ou bônus.
É hipótese de mesma regra de liquidação histórica, não promessa de execução/limites
da casa. A regra só reconhece os campos representados no domínio atual.

Sem vínculo/equal stake mantém integralmente valores reais, incluindo cashout
e freebet; nenhuma divisão é feita. Fora do financeiro/revisão grave fica
inalterado e contado, com limitação se teria stake diferente. Se uma mudança
financeira não puder ser calculada, métricas totais hipotéticas e diferenças
ficam null; não apresenta soma aparentemente completa de uma parte do recorte.
Pendentes têm custo/face expostos separadamente; só pendentes ⇒ retorno/lucro/ROI
null, jamais lucro 0 de aposta supostamente resolvida. Mistura liquidada/pendente
mantém métricas explicitamente liquidadas e sinaliza quantidade pendente. Retorno
liquidado desconhecido invalida retorno/lucro/ROI, conservando giro real conhecido.

`estado`: sem_apostas, sem_vinculos_elegiveis, sem_stakes_diferentes, comparado,
parcial (mudanças seguras e casos indisponíveis) ou indisponivel. Esses estados
descrevem a comparação; `limitacoes`/contagens descrevem conhecimento financeiro.
`apostas_alteradas` conta mudança segura da stake/face em memória, inclusive
exposição pendente; não implica que toda aposta já foi liquidada.

## Exemplos, contrato e integração

[Exemplos completos](examples/hipotetica.json): comparado (16000/32000 →
2000/4000, lucro 2000), vazio, cashout indisponível e erros. Frontend formata
centavos, exibe rótulos real/hipotético e diferenças recebidas, não calcula.
Visão separada fora da capa principal; Excel real e Caixa não são alterados.

Documento oficial completo: `tests/contract/schemas/openapi.json`, gerado por
`python scripts/generate_openapi.py` da aplicação deste commit, não união manual
de documentos #184/#189. O SHA exato e SHA-256 do blob estão na PR. Gerar todos
os tipos desse único arquivo e piná-lo. Contrato candidato em PR não significa
merge, deploy ou recurso integrado. Merge reservado ao administrador.

ADRs 002/003 (modelo), 005/009 (isolamento/consistência), 019/025 (acesso comercial);
frontend ADR-019. Regressão lida integralmente no monólito:
`planilhador/dominio/hipotetica.py`, `TestAVisaoHipotetica`, rota `/e-se`.
Intenção e exemplo preservados; sem floats nem alterações nos fatos reais.

Aceite dedicado real API/PostgreSQL/RLS, sem skips, exigido pela CI e checker;
testes incluem >100 itens, arredondamento, sete estados, freebet, filtros completos,
datas, RLS/FKs, READ_ONLY, imutabilidade de fatos/eventos/Caixa e igualdade de
Painel/Excel antes/depois. Gates existentes permanecem exigidos no HEAD final.
