# Preflight comercial da conta brasileira — #89

Data da consulta: 2026-10-06. Provedor mantido: Stripe Checkout + Billing comum.
**Produção permanece NO_GO.** Este documento prepara decisões e evidências; não
ativa Payments, publica preços, contrata assessoria nem autoriza cobranças.

## Resultado observado na conta

A consulta somente de leitura ao Dashboard autenticado encontrou:

| Superfície | Resultado | O que comprova |
| --- | --- | --- |
| Área restrita, título `Stripe [Teste]`, rota `/test/` | Um assinante ativo e MRR fictícia de R$123,45 | Métricas de sandbox; não cliente pagante, receita real ou disponibilidade de repasse |
| Alternar para a conta de produção | Tela `Ativar Payments`, etapa inicial de localização/tipo de empresa | Onboarding de produção ainda pendente; não há prova atual de capacidades habilitadas |
| Campos de capacidades e condições privadas | Não consultados nesta etapa inicial | Continuam não verificados; não reaproveitar valores de setembro como leitura atual |

Nenhum formulário foi submetido, aceite marcado, dado fiscal/bancário preenchido,
chave revelada ou transação realizada. A conta voltou ao Dashboard de teste.
Identificadores de conta e documentos ficam fora do repositório. A origem exata
da assinatura exibida não foi identificada nesta consulta; seu ambiente de teste
foi confirmado. O ciclo sintético de setembro continua sendo evidência histórica
[separada](https://github.com/pradyumna-001/bancaemdia-api/blob/codex/stripe-118-validation/docs/validation/stripe-sandbox-2026-09-27.md).

O titular precisa concluir a verificação e os requisitos apresentados pela conta.
Depois, o operador registra privadamente a leitura de `charges_enabled`,
`payouts_enabled`, `details_submitted`, pendências vencidas/atuais e verificações
pendentes. `details_submitted` isoladamente não prova capacidade de receber ou
repassar; são campos distintos no [objeto Account](https://docs.stripe.com/api/accounts/object).
Não foi restabelecida a consulta preventiva ao suporte, dispensada pelo titular.

## Condições a conferir antes de definir preço

A [tabela pública brasileira](https://stripe.com/br/pricing), consultada nesta
data, anuncia cartões nacionais a 3,99% + R$0,39, adicional de 2% para cartões
internacionais e Billing a 0,7%. São referências do provedor, sem aprovação de
preço do produto ou confirmação de contrato particular. O operador deve conferir
tarifas efetivas, conversão, disputas, tratamento de tarifas após reembolso,
moedas de apresentação/repasse e cronograma específico da conta. Registrar
cotação e condições em evidência privada, sem copiar dados bancários para Git.

A documentação distingue frequência de repasse de disponibilidade dos fundos:
a conta brasileira usa repasses automáticos diários, mas isso não significa
liquidação imediata. Confirmar a condição concreta da conta nos
[repasses](https://docs.stripe.com/payouts). Cancelamento e devolução também são
ações distintas; a política comercial deve especificar ambas, incluindo falhas
e conciliação dos [reembolsos](https://docs.stripe.com/refunds).

## Solução fiscal: decisão preparada, ainda não aprovada

Stripe Tax recusou a conta brasileira no teste de setembro. A lista pública de
[países do estabelecimento suportados](https://docs.stripe.com/tax/supported-countries)
consultada agora não inclui Brasil. País do comprador e país do estabelecimento
não são intercambiáveis. A API standalone não contorna essa limitação.
Managed Payments também não inclui estabelecimento brasileiro na
[elegibilidade](https://docs.stripe.com/payments/managed-payments/eligibility).
Mantemos Stripe comum e a responsabilidade fiscal do vendedor.

| Caminho | Capacidade documentada | Decisão/evidência que falta | Trabalho técnico posterior |
| --- | --- | --- | --- |
| Escopo inicial restrito de mercados, com regras aprovadas pela assessoria e taxas manuais | Stripe oferece taxas inclusivas/exclusivas em Checkout e assinaturas; não decide a taxa aplicável | Assessoria definir mercados, incidência, registros, documentos e recolhimento; proprietário aprovar o alcance | Implementar taxas aprovadas/versionadas e provar criação, renovação, estorno e conciliação; adapter atual não implementa isso |
| Aplicativo fiscal de terceiro integrado à Stripe | Integração nativa documentada para Avalara/Anrok, com cobrança direta do fornecedor | Confirmar atendimento ao estabelecimento BR, mercados, custo e obrigações cobertas; escolher fornecedor somente após parecer e cotação | Validar compatibilidade com versão da API, Checkout/Billing, renovação, indisponibilidade e conciliação; nenhum aplicativo instalado |

As [taxas manuais](https://docs.stripe.com/tax/tax-rates) não automatizam a escolha
da jurisdição nem a declaração/recolhimento. A
[integração com terceiros](https://docs.stripe.com/tax/third-party-apps) admite
um provedor fiscal por conta e pode prosseguir com imposto zero quando o
fornecedor está indisponível; a renovação tem comportamento de retry próprio.
Esse comportamento exige política explícita, alertas e conciliação antes de
produção. A integração pública não comprova que um fornecedor aceite este
estabelecimento brasileiro, nem resolve emissão fiscal local. A assessoria deve
determinar a documentação aplicável, usando também o
[portal oficial NFS-e](https://www.gov.br/nfse/pt-br), sem presumir regime ou alíquota.

Proposta para a decisão: limitar o lançamento aos mercados que tenham tratamento
fiscal aprovado e ampliar após validar cálculo, recolhimento e documentos de cada
mercado. Não publicar uma promessa de cobertura global com imposto automático
desabilitado. A escolha e o custo de assessoria/fornecedor permanecem com o
titular; não foram contratados serviços ou criada entidade estrangeira.

## Pacote concreto para decisão do titular e assessoria

Preencher e aprovar em armazenamento privado. Nenhum campo abaixo recebe valor
comercial presumido pelo agente.

| Gate do manifesto | Responsável | Conteúdo mínimo da evidência |
| --- | --- | --- |
| `account_verification` | Titular/operador da conta | Onboarding concluído, capacidades atuais e pendências conferidas, país BR, data e responsável |
| `account_commercial_conditions` | Titular/operador da conta | Tarifas efetivas, FX, moedas de repasse, prazos, disputas e tratamento de tarifas/reembolsos |
| `market_and_currency_scope` | Titular | Lista explícita de países/segmentos de compradores e moedas de cobrança aprovados |
| `tax_calculation_and_collection` | Assessoria fiscal | Regime aplicável, incidência por mercado, responsável por cálculo, cobrança e recolhimento; fornecedor/método aprovado |
| `tax_reporting_and_invoicing` | Assessoria fiscal | Registros, declarações, documentos fiscais, responsável e procedimento de emissão/correção |
| `tax_failure_and_reconciliation` | Assessoria fiscal | Tratamento aprovado para falhas, imposto zero indevido, renovação, estorno e conciliação, com responsabilidades |
| `commercial_catalog` | Titular | Valor em unidades menores, moeda, frequência, versão e aprovação de cada preço; sem Prices de fixture |
| `terms_cancellation_refunds` | Titular | Termos, trial de 168 horas após confirmação do cartão, cancelamento, reembolsos, suporte e versão aceita |
| `owner_launch_authorization` | Titular | Autorização explícita de produção após as decisões e validações; não substituída por sandbox/CI |

Toda mudança de mercado, preço, método fiscal ou condição da conta exige renovar
a evidência afetada. O produto continua software de registro/análise de apostas
externas, sem receber apostas, depósitos, prêmios ou fundos destinados a apostas,
como documentado no ADR 025 e nas
[restrições da Stripe](https://stripe.com/br/legal/restricted-businesses).

## Verificação offline do pacote

Copiar `docs/validation/billing-preflight-template.json` para uma pasta privada
fora do Git. O template é intencionalmente pendente. Para cada decisão realmente
verificada, preencher `status=verified`, hash SHA-256 do documento privado,
`reviewed_on` em ISO e `reviewer_role` conforme a tabela. O hash é uma referência
ao documento guardado pelo responsável; não copiar documentos, identificadores,
URLs privadas ou segredos ao manifesto.

Com o projeto instalado no ambiente Python:

```powershell
python scripts/check_billing_preflight.py C:\pasta-privada\preflight.json
```

| Exit code | Estado | Uso |
| --- | --- | --- |
| 2 | `INVALID_INPUT` | Corrigir estrutura/acesso ao arquivo; nenhum conteúdo bruto ou detalhe privado aparece no erro |
| 1 | `INCOMPLETE` | Resolver os gates listados; resultado esperado para o template vazio |
| 0 | `OWNER_REVIEW_REQUIRED` | Pacote estruturalmente completo para conferência humana dos documentos reais |

O verificador usa somente arquivo local, sem rede/banco, e limita a entrada a
16 KiB. Rejeita campos extras, tipos coercíveis e referências malformadas;
capacidades de sandbox não passam pelo gate produtivo. A janela de evidência de
30 dias é uma convenção de atualização deste relatório, **não prazo fiscal,
regra da Stripe ou garantia de validade por 30 dias**. Datas futuras são recusadas.

O manifesto é uma autodeclaração: o programa não autentica o autor, confere o
documento pelo hash nem valida juridicamente o parecer. Portanto sempre retorna
`production_authorized=false`, inclusive com exit 0. Não configura o adapter,
cria Prices, instala aplicativos, inicia trial ou modifica `BillingRollout`.
O adapter atual aceita somente credenciais e objetos de teste; um pacote
completo não o transforma em implementação live.

Após as decisões humanas, implementar e testar o método fiscal escolhido,
preparar a habilitação live autorizada e validar configuração/integração no
ambiente efetivo. Até lá #89 permanece **Próximas issues**, com ações específicas
do titular/assessoria. Publicação desta preparação técnica não conclui o aceite
comercial nem permite classificar a issue inteira como Esperando revisão.
