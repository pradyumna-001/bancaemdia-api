# Validação final da Fase 1 (issue #44)

`scripts/final_validation.py` consolida evidências; não implanta nem executa ensaios. Um relatório
`GO CANDIDATE` exige revisão humana dos links e aprovação operacional. O exemplo de manifesto
começa em `NO-GO` e corresponde à [decisão AWS](../decisions/aws-initial-budget.md): Lightsail
com Compose, backup diário, snapshot semanal, restore testado e limite de R$ 200/mês. O antigo
modo de staging ainda existe no validador para a Fase 2, mas não condiciona o lançamento inicial.

## Preparar evidências

1. Fixar um SHA de release e o digest da imagem. Guardar os artefatos em `validation-evidence/`
   (ignorado pelo Git e Docker). Não colocar tokens, dados pessoais ou links com credenciais.
2. Em um banco local autorizado com pelo menos 5 bancas e 16 mil apostas reprocessáveis, usar
   `APP_ENV=testing` e `RELEASE_SHA` igual ao SHA fixado e executar:

   ```bash
   python scripts/conferir_numeros.py --todos --phase1-gate --json-out validation-evidence/integrity.json
   ```

   O comando é `dry_run` e falha com divergências, erro de replay ou amostra insuficiente.
3. No mesmo SHA, executar os contratos com
   `pytest tests/contract --junitxml=validation-evidence/contracts.xml` e registrar o link da CI.
4. Executar o perfil `LOAD_PROFILE=all k6 run k6/load-test.js` localmente com `APP_ENV=testing`,
   `RELEASE_SHA` e tokens de teste. Guardar `load-test-summary.json`. O validador exige P95 < 1 s,
   falhas < 1%, thresholds verdes e duração de pelo menos 50 minutos. Registrar hardware, número
   de usuários, bancas e apostas para que a capacidade seja interpretável.
5. Em uma janela autorizada na instância Lightsail, medir o deploy e rollback do **mesmo digest**
   da imagem testada. Verificar `/ready`, TLS, RLS, JWT, cabeçalhos, 429 com `Retry-After`,
   rastreamento, métricas, logs, circuit breaker, alarmes e ausência de SEV1/SEV2. Não fazer
   teste de falha de produção sem plano de recuperação.
6. Anexar evidência real de backup diário, restore em ambiente descartável, snapshot semanal,
   acesso privado ao bucket e recebimento do alerta AWS Budgets de US$ 50. Registrar responsável,
   plantão e revisão dos runbooks. O [runbook Lightsail](lightsail-phase1.md) detalha a operação.
7. Após pelo menos 24 horas de operação, registrar custos reais de infraestrutura e IA em USD,
   apostas processadas, câmbio usado com fonte e evidência da aprovação do teto de R$ 200/mês.
   O validador projeta `(infra + IA) / horas × 24 × 30 × câmbio` e exige até R$ 200.

Copiar [o exemplo](final-validation-evidence.example.json) para
`validation-evidence/evidence.json`, preencher campos e links HTTPS sem credenciais ou arquivos
relativos existentes. Executar:

```bash
python scripts/final_validation.py --evidence validation-evidence/evidence.json --report validation-evidence/report.md
```

Enquanto algum item faltar, manter `NO-GO`. A decisão do administrador aprova a **arquitetura**
da Fase 1, não atesta que a instância já foi criada, testada ou autorizada a receber dados.
