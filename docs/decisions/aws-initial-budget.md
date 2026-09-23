# Orçamento inicial de AWS: decisão pendente

**Contexto (22/09/2026):** o limite informado para AWS no início do projeto é de **R$ 100 por mês**. Esta nota não autoriza provisionamento.

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

## Caminho dentro do teto inicial

Manter desenvolvimento e testes locais, sem `terraform apply` de staging ou produção, preserva o custo AWS em **R$ 0/mês**. Isso permite avançar no código, mas **não satisfaz** a aceitação operacional da issue #41 nem comprova failover, CD e carga em AWS. Testes curtos na nuvem podem ser planejados depois, com custo estimado e duração definidos antes de iniciar e recursos desligados ao fim. Um ambiente AWS contínuo dentro de R$ 100/mês exigiria outra arquitetura e metas de disponibilidade; ainda não há desenho nem orçamento validados para ele.

## Decisão solicitada ao administrador

Escolher e registrar uma das direções antes de configurar `TF_APPLY_ENABLED` ou provisionar a stack:

1. **Manter a fase local**, com custo AWS zero, e adiar a aceitação de implantação da issue #41 até existir orçamento para staging e produção; ou
2. **Reformular a issue #41 para um piloto AWS de até R$ 100/mês**, definindo quais requisitos de alta disponibilidade, réplicas, workers, domínio e testes podem ser adiados, além do tempo em que o piloto ficará ligado; ou
3. **Manter a arquitetura da issue #41** e aprovar um orçamento compatível, após uma estimativa completa e um `terraform plan` na conta de destino.

Enquanto essa decisão estiver aberta, não tratar `deploy_enabled=false` como modo econômico nem aplicar o Terraform atual na conta AWS.
