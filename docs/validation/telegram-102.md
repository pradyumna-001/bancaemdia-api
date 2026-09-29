# Evidência da issue #102

## Base e dependências verificadas

`origin/main` ainda não contém o fluxo do bot. Esta entrega usa o HEAD
`71ab6b591325387122a2315133ee7d32b4e4f0d6` do PR #141 e possui diff exclusivo
de hardening; não incorpora a fila inteira. #137–#141 continuam abertos, sem
revisões técnicas pendentes na consulta de 29/09/2026. A integração administrativa
da cadeia continua responsabilidade do administrador.

O job Telegram também carrega o contrato SQL de billing do HEAD
`66b80ad44b2ebdbb6de6559c0fa027308a075891` do #154 e repete a suíte com os
triggers reais. Esse ensaio testa especificamente a sobreposição: RLS, recusa de
confirmação de assinatura expirada, cancelamento permitido e purge sem liberar
alteração financeira. Não representa merge nem teste de todos os endpoints de billing.
O ensaio amplo do #155 permanece a referência para convergência dos routers e contratos.

Após integrar as duas cadeias, a migration de convergência deve executar
`scripts/telegram_billing_privacy.sql` **depois** das migrations de billing e do
bot. O script é idempotente, recusa um contrato desconhecido e permite somente
remoção de dados de rascunhos terminais. Não remove triggers nem permite aumentar
stake ou reabrir rascunho. `b102hard2026` já o executa quando billing estiver
presente antes dela. Na ordem inversa, executá-lo na convergência reproduz a ordem
testada pela CI. Preservar IDs e ancestralidade existentes.

Em Celery, conservar os módulos `billing`, `telegram` e `telegram_privacy`, as rotas
`billing.*`/`telegram.*` para materialização e as três agendas: `billing-reconcile`
(60 s), `telegram-transport-tick` (5 s), `telegram-privacy-purge` (3.600 s). O purge
é acrescentado com `beat_schedule.update`, sem substituir a agenda existente.
Se o administrador optar por squash/rebase dos pré-requisitos, renovar o ensaio
e a CI do novo HEAD antes do merge; a evidência não autoriza esse merge.

## Matriz requisito → comportamento anterior → entrega → prova

Todos os nomes abaixo pertencem a `tests/integration/test_telegram_hardening.py`,
salvo indicação. O job produz JUnit tanto do fluxo independente quanto do contrato
de billing. Infraestrutura ausente é falha, não skip.

