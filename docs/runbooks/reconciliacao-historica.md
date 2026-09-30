# Reconciliação histórica Casa / Telegram (#111)

A CLI inspeciona histórico persistido, publica um plano verificável e aplica somente decisões
exatas expressamente aprovadas. Casa conserva stake, odd, retorno e lifecycle; Telegram conserva
mensagem, mídia e contexto. Nenhuma fonte é apagada. A conta padrão segue **a data do jogo**;
a referência explícita válida de multicontas conserva a conta que realizou a aposta.

## Pré-requisito e integração

Este PR tem base direta em main, sem os commits do #166. A execução exige o serviço compartilhado
da #110, entregue no [PR #166](https://github.com/pradyumna-001/bancaemdia-api/pull/166), e suas
migrations. A referência testada é `db7f63eccd03635bac20235270bffa30351e41a9`.
Main isolada recusa a operação antes de conectar ao banco: não existe fallback para o pareador legado.
O administrador deve integrar o pré-requisito antes de habilitar esta operação.

A migration `r111journal2026` descende da main `a9d6e3f1c210`, adiciona apenas o journal operacional
e preserva todos os IDs publicados. Integrar também #166 cria dois heads (`c110fact2026` e
`r111journal2026`); o administrador deve publicar uma migration Alembic de merge dessas pontas
antes do upgrade no ambiente final. Não renumerar ou substituir revisões existentes.
O ensaio abaixo já executa essa convergência em montagem descartável, sem alterar as branches:

```bash
git fetch --no-tags origin db7f63eccd03635bac20235270bffa30351e41a9
python scripts/prepare_reconciliation_integration.py /tmp/reconciliation
pip install -e '/tmp/reconciliation[dev]'
cd /tmp/reconciliation
TEST_DATABASE_URL=postgresql+asyncpg://... pytest \
  tests/cli/reconciliation_acceptance.py tests/integration/cruzamento/ -n 0
```

O destino precisa ser novo. `reconciliation-heads.json` registra o HEAD atual da CLI, o SHA do
pré-requisito, os arquivos sobrepostos e o merge temporário. A CI obrigatória executa essa composição
em PostgreSQL 16 com papel sem bypass de RLS; não é prova de código integrado/deployado em main.
A suíte independente da main testa contrato, serialização e filtros; o job dependente invoca
explicitamente `reconciliation_acceptance.py`, recusa falta de banco e exige cenários sem skips.

## Preparar e revisar

Use o primary, credenciais operacionais restritas ao banco (sem superuser/BYPASSRLS), dados
sintéticos no ensaio e a configuração normal `DATABASE_URL`. A operação é local ao servidor,
sem HTTP, Telegram, IA, AWS ou custos externos. Não publicar os relatórios: IDs, sinais e totais
pertencem ao usuário. Arquivos novos recebem modo 0600 onde o sistema operacional o suporta;
no Windows, aplicar também ACL da pasta operacional. A CLI não sobrescreve arquivos existentes.

Antes de apply: fazer backup consistente pelo procedimento de backup do ambiente, registrar o
identificador/LSN, conferir sua restauração em banco descartável e preservar o relatório aprovado.
Os arquivos e o journal complementam o backup; não o substituem. Suspender coleta/materialização
e alterações manuais durante a operação para reduzir recusas por fontes obsoletas.

```bash
python scripts/reconciliar_casa_telegram.py --usuario-id 42 \
  --desde 2026-09-01 --ate 2026-09-30 --casa Betano \
  --algorithm-version casa-telegram/1 --batch-size 100 \
  --report review-42.json --csv review-42.csv
```

Sem `--apply`, a execução usa uma transação PostgreSQL **REPEATABLE READ READ ONLY**. Ela não
gera/persiste candidatos, revisões, checkpoints ou sequências: não é uma escrita seguida de rollback.
JSON é a autoridade; CSV é uma vista completa com metadados e proteção contra fórmulas de planilha.
Um CSV editado nunca é entrada de apply. O terminal mostra contagens e hash, sem payloads das fontes.

Os filtros de data selecionam a fonte Casa pela data do jogo; nenhuma data de postagem, captura ou
aposta substitui uma data de jogo ausente. Datas sem fuso usam America/Sao_Paulo. `--ate YYYY-MM-DD`
inclui todo o dia local; um limite com horário é exclusivo. Vizinhos/concorrentes continuam sendo
avaliados fora do intervalo filtrado, na janela de ocorrência do motor #109. Essa ocorrência é
evidência de matching e não muda o instante usado para atribuir conta.

