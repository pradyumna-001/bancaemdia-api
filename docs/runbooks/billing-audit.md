# Billing inventory and acceptance ledger — 2026-09-27

This ledger is not a completion claim. Only payment scope of mixed issues is included.

| Item | Inventory / disposition | Remaining acceptance evidence |
| --- | --- | --- |
| #89 / #132 | Mercado Pago preflight preserved verbatim as historical ADR appendix; superseded by Stripe ADR 025. No Mercado Pago or Asaas future adapter. | Account-specific approval/identity, fees/payouts and commercial/legal launch gates. |
| #90 / #133 | Reused d456bda foundation (models, catalog, trial guard, RLS, tests). Card requirement supersedes its original cardless behavior. Currency/cadence versions and empty commercial catalog retained. | Real PostgreSQL/CI, reviewed migration, rollout validation. |
| #91 | Stripe hosted Checkout/Billing, durable customer/checkout mapping, bounded-key ambiguity guard, portal/cancel APIs. | Real account sandbox, authenticated frontend deployment, approved commercial prices. |
| #92 | Raw-body signature, durable inbox, dedup, locked fresh remote reconciliation, retry/dead-letter, refund/payment/cancellation projection. | Real Stripe event/test-clock evidence and worker monitoring deployment. |
| #93 | API/collection/worker checks, SQL write trigger, preserved reads/export/cancel. | CI RLS/end-to-end evidence; compatibility with pending Telegram/titulares branches. |
| #118 billing only | PostgreSQL lifecycle slice plus deterministic Stripe contract tests. | Full billing scenario report and real sandbox supplement. Nonbilling requirements untouched. |
| #96 / #113 billing clauses | Read-only matrix/analytics and collection/outbox behavior must consume common access contract. No unrelated implementation imported. | Their pending implementations must pass shared guard tests when integrated. |
| #130 dependency | Only audit migration/model and owned-data export snapshot code extracted because #133 requires them. Other HTTP, infrastructure, JWT and cost changes are not included. | Coordinate #130 rebase after billing merges; shared migration identity retained. |
| #134–141 | Existing holders/Telegram stack depends on #133. Branches retained without rebasing/mixing their unrelated work here. | Integration/compatibility review is a separate unresolved dependency of end-to-end bot coverage. |
| frontend | Repository currently contains planning only; identity provider and app foundation are unimplemented. Billing feature can be prepared independently against generated API types. | Authenticated host integration and mobile/desktop browser evidence. |

No issue should close automatically from this PR. Sandbox, PR opened or CI green is not operational completion. Keep #89–93 and billing criteria of #118 open until each remaining cell is resolved.
