# Preflight Stripe — pendente

Leia ADR 025. Nunca registrar chaves, cartões, documentos ou dados pessoais em Git, logs, issues ou chat.

1. O titular abre a conta brasileira correta e usa somente modo de teste. Confirma modalidade PF/CPF, verificações e pendências diretamente no painel. Não copiar documentos aqui.
2. Solicitar à Stripe classificação escrita usando: “Banca em Dia é software por assinatura para registro e análise de apostas feitas pelos próprios usuários em casas externas. Não recebe apostas, depósitos, prêmios ou fundos destinados a apostas. Podemos vender este software internacionalmente por Checkout/Billing a partir desta conta brasileira de pessoa física? Quais restrições de produto/país e exigências se aplicam?”
3. Registrar apenas data, conclusão, referência sanitizada e pendências; não publicar resposta com dados privados.
4. Verificar moedas de apresentação e repasse, cartões internacionais, tarifas/FX/disputas/reembolsos e prazo real de repasse. Managed Payments não suporta estabelecimento BR segundo a lista consultada em 2026-09-27.
5. Chaves de teste e signing secret entram somente no gerenciador de segredos/ambiente local. Configurar webhook com versão de API fixada pelo adapter e portal de teste (cancelamento ao fim do período, sem troca de plano).
6. Executar o runbook billing-stripe e guardar resultados sanitizados. Preço de fixture é exclusivo de sandbox, não publicar catálogo comercial.
7. Antes de produção: titular decide preço/moedas, confirma obrigações fiscais e termos, Stripe confirma elegibilidade, revisão e testes passam, e titular autoriza explicitamente ativação. Até lá NO_GO.
