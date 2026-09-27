# Billing inventory and acceptance ledger — 2026-09-27

This ledger is not a completion claim. Only payment scope of mixed issues is included.

Review units (all based on main): #150 → #89; #151 → #90; #152 → #91; #153 → #92; #154 → #93; #155 → billing #118. Consolidated #149 was withdrawn at the owner's request. #132 and #133 were closed as superseded; their branches remain available. The temporary combined branch is only an integration test fixture, never a merge/review replacement for these six PRs. No frontend implementation was added.

CI evidence: integrated commit 6d0cebdfcc1c169286340eb262370b64f9b8e994 passed full pytest/PostgreSQL, coverage threshold, mypy, Ruff, OpenAPI compatibility and Docker in [run 36337037802](https://github.com/pradyumna-001/bancaemdia-api/actions/runs/36337037802). Foundation PR #151 also passed its independent run 36337039564. Later changes require fresh evidence. Other PRs cannot pass independently until their prerequisite code reaches main. Documentation PR #150 inherits the baseline SQLAlchemy/instrumentation failures corrected in #151; it does not modify runtime code.

| Item | Inventory / disposition | Remaining acceptance evidence |
| --- | --- | --- |
| #89 / #132 | Mercado Pago preflight preserved verbatim as historical ADR appendix; superseded by Stripe ADR 025. No Mercado Pago or Asaas future adapter. | Account-specific approval/identity, fees/payouts and commercial/legal launch gates. |
| #90 / #133 | Reused d456bda foundation (models, catalog, trial guard, RLS, tests). Card requirement supersedes its original cardless behavior. Currency/cadence versions and empty commercial catalog retained. | Real PostgreSQL/CI, reviewed migration, rollout validation. |
| #91 | Stripe hosted Checkout/Billing, durable customer/checkout mapping, bounded-key ambiguity guard, portal/cancel APIs. | Real account sandbox, authenticated frontend deployment, approved commercial prices. |
| #92 | Raw-body signature, durable inbox, dedup, locked fresh remote reconciliation, retry/dead-letter, refund/payment/cancellation projection. | Real Stripe event/test-clock evidence and worker monitoring deployment. |
| #93 | API/collection/worker checks, SQL write trigger, preserved reads/export/cancel. | CI RLS/end-to-end evidence; compatibility with pending Telegram/titulares branches. |
| #118 billing only | PostgreSQL lifecycle slice plus deterministic Stripe contract tests. | Full billing scenario report and real sandbox supplement. Nonbilling requirements untouched. |
| #96 billing clause | Holder matrix/analytics must remain readable after trial expiry. No unrelated implementation imported. | Pending #136 must pass shared guard/RLS tests when integrated. |
| #113 search match only | Its “read-only” catalog projection concerns catalog permissions, not subscription access. Re-read the full issue and excluded it from billing scope. Collection/outbox billing behavior belongs to #93 and billing #118. | No #113 implementation or acceptance changes in this task. |
| #130 dependency | Only audit migration/model and owned-data export snapshot code extracted because #133 requires them. Other HTTP, infrastructure, JWT and cost changes are not included. | Coordinate #130 rebase after billing merges; shared migration identity retained. |
| #134–141 | Existing holders/Telegram stack depends on #133. Branches retained without rebasing/mixing their unrelated work here. | Integration/compatibility review is a separate unresolved dependency of end-to-end bot coverage. |
| frontend | Repository currently contains planning only; identity provider and app foundation are unimplemented. The owner explicitly deferred frontend implementation to its own issues; API provides generated contracts and billing handoff only. | Authenticated host integration and mobile/desktop browser evidence. |

No issue should close automatically from this PR. Sandbox, PR opened or CI green is not operational completion. Keep #89–93 and billing criteria of #118 open until each remaining cell is resolved.
