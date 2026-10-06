# API de extração de bilhetes: decisão do administrador

**Situação em 22/09/2026.** O código envia a imagem e a legenda à Anthropic: cache Redis → Claude Haiku 4.5 → conferências de negócio → Claude Sonnet 5 quando necessário. O OCR local descrito no [plano](../adrs/HIGH_LEVEL_PLAN.md) **ainda não existe** nessa escada. A conta Anthropic já tem chave e créditos, mas este PR não cria chave nem altera cobrança. O proprietário autorizou até US$ 3 em testes, se necessários.

| Caminho | Preço publicado | O que falta verificar |
| --- | --- | --- |
| [Anthropic: Haiku 4.5 → Sonnet 5](https://platform.claude.com/docs/en/about-claude/pricing) (atual) | Haiku **US$ 1/5**; Sonnet **US$ 2/10** por 1 milhão de tokens de entrada/saída | Custo e acerto reais da escada atual. |
| [OpenAI: GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | **US$ 0,10/0,50** por 1 milhão de tokens de entrada/saída | Imagem e JSON estruturado são suportados; medir leitura de bilhetes e custo por imagem. |
| [Google: Gemini 3.1 Flash-Lite](https://ai.google.dev/gemini-api/docs/pricing) | **US$ 0,25/1,50** por 1 milhão de tokens de entrada/saída no nível pago | [Imagem e JSON estruturado](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite) são suportados; medir leitura. O nível pago não usa dados para melhorar produtos, segundo a tabela de preços. |
| OCR local ou [Mistral OCR 4](https://docs.mistral.ai/models/ocr-4-0) antes do modelo | OCR local: sem tarifa de API, mas consome infraestrutura; Mistral: **US$ 4/1.000 páginas** | OCR sozinho pode não interpretar aposta, mercado, seleção e contexto; medir necessidade de um modelo complementar. |

**Preço por token ou página não é custo por aposta correta.** Imagens podem consumir quantidades diferentes de tokens; também contam saída, repetição, escalonamento e revisão humana. A [referência histórica](../../src/bancaemdia/extracao/precos.py) de US$ 0,0054 por bilhete veio de 279 bilhetes limpos em junho/2026, com 20,4% de escalonamento; não compara provedores nem prevê a carga futura. O histórico da conta Anthropic registra gasto por modelo, mas não separa custo por aposta correta.

**Histórico informado pelo proprietário:** OCR já foi testado em outra etapa do projeto e descartado porque tornava a resposta lenta demais para o usuário. Esta informação deve entrar na avaliação caso alguém proponha retomá-lo.

Para decidir, propomos comparar os candidatos sobre o **mesmo conjunto autorizado e anonimizado de prints reais**, incluindo casas diferentes, vários cupons, texto pequeno, imagens ruins e legendas. Medir taxa de acerto nos campos críticos, aprovações incorretas, taxa de revisão, latência e **custo total por bilhete corretamente extraído**, com a mesma validação de negócio e eventual fallback. Limitar o piloto ao crédito de US$ 3 autorizado para Anthropic; testes pagos em outros provedores exigem orçamento próprio. A meta informada pelo proprietário para a operação inteira é cerca de **R$ 200/mês nos primeiros 100 usuários** e **US$ 300/mês com 1.000 usuários**, conforme a [decisão de infraestrutura](aws-initial-budget.md); é preciso avaliar IA e hospedagem juntas.

**Pedido ao administrador:** qual provedor e arquitetura de extração devemos adotar? Pode manter Anthropic, trocá-la, combinar provedores, usar OCR ou propor outra solução. Indique o piloto mínimo que considera suficiente, como tratar privacidade dos prints e qual custo estimado por aposta correta permite cumprir o orçamento total. Após essa decisão, podemos configurar as credenciais e implementar a integração escolhida.

---

## Decisão do administrador — 23/09/2026

**GO: lançar a v1 com a escada Anthropic atual (cache Redis → Haiku 4.5 → conferências de negócio → Sonnet 5) e aprovar o piloto de substituição do primeiro estágio.** A decisão foi verificada contra as fontes oficiais na data da decisão: Anthropic Haiku 4.5 US$ 1/5 e Sonnet 5 US$ 2/10 por MTok, OpenAI GPT-6 Luna US$ 0,10/0,50 por MTok com imagem e saída estruturada suportadas, e Mistral OCR 4 US$ 4/1.000 páginas estão conferidos no documento. O preço do Gemini 3.1 Flash-Lite (US$ 0,25/1,50) **não foi verificado** — a página de preços do Google não abriu; confirmar antes do piloto.

### Por que manter a Anthropic na v1

- **Pré-lançamento o custo é irrelevante.** Com poucos usuários reais, a escada atual custa centavos por dia. Trocar o provedor agora, sem dado comparativo de acurácia em prints de casas brasileiras, é otimização prematura com risco real à qualidade.
- **O primeiro estágio barato é inevitável, mas não urgente.** Na projeção de 100 usuários ativos (~15.000 bilhetes/mês), a escada atual custaria ~US$ 81/mês pelo histórico de US$ 0,0054/bilhete — acima do teto total de ~R$ 200/mês para toda a operação. A troca do estágio 1 não é "se", é "quando"; o piloto abaixo define o "quando" com dados.

### Piloto aprovado (GO com condições)

- **Escopo:** substituição do estágio 1 apenas (Haiku 4.5). Candidatos: **GPT-6 Luna** e **Gemini 3.1 Flash-Lite**. **Sonnet 5 permanece como escalonamento** — não mexer no que já valida.
- **Corpus:** o conjunto autorizado e anonimizado de prints reais descrito acima, incluindo casas diferentes, vários cupons, texto pequeno e imagens ruins. Nenhum print novo sem anonimização prévia.
- **Orçamento:** limitado aos US$ 3 autorizados para Anthropic; testes pagos em OpenAI/Google exigem orçamento próprio aprovado antes de começar.
- **Critérios de GO para o piloto (medidos sobre o mesmo corpus, mesma validação de negócio):**
  - Custo por bilhete corretamente extraído **≥ 4× menor** que o da escada atual;
  - Taxa de acerto dos campos críticos caindo **no máximo 2 p.p.**;
  - Taxa de aprovações incorretas **não pior** que a atual;
  - Latência por bilhete não pior que a atual de forma perceptível ao usuário.
- **Gatilho para executar o piloto:** antes de atingir **20–30 usuários ativos** ou **5.000 bilhetes/mês**, o que ocorrer primeiro. Abaixo disso, a Anthropic permanece.

### O que fica descartado agora

- **Mistral OCR 4 e OCR local:** descartados sem piloto. OCR sozinho custaria ~US$ 60/mês no volume projetado e ainda exigiria um modelo interpretando o texto; o OCR local já foi rejeitado pelo proprietário por latência. Reabrir apenas se os dois candidatos do piloto falharem nos critérios.
- **Combinação de provedores em produção:** descartada para a v1. Um único provedor por estágio mantém observabilidade, orçamento e rotação de chaves simples (rotação trimestral já prevista na issue #43).

### Privacidade

Antes de qualquer teste pago fora da Anthropic, confirmar por escrito a política de **não uso de imagens de clientes para treinamento** do provedor candidato (equivalente à garantia já citada para o nível pago do Gemini). Isso é requisito LGPD do produto e conecta com os limites de propriedade de dados tratados na issue #43. Prints fora do corpus anonimizado aprovado **não** podem ser enviados a nenhum provedor.

### Revisão desta decisão

Revisitar quando: (a) o piloto produzir resultados; (b) o preço de qualquer candidato mudar; ou (c) o gatilho de usuários/bilhetes for atingido sem piloto executado. A decisão de infraestrutura/hospedagem do PR #128 (pendente) pode alterar as premissas de custo total e deve ser lida junto com esta.