O relatório contém versão de contrato/algoritmo, filtros literais, batch-size, timestamp UTC,
IDs de candidatos determinísticos por usuário/par/versão, scores, sinais iguais/conflitantes/ausentes,
classe, ação prevista, motivo, contagens e totais atuais/projetados de **todo o usuário por casa**.
Classes: exact, probable, incompatible, competing, already-consolidated, error. Um exact sem conta
única, com conflito de tipster ou anteriormente desvinculado permanece com ação review.
Competing inclui saturação: a busca incompleta nunca autoriza finanças automáticas.

O SHA-256 cobre o JSON canônico UTF-8 (chaves ordenadas, separadores `,`/`:`, Unicode literal,
sem NaN/Infinity), excluindo apenas a própria chave `sha256`. Inclusive o timestamp e os totais
são protegidos. A nova execução tem um novo timestamp/hash; conteúdo, ordenação e IDs permanecem
determinísticos na mesma fotografia. Aprovar o hash mostrado, não a localização do arquivo.

O high-water mark inclui máximos de IDs de eventos/apostas, contagens e digest do conteúdo integral
das entradas que podem alterar matching, finanças, conta ou revisão: apostas/eventos, contas/usos,
unidades, coletas Casa, relações, candidatos/snapshots, revisões e dicionários/casas/contexto. Não confia apenas
no maior ID, que não detectaria uma correção in-place. Mudanças de outro usuário em suas apostas
não contaminam o tenant; mudanças de catálogo compartilhado invalidam a aprovação conservadoramente.

A leitura é paginada em 1–200 apostas; cada histórico tem orçamento de 1.000 eventos e o plano
admite até 20.000 registros de origem por usuário. Ultrapassar orçamento falha explicitamente;
não selecionar um subconjunto silencioso para aparentar unicidade. Aumentar orçamento exige ensaio
de capacidade/revisão operacional, mantendo a janela completa e o veto de 200 vizinhos do motor.
Histórico ausente/inválido de fonte elegível gera error mesmo fora do filtro, pois essa fonte
poderia esconder concorrência; um relatório com errors não pode ser aplicado.

## Aprovar, aplicar e retomar

Conferir todas as ações e totais, principalmente os motivos de review e os valores que deixam
de ser contados como uma segunda aposta. Comparar o hash com o artefato preservado. Apply exige
o **mesmo JSON, hash, filtros literais, algoritmo e batch-size**:

```bash
python scripts/reconciliar_casa_telegram.py --usuario-id 42 \
  --desde 2026-09-01 --ate 2026-09-30 --casa Betano \
  --algorithm-version casa-telegram/1 --batch-size 100 \
  --report review-42.json --apply --sha256 HASH_APROVADO
```

Não há aceitação automática de relatório novo/alterado. Na primeira execução a CLI reavalia o plano
com o motor instalado e compara o objeto completo, além do watermark/hash: adulterar um plano e
recalcular seu hash não converte probable em autorização financeira. Somente ação consolidate
com classificação exact chama `domain.consolidacao_aposta.consolidate`, com candidato revalidado.
Probable/competing geram revisões pelo mesmo gerador; bloqueios de conta/contexto usam a revisão
do candidato. Incompatible/already-consolidated não fazem decisão financeira.

O índice derivado de matching é reconstruído com snapshots das fontes elegíveis do usuário,
incluindo vizinhos fora do filtro. Esse preparo é uma escrita exclusiva de apply e não consolida
pares fora do plano. A adjudicação compartilhada pode atualizar revisões de concorrentes fora
do filtro para manter a mesma proteção do fluxo online; fontes financeiras continuam preservadas.

Chunks seguem a ordem `(house_id, telegram_id)` do relatório, incluindo entradas sem ação.
O lock inicial é o mesmo advisory lock `pareador:{usuario}` do domínio; consolidate conserva
o caminho usuário → chaves ordenadas → IDs de apostas ordenados → conta. Durante cada chunk,
locks de tabela `SHARE ROW EXCLUSIVE NOWAIT` estabilizam entradas, catálogos e journal contra
escritores que não cooperam com o advisory lock. Isso requer permissões de lock/escrita nessas
tabelas e uma janela operacional; a execução pode recusar quando um escritor está ativo.
Não executá-la em papel superuser para contornar RLS/locks.

Cada commit grava fontes/decisões/revisões e uma linha append-only em `reconciliacao_chunks`
atomicamente: usuário/hash/índice, offsets, versão/filtros, hash do audit anterior, ações e IDs,
totais pré/pós e watermark pré/pós. O journal tem FK de usuário, PK por usuário/hash/chunk,
RLS forçada e trigger que recusa UPDATE/DELETE. Não faz parte da projeção de aposta/replay.

`--max-chunks 1` permite parar após um commit confirmado. Executar novamente o mesmo comando
retoma o offset registrado; retirar o limite permite terminar. Se o processo morrer após commit
e antes de imprimir, o journal continua sendo a autoridade. Retry concorrente do mesmo hash usa
o mesmo lock e não duplica relação, evento ou checkpoint. Um relatório já concluído retorna
`complete=true,idempotent=true` se as fontes continuam iguais ao último checkpoint.

