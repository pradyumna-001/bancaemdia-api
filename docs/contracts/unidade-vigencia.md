# Valor da unidade com vigência — contrato da issue #210

Destino: frontend #33/#29. Implementação em [`src/bancaemdia/api/v1/unidade.py`](../../src/bancaemdia/api/v1/unidade.py).

| Operação | Acesso | Resultado |
| --- | --- | --- |
| `GET /api/v1/unidade` | READ_ONLY ou FULL_WRITE | `vigente` agora e `historico` (mais recente primeiro), `fuso_horario`, `padrao_centavos` (10.000) e `limite_centavos` (10.000.000). |
| `POST /api/v1/unidade/vigencias` + `Idempotency-Key` | FULL_WRITE, CSRF no cookie | 201 com o recibo: `vigencia`, `anterior`, `vale_a_partir_de`, `apostas_existentes_alteradas` (sempre 0), `fuso_horario`. |
| `GET /api/v1/unidade/vigencias/intencao` + `Idempotency-Key` | READ_ONLY ou FULL_WRITE, primário | 200 com o recibo original; 404 `intencao_nao_encontrada`. |

Cada vigência traz `id`, `valor_centavos` (inteiro exato), `origem` (`padrao` quando não há vigência do usuário, `usuario` caso contrário), `vigente_de`/`vigente_ate` em UTC e `vigente_de_local` no fuso do usuário.

## Regras

- O servidor escolhe o início: o instante da troca, lido depois da trava por usuário. A vigência anterior termina nesse mesmo instante. Nada é reaberto ou reescrito no passado.
- Apostas guardam `stake_centavos`, `valor_aposta_centavos` e, no evento de criação, `valor_unidade_centavos`. A troca nunca altera apostas existentes.
- Criação manual (#202/#203), Coleta, Telegram e importação XLSX leem a vigência do instante de referência de cada aposta: a data da aposta ou, no Telegram e na materialização, o instante da mensagem (`UnidadeRepo.get_vigente`). Toda aposta criada depois da troca usa o valor novo.
- O replay usa o `valor_unidade_centavos` gravado no evento e não recalcula a unidade. Um evento legado sem esse valor consulta a vigência do seu instante, que a troca não muda; se a consulta divergir da aposta gravada, o replay recusa em vez de reescrever.
- Idempotência: a mesma chave com o mesmo corpo devolve o recibo original (201 no POST, 200 na recuperação); com outro corpo, 409 `idempotency_key_conflict`. A chave vale por usuário. Retenção: enquanto a vigência existir.
- Concorrência: duas trocas simultâneas são serializadas e resultam em vigências contíguas, com uma única aberta.
- Validação: `valor_centavos` inteiro estrito (recusa `100.5`, `5000.0`, `"5000"` e `true`), maior que zero, até o limite. 422 `{"detail": "unidade_invalida", "campos": [{"campo", "codigo"}]}`, com `codigo` em `obrigatorio`, `inteiro`, `positivo`, `acima_do_limite`, `desconhecido` ou `invalido`.
- Sem acesso de escrita, o POST responde 402 `account_read_only` sem efeito.
