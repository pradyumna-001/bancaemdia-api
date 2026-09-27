# ADR 025 — Stripe para assinaturas globais

Data: 2026-09-27. Decisão técnica: Stripe Checkout hospedado + Billing + Customer Portal.
Estado operacional: NO_GO para produção; desenvolvimento autorizado apenas em teste.
Substitui a escolha Mercado Pago/Asaas. OddsNotifier não comprova a elegibilidade desta conta.

## Produto e classificação
Banca em Dia vende software de registro e análise de apostas que o usuário faz em casas externas. Não recebe apostas, depósitos, prêmios nem fundos destinados a apostas. O suporte Stripe deve avaliar este escopo verdadeiro; não classificar por conveniência como outro negócio. A lista de negócios restritos inclui jogos de azar e restrições regionais a aconselhamento/previsão. Registro e análise não equivalem automaticamente a uma aprovação: falta resposta escrita específica.
Fonte: https://stripe.com/br/legal/restricted-businesses

## Conta brasileira e lançamento global
A documentação admite pessoa física com CPF (CNPJ opcional nessa modalidade), exige representante residente e conta bancária brasileira em BRL sob a mesma identificação. As verificações de 2026 se aplicam. Isso é elegibilidade geral, não verificação da conta do titular; identidade, capacidades, pendências e condições comerciais privadas permanecem não verificadas.
Fontes: https://support.stripe.com/questions/brazil-specific-information-to-open-a-stripe-account
https://support.stripe.com/questions/2026-updates-to-brazil-verification-requirements
https://support.stripe.com/questions/supported-bank-accounts-in-brazil

Stripe Payments permite vendas internacionais a partir de países suportados, sujeitas a restrições de país/produto. Managed Payments seria merchant of record, mas a lista de estabelecimentos elegíveis não inclui BR. Portanto o lançamento da conta brasileira usa Checkout comum: o vendedor continua responsável por tributos, registros, documentos fiscais, termos, reembolsos e suporte. Stripe Tax não substitui a análise/obrigações fiscais. Não criar entidade estrangeira nem assumir aprovação para contornar essa limitação.
Fontes: https://stripe.com/br/global
https://docs.stripe.com/payments/managed-payments/eligibility
https://docs.stripe.com/payments/managed-payments

## Tarifas e operação
Tabela pública BR consultada nesta data: cartões nacionais 3,99% + R$0,39; adicional de 2% para cartões internacionais; Billing 0,7% do volume; Checkout incluso em Payments. Tarifas de conversão, disputas, reembolso e condições/prazos de repasse devem ser confirmados na conta antes do GO, sem assumir restituição das tarifas originais. Valores são tarifas do provedor, nunca preço do produto.
Fontes: https://stripe.com/br/pricing
https://docs.stripe.com/refunds
https://docs.stripe.com/payouts
https://docs.stripe.com/disputes

## Arquitetura e moedas
Um produto, catálogo vazio e não publicado. Montantes inteiros em unidades menores da moeda; versões independentes por moeda e frequência, sem conversão calculada pelo cliente e sem preço inferido. Moedas cobradas devem estar explicitamente habilitadas após verificar country specs/capacidades da conta e um Price de teste correspondente; disponibilidade global de compradores não implica disponibilidade de toda moeda na conta brasileira. BRL é a opção inicial conservadora, não restrição estrutural do catálogo. Cada preço publicado fixa moeda, valor, frequência e Stripe Price; assinatura mantém seus termos. Checkout valida novamente esses dados contra Stripe. Adaptive Pricing deve permanecer desabilitado no Dashboard até validação específica; isso é um requisito da configuração da conta, pois a API fixada em 2024-06-20 não expõe o parâmetro mais recente. Nenhum valor comercial é definido aqui.
Fontes: https://docs.stripe.com/currencies
https://docs.stripe.com/api/country_specs

Decisão posterior do titular nesta sessão: cartão obrigatório e 168 horas contadas a partir da confirmação pela Stripe. Checkout coleta cartão sempre; a primeira assinatura recebe trial_period_days=7. Cadastro/backfill cria apenas uma reserva, sem liberar escrita nem consumir trial. Confirmação remota fixa trial_start/trial_end uma única vez. Assinatura posterior não recebe novo trial; durante o trial original não se cria outro checkout, evitando cobrança antecipada. Eventos assinados no corpo bruto apenas acionam sincronização do estado remoto; redirect nunca libera acesso. Inbox durável, idempotência, reconciliação periódica, cancelamento no fim do período pago e portal hospedado. Escritas protegidas na API e banco, inclusive workers/CLI; leitura/exportação continuam disponíveis.

## GO pendente
Faltam: classificação escrita, conta/capacidades verificadas, sandbox real com evidência, preços comerciais e moedas aprovados, impostos/termos/política de reembolso, revisão/CI e autorização específica do titular para produção. Sandbox não conclui #89 nem autoriza cobrança real. Stripe permanece o provedor escolhido em caso de bloqueio.

