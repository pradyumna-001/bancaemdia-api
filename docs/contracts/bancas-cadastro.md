# Bancas do site — contrato da issue #211

Destino: frontend #31/#33. Implementação em [`src/bancaemdia/api/v1/bancas.py`](../../src/bancaemdia/api/v1/bancas.py).

| Operação | Acesso | Resultado |
| --- | --- | --- |
| `GET /api/v1/bancas` | READ_ONLY | `bancas`: `id`, `nome`, `estado` (`ativa`/`arquivada`), `arquivada_em`, `saldo_inicial_centavos` (capital inicial ou null) e `contas` vinculadas (`id`, `casa_id`, `apelido`). |
| `POST /api/v1/bancas` + `Idempotency-Key` | FULL_WRITE + CSRF | 201 com a banca. `nome` único por usuário (409 `nome_em_uso`, inclusive entre arquivadas). Capital ausente ou null fica não informado. |
| `PATCH /api/v1/bancas/{banca_id}` + `Idempotency-Key` | FULL_WRITE + CSRF | 200. Só os campos enviados mudam; `saldo_inicial_centavos: null` apaga o capital. 404 `banca_nao_encontrada`, 409 `nome_em_uso`/`banca_arquivada`. |
| `POST /api/v1/bancas/{banca_id}/arquivar` + `Idempotency-Key` | FULL_WRITE + CSRF | 200 com `estado: arquivada`. 409 `banca_com_contas` enquanto houver conta vinculada; 409 `banca_arquivada` se já estiver arquivada. |
| `GET /api/v1/bancas/intencao` + `Idempotency-Key` | READ_ONLY, primário | `operacao`, `status_code` e `resposta` originais; 404 `intencao_nao_encontrada`. |

## Regras

- Titular, conta, casa e banca continuam entidades distintas. O vínculo conta→banca segue em `PATCH /api/v1/caixa/contas/{conta_casa_id}/banca`, que agora recusa banca arquivada com 409.
- Arquivar não apaga movimentos, apostas nem o `banca_id` histórico das apostas. A banca sai das escolhas e de novos vínculos e continua nas leituras (saldo e análises) com seu nome.
- Capital: `null` quer dizer não informado. `GET /api/v1/caixa/saldo` devolve `saldo_inicial_centavos: null` e as análises devolvem `capital_banca_centavos: null` com `progressao: null` (`capital_ausente_ou_zero`). Zero é mostrado como zero e também não produz progressão, porque não há base para dividir.
- Idempotência: a chave vale por usuário e por intenção. O mesmo pedido devolve a resposta original (status e corpo); outro pedido com a mesma chave dá 409 `idempotency_key_conflict`. Recusas (404/409) não ficam gravadas, então a mesma intenção pode ser repetida depois de resolver o conflito. Escritas do mesmo usuário são serializadas.
- Validação: nome de 1 a 80 caracteres após remover espaços nas pontas; capital inteiro estrito, ≥ 0 e até R$ 1 bilhão. 422 `{"detail": "banca_invalida", "campos": [{"campo", "codigo"}]}`, com `codigo` em `obrigatorio`, `inteiro`, `nao_negativo`, `acima_do_limite`, `tamanho`, `vazio`, `desconhecido` ou `invalido`.
- Sem acesso de escrita: 402 `account_read_only`, sem banca nem recibo gravados. A trava comercial também protege `bancas_requisicoes` no banco.

## Modo conjunta/por grupo: não entra como modo

Na API, o dinheiro está nas contas das casas, e cada conta aponta para no máximo uma banca. "Conjunta" é uma banca com todas as contas; "por grupo" é uma banca para cada conjunto de contas. As duas formas já se expressam pelos vínculos, sem uma chave de modo que trocasse o significado de leituras antigas. Trocar de forma é revincular contas: nada destrói capital, apostas ou movimentos. Os grupos de tipsters da #184 são dimensão de leitura e filtro, não dinheiro; os filtros `banca_id` da #184, quando integrada, leem as bancas criadas aqui.

O caminho e os nomes desta capacidade vieram da issue e do código existente (`saldo_inicial_centavos` já era o campo do capital em `/caixa/saldo`).