| Requisito | Existente / lacuna | Implementação e evidência |
| --- | --- | --- |
| Jornada completa e campos faltantes | Worker e materializador existentes; stake explícita não era propagada | Modelo/mapeamento compartilhados preservam unidades declaradas; `test_complete_and_missing_field_e2e` (duas variantes), HTTP real, PostgreSQL restrito, Redis, API externa falsa, aposta/evento únicos e resposta comparada a centavos salvos |
| Encaminhamento, ilegível, múltiplos bilhetes, correção, cancelamento, reinício | Rascunho e leases existentes | `test_ambiguity_correction_cancel_and_resume`, encaminhamento na jornada e engine nova após persistência; zero aposta antes de confirmar |
| Replays/concorrência/rollback | Inbox e chave financeira existentes | Dez replays, consumidores concorrentes, respostas concorrentes; `test_financial_rollback_retries_one_event_and_one_bet`; testes de dez confirmações concorrentes do #101 preservados |
| Falhas externas e worker | Retry/outbox existentes | `test_delivery_outage_crash_and_recovery` (500/429/timeout), `test_extraction_timeout_and_abandoned_lease_recover`; `test_real_redis_celery_tick_consumes_durable_inbox` consome mensagem pelo broker real |
| Limites por chat/usuário/globais | Só vínculo tinha proteção parcial | Contadores PostgreSQL transacionais/HMAC; `test_shared_limits_atomic_expiry_and_tenant_separation` (quatro ações), `test_each_quota_dimension_is_enforced`; falha do backend mantém inbox recuperável |
| Fronteiras de confiança | Segredo e chat já validados; bytes só tinham assinatura superficial | Corpo lido com limite incremental e verificação completa de imagem; `test_http_rejections_do_not_enter_durable_inbox`, `test_invalid_media_cannot_be_confirmed`, `test_trust_boundaries_have_no_financial_or_draft_effect`; callbacks v1 sempre ignorados |
| Retenção/purge | Inbox processada apagava ciphertext; demais dados sem purge | `telegram.purge`, prazos/lotes configuráveis, tombstones de origem; `test_retention_preserves_pending_work_and_replay_tombstones`, duas execuções concorrentes, reexecução e replay pós-remoção |
| RLS e mídia | Rascunho/aposta isolados; bot usava blobs globais legados | Mídia nova criptografada/RLS/composite FK; proteção condicional para mídia antiga; `test_restricted_rls_blocks_valid_foreign_ids`, `test_legacy_photo_rls_and_purge` com tenant alheio e ausência de tenant |
| Revogação e billing | Entrada bloqueada; trabalho já enfileirado precisava tratamento | `test_unlink_cancels_draft_and_stops_claimed_extraction_and_delivery`, `test_subscription_denial_preserves_cancellation_and_purge`; nenhuma remoção dos gates existentes |
| Ausência de vazamento | Cliente suprimia instrumentação; causas de exceção e cache exigiam proteção | Cache privado criptografado/TTL 60 s; causas externas suprimidas, token redigido também no logger; sentinelas em `test_private_cache_and_transport_diagnostics_do_not_leak` e `tests/unit/test_telegram_privacy.py` para logs, traces, relatório de exceção, métricas e DLQ |
| Migrations e rollback | Histórico publicado preservado | `test_migration_roundtrip_preserves_prerequisite_draft`: banco descartável separado, upgrade, downgrade recusado com mídia, downgrade seguro e reupgrade preservando rascunho |
| Alertas e runbooks | Métricas básicas existentes; faltavam limiares e procedimentos completos | `monitoring/telegram-alerts.yml`, testes promtool de disparo/recuperação, runbook canônico e política de privacidade |

## Execução

Windows/Python 3.12 valida Ruff, formato, mypy e unidade. Não há Docker local;
somente a CI Linux com PostgreSQL 16/Redis 7 comprova integração. O workflow mantém
lint, formato, mypy estrito, contratos/OpenAPI contra `origin/main`, pip-audit,
Bandit, cobertura mínima 80% e Docker build. O relatório final do PR registra SHA,
links e resultados efetivos; esta matriz não é declaração de CI verde.

Comandos reproduzíveis:

```bash
pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy src/ --strict --ignore-missing-imports
pytest -n 8 --dist loadgroup --cov=src/bancaemdia --cov-fail-under=80 --junitxml=test-results.xml
pytest tests/integration/test_telegram_hardening.py -n 0 --junitxml=telegram-results.xml
python scripts/generate_openapi.py --check
pip-audit --local --skip-editable
bandit -r src/bancaemdia -q -lll
docker build -t bancaemdia-api .
promtool check rules monitoring/telegram-alerts.yml
promtool test rules monitoring/telegram-alerts.test.yml
```

Skips de réplica real dependem de hot standby e não comprovam essa infraestrutura.
Casos Schemathesis sem entrada podem não admitir geração negativa; autenticação
tem testes específicos. Nenhum cenário novo obrigatório permite skip.

## Limite operacional

A semântica financeira é idempotente por rascunho; a mensagem externa é pelo menos
uma vez. Produção depende do proprietário aprovar retenção, segredos e ativação
perto do lançamento. Validação usa dados sintéticos, não aciona Telegram real,
AWS, cobrança ou webhook produtivo. Não há autorização de merge. Não há trabalho
da #107, frontend ou extensão neste diff.