## Histórico preservado (substituído, sem implementação futura)
O texto abaixo documenta a investigação anterior e não é uma instrução de implementação ou fallback.

<details><summary>Preflight anterior do Mercado Pago</summary>

# ADR 025 â€” Provedor de assinaturas para a conta CPF

- **Data da revisÃ£o:** 23/09/2026
- **Status:** proposto; **NO_GO para habilitar cobranÃ§as em produÃ§Ã£o**
- **Candidato principal:** Mercado Pago Assinaturas (`/preapproval`)
- **Alternativa condicionada:** Asaas, somente se o Mercado Pago rejeitar a operaÃ§Ã£o ou nÃ£o confirmar sua elegibilidade de produÃ§Ã£o

## Contexto e escopo declarado

O Banca em Dia vende uma assinatura de **software de registro, anÃ¡lise e calculadoras** para apostas feitas pelo prÃ³prio usuÃ¡rio em casas externas. O produto atual nÃ£o oferece palpites/sinais, comunidade com dicas ou links de afiliado. Ele nÃ£o aceita apostas, nÃ£o intermedeia pagamentos de apostas e nÃ£o recebe depÃ³sitos, prÃªmios ou fundos dos usuÃ¡rios. A cobranÃ§a Ã© exclusivamente pela licenÃ§a de uso do software, por conta brasileira de pessoa fÃ­sica (CPF), inicialmente com menos de dez assinantes.

