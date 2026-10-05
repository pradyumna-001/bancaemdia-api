# Part 6: infrastructure, analytics and executable launch evidence

This consolidation targets `main` directly. It incorporates the reviewed implementations
from #146, #148, #155, #156, #158 and #173, together with the intrinsic account,
collection, reconciliation and Telegram prerequisites from #177, #178 and #180.
Those PRs overlap this diff; they are not its GitHub base. Review and merge one coherent
version of the shared code. Published Alembic revision IDs are preserved and converge
at `h6review2026`. Authentication feature #168 remains a separate review; this change
uses the current backend JWT contract and does not choose/provision an identity provider.

The administrator's [summary #176](https://github.com/pradyumna-001/bancaemdia-api/pull/176)
is evidence of their review priorities, not an implementation dependency. Its older
repository snapshot deletes current backend files; those deletions are not adopted.
Original judgments and historical PR mappings stay in the external 94-PR review inventory.
CI results and pending work must be evaluated against this consolidation's actual HEAD.

| Source | Integrated behavior and acceptance |
| --- | --- |
| #146 | Single-host Lightsail preparation; exact snapshot OIDC trust and instance policy; private metrics, sanitized edge logs, Python healthcheck, backup/restore and digest rollback checks. |
| #148 | 16k-bet, 28k-event replay and 200 authenticated HTTP request benchmark; five banks; P95 below 500 ms and refresh freshness below 30 s. |
| #155 | Eight PostgreSQL/Redis billing lifecycle scenarios and both goal migration orders run on the actual product tree. Temporary assembly/patches are removed. This is partial evidence for broad issue #118. |
| #156 | Four Decimal calculators and independent golden examples through the API. |
| #158 | Tenant-private preferences, advanced analytics and audited goals use the canonical financial projection; upgrade orders and rollback retain privacy/billing guards. |
| #173 | Real API, workers, JWKS, PostgreSQL primary/standby and Redis; current installation tokens and game-date account usages; 100-user collection preflight and the complete four-profile load run. |
| #176 | Reconcile current review/launch status with source judgments; preserve the review history without importing its outdated code snapshot. |

Default account attribution uses the game date. Explicit multicontas keeps the actual
valid owner account. Placement, ingestion and capture clocks never provide a default
account. Confirmed `telegram_bot` sources now participate in reviewed consolidation,
analytics and goals; the database source guard and replay use the same contract.

## Required evidence on the final HEAD

All applicable GitHub checks must pass, including full tests/coverage, Ruff, mypy,
OpenAPI contracts (83 operations, 32 request media bodies), security/dependency scans,
Docker, Terraform validation, collection integrity, Telegram acceptance, both billing
integration jobs, Phase 1 recovery/edge acceptance and real k6 staging. The load job
does not substitute the mock/smoke job for its full run or suppress regression failures.
The final delivery records the commit, runs, JUnit and load measurements externally.

## Remaining decisions and launch dependencies

**Esperando revisão** means the technical delivery and applicable CI are complete and
the administrator's review is pending. **Esperando lançamento** means only activation
or evidence requiring the real launch environment remains. Remaining code, tests,
integration or available evidence belong to **Próximas issues**.

The #114 package of 11 synthetic reader fixtures (33 files) remains outside Git pending
administrator review. Digest: `4df5308bfc391564e6e2cb0e6ace49e29f7194a640f5c74ce53aec55353de601`.
It is not consumed by the k6 cache. No reader approval or complete end-to-end #118
acceptance is inferred from synthetic backend/billing runs.

AWS account, domain, reviewed cost/plan, apply, real bucket access, snapshot activation,
real disaster recovery, production alerts, paid provider setup and launch GO remain
operator/administrator launch dependencies. Local/CI staging cannot certify the whole
scope of #40, #41 or #44 on production infrastructure. Nothing here applies Terraform,
merges PRs, contacts real Telegram/payment services or releases production traffic.
