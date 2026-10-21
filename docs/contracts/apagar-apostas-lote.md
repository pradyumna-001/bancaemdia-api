# Apagar todas as apostas em lote — contrato da issue #212

Destino: frontend #33. Implementação em [`src/bancaemdia/api/v1/apostas_lotes.py`](../../src/bancaemdia/api/v1/apostas_lotes.py). Não confundir com `DELETE /api/v1/usuario/me`, que encerra a conta.

| Operação | Acesso | Resultado |
| --- | --- | --- |
| `GET /api/v1/apostas/lotes/previa-apagar` | READ_ONLY | `total` de apostas ativas, `frase` a digitar (`APAGAR <total> APOSTAS`), `confirmacao` assinada e `expira_em` (10 minutos). |
| `POST /api/v1/apostas/lotes` + `Idempotency-Key` | FULL_WRITE + CSRF | 201 com o recibo: `lote_id`, `escopo`, `apostas_afetadas`, `criado_em`, `restaurado_em: null`. |
| `GET /api/v1/apostas/lotes/{lote_id}` | READ_ONLY, primário | Recibo atual, incluindo `restaurado_em`/`apostas_restauradas` depois da restauração. |
| `POST /api/v1/apostas/lotes/{lote_id}/restaurar` + `Idempotency-Key` | FULL_WRITE + CSRF | 200 com o recibo restaurado. |
| `GET /api/v1/apostas/lotes/intencao` + `Idempotency-Key` | READ_ONLY, primário | `operacao`, `status_code` e `resposta` originais; 404 `intencao_nao_encontrada`. |

## Regras

- **Confirmação do servidor.** O pedido leva `acao: apagar_todas`, a `confirmacao` da prévia e a `frase`; a comparação ignora espaços nas pontas e maiúsculas/minúsculas. A confirmação é assinada pelo servidor e presa ao usuário, ao total e à impressão digital do conjunto exato de apostas ativas. Uma aposta criada, apagada ou restaurada depois da prévia dá 409 `confirmacao_desatualizada`; prévia vencida dá 409 `confirmacao_expirada`; frase errada, 422 `frase_incorreta`; token alterado ou de outra pessoa, 422 `confirmacao_invalida`; nenhuma aposta ativa, 409 `nada_a_apagar`.
- **Mesma semântica do DELETE individual.** Cada aposta ganha o mesmo evento `APOSTA_CANCELADA` (motivo "apagada"), fica apagada e não é removida. Ela aparece com `incluir_apagadas=true` (com a `visibilidade=apagadas` da #184, quando integrada), e as revisões abertas dela saem da fila, exceto as de conta, consolidação e cruzamento, que dependem de decisão da pessoa. O replay reconstrói o mesmo estado sem conhecer o lote.
- **Atômico e síncrono.** Uma transação faz tudo ou nada, inclusive com milhares de apostas. Enquanto ela roda, nenhum outro escritor de apostas do usuário grava, e um segundo lote recebe 409 `lote_em_curso`.
- **Restauração exata.** Restaura, com o mesmo evento da restauração individual, só as apostas que este lote apagou e que continuam apagadas. A que foi apagada antes do lote, ou restaurada à mão depois dele, fica como está. Um lote restaura uma vez (409 `lote_restaurado`).
- **Escopo: apostas apenas.** Movimentos de caixa, contas, bancas, grupos, cadastro, titulares, assinatura e vínculos de Telegram e Coleta não mudam. Painel, análises e saldo, que somam resultados das apostas, deixam de contá-las e voltam exatamente ao estado anterior ao restaurar.
- **Idempotência.** A chave vale por usuário. O mesmo pedido devolve o recibo original; outro pedido com a mesma chave dá 409 `idempotency_key_conflict`. Recusas não ficam gravadas.
- **Validação.** 422 `{"detail": "lote_invalido", "campos": [{"campo", "codigo"}]}`, com `codigo` em `obrigatorio`, `tamanho`, `desconhecido` ou `invalido`. Sem acesso de escrita: 402 `account_read_only`, sem efeito.
