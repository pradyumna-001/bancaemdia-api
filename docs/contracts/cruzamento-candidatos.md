# Candidatos Casa × Telegram — casa-telegram/1

O worker de materialização registra e compara capturas da casa e apostas Telegram/
print do mesmo usuário. Persiste o resultado em `cruzamento_candidatos`, sem mudar
seleção, stake, retorno, saldo, titular ou eventos financeiros. `exact` significa
elegibilidade para a consolidação da #110; não significa que ela aconteceu.
O antigo pareador automático não é mais chamado. A confirmação manual existente
continua explícita na revisão e invalida os candidatos envolvidos.

## Critérios versionados

O arquivo `domain/cruzamento.py` fixa a versão, pesos e limites. Mudança em qualquer
regra exige revisão e nova versão, com os mesmos fixtures reproduzidos. Esta é a
proposta conservadora da #109 para revisão do PR, não autorização de lançamento de
consolidação automática. Não há tuning mutável por variável de ambiente.

| Sinal | Pontos / tratamento |
| --- | --- |
| Mesmo usuário e casa canônica | Obrigatórios; casa soma 10 |
| Identidade do bilhete em ambas as fontes | 50; diferença é incompatível |
| Stake em centavos | 15; diferença é incompatível, zero/ausência bloqueia exact |
| Moeda | Obrigatória para exact; divergência incompatível |
| Ocorrência da fonte até 300 segundos | 15; até 14 dias permite revisão; além é incompatível |
| Odd total | 10; diferença absoluta até 0,005 com Decimal; maior é incompatível |
| Evento/participantes | 20; ordem casa/fora preservada |
| Mercado / seleção | 10 cada; divergência sem identidade comum é incompatível |
| Estrutura / pernas completas | 5 cada; máximo 32, ordenação determinística das pernas |
| Competição, esporte, linha e lifecycle | Conflitos bloqueiam exact, mesmo sem peso adicional |

A pontuação é limitada a 100. `probable` exige pelo menos 60 e nenhuma
incompatibilidade decisiva. `exact` exige pelo menos 90, identidade comum, todos
os campos decisivos presentes, estrutura completa, nenhuma divergência, ausência
de revisão grave e elegibilidade um-para-um nas duas pontas. Pontuação alta
sozinha não comprova identidade. Open → settled/cashout/cancelled é compatível;
resultados finais diferentes exigem revisão. Não se infere que são bilhetes
diferentes somente porque um liquidou depois do outro.

Somente o vocabulário de casas existente e nomes/aliases confirmados das tabelas
canônicas normalizam textos. Nomes desconhecidos permanecem literais. Não há
fuzzy match, tradução de mercado ou equivalência inferida entre linhas. A evidência
guarda valores originais, valores normalizados, mapa de dicionário usado,
configuração, pontuação, sinais e explicação. Mesmas entradas normalizadas e versão
produzem exatamente o mesmo veredicto; elegibilidade considera também concorrentes.

O relógio usado é `ocorrido_em` quando a fonte o fornece ou `comeca_em` do evento
lido no bilhete. Nunca usa `criada_em`, captura HTTP ou horário de ingestão como
substituto. Parsers legados produzem horário local brasileiro sem offset; sua
convenção existente é aplicada explicitamente. Sem relógio de fonte, não busca
par automaticamente. Dados financeiros usam a projeção da API. BRL é a moeda
do domínio financeiro atual; isso não implementa suporte multimoeda.

Identidade ausente no OCR/extração legado permanece ausente: não se inventa um
ticket com chat/message ID. Essa captura pode gerar probable, jamais exact. As
novas criações preservam as pernas lidas; históricos que não possuíam pernas
continuam conservadores. O backfill não inventa dados apagados pela extração antiga.

## Persistência, revisão e concorrência

`cruzamento_entradas` é uma projeção reconstruível por aposta, com índice
`(usuario_id, casa, origem, ocorrido_em, aposta_id)`. `cruzamento_candidatos` guarda
as duas referências de aposta, versão, pontuação, sinais, snapshots, revisão e
timestamps. Unicidade por par/versão torna reexecuções idempotentes. A evidência
representa a última avaliação daquele par/versão; não é log de todas as execuções.

Um lock transacional por usuário serializa geração e confirmação. Concorrentes
nas duas direções retiram elegibilidade exact. `probable` cria/atualiza uma única
`RevisaoPendente`, com chaves para a revisão manual existente; não modifica o fato
financeiro. `incompatible` é diagnóstico persistido, sem revisão/par visível.
APIs de edição também invalidam snapshots/candidatos por trigger, impedindo que
uma decisão exact sobreviva à correção dos dados. Apostas apagadas, duplicadas ou
pareadas são excluídas. FK composta e RLS impedem referências cruzadas entre tenants.

`iguais_a_existentes` e `em_duvida` da coleta passam a contar apostas do envio que
possuem candidatos exact/probable persistidos. Como o processamento é assíncrono,
o primeiro envio pode retornar zero; uma consulta por reenvio posterior observa
os candidatos materializados. Nenhuma dessas contagens declara consolidação.

## Orçamento e operação

Cada busca usa mesma casa/tenant e uma janela de ±14 dias, pede no máximo 201
vizinhos e avalia 200. O registro adicional detecta saturação: uma busca incompleta
nunca produz exact. A saturação também retira exact de pares existentes ligados
a vizinhos ocultos além do limite, em páginas de até 200 pares na mesma janela
indexada. O marcador persistido veta reativação por reprocessamento da outra ponta;
uma busca completa posterior permite reavaliar esse veto. A busca não percorre
todo o histórico do usuário. O teste
PostgreSQL com 100 mil entradas exige uso de `ix_cruzamento_busca`, retorno de no
máximo 201 registros e execução SQL abaixo de 250 ms; o tempo medido é registrado
no JUnit. Esse orçamento é da busca, não uma promessa de latência HTTP para todo
o processamento e criação de revisões. Métrica `cruzamento_candidates_total`
usa apenas versão e classificação; mede avaliações tentadas, inclusive retries.

Aplicar migration `c109match2026` após `a9d6e3f1c210`, com papel administrativo.
API/worker usam papel restrito, grants usuais e RLS. A migration não reescreve
eventos financeiros. Para o histórico, executar páginas por tenant com a URL de
banco restrita configurada fora do comando:

```sh
python scripts/rebuild_matching.py --usuario 123 --after 0 --limit 100
```

Repetir usando o cursor devolvido até ele não avançar. Limite máximo de 200 apostas
por página e 1.000 eventos por aposta; exceder o orçamento aborta a página e exige
revisão explícita do histórico. O comando e o worker usam o mesmo motor; só
reconstroem snapshots/candidatos/revisões. Executar o backfill antes de oferecer
consolidação #110 sobre históricos antigos. Não é necessário acessar produção
para comprovar esse procedimento: há teste real de paginação e idempotência.

O downgrade preserva a evidência: recusa-se enquanto houver candidatos. Preferir
rollback de aplicação mantendo tabelas, sem reativar o pareador antigo que alterava
totais. Um ensaio de downgrade vazio/upgrade e recusa com dados roda em banco
descartável separado, nunca no banco compartilhado dos outros testes.

Esta entrega parte de main. Na futura convergência com #163/#164, preservar a
admissão/autenticação e o corte v2, a chamada de geração após materialização, e
conciliar as novas pontas de Alembic sem reescrever revisões publicadas. A #110
deverá revalidar sob lock fontes, elegibilidade e versão antes de consolidar.
Não foi implementada nem autorizada consolidação automática nesta issue.
