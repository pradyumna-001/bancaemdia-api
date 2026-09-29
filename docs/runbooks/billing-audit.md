# Billing inventory and acceptance ledger — 2026-09-27

This is a criterion-by-criterion audit, not a completion claim. Production remains disabled. Only billing clauses of mixed issues are included.

## Review units and current integration strategy — 2026-09-29

The review units remain separate. #151 depends on the privacy/audit fixes in #145;
#152 uses #151, #153 uses #152, #154 uses #153, and this billing-only audit #155
uses #154. These are source and migration dependencies, not a claim that another
issue is delivered by the child PR. Original migration IDs and history remain intact;
new merge revisions reconcile independently published branches.

The holder/Telegram chain #134–141 now incorporates current #151. Its own PostgreSQL
checks run on every PR. Cross-flow acceptance also needs #141. Standard CI tests
#155's own HEAD and billing lifecycle; two modules requiring Telegram report that
missing prerequisite explicitly. The additional **Billing cross-flow (current HEAD
+ pinned Telegram)** job assembles the exact #155 HEAD with #141 commit
`71ab6b591325387122a2315133ee7d32b4e4f0d6` in a disposable checkout, applies the
reviewable `tests/fixtures/billing-cross-flow.patch`, regenerates OpenAPI and runs
the full suite. A final assertion requires all eight PostgreSQL/migration/Redis
scenarios to execute successfully: missing modules, skipped scenarios or an
unexpected merge conflict fail that job. It no longer checks out the historical
`ffd9984` snapshot as if that validated this HEAD.

Reproduce in a clean disposable checkout of this PR:

```sh
python scripts/assemble_billing_cross_flow.py --disposable-checkout
pip install -e '.[dev]'
python scripts/generate_openapi.py
BILLING_CROSS_FLOW_REQUIRED=1 pytest -n 8 --dist loadgroup --cov=src/bancaemdia --cov-fail-under=80 --junitxml=billing-integration.xml
```

The assembly does not create a merge candidate, commit, or push. The fixture patch
also records the predictable router, Celery schedule, schema and migration
resolutions for administrative integration. #155 remains blocked on review/merge
of its prerequisites; the administrator must not merge the test assembly as an
aggregate PR. After the prerequisite is an ancestor of HEAD, the assembly script
uses HEAD directly. The review state of #118 is separate: this PR covers billing
only and cannot close the broader expansion acceptance.

Historical #132/#133 were superseded by #150/#151; #149 was withdrawn. The old
cross-flow fixture remains historical evidence only. No frontend or extension
repository is modified by this audit.

## Acceptance audit

| Issue | Implemented and verified | Remaining acceptance |
| --- | --- | --- |
| #89 | ADR 025 selects Stripe Checkout + Billing + Portal. Brazilian establishment, public tariffs, international currencies and Managed Payments eligibility researched; BR is absent from Managed Payments establishment list. Product described truthfully. Owner waived the preventive support inquiry; no message sent. Real BR sandbox/country specs exercised. | Identity/capabilities and private fees/payout conditions; international tax solution (real Stripe Tax API rejected account country); commercial prices/currencies, terms and explicit production authorization. No claim of provider-issued approval. |
| #90 | Reused foundation; provider-neutral states and FULL_WRITE/READ_ONLY decision; immutable lifetime trial identity, card-confirmed bounds, exact 604800 seconds including DST; no reset through deletion/recreation/resubscription; RLS/audit; versioned integer-minor-unit currency/cadence catalog and existing subscription terms; unpublished catalog denies checkout. | Administrator review and migration/rollout in the actual target environment. Commercial catalog intentionally empty because the owner answered “a definir”; that is not a code failure or an invented price. |
| #91 | Test-only adapter, hosted Checkout, durable customer/subscription/session mapping, stable idempotency and ambiguous-operation guard, fresh remote card/setup/Checkout ownership before first grant, exact trial boundary, configured Price verification, sanitized public contracts/logs, customer Portal and idempotent cancellation. Actual hosted sandbox flow passed. Redirect grants nothing. | Prerequisites reviewed into main, authenticated subscription screen in the separately owned frontend scope, deployment and commercial/production decisions. Live keys/objects remain rejected. |
| #92 | Raw-body signature and timestamp checks; unique durable inbox before ACK; no raw personal/payment payload; current remote state under subscription lock; duplicate/reverse-order convergence; payment failure/cancel/refund projection; bounded retry/eight-attempt dead letter; missed-event repair; real Redis/Celery beat/worker; aggregate metrics and alert firing/recovery contracts. Actual signed sandbox events and Billing test-clock lifecycle passed. | Public endpoint, worker and monitoring deployment; actual delivery to the approved operator receiver; reviewed integration. Local Stripe CLI forwarding is not deployed infrastructure. |
| #93 | Shared authenticated mutation dependency, token collection check, AI/materialization rechecks and fresh-time SQL guards cover API, workers, replay/CLI and batches. Stable 402; administrative account state independent. Login/read/analytics/billing/cancel/export preserved. Telegram denial ACK has no business write/retry/DLQ; paused photo survives expiry and resumes after recovery. Extension retains unacknowledged capture buckets. | Review/merge of separate billing and existing feature PRs, environment rollout and frontend UX in its own scope. No remaining unimplemented bot/holder billing hook is being deferred. |
| #118 billing only | Exact trial/cutoff, dedup/order, configuration, cancellation/payment retry/refund, retry/dead letter, missed notifications, RLS/export, real worker schedule, Telegram draft/photo/confirmation, collection recovery, extension outbox and both migration orders tested. | Required review/integration and operational staging evidence. Nonbilling holders, calculators, reader coverage, pairing/matching and other expansion criteria remain outside this task. |
| #96 billing clause | Actual holder routes preserve list/matrix/financial/export reads after expiry; create/edit/delete deny with 402; other-tenant read remains 404. Database holder/history guards install in either migration order. | Reviewed integration/rollout. No claim of completing unrelated holder acceptance. |
| #113 search match | Full issue re-read: its read-only catalog projection concerns catalog permissions, not subscription access. | Excluded from billing scope; no unrelated implementation or acceptance changed. |

