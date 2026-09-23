# Preflight de provedor de assinaturas (issue #89)

**Estado em 23/09/2026:** Mercado Pago é o candidato principal; cobranças continuam `NO_GO` conforme [ADR 025](../adrs/025-billing-provider.md). O suporte respondeu no [protocolo WCS-51759](https://www.mercadopago.com.br/developers/pt/support/center/tickets/detail/WCS-51759). O titular validou a identidade e criou a aplicação **Banca em Dia** para **Assinaturas**. O painel ainda pede setor, URL pública do site e aceite dos termos para ativar credenciais de produção. Nunca enviar credenciais, CPF, dados de cartão ou capturas do painel ao GitHub.

## Fechar o gate do Mercado Pago

1. Na conta CPF correta, abrir a aplicação “Banca em Dia” em [Suas integrações](https://www.mercadopago.com.br/developers/panel/app). A criação da aplicação para **Assinaturas** já foi concluída; não criar duplicata.
2. Abrir `Produção > Credenciais de produção` da aplicação. A [documentação de ativação](https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-preferences/go-to-production) pede ramo, URL do site, aceite dos termos, reCAPTCHA e HTTPS para produção. Se não houver site público apropriado ou a ativação for recusada, manter `NO_GO`. Não usar site fictício, de terceiro ou dados de outra atividade.
3. Verificar **somente o estado** de ativação das credenciais. Guardar em cofre privado o Access Token e a Public Key; registrar no ADR apenas data e prova sanitizada de que o painel habilitou produção, nunca os valores.
4. Confirmar na conta a tabela vigente de tarifa por meio de pagamento e prazo de recebimento. A [página pública de Assinaturas](https://www.mercadopago.com.br/ferramentas-para-vender/assinaturas) é referência geral e pode não refletir a oferta da conta. Registrar evidência privada da tabela da conta.
5. No ambiente de teste, verificar o contrato `POST /preapproval`, `GET /preapproval/{id}` e `PUT /preapproval/{id}` para cancelamento, usando apenas usuários e meios de pagamento de teste. Confirmar que o checkout hospedado devolve a URL esperada, que o status é consultável e que cancelamento impede novas cobranças. Conferir na [referência oficial](https://www.mercadopago.com.br/developers/pt/reference/online-payments/subscriptions/overview) os campos e estados atuais. Não criar assinatura real nem executar cobrança real para este preflight.
6. Registrar que o trial de sete dias sem cartão é controlado pelo Banca em Dia. O adapter futuro não pode encurtá-lo: a primeira possibilidade de cobrança deve ocorrer depois dos sete dias completos, conforme a issue #91. Validar a estratégia em teste antes de habilitar checkout.
7. Atualizar o ADR com data, evidência e decisão `GO` apenas quando classificação escrita, conta CPF produtiva, fluxo de assinatura e condições da conta estiverem comprovados. Revisar novamente se o produto passar a oferecer outra atividade ou receber dinheiro ligado às apostas.

## Quando usar o Asaas

Se o Mercado Pago negar a atividade, negar credenciais de produção para esta conta CPF ou não confirmar um bloqueio relevante, manter `NO_GO` e repetir o mesmo preflight com o Asaas: descrição integral do produto, pergunta por escrito sobre CPF/recorrência produtiva, protocolo, taxas/prazos, cancelamento/estorno/contestação, termos e credenciais de produção. Escolher o provedor só após resposta escrita e conta elegível. Preservar o contrato neutro de assinatura do ADR; não implementar dois adapters ativos para contornar a decisão.

## Evidência mínima para revisão

| Item | Local seguro |
|---|---|
| Resposta de classificação e protocolo | Link ao chamado, sem reproduzir dados pessoais no repositório |
| Aplicação e habilitação de credenciais de produção | Registro sanitizado em cofre privado, sem valores de tokens |
| Tarifa/prazo específicos da conta | Registro privado datado |
| Teste `/preapproval` e cancelamento | IDs de teste e relatório sem tokens ou dados de cartão |
| Decisão `GO|NO_GO` e responsável | ADR 025, com data e links de evidência |
