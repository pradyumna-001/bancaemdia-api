# Consolidação Casa × Telegram

Uma relação ativa associa uma fonte Casa a uma fonte Telegram/print do mesmo usuário.
Casa fornece stake, odd, resultado, retorno e identidade da casa; Telegram preserva
mensagem, mídia, seleções extraídas e contexto de tipster. Nenhum registro bruto ou
evento é apagado. `selecionada` continua sendo a escolha de inclusão/exclusão do usuário.
Uma fonte contextual selecionada permanece consultável, mas não soma dinheiro.

## Conta: regra confirmada pelo usuário em 29/09/2026

O padrão usa **a data do jogo**, `data_jogo`/`comeca_em`. Horário de aposta,
publicação, captura e ingestão não substituem o jogo ausente. Os intervalos são
`[desde, ate)`; contas históricas fechadas podem ser válidas na data do jogo.
Exatamente uma conta deve corresponder ao usuário/casa e ao intervalo. Ausência,
data ausente ou sobreposição deixam a aposta UNASSIGNED e abrem revisão; a aposta
continua nos totais gerais. Consolidação automática exige conta resolvida.

No multicontas, `conta_casa_ref` identifica a conta que efetivamente fez a aposta.
A referência explícita validada tem precedência, mesmo quando outra conta seria
o padrão na data do jogo. ID inválido ou de outro usuário/casa não recebe fallback.
Correção manual de `conta_casa_id` é uma decisão explícita auditada. O mesmo
resolver serve materialização Casa/Telegram, CRUD, importação e replay. A tabela
de usos publicada na #94, quando presente, substitui os intervalos legados, sem
alterar esta regra. A ocorrência real continua relevante para matching e cortes
de coleta; essas operações não são atribuição de conta.

## Transação e decisões

O chamador controla commit/rollback. Ordem global: advisory `pareador:usuario`,
advisories de chave ordenados, apostas por ID ascendente, conta/uso por ID.
Atualização de aposta adquire o advisory do usuário antes dos locks de linha,
compatível com a invalidação de matching da migration c109match2026.

`consolidate` relê eventos e valores persistidos, ambas as direções do matching,
concorrentes, versão, saturação, seleção, conta, tenant e vínculos existentes.
Exact é elegibilidade transitória. Evidência financeira inclui os snapshots das
fontes realmente usados e fica imutável na relação. Não se inventa ticket para
formatos legados sem identidade: permanecem prováveis e exigem decisão revisada.
Índices únicos parciais garantem uma relação ativa por cada ponta; FKs compostas
impedem referências entre usuários. RLS é forçada na tabela.

Retries do mesmo par ativo retornam a decisão existente sem repetir auditoria.
Prováveis, ambiguidades, contas pendentes e conflitos de tipster ficam em revisão.
Escolha de tipster conflitante é explícita (`casa` ou `telegram`); ambos os valores
originais ficam no contexto/evidência. Referências de mídia continuam usando os
caminhos privados existentes, autenticados por usuário.

## Leituras e correções

Listagens financeiras, caixa e extrato excluem Telegram de relação ativa por um
anti-join; `public.apostas_financeiras` aplica a mesma regra no painel. Contagens
de registros por origem permanecem contagens de fontes. Dimensões de tipster
usam o contexto escolhido na projeção Casa; não há join que multiplique fatos.
Depósitos, saques, bônus, transferências, freebet e UNASSIGNED mantêm suas fórmulas.
Liquidação e correções Casa atualizam o mesmo fato; releitura Telegram não altera
seus valores. Exclusão/restauração de qualquer fonte não desfaz a relação.

O painel usa materialized views com refresh explícito e respostas HTTP `private, no-store`. Não há cache financeiro Redis neste caminho.
Commit não promete refresh instantâneo: o painel informa seu frescor; caixa,
extrato e listagens leem a projeção transacional atual. A migration recompõe as
views e preserva os grants do resumo. Não há movimento compensatório artificial.

POST `/api/v1/consolidacoes` registra decisão revisada do usuário autenticado.
POST `/api/v1/consolidacoes/{relacao_id}/desvincular` exige motivo, encerra a
relação ativa e preserva histórico/fontes. As fontes selecionadas passam a somar
separadamente; uma nova vinculação desse par exige decisão revisada. O detalhe
da aposta expõe histórico de relações e `fonte_contextual`, separado de `apagada`.

## Auditoria, replay e migração

`APOSTAS_CONSOLIDADAS` e `CONSOLIDACAO_DESVINCULADA` são append-only nas duas
chaves. Payload contrato 1 guarda ID, usuário, pontas, conta, decisão, versão,
evidência, contexto, ator e timestamps. Replay recompõe relações a partir desses
eventos, sem candidatos atuais ou memória de worker, e detecta divergência da
evidência imutável. Dry-run é somente leitura.

Migration c110fact2026 mantém as revisões publicadas. Pares manuais recíprocos
legados são adotados como `legacy/manual`, com flags originais e eventos próprios;
não recebem evidência automática inventada. Parceiras não recíprocas interrompem
upgrade para reconciliação explícita. Desvinculação legada retira parceiras e
restaura a fonte suprimida, com evento correspondente.

Downgrade com decisões/eventos é recusado para preservar auditoria. Rollback de
aplicação deve conservar schema/relações e a leitura financeira canônica; voltar
a uma versão que desconhece relações exige primeiro decisão revisada de todas
as relações e adaptação das leituras, não apagar tabelas/eventos. Banco vazio
permite downgrade e novo upgrade. Métrica `aposta_consolidation_total` usa somente
decisão e resultado de baixa cardinalidade; a auditoria persistida comprova os
commits, enquanto o contador mede tentativas do serviço, inclusive rollback.

A #111 poderá chamar o serviço compartilhado dentro de transação e usar as mesmas
revalidações. Nenhuma CLI histórica/backfill produtivo é implementada aqui.
