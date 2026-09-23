# Orçamento AWS e arquitetura inicial: revisão pelo administrador

**Contexto (22/09/2026):** as metas de gasto informadas pelo proprietário são cerca de **R$ 100/mês no começo**, **até R$ 200/mês nos primeiros 100 usuários** e **até US$ 300/mês com 1.000 usuários**. O administrador pode propor qualquer arquitetura para atendê-las ou explicar quais premissas precisariam mudar. Esta nota não autoriza provisionamento.

## Para que a AWS seria usada

Na implantação planejada, a AWS hospeda a API e os workers, PostgreSQL, Redis, arquivos enviados, backups, rede e monitoramento. Durante o desenvolvimento, o [plano original](../adrs/HIGH_LEVEL_PLAN.md) prevê execução local e **nenhum recurso AWS obrigatório**. O [Docker Compose](../../docker-compose.yml) já inicia API, PostgreSQL e Redis localmente; os workers podem ser iniciados no ambiente de desenvolvimento conforme os comandos do projeto.

## Divergência entre plano, issue e implementação

| Referência | Previsão |
| --- | --- |
| [Plano original](../adrs/HIGH_LEVEL_PLAN.md) | Desenvolvimento local; infraestrutura AWS de lançamento estimada em US$ 151/mês para 50 usuários. Mesmo seu mínimo de desenvolvimento na AWS era estimado em US$ 50/mês. O plano não promete produção na AWS por R$ 100/mês. |
| [Issue #41](https://github.com/pradyumna-001/bancaemdia-api/issues/41) | Pede staging semelhante a produção com RDS Multi-AZ `db.r6g.large`, réplica em outra região, Redis `cache.r6g.large`, NAT, ALB/WAF e 15 tarefas Fargate. Essas especificações são maiores que as da tabela de US$ 151/mês. |
| [Terraform atual](../../infra/terraform/README.md) | Cria dois NAT gateways, RDS Multi-AZ e duas réplicas, seis nós Redis, ALB e outros componentes em **cada ambiente**. `deploy_enabled=false` desliga apenas as tarefas ECS; banco, Redis e rede continuam cobrados. |

### Piso de custo do staging atual

Estimativa para `us-east-1`/`us-west-2`, preços sob demanda em 22/09/2026, 730 horas por mês, **sem tarefas ECS**:

| Componente | Cálculo | US$/mês |
| --- | ---: | ---: |
| Redis, 6 nós `cache.r6g.large` | 6 × US$ 0,206/h × 730 | 902,28 |
| RDS PostgreSQL, primário Multi-AZ e 2 réplicas `db.r6g.large` | (US$ 0,450/h + 2 × US$ 0,225/h) × 730 | 657,00 |
| NAT gateways, 2 | 2 × US$ 0,045/h × 730 | 65,70 |
| Application Load Balancer, 1 | US$ 0,0225/h × 730 | 16,43 |
| **Subtotal** | | **1.641,41** |

Fontes: listas públicas atuais da AWS para [ElastiCache](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonElastiCache/current/us-east-1/index.json), [RDS em us-east-1](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-1/index.json) e [RDS em us-west-2](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-west-2/index.json); tabelas de [NAT](https://aws.amazon.com/vpc/pricing/) e [ALB](https://aws.amazon.com/elasticloadbalancing/pricing/). A conta real será maior: armazenamento e backups, tráfego, processamento NAT/ALB, IPv4, WAF, segredos, logs e, quando habilitadas, tarefas ECS. Produção separada ampliaria o custo. Os US$ 100 de créditos da [conta Free Plan](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html) são **saldo total**, não orçamento mensal; alguns recursos podem nem estar disponíveis nesse plano.

## Há caminhos potencialmente mais baratos

Manter desenvolvimento e testes locais, sem `terraform apply` de staging ou produção, preserva o custo AWS em **R$ 0/mês**. Isso permite avançar no código, mas **não satisfaz** a aceitação operacional da issue #41 nem comprova failover, CD e carga em AWS.

Há também uma direção plausível para hospedagem econômica: a [AWS Lightsail](https://aws.amazon.com/lightsail/pricing/) oferece servidores Linux com 4 GB por **US$ 24/mês**, 8 GB por **US$ 44/mês** e 16 GB por **US$ 84/mês**; banco gerenciado começa em **US$ 15/mês**. Pela [PTAX de 21/09/2026](https://ptax.bcb.gov.br/ptax_internet/consultarUltimaCotacaoDolar.do), US$ 24 equivalem a cerca de **R$ 123** antes de impostos, backups, armazenamento adicional e IA. Assim, a hipótese de hospedar o início do projeto por menos que a stack Terraform é real. Também podem existir outras opções dentro ou fora da AWS.

O valor de uma instância **não prova** que a aplicação inteira caiba no teto nem que atenda 100 ou 1.000 usuários. Concentrar API, workers, PostgreSQL e Redis em poucos servidores muda disponibilidade, isolamento, backups e capacidade. É preciso dimensionar com carga representativa, requisitos de segurança, restauração e custos completos. Não extrapolar capacidade apenas pelo número de contas cadastradas.

## Pedido aberto ao administrador

Por favor, proponha a arquitetura e o cronograma que considere adequados para as metas de custo acima. Explique como executar API, workers, PostgreSQL, Redis, arquivos, backups e deploy; apresente custo mensal completo para o início, 100 e 1.000 usuários; indique quais garantias da issue #41 serão mantidas ou revistas e como validar capacidade e recuperação. Se alguma meta for inviável com os requisitos atuais, explique a incompatibilidade e recomende a mudança necessária. A escolha de serviços, provedor e etapas fica aberta.

Até essa revisão, `deploy_enabled=false` não deve ser tratado como modo econômico, e o Terraform atual não deve ser aplicado na conta com o orçamento informado.
