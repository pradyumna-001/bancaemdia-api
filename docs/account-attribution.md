# Atribuição de conta de aposta

Cada aposta mantém `conta_casa_id` no evento de criação e no `snapshot_replay`. A decisão é tomada pelo instante em que a aposta foi feita, consultando os intervalos de `usos_conta_casa` da mesma pessoa e casa. O início é inclusivo; o fim é exclusivo. A hora de recebimento ou processamento não participa da escolha. Uma releitura só recalcula a conta se corrigir a casa ou a data da aposta. O replay usa a decisão gravada para que mudanças posteriores nos intervalos não alterem apostas antigas.

Uma referência explícita `conta_casa_id` tem precedência depois de validar que a conta pertence à pessoa e à casa sob RLS. A criação manual aceita esse campo opcionalmente. O contrato `contrato: 1` de `POST /api/v1/coleta` reserva o mesmo campo opcional dentro de cada objeto de `apostas`; o coletor conserva o objeto bruto e o trabalhador valida a referência. Não se usa nome de login da casa para identificar a conta.

Sem referência explícita, uma única conta temporal é atribuída. Se não houver conta ou houver mais de uma candidata, `conta_casa_id` permanece nulo e `conta_atribuicao` na resposta da aposta é `UNASSIGNED`. Surge uma revisão pendente vinculada à chave da aposta. A aposta continua nos totais da pessoa. Corrigir a conta fecha a revisão de atribuição sem fechar revisões de extração.

Nas coletas de casas, a data financeira pode ser a do jogo. Os leitores que fornecem o horário de colocação guardam esse horário separadamente para a atribuição da conta. O leitor Betfair atual não fornece um horário de colocação verificado: suas apostas ficam sem conta atribuída automaticamente, salvo referência explícita, e entram em revisão.
