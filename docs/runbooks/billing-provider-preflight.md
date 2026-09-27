# Preflight Stripe — pendente

Leia ADR 025. Nunca registrar chaves, cartões, documentos ou dados pessoais em Git, logs, issues ou chat.

1. O titular abre a conta brasileira correta e usa somente modo de teste. Confirma a modalidade real PF/CPF ou PJ/CNPJ, verificações e pendências diretamente no painel. Não copiar documentos aqui.
2. Solicitar à Stripe classificação escrita usando: “Banca em Dia é software por assinatura para registro e análise de apostas feitas pelos próprios usuários em casas externas. Não recebe apostas, depósitos, prêmios ou fundos destinados a apostas. Podemos vender este software internacionalmente por Checkout/Billing a partir desta conta brasileira (informar a modalidade real no atendimento privado)? Quais restrições de produto/país e exigências se aplicam?”
3. Registrar apenas data, conclusão, referência sanitizada e pendências; não publicar resposta com dados privados.
4. Verificar moedas de apresentação e repasse, cartões internacionais, tarifas/FX/disputas/reembolsos e prazo real de repasse. Managed Payments não suporta estabelecimento BR segundo a lista consultada em 2026-09-27.
5. Chaves de teste e signing secret entram somente no gerenciador de segredos/ambiente local. Configurar webhook com versão de API fixada pelo adapter e portal de teste (cancelamento ao fim do período, sem troca de plano).
6. Executar o runbook billing-stripe e guardar resultados sanitizados. Preço de fixture é exclusivo de sandbox, não publicar catálogo comercial.
7. Antes de produção: titular decide preço/moedas, confirma obrigações fiscais e termos, Stripe confirma elegibilidade, revisão e testes passam, e titular autoriza explicitamente ativação. Até lá NO_GO.

## Verificado em 2026-09-27

Conta brasileira acessada e sandbox isolado configurado com descrição fiel do software. Portal de teste permite cancelamento no fim do período e bloqueia troca de plano; Adaptive Pricing foi desabilitado no sandbox. A API de country specs inclui BRL, USD, EUR e JPY; Prices exclusivamente fictícios nessas moedas passaram pela validação do adapter. Isso não publica catálogo comercial nem confirma repasses em tais moedas.

Checkout hospedado, confirmação do cartão, sete dias exatos, eventos assinados, renovação, falha/retry, reembolsos e cancelamento foram exercitados no sandbox. A API de conta retornou charges_enabled=false, payouts_enabled=false e details_submitted=false; nenhum recurso de produção foi ativado. A API de Stripe Tax rejeitou a conta brasileira por país não suportado. Permanecem necessárias classificação escrita do produto, verificações do titular, condições da conta, solução fiscal internacional, termos e preços comerciais. Não inferir aprovação a partir do sandbox.
