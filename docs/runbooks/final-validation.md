# Validação final de staging (issue #44)

O comando `scripts/final_validation.py` **não implanta, não reinicia RDS e não dispara
alarmes**. Ele consolida resultados de máquina e evidências operacionais do mesmo SHA. Sai com
código 1 enquanto faltar qualquer item; código 0 significa **candidato a GO com evidências
registradas**, sujeito à revisão humana dos links. Não publica nem promove produção.

## Pré-condições

- Resolver a arquitetura/custo AWS do [PR #128](https://github.com/pradyumna-001/bancaemdia-api/pull/128)
  e o provedor de extração do [PR #129](https://github.com/pradyumna-001/bancaemdia-api/pull/129).
  A meta antiga de US$ 151/mês e US$ 0,0054/aposta da issue não substitui os limites aprovados
  para o lançamento. Registrar os dois tetos aprovados em dólares no manifesto, com link à decisão.
- Configurar staging espelhado na produção aprovada, com URL, credenciais de teste e telemetria.
  Separar um conjunto autorizado de **pelo menos 5 bancas e 16 mil apostas com eventos
  reprocessáveis**. Sem esse conjunto, o teste de integridade não atesta a meta.
- Marcar um único SHA de release e guardar os artefatos em `validation-evidence/` (ignorado por
  Git e Docker). Nunca colocar tokens, dados pessoais ou URLs com credenciais no manifesto.

## Evidência automática

1. No ambiente de staging, com `APP_ENV=staging` e `RELEASE_SHA` igual ao SHA implantado,
   executar:

   ```bash
   mkdir -p validation-evidence
   python scripts/conferir_numeros.py --todos --staging-gate --json-out validation-evidence/integrity.json
   ```

   A conferência é `dry_run` e falha com projeção alterada/ausente, lançamento a restaurar,
   erro de replay, menos de 5 bancas ou menos de 16 mil apostas reprocessadas. Copiar o JSON
   privado para a pasta de evidências do operador.

2. No checkout do mesmo SHA, executar
   `pytest tests/contract --junitxml=validation-evidence/contracts.xml` e guardar o link da
   execução CI. O validador exige casos de contrato sem falha; só aceita o skip conhecido do
   caso negativo do DELETE sem corpo, que o Schemathesis não consegue gerar.

3. Na janela de carga autorizada de staging, executar o perfil **all** (aprox. 55 minutos),
   com `APP_ENV=staging`, `RELEASE_SHA`, `BASE_URL` e tokens de teste configurados:
   `LOAD_PROFILE=all k6 run k6/load-test.js`. Guardar `load-test-summary.json`.
   O validador exige P95 < 1 s, falhas < 1%, todas as thresholds verdes e duração ≥ 50 min.
   O smoke local e o perfil `cd-smoke` não servem como substitutos.

## Evidência operacional e aprovação

Copiar [o exemplo de manifesto](final-validation-evidence.example.json) para
`validation-evidence/evidence.json`, preencher o SHA e os caminhos dos três artefatos. Para
cada item em `checks`, anexar um link HTTPS sem credenciais ou um arquivo relativo existente:

- Trilha upload → extração → materialização → painel; métricas customizadas; logs JSON
  pesquisáveis por `request_id` e `usuario_id`; alarme de teste recebido no Slack.
- Circuit breaker abre e recupera após 5xx do provedor escolhido; 429 com `Retry-After`;
  failover RDS com recuperação < 30 s. Injeção de falhas só em staging, na janela aprovada,
  com responsável e rollback disponíveis.
- RLS com zero linhas de outro usuário; JWT vencido e inválido com 401; CSP, HSTS e
  X-Frame-Options observados em staging.
- Deploy saudável < 10 min e rollback saudável < 5 min, medidos por URL de execução e
  observabilidade; zero bugs SEV1/SEV2 abertos; runbooks revisados; escala de plantão e
  aprovação do responsável registradas.
- Pelo menos 24 h de gasto real de staging e número de apostas processadas. Informar custo
  de infraestrutura e IA em USD e tetos aprovados. O script calcula infraestrutura/mês como
  `gasto / horas × 24 × 30` e IA/aposta como `gasto / apostas`; não transforma estimativa
  antiga em aprovação.

Por fim:

```bash
python scripts/final_validation.py --evidence validation-evidence/evidence.json --report validation-evidence/report.md
```

Se o relatório disser `NO-GO`, manter produção fechada e corrigir os itens FAIL.
Após `GO CANDIDATE`, revisar os links e obter o sign-off previsto no manifesto antes de
executar [o runbook de deploy](deploy.md).

**Estado em 23/09/2026:** o repositório ainda não tem variáveis nem ambientes GitHub de
staging configurados. Não existe medição real de 24 h, failover, alerta ou rollback para esta
issue. Portanto, a aceitação de produção permanece **NO-GO** até o provisionamento e os
ensaios da arquitetura aprovada.
