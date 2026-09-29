# ADR 025 — Stripe para assinaturas globais

Data: 2026-09-27. Decisão técnica: Stripe Checkout hospedado + Billing + Customer Portal.
Estado operacional: NO_GO para produção; desenvolvimento autorizado apenas em teste.
Substitui a escolha Mercado Pago/Asaas. OddsNotifier não comprova a elegibilidade desta conta.

## Produto e classificação
Banca em Dia vende software de registro e análise de apostas que o usuário faz em casas externas. Não recebe apostas, depósitos, prêmios nem fundos destinados a apostas. O produto deve ser descrito sem omissões e sem classificá-lo por conveniência como outro negócio. Em decisão posterior nesta sessão, o titular determinou prosseguir com Stripe sem abrir consulta de elegibilidade ao suporte. O rascunho foi descartado sem envio. Confirmação escrita preventiva deixa de ser um gate deste projeto; esta é uma decisão do titular, não uma declaração de aprovação emitida pela Stripe. Continuam aplicáveis os requisitos efetivamente apresentados pela conta/provedor.
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

Verificação real do sandbox brasileiro em 2026-09-27: a API `GET /v1/tax/settings` recusou o acesso porque Stripe Tax não suporta o país da conta. Portanto não é possível pressupor `automatic_tax=true` nesta arquitetura. O adapter exclusivamente de teste permite validar o ciclo com `STRIPE_TEST_AUTOMATIC_TAX=false`; isso não resolve tributos nem autoriza vendas globais em produção. Antes do GO, o titular precisa definir com assessoria fiscal uma solução de cálculo/recolhimento compatível com o estabelecimento brasileiro e os mercados atendidos. Não houve troca de provedor, ativação de produção ou declaração fiscal fictícia. Fonte pública: https://docs.stripe.com/tax/supported-countries

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

O sandbox real foi exercitado em 2026-09-27: Checkout com cartão, trial de 604800 segundos, eventos assinados, renovação, falha/retry, reembolsos e cancelamento. A evidência não confirma elegibilidade, repasses ou operação produtiva.

## GO pendente
Faltam: conta/capacidades verificadas, preços comerciais e moedas aprovados, impostos/termos/política de reembolso, revisão/CI e autorização específica do titular para produção. Sandbox não conclui #89 nem autoriza cobrança real. Stripe permanece o provedor escolhido em caso de bloqueio.

## Histórico preservado (substituído, sem implementação futura)
O texto abaixo documenta a investigação anterior e não é uma instrução de implementação ou fallback.

<details><summary>Preflight anterior do Mercado Pago</summary>

# ADR 025 — Provedor de assinaturas para a conta CPF

- **Data da revisão:** 23/09/2026
- **Status:** proposto; **NO_GO para habilitar cobranças em produção**
- **Candidato principal:** Mercado Pago Assinaturas (`/preapproval`)
- **Alternativa condicionada:** Asaas, somente se o Mercado Pago rejeitar a operação ou não confirmar sua elegibilidade de produção

## Contexto e escopo declarado

O Banca em Dia vende uma assinatura de **software de registro, análise e calculadoras** para apostas feitas pelo próprio usuário em casas externas. O produto atual não oferece palpites/sinais, comunidade com dicas ou links de afiliado. Ele não aceita apostas, não intermedeia pagamentos de apostas e não recebe depósitos, prêmios ou fundos dos usuários. A cobrança é exclusivamente pela licença de uso do software, por conta brasileira de pessoa física (CPF), inicialmente com menos de dez assinantes.

