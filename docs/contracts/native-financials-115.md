# #115 — leitor 1win e valores nativos em USDT

Revisão: 08/10/2026. PR [#185](https://github.com/pradyumna-001/bancaemdia-api/pull/185), diretamente contra `main` API `b916f54331f14cf47a3800324bd61d8638043c06`.
O titular autorizou incluir USDT nativo na #115. A implementação preserva as unidades da fonte, com contas e relatórios por moeda; não existe conversão implícita para reais.

## Evidência recebida e interpretação

Dependência externa: [extensão #17 / PR #33](https://github.com/pradyumna-001/bancaemdia-extension-client/pull/33), commit `abd0e546a0b5d905b3ce01541c190e4dc4e47e1e`. O delta real de 08/10/2026 forneceu 30 envelopes, 10 bilhetes distintos, 20 repetições exatas e 13 seleções: oito simples e duas múltiplas, quatro ganhos e seis perdas. Todos estão em USDT, com horários válidos e sem campos necessários ausentes. Não é necessária outra coleta igual da extensão.

| Artefato local privado | SHA-256 |
| --- | --- |
| Export revisado pelo titular | `5ac35d75c40f3658b3560d6d92ed9f7b51d324acda6f3bb8686472b658bec42c` |
| Inspeção da extensão | `58e18d2d20488ecfc61a6b47f2eb7bec0eaf50215c8e36f0752c6db74ee76515` |
| Diagnóstico de origem | `3c5a554849d98bcb0ca32fbc9d63daa80f46804de036f0b960282f97517635e0` |
| Vínculo fonte/exibição | `957b39bb354d3141716d31e87316300d7ca6e1bc98ab9cfd4b0112f5a7ce5c8f` |
| Bundle financeiro raw/envelope/golden para revisão | `14870b5123cf8aed01fb7a878517005b2ad990a5f1726f295c7fd24e056385c3` |

Os hashes das quatro evidências foram conferidos com o recibo. Os dados privados permanecem fora do Git. A aprovação do export não substitui a aprovação administrativa deste novo bundle.

A tela vincula cada alias a Aposta/Ganhos por valor, odd, moeda, estado e tipo. Nos quatro ganhos, o campo `profitAmount` coincide com o retorno bruto observado; nas perdas, é zero. A relação com stake × odd corrobora essa interpretação, mas **o código lê o retorno da fonte, nunca o calcula pela odd**. Um retorno tem precisão superior à exibição da tela: conserva-se o valor original, sem arredondá-lo para duas casas. O lucro é retorno bruto menos stake, calculado no servidor.

Essa interpretação é delimitada a apostas simples/múltiplas liquidadas, em dinheiro USDT, sem bônus/freebet e com flags de resultado parcial explicitamente falsas. Aberto, void, cashout, bônus, freebet, parciais, transições e BRL deste host não foram observados e não são declarados suportados. As seleções devem concordar com o resultado do bilhete. Ausência, contradição e variantes desconhecidas recebem erro estável e quarentena; dados sensíveis deixam somente fingerprint e diagnóstico.

## Dinheiro, identidade e conflitos

`OneWinReader` versão `0.2.0` oferece `parse_native`. O contrato monetário 2 usa `Decimal`, moeda explícita `BRL|USDT` e valores serializados como **texto decimal**. O armazenamento é `NUMERIC(50,30)`: magnitude menor que 10²⁰ e até 30 casas exatas. Valores fora dessa representação são recusados, sem arredondamento. O reader específico admite somente a variante USDT comprovada; suporte de armazenamento a BRL não prova captura BRL da 1win.

O contrato v1 em centavos, suas fixtures, tabelas `apostas`/`eventos`, relatórios e transporte coleta-v2 permanecem compatíveis. O ledger denominado usa `native_accounts`, `native_bets` e `native_bet_evidence`; um valor USDT nunca é gravado como centavos BRL ou como zero fictício. Os relatórios nativos abrangem somente esse ledger. O relatório legado continua abrangendo seus fatos em BRL; somar os dois exige seleção explícita por moeda, nunca um total misto.

Identidade = marca + host final + ID externo, isolada por usuário. Hash do conteúdo e hash canônico são distintos. Repetições não criam outro fato; bytes diferentes com o mesmo canônico preservam evidência adicional. Escritores concorrentes são serializados por identidade. A evidência é imutável por UPDATE/DELETE ordinário.

A fonte não fornece relógio de revisão autoritativo. `source_updated_at` é null e a política explícita é `unversioned_conflict_review`. Conteúdo financeiro divergente preserva ambas as evidências, mantém o fato anterior e exclui a identidade dos totais até revisão. Captura/recebimento/colocação não passam a ser relógio de revisão; replay antigo não limpa conflito. Esta entrega não oferece resolução automática ou manual de conflitos sem versão. Isso é uma limitação operacional explícita, sem promessa de atualização de lifecycle ausente no corpus.

## Contas e seleção temporal

A conta corresponde ao **instante do jogo**, o menor `match.startAt` entre seleções, em UTC. Procura exatamente uma conta do mesmo usuário, casa e moeda com intervalo `[valid_from, valid_to)`. A ausência ou sobreposição resulta em `account_review`, preservando o fato fora dos totais; não se escolhe o menor ID, a carteira da fonte ou a data da aposta. O estado `active` não elimina um intervalo histórico válido.

Somente `multicontas=true` com `explicit_account_id` válido pode manter a conta que efetivamente apostou. A referência precisa pertencer ao mesmo usuário, casa e moeda; no multicontas pode ser histórica e estar fora do intervalo do jogo. Um ID fornecido sem multicontas é recusado. Criar uma conta depois de uma captura não reatribui silenciosamente o histórico já em revisão.

## API e ponte lossless

| Método e endpoint | Contrato |
| --- | --- |
| POST `/api/v1/financeiro/nativo/contas` | Sessão/JWT; `casa_id`, `currency`, `label`, `valid_from`, `valid_to` opcional. Usuário deriva da autenticação. |
| GET `/api/v1/financeiro/nativo/contas` | Até 100 contas próprias, incluindo intervalos históricos. |
| GET `/api/v1/financeiro/nativo/apostas` | `limit` 1–100 e `currency` opcional; inclui revisão e valores exatos em texto. |
| GET `/api/v1/financeiro/nativo/resumo` | `since` inclusivo / `until` exclusivo pelo jogo, com fuso; `account_id` opcional. Totais separados por moeda, `combined_monetary_total=null`. ID inexistente/de terceiro produz conjunto vazio. |
| POST `/api/v1/coleta/reader-captures` | HTTPS + `X-Coleta-Token` de instalação; `transport_contract="reader-capture-1"`, `envelope`, `multicontas` opcional e `explicit_account_id` opcional. |

O envelope interno versão 1 preserva o `{bet,selections}` mínimo em `payload_text`, **texto original sanitizado byte a byte**, e SHA-256 desses bytes UTF-8. Host final exato `api-gateway.top-parser.com`, marca `1win`, schema `1win-history-game-v2`, endpoint observado `/bets/history/get-many`, canal Fetch/XHR passivo. O host da página `1win.com` não substitui o host final. Numbers dentro do texto são lidos diretamente com Decimal; reserialização por `JSON.parse`/numbers JavaScript não é uma ponte lossless. Este endpoint não aceita o export inteiro nem altera `contrato:2`.

Aliases do export de revisão são válidos somente dentro daquela captura. O cliente operacional precisa preservar a identidade estável entre sessões; não deve reutilizar o export como ingestão operacional, pois seus aliases reiniciam. Essa prova pertence à integração cliente/E2E. Os limites defensivos de leitura da fonte (128 dígitos, magnitude até 10¹⁵) continuam aplicados além dos limites de armazenamento.

O cliente deve enviar um envelope por chamada, sem converter seus numbers, autenticar a instalação pareada e manter o mesmo corpo em retries. A resposta fornece `bet_id`, `result` (`created|noop|account_review|conflict_review`) e `needs_review`; 422 é rejeição/quarentena, 429 limite, 503 admissão fechada. Respostas não repetem fonte privada. Pareamento, expiração/revogação, RLS, restrição comercial de escrita, limites de corpo/rate e cota diária compartilhada com coleta v1/v2 permanecem aplicados. Cota usa recebimento UTC, independente de jogo ou captura.

Exportação de privacidade JSON/Excel inclui as três tabelas e preserva valores monetários nativos em texto. Exclusão autenticada remove dados nativos sob a exceção de apagamento do próprio usuário; não permite DELETE ordinário de evidências. Triggers aplicam usuário ativo, acesso comercial e auditoria sem valores privados. Migration `n115native2026` segue `j6main2026`; downgrade recusa remover dados existentes. Rollback operacional conserva schema/evidência.

## Revisão, ativação e dependências

O bundle completo possui dez casos reais com raw/envelope/golden, hashes por arquivo e expectativas independentes construídas da fonte e evidência de exibição. Está fora do Git para `pradyumna-001` revisar. Conforme [backend-readers.md](backend-readers.md), exige comentário próprio:

```text
Approved reader fixture bundle SHA256: 14870b5123cf8aed01fb7a878517005b2ad990a5f1726f295c7fd24e056385c3
```

Após essa aprovação, publicar exatamente os bytes revisados e a referência do comentário, executar o harness/gate com verificação do autor e promover o digest em `native_admission.py` mediante PR. `ONE_WIN_NATIVE_ENABLED=false` por padrão; mesmo true não permite ingestão quando não existe admissão compilada do corpus. Nenhuma aprovação foi presumida. `DEFAULT_REGISTRY`, catálogo e produção não passam a declarar suporte completo.

Extensão PR #33 precisa de revisão/merge independente. A integração cliente deve adotar esta ponte, depois validar release contra API real HTTPS controlada; evidência E2E e expansão dos estados são acompanhadas em #117. A #115 permanece aberta: corpus administrativo e estados/transições ainda ausentes delimitam o que falta, sem exigir outra captura igual e sem anunciar conclusão completa.

## Provas exigidas

CI executa 32 aceites nativos obrigatórios, sem skips, em PostgreSQL separado: HTTP/JWT/pareamento real, valores exatos, replay, concorrência, conflito, jogo/multicontas, BRL separado, RLS, acesso comercial, quarentena/privacidade, cota compartilhada, exportação/exclusão e downgrade com dados. Mantém os 16 aceites do harness e 15 de leitura/quarentena 1win, mais regressões históricas, contratos, segurança e carga de 55 minutos.

O replay local privado valida os 30 envelopes reais contra PostgreSQL: dez fatos, vinte no-ops, dez evidências únicas, valores e totais exatos conferidos com oráculo independente, sem `Aposta`/`Evento` legado. Esse replay não substitui aprovação administrativa, CI do SHA final ou E2E da extensão.
