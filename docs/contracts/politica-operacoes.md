# Política por operação no OpenAPI — contrato da issue #213

Destino: frontend #9/#10/#52. Registro em [`src/bancaemdia/api/policies.py`](../../src/bancaemdia/api/policies.py); schema publicado em `components.schemas.BancaemdiaPolicy` do próprio documento.

Toda operação tem exatamente uma extensão `x-bancaemdia-policy`:

```json
{
  "access_modes": ["FULL_WRITE", "READ_ONLY"],
  "automatic_retry": {"allowed": true, "max_attempts": 3},
  "idempotency": {
    "identity": "header:Idempotency-Key",
    "retention": "until_account_deletion",
    "recovery": "replay"
  }
}
```

| Campo | Significado |
| --- | --- |
| `access_modes` | Modos de acesso comercial em que a operação pode ter efeito. `["FULL_WRITE"]` quer dizer que READ_ONLY recebe 402 `account_read_only` sem efeito. Só essas operações publicam 402; leituras, cobrança, cálculo e controles de identidade/privacidade (vincular e desvincular Telegram, parear, girar e revogar instalação, excluir a conta) funcionam em READ_ONLY e não publicam 402. |
| `automatic_retry` | Se o cliente pode repetir sozinho depois de falha de rede ou 503, e o total máximo de tentativas. Escrita sem identidade nunca se repete sozinha; só cálculo puro e leituras. |
| `idempotency.identity` | `header:Idempotency-Key`, `path:<parâmetro>` (o recurso é a identidade, como no DELETE/restaurar de aposta), `body:<campo>` (lote da Coleta) ou `none`. |
| `idempotency.retention` | `until_account_deletion`, `while_resource_exists` ou null. |
| `idempotency.recovery` | `replay` (repetir o mesmo pedido devolve a resposta original), `GET <caminho>` (operação de recuperação publicada) ou null. |

## Garantias de CI

- A geração do OpenAPI falha se uma operação nova não tiver entrada em `POLITICAS`, ou se uma entrada apontar para operação removida (como já acontece com a documentação humana).
- `tests/contract/test_policy_contract.py` valida cada extensão contra o schema publicado e confere três coisas: 402 presente se e somente se `access_modes == ["FULL_WRITE"]`; identidade de cabeçalho ou caminho existe entre os parâmetros da operação; recuperação `GET` aponta para operação publicada. Também confere que o acesso declarado das rotas do usuário é exatamente a regra de runtime `requires_full_write` (agora em `domain/access.py`, a mesma função usada por `_current_user`).
- `tests/integration/test_policy_runtime_db.py` chama cada operação com gate de usuário com uma conta READ_ONLY real e exige 402 exatamente onde a política declara FULL_WRITE.
- Rotas do artefato congelado da extensão (`openapi/extension-collection-v2.json`: `/coleta`, `/api/v1/coleta`, sessões, lotes e jobs) recebem a política no documento da API, mas suas respostas ficam como publicadas na v2; o artefato servido à extensão continua byte a byte igual, sem a extensão nova. A correção do 402 das sessões e jobs entra no próximo release do contrato da extensão.
- As rotas fora do gate de usuário (`FORA_DO_USUARIO`: saúde, `/auth/*`, provedores, operador, calculadoras e credencial de instalação da Coleta) são declaradas no registro e cobertas pelos testes próprios de cada uma.

## Migração das candidatas na reconciliação

O comportamento HTTP não muda; o único efeito no documento é a extensão nova e a retirada de 402 onde ele nunca ocorre. Ao integrar cada PR aberto, suas operações ganham entrada em `POLITICAS` e as extensões próprias saem:

| Extensão da candidata | Campo da política única |
| --- | --- |
| `x-access-modes`, `x-permission`, `x-commercial-access` | `access_modes` |
| `x-automatic-retry`, `x-client-auto-retry` | `automatic_retry` |
| `x-idempotency` | `idempotency` (`identity`, `retention`, `recovery`) |
| `x-bancaemdia-resultados`, `x-bancaemdia-review` | Os mesmos três campos; dados de domínio que não são política continuam no corpo ou na descrição da operação. |

O teste de contrato recusa qualquer outra extensão `x-` em operação.