Open-issue inventory was refreshed after the implementation: only #89–93, #96, #113 and #118 matched billing/payment/trial/subscription searches. The exclusions above are deliberate.

## Historical evidence (not current-HEAD acceptance)

- Combined source `4bf08f9e98dc45d46ced4303831646a5d43e3361`, [CI 36349313261](https://github.com/pradyumna-001/bancaemdia-api/actions/runs/36349313261): 1,877 collected, 1,866 passed, zero failures/errors, 11 skips, 91.84% coverage. Ruff, format, mypy, generated OpenAPI and alert syntax also passed. The five cross-flow PostgreSQL tests and real Redis/Celery runtime test passed, not skipped. Skips: seven Schemathesis cases without negative inputs, three replica-only hot-standby tests, one pre-existing legacy panel comparison.
- Final fixture `ffd9984f8665daecedd45fdbe047484240842259`, [CI 36350126677](https://github.com/pradyumna-001/bancaemdia-api/actions/runs/36350126677): **1,881 collected, 1,870 passed, zero failures/errors, the same 11 documented skips, 91.83% coverage**. Both disposable-database migration orders, both collection aliases, all five cross-flow cases and real Redis/Celery execution passed. Ruff, format, mypy, OpenAPI and alert syntax passed. No billing scenario was skipped.
- Foundation #151 head `48a9873`: independent [CI 36347261219](https://github.com/pradyumna-001/bancaemdia-api/actions/runs/36347261219) passed. That result applies only to the historical SHA. Current PR states and exact HEAD checks are recorded in each PR description; combined CI never waives standalone checks.
- Existing holder/Telegram stack #134–140: latest CI runs 36349374248, 36349378034, 36349379322, 36349382798, 36349384360, 36349386615 and 36349390160 passed. #141 source `2313195`: [CI 36350092731](https://github.com/pradyumna-001/bancaemdia-api/actions/runs/36350092731) passed every job including full pytest, dedicated Telegram PostgreSQL, security, OpenAPI and Docker. Its isolated fault-injection fixtures substitute the new billing DB boundary; actual access checks are covered by the combined real-PostgreSQL suite.
- Alert semantics at #153 source `6083734`: [CI 36349474485](https://github.com/pradyumna-001/bancaemdia-api/actions/runs/36349474485) passed all five promtool scenario groups. Firing, recovery and missing metrics are covered; no external notification was sent.
- Extension source `96e60ed`: [CI 36349211519](https://github.com/wfcgit-hub/bancaemdia-extension/actions/runs/36349211519) passed repository validation and all eight Node tests, including three tests executing the real service-worker outbox flow.
- Actual Stripe sandbox assertions and their limits: [2026-09-27 report](../validation/stripe-sandbox-2026-09-27.md). No secrets, card data or customer identifiers are included.

## Outstanding external decisions and access

Repository and `staging` environment variable/secret **names only** were checked: no CD deployment variables or staging secrets are configured. The only recorded staging deployment failed and has no environment URL; no AWS CLI/config/credentials or applied Terraform configuration was found locally. The owner replied that no known staging exists. The administrator's [2026-09-23 budget decision](../decisions/aws-initial-budget.md) explicitly keeps the current phase local, postpones production-like staging, and prohibits applying the current Terraform stack. Public deployment/alert delivery therefore remain launch prerequisites, not a request to build an unapproved staging stack. The documented launch direction is Lightsail/Compose/Caddy after the release decision.

The owner has explicitly left commercial prices undecided and frontend implementation to its own repository. These choices are preserved. Account identity/required capabilities, tax treatment and actual production authorization remain owner-controlled. No issue is closed automatically; PRs, sandbox and CI are not substitutes for its remaining operational criteria.