Essa descrição foi enviada sem omissões relevantes ao suporte do Mercado Pago no protocolo [WCS-51759](https://www.mercadopago.com.br/developers/pt/support/center/tickets/detail/WCS-51759), em 22/09/2026. Qualquer mudança do produto que altere esse enquadramento exige novo preflight antes de usar o mesmo arranjo de pagamento.

## Evidência e decisão

| Pergunta | Evidência em 23/09/2026 | Resultado |
|---|---|---|
| O suporte aceita o escopo de software descrito? | Resposta escrita em [WCS-51759](https://www.mercadopago.com.br/developers/pt/support/center/tickets/detail/WCS-51759): para o cenário descrito, o canal confirma a viabilidade da integração pública de Assinaturas para cobrar o software. Ressalva que não emite parecer regulatório, certificado de compliance ou autorização especial. | **Sim, orientação técnica limitada ao escopo atual.** |
| CPF exige CNPJ ou aprovação prévia para `/preapproval`? | O mesmo chamado afirma que a documentação pública brasileira não traz exigência específica de CNPJ nem aprovação prévia apenas por a conta ser CPF; não encontrou limite publicado para o volume inicial. A [referência da API](https://www.mercadopago.com.br/developers/pt/reference/online-payments/subscriptions/overview) inclui `POST /preapproval`. | **Nenhuma exigência publicada identificada; a conta concreta ainda precisa ser habilitada.** |
| A conta já possui aplicação e credenciais de produção ativas? | Em 23/09, o titular validou a identidade e criou a aplicação **Banca em Dia** para **Assinaturas** na conta CPF. Na página de credenciais de produção, o painel ainda exigia setor, URL do site e aceite dos termos; o botão de ativação estava desabilitado. A [documentação de produção](https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-preferences/go-to-production) também exige HTTPS para operar. | **Aplicação criada; credenciais de produção ainda não ativas.** |
| O fluxo produtivo foi testado? | A [documentação de Assinaturas](https://www.mercadopago.com.br/developers/pt/docs/subscriptions/overview) descreve criar assinatura, testar e subir em produção. Não há conta de teste, checkout ou `/preapproval` do Banca em Dia criado/validado nesta revisão. | **Pendente. Sandbox isolado não prova elegibilidade produtiva.** |

**Decisão:** usar o Mercado Pago como único candidato de implementação, condicionado à verificação de credenciais de produção da conta CPF e ao teste seguro do fluxo `/preapproval`. Até lá, **NO_GO** para habilitar pagamentos. A orientação do suporte não é uma aprovação regulatória irrevogável. Não acionar Asaas agora: o Mercado Pago respondeu positivamente sobre a viabilidade técnica; se a conta ou o produto forem rejeitados, executar o mesmo preflight escrito no Asaas e registrar um novo ADR/atualização antes de trocar o adapter.

## Condições comerciais e operacionais observadas

Os percentuais publicados na [página de Assinaturas](https://www.mercadopago.com.br/ferramentas-para-vender/assinaturas) são referência, **não cotação para esta conta**: a página mostra 4,99%, 4,49% e 3,99% para alternativas de meio de pagamento e prazo; o valor de 3,99% aparece com recebimento em 30 dias. A tabela pública tem rótulos ambíguos. Confirmar no painel da conta a combinação exata de meio, tarifa, prazo e eventuais taxas adicionais antes de publicar um preço. O saldo de uma venda só deve ser considerado disponível no prazo efetivamente configurado para a conta.

- **Cancelamento:** a [gestão de assinaturas](https://www.mercadopago.com.br/developers/pt/docs/subscriptions/subscription-management) permite `PUT /preapproval/{id}` com `status=canceled` ou `paused`. Cancelar a assinatura interrompe cobranças futuras; não equivale automaticamente a devolver cobrança passada. Cancelar um plano não cancela assinantes já ativos, conforme a [documentação de planos](https://www.mercadopago.com.br/developers/pt/docs/subscription-plans/manage-subscription-plan).
- **Reembolso:** a [documentação de pagamentos](https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-orders/refunds-cancellations) distingue reembolso de pagamento capturado e cancelamento de pagamento ainda não aprovado. O endpoint exato para reembolsar a cobrança gerada pela assinatura deve ser confirmado no teste do tipo de pagamento real; não assumir que cancelar `/preapproval` estorna a cobrança.
- **Contestação:** uma [contestação de cartão](https://www.mercadopago.com.br/developers/pt/docs/checkout-api-orders/payment-management/chargebacks) pode reter ou reverter valores; o operador precisa receber notificações e guardar prova de consentimento, serviço prestado e cancelamento. Não tratar retorno de checkout como prova de pagamento definitivo.
- **Restrições:** os [termos de desenvolvedor](https://www.mercadopago.com.br/developers/pt/docs/resources/legal/terms-and-conditions) proíbem uso enganoso ou ilegal e armazenamento indevido de dados de cartão. O protocolo vale somente para a cobrança do software descrito; fluxos de aposta, depósitos ou prêmios requerem reavaliação com o provedor. Cartão do assinante permanece no checkout hospedado do provedor.

## Contrato neutro e próximos gates

O domínio de acesso deve depender de `provider`, `customer_ref`, `subscription_ref`, estado e períodos de cobrança, sem codificar nomes de status ou URLs do Mercado Pago como verdades internas. O adapter futuro implementará criar assinatura, consultar status, cancelar, obter URL hospedada e reconciliar eventos; só o adapter escolhido conhece `/preapproval`. O trial de sete dias sem cartão é uma regra do Banca em Dia e não pode ser encurtado pelo provedor.

Para converter este ADR em `GO`, guardar evidência privada de: (1) aplicação criada na conta CPF; (2) credenciais de **produção ativas**, sem copiá-las ao repositório; (3) `/preapproval` testado com os fluxos de criação, consulta, cancelamento e cobrança após trial, sem cobrança real não autorizada; (4) tarifa/prazo da conta conferidos; (5) confirmação de que a classificação do protocolo ainda corresponde ao produto. O [runbook](../runbooks/billing-provider-preflight.md) descreve a coleta. Preço e data de lançamento pertencem à configuração e decisão posteriores.

</details>