Essa descriÃ§Ã£o foi enviada sem omissÃµes relevantes ao suporte do Mercado Pago no protocolo [WCS-51759](https://www.mercadopago.com.br/developers/pt/support/center/tickets/detail/WCS-51759), em 22/09/2026. Qualquer mudanÃ§a do produto que altere esse enquadramento exige novo preflight antes de usar o mesmo arranjo de pagamento.

## EvidÃªncia e decisÃ£o

| Pergunta | EvidÃªncia em 23/09/2026 | Resultado |
|---|---|---|
| O suporte aceita o escopo de software descrito? | Resposta escrita em [WCS-51759](https://www.mercadopago.com.br/developers/pt/support/center/tickets/detail/WCS-51759): para o cenÃ¡rio descrito, o canal confirma a viabilidade da integraÃ§Ã£o pÃºblica de Assinaturas para cobrar o software. Ressalva que nÃ£o emite parecer regulatÃ³rio, certificado de compliance ou autorizaÃ§Ã£o especial. | **Sim, orientaÃ§Ã£o tÃ©cnica limitada ao escopo atual.** |
| CPF exige CNPJ ou aprovaÃ§Ã£o prÃ©via para `/preapproval`? | O mesmo chamado afirma que a documentaÃ§Ã£o pÃºblica brasileira nÃ£o traz exigÃªncia especÃ­fica de CNPJ nem aprovaÃ§Ã£o prÃ©via apenas por a conta ser CPF; nÃ£o encontrou limite publicado para o volume inicial. A [referÃªncia da API](https://www.mercadopago.com.br/developers/pt/reference/online-payments/subscriptions/overview) inclui `POST /preapproval`. | **Nenhuma exigÃªncia publicada identificada; a conta concreta ainda precisa ser habilitada.** |
| A conta jÃ¡ possui aplicaÃ§Ã£o e credenciais de produÃ§Ã£o ativas? | Em 23/09, o titular validou a identidade e criou a aplicaÃ§Ã£o **Banca em Dia** para **Assinaturas** na conta CPF. Na pÃ¡gina de credenciais de produÃ§Ã£o, o painel ainda exigia setor, URL do site e aceite dos termos; o botÃ£o de ativaÃ§Ã£o estava desabilitado. A [documentaÃ§Ã£o de produÃ§Ã£o](https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-preferences/go-to-production) tambÃ©m exige HTTPS para operar. | **AplicaÃ§Ã£o criada; credenciais de produÃ§Ã£o ainda nÃ£o ativas.** |
| O fluxo produtivo foi testado? | A [documentaÃ§Ã£o de Assinaturas](https://www.mercadopago.com.br/developers/pt/docs/subscriptions/overview) descreve criar assinatura, testar e subir em produÃ§Ã£o. NÃ£o hÃ¡ conta de teste, checkout ou `/preapproval` do Banca em Dia criado/validado nesta revisÃ£o. | **Pendente. Sandbox isolado nÃ£o prova elegibilidade produtiva.** |

**DecisÃ£o:** usar o Mercado Pago como Ãºnico candidato de implementaÃ§Ã£o, condicionado Ã  verificaÃ§Ã£o de credenciais de produÃ§Ã£o da conta CPF e ao teste seguro do fluxo `/preapproval`. AtÃ© lÃ¡, **NO_GO** para habilitar pagamentos. A orientaÃ§Ã£o do suporte nÃ£o Ã© uma aprovaÃ§Ã£o regulatÃ³ria irrevogÃ¡vel. NÃ£o acionar Asaas agora: o Mercado Pago respondeu positivamente sobre a viabilidade tÃ©cnica; se a conta ou o produto forem rejeitados, executar o mesmo preflight escrito no Asaas e registrar um novo ADR/atualizaÃ§Ã£o antes de trocar o adapter.

## CondiÃ§Ãµes comerciais e operacionais observadas

Os percentuais publicados na [pÃ¡gina de Assinaturas](https://www.mercadopago.com.br/ferramentas-para-vender/assinaturas) sÃ£o referÃªncia, **nÃ£o cotaÃ§Ã£o para esta conta**: a pÃ¡gina mostra 4,99%, 4,49% e 3,99% para alternativas de meio de pagamento e prazo; o valor de 3,99% aparece com recebimento em 30 dias. A tabela pÃºblica tem rÃ³tulos ambÃ­guos. Confirmar no painel da conta a combinaÃ§Ã£o exata de meio, tarifa, prazo e eventuais taxas adicionais antes de publicar um preÃ§o. O saldo de uma venda sÃ³ deve ser considerado disponÃ­vel no prazo efetivamente configurado para a conta.

- **Cancelamento:** a [gestÃ£o de assinaturas](https://www.mercadopago.com.br/developers/pt/docs/subscriptions/subscription-management) permite `PUT /preapproval/{id}` com `status=canceled` ou `paused`. Cancelar a assinatura interrompe cobranÃ§as futuras; nÃ£o equivale automaticamente a devolver cobranÃ§a passada. Cancelar um plano nÃ£o cancela assinantes jÃ¡ ativos, conforme a [documentaÃ§Ã£o de planos](https://www.mercadopago.com.br/developers/pt/docs/subscription-plans/manage-subscription-plan).
- **Reembolso:** a [documentaÃ§Ã£o de pagamentos](https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-orders/refunds-cancellations) distingue reembolso de pagamento capturado e cancelamento de pagamento ainda nÃ£o aprovado. O endpoint exato para reembolsar a cobranÃ§a gerada pela assinatura deve ser confirmado no teste do tipo de pagamento real; nÃ£o assumir que cancelar `/preapproval` estorna a cobranÃ§a.
- **ContestaÃ§Ã£o:** uma [contestaÃ§Ã£o de cartÃ£o](https://www.mercadopago.com.br/developers/pt/docs/checkout-api-orders/payment-management/chargebacks) pode reter ou reverter valores; o operador precisa receber notificaÃ§Ãµes e guardar prova de consentimento, serviÃ§o prestado e cancelamento. NÃ£o tratar retorno de checkout como prova de pagamento definitivo.
- **RestriÃ§Ãµes:** os [termos de desenvolvedor](https://www.mercadopago.com.br/developers/pt/docs/resources/legal/terms-and-conditions) proÃ­bem uso enganoso ou ilegal e armazenamento indevido de dados de cartÃ£o. O protocolo vale somente para a cobranÃ§a do software descrito; fluxos de aposta, depÃ³sitos ou prÃªmios requerem reavaliaÃ§Ã£o com o provedor. CartÃ£o do assinante permanece no checkout hospedado do provedor.

## Contrato neutro e prÃ³ximos gates

O domÃ­nio de acesso deve depender de `provider`, `customer_ref`, `subscription_ref`, estado e perÃ­odos de cobranÃ§a, sem codificar nomes de status ou URLs do Mercado Pago como verdades internas. O adapter futuro implementarÃ¡ criar assinatura, consultar status, cancelar, obter URL hospedada e reconciliar eventos; sÃ³ o adapter escolhido conhece `/preapproval`. O trial de sete dias sem cartÃ£o Ã© uma regra do Banca em Dia e nÃ£o pode ser encurtado pelo provedor.

Para converter este ADR em `GO`, guardar evidÃªncia privada de: (1) aplicaÃ§Ã£o criada na conta CPF; (2) credenciais de **produÃ§Ã£o ativas**, sem copiÃ¡-las ao repositÃ³rio; (3) `/preapproval` testado com os fluxos de criaÃ§Ã£o, consulta, cancelamento e cobranÃ§a apÃ³s trial, sem cobranÃ§a real nÃ£o autorizada; (4) tarifa/prazo da conta conferidos; (5) confirmaÃ§Ã£o de que a classificaÃ§Ã£o do protocolo ainda corresponde ao produto. O [runbook](../runbooks/billing-provider-preflight.md) descreve a coleta. PreÃ§o e data de lanÃ§amento pertencem Ã  configuraÃ§Ã£o e decisÃ£o posteriores.

</details>