Qualquer mudança externa desde o checkpoint recusa retomada. Não editar watermark/offset nem
apagar journal para forçar apply. Gerar/revisar um novo relatório sobre o estado atual; relações
já concluídas aparecem como already-consolidated. O trabalho confirmado permanece auditado.

## Integridade, falha e rollback

Antes/depois de cada chunk conferir por casa: número de registros de origem (bet_count), fatos
financeiros selecionados excluindo Telegram de relação ativa, stake, retorno, lucro e valor
não resolvido, todos em centavos. Retorno nulo soma zero em retorno, mas não inventa prejuízo:
lucro soma retorno−stake apenas quando retorno foi informado/materializado. Valor não resolvido
soma stake das fontes pendentes, sem retorno ou com revisão grave. Esses checks comparam a
projeção persistida e a dedução aprovada do Telegram; não substituem o Resumo/ROI da API.
Contagem de fontes nunca diminui. Review não deduz stake/retorno/lucro. Uma Casa liquidada de
10.000/20.000 deve permanecer um fato com lucro 10.000 após consolidar sua cópia Telegram.

Divergência anterior ou posterior aborta a transação corrente. Inclusive relações, auditorias
do domínio, snapshots, revisões e checkpoint desse chunk são revertidos juntos. Chunks anteriores
permanecem; mensagens de integridade identificam o próximo offset. Códigos de saída: 0 sucesso,
2 recusa segura (hash/escopo/versão/fonte/integridade), 3 falha operacional. Exceções de driver
não imprimem SQL, DSN, tokens ou payloads. Investigar a causa antes de retomar.

Para desfazer uma consolidação confirmada, usar a desvinculação **revisada** da #110, por usuário
autorizado e com motivo, preservando ambas as fontes e os eventos. Não deletar a relação ou o
journal, não criar movimentos compensatórios e não tentar reconsolidar automaticamente um par
rejeitado. Uma nova decisão exige nova evidência/revisão conforme o domínio compartilhado.

Rollback de aplicação pode parar a CLI e manter os checkpoints. A migration não admite downgrade
destrutivo se existe auditoria. Um downgrade vazio é permitido. Restaurar backup exige procedimento
do ambiente e janela de manutenção, preservando o audit fora do banco para investigação.

## Reprojeção e validação após execução

Executar o replay da #110 no primary em janela de manutenção conforme seu runbook. Ele reconstrói
relações pelos eventos de consolidação/desvinculação e mantém a autoridade financeira Casa;
não reexecuta o plano da CLI e não apaga seus checkpoints. Mudanças legítimas de replay podem
invalidar um watermark antigo: gerar relatório novo em vez de alterar o relatório aprovado.

Conferir novamente dry-run, uma relação ativa por ponta, integridade, eventos das duas fontes,
revisões abertas, caixa/extrato/listagens e painel após seu refresh documentado. Conferir o último
next_offset, ausência de gaps no journal e o hash do relatório. Não considerar a mensagem final
da CLI substituto dessa conferência. Reativar escritores depois da validação operacional.

## Matriz de evidências executadas pela CI

| Requisito | Implementação | Prova independente |
| --- | --- | --- |
| Zero writes e estabilidade | READ ONLY + avaliação pura; hash integral | fotografia do banco antes/depois; tentativa de escrita recusada pelo PG; oracle JSON |
| Conta pelo jogo / multicontas | account_for_state sem lock no plano; consolidate no apply | intervalos de contas trocando entre aposta/jogo; referência explícita histórica |
| Reviewed apply | hash + filtros + versão + watermark + recomputação | mudanças em linha/evento/conta/dicionário/candidato/competidor e plano rehashado recusadas |
| Exact e revisão | motor/gerador/serviço online compartilhados | 10.000/20.000/lucro 10.000; probable/competing/incompatible sem mutação financeira |
| Busca conservadora | concorrentes não filtrados e veto de saturação | adversário fora do intervalo; 201 vizinhos, zero exact |
| Retry/retomada/rollback | journal atomicamente append-only | interrupção por limite, falha após auditoria real, invariante pós-chunk adulterado, dez retries concorrentes |
| Tenant/audit/replay | RLS forçada, FK, trigger, eventos do domínio | leitura/insert cruzado e UPDATE/DELETE recusados; relação reconstruída pelos eventos |
| CLI utilizável | script + JSON/CSV + exit codes | subprocesso real de dry-run → apply → rerun idempotente |

A CI publica JUnit e SHAs exatos da composição e também executa a aceitação da #110 para conferir
o caminho online compartilhado. Nenhum teste dessa matriz depende de credenciais produtivas,
reconcilia usuários reais ou é substituído por mock de persistência.
