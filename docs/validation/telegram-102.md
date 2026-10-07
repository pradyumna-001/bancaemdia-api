# Evidência da issue #102

## Base e dependências verificadas

A revisão consolidada da parte 3 reúne #97–#102, originalmente entregues em
#137–#141 e #162. O novo PR aponta diretamente para `main`; os PRs históricos
mantêm seus registros. O pré-requisito é a parte 2 (#177), no SHA
`8d727d2fd1acb941552b49d0d60829cf3df9c3ce`. Esta branch contém esse commit
para testar a composição completa sem aplicar migrations ou patches artificiais
nas fixtures. O administrador integrou #163, #168 e #177 em 06/10/2026;
a base aceita `78b322ad7a2b27eae782d89404f85e7b30021864` já contém esses
pré-requisitos, incluindo a identidade OIDC. Esta composição concilia o bot
com essa base, sem depender de outro merge dessas entregas.
Não houve merge de PRs nem alteração de `main`.

`h3review2026` converge as pontas publicadas `h2review2026` e `b102hard2026`,
reinstala os guards nos módulos agora presentes e aplica a exceção restrita de
retenção. Os IDs e pais publicados foram preservados. A convergência adicional
`j168base2026`/`j3main2026` une essa cadeia à main aceita; a ponta atual da
parte 3 é `j3main2026`. A suíte obrigatória valida
a migration de produto real, sem importar uma migration antiga por variável de
ambiente e sem modificar funções SQL dentro de fixtures.

Contas padrão usam exclusivamente a data/hora do jogo. A extração `quando` já
representa esse relógio e agora alimenta explicitamente `data_jogo` no rascunho.
Sem esse dado, o bot pede `jogo=...` antes de confirmar. Multicontas preserva a
conta explicitamente escolhida e valida usuário/casa; não substitui uma referência
inválida por conta padrão. A data da aposta continua separada nos registros.

Celery preserva os módulos de billing e Telegram e as três agendas:
`billing-reconcile` (60 s), `telegram-transport-tick` (5 s),
`telegram-privacy-purge` (3.600 s). Uma assinatura expirada pausa a leitura da
foto sem consumir IA nem destruir o rascunho; restabelecer o acesso permite
retomar a leitura, ainda exigindo confirmação explícita para gravar a aposta.

Os seis pontos do comentário do administrador em #162 estão ligados à matriz
abaixo. O segredo é verificado no endpoint pelo header Telegram, com comparação
constante; não há segredo em regras Prometheus. Alertas têm regras e testes de
disparo/recuperação; carregá-los e configurar segredos no ambiente definitivo são
ativações de lançamento. O vínculo/revogação #97 está incluído nesta parte.

## Matriz requisito → comportamento anterior → entrega → prova

Todos os nomes abaixo pertencem a `tests/integration/test_telegram_hardening.py`,
salvo indicação. O job produz JUnit do fluxo completo com os guards de billing instalados. Infraestrutura ausente é falha, não skip.

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
| Revogação e billing | Entrada bloqueada; trabalho já enfileirado precisava tratamento | `test_unlink_cancels_draft_and_stops_claimed_extraction_and_delivery`, `test_subscription_denial_preserves_cancellation_and_purge`, `test_expired_photo_pauses_without_provider_and_resumes_after_payment`; nenhuma remoção dos gates existentes |
| Ausência de vazamento | Cliente suprimia instrumentação; causas de exceção e cache exigiam proteção | Cache privado criptografado/TTL 60 s; causas externas suprimidas, token redigido também no logger; sentinelas em `test_private_cache_and_transport_diagnostics_do_not_leak` e `tests/unit/test_telegram_privacy.py` para logs, traces, relatório de exceção, métricas e DLQ |
| Migrations e rollback | Histórico publicado preservado | `test_migration_roundtrip_preserves_prerequisite_draft`: banco descartável separado, upgrade, downgrade recusado com mídia, downgrade seguro e reupgrade preservando rascunho |
| Alertas e runbooks | Métricas básicas existentes; faltavam limiares e procedimentos completos | `monitoring/telegram-alerts.yml`, testes promtool de disparo/recuperação, runbook canônico e política de privacidade |

## Execução

Windows/Python 3.12 valida Ruff, formato, mypy e unidade. Não há Docker local;
PostgreSQL 16 portátil valida localmente vínculo, transporte, rascunho, foto e
confirmação; a CI Linux com PostgreSQL 16/Redis 7 comprova a jornada completa. O workflow mantém
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
AWS, cobrança ou webhook produtivo. Não há autorização de merge. Não há implementação da parte 4, frontend ou extensão nesta revisão.
