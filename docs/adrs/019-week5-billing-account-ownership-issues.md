# Week 5: Billing & Account Ownership — Milestone & GitHub Issues

## Milestone: **Week 5 — Billing & Account Ownership**

**Target Date**: 7 days from Week 4 completion

**Depends on**: authentication/RLS (#24), aposta lifecycle (#27), caixa (#28), revisão (#29), and export/hardening (#43)

**Success Criteria**:
- [ ] Provider eligibility is proven before payment integration is enabled
- [ ] Every user receives exactly seven days without a card
- [ ] Expired users retain reads/analytics/export but cannot mutate through API or workers
- [ ] Price remains configurable and unpublished until the owner decides it
- [ ] Holders and their bookmaker accounts have stable identities and temporal usage history
- [ ] Version 1 permits one account in use per bookmaker without blocking future concurrency
- [ ] Bets are assigned by occurrence time or sent to review; no “first active account” fallback remains
- [ ] Profit, turnover, ROI, and exposure reconcile by account and holder

---

## GitHub Issues (8 issues)

### Issue 1: Billing Provider Preflight — Mercado Pago PF + Asaas Fallback
**Labels**: `week-5`, `billing`, `payments`, `decision`

**Size**: M (3-4 hours)

**Files**:
- `docs/adrs/025-billing-provider.md`
- `docs/runbooks/billing-provider-preflight.md`

**Tasks**:
- [ ] Describe the product truthfully to each provider: subscription SaaS for betting recordkeeping and analytics; never receives wagers, deposits, prizes, or customer funds
- [ ] Verify in writing whether a Brazilian individual account (CPF, no CNPJ) may use recurring subscriptions in production
- [ ] Validate production application/credential eligibility and the `/preapproval` flow with Mercado Pago before integration work starts
- [ ] Record Mercado Pago fees, settlement, cancellation, refund, chargeback, and prohibited-business constraints as of the decision date
- [ ] If Mercado Pago rejects or cannot confirm the business, run the same preflight for Asaas and select it through the same provider-neutral contract
- [ ] Store evidence links, support protocol numbers, decision date, and explicit `GO|NO_GO`; sandbox access alone is not approval
- [ ] Keep price and launch date out of this ADR; both are later configuration

**Acceptance**: Written provider classification plus verified CPF production eligibility yields a documented `GO`; otherwise the ADR records `NO_GO` and the approved fallback before payment code is enabled

---

### Issue 2: Billing Foundation — 7-Day Trial, Subscription State & Configurable Price
**Labels**: `week-5`, `billing`, `domain`, `database`, `catalog`

**Size**: L (6-8 hours)

**Files**:
- `src/bancaemdia/models/assinatura.py`
- `src/bancaemdia/domain/billing.py`
- `src/bancaemdia/domain/billing_catalog.py`
- `src/bancaemdia/repositories/assinatura_repo.py`
- `src/bancaemdia/models/billing_price.py`
- `src/bancaemdia/config.py`
- `alembic/versions/xxx_billing_domain.py`
- `alembic/versions/xxx_billing_catalog.py`
- `tests/unit/test_billing_domain.py`

**Tasks**:
- [ ] Model internal access independently of the provider: `TRIALING`, `ACTIVE`, `PAST_DUE`, `CANCELED`, `EXPIRED`
- [ ] Give every user one lifetime seven-day trial without requiring a card
- [ ] New users start at account creation; existing users receive seven full days from the billing rollout timestamp
- [ ] Persist trial bounds, current period, provider/customer/subscription references, and last reconciliation time
- [ ] Use one domain function to return `FULL_WRITE` or `READ_ONLY` from trusted server time
- [ ] Prevent login recreation, provider-customer recreation, or resubscription from granting a second trial
- [ ] Apply RLS and append audit events for every access transition
- [ ] Keep price, cadence, and provider-specific statuses outside the access decision
- [ ] Represent one billable product without inventing Free/Pro/Team tiers
- [ ] Configure amount, currency, frequency, effective dates, and provider plan reference outside source code
- [ ] Allow the catalog to remain unpublished while price is undecided; unpublished catalog cannot create checkout
- [ ] Version price changes so existing subscriptions retain agreed terms unless explicitly migrated
- [ ] Validate positive integer centavos, `BRL`, supported cadence, and exactly one active public price
- [ ] Expose a provider-neutral read model for a future client without implementing frontend here

**Acceptance**: Clock-controlled tests prove exactly seven cardless days per user, deterministic transition to `READ_ONLY`, no repeat trial, and no dependency on provider-specific values; price can be set or changed without a deploy, no guessed tier exists in code, and checkout stays unavailable until a catalog entry is published

---

### Issue 3: Approved Billing Provider Adapter — Recurring Checkout + Customer Mapping
**Labels**: `week-5`, `billing`, `payments`, `integration`

**Size**: L (6-8 hours)

**Files**:
- `src/bancaemdia/integrations/billing/base.py`
- `src/bancaemdia/integrations/billing/{mercado_pago|asaas}.py`
- `src/bancaemdia/api/v1/billing.py`
- `tests/integration/test_approved_provider_billing.py`

**Tasks**:
- [ ] Implement only the provider for which Issue 1 records a production `GO`: Mercado Pago first, or Asaas if Mercado Pago is `NO_GO`
- [ ] Define a provider interface for customer, subscription, status, cancellation, and hosted payment URL
- [ ] Map each internal subscription to one provider customer/payer and subscription reference with idempotency keys; use `/preapproval` only when Mercado Pago is the approved provider
- [ ] `GET /api/v1/billing/status` returns trial/access/subscription state and catalog data
- [ ] `POST /api/v1/billing/subscribe` returns a hosted provider URL; card data never crosses this API
- [ ] Guarantee that no charge is taken before the seven free days end; if the approved provider cannot schedule that safely, delay provider checkout/activation rather than shorten or condition the trial
- [ ] Read amount/frequency only from the active catalog
- [ ] Redact credentials, payer data, and hosted URL secrets from logs
- [ ] Keep the non-selected provider replaceable without changing domain models or public endpoint contracts

**Acceptance**: For the provider approved in Issue 1, contract tests create exactly one subscription per idempotency key, use configured pricing, expose no card/secrets, and prove the first possible charge occurs only after seven complete cardless days

---

### Issue 4: Billing Webhooks — Idempotency, Retry & Reconciliation
**Labels**: `week-5`, `billing`, `webhook`, `idempotency`

**Size**: L (5-6 hours)

**Files**:
- `src/bancaemdia/api/v1/billing_webhook.py`
- `src/bancaemdia/models/billing_event.py`
- `src/bancaemdia/repositories/billing_event_repo.py`
- `src/bancaemdia/workers/billing.py`
- `tests/integration/test_billing_webhook.py`

**Tasks**:
- [ ] Validate provider signature/request identity from the raw request before business parsing
- [ ] Persist provider + event/resource identity under a unique constraint before enqueueing
- [ ] Acknowledge duplicates as no-op; never grant access from a checkout redirect
- [ ] Fetch current remote subscription/payment state instead of trusting event order
- [ ] Map authorized, pending, failed, paused, canceled, and refunded outcomes into the internal state machine
- [ ] Retry transient failures with bounded backoff and a dead-letter path
- [ ] Reconcile local active subscriptions with the provider and record auditable corrections
- [ ] Retain the minimum payload needed for audit and discard unnecessary personal/payment data

**Acceptance**: Duplicate and out-of-order notifications converge exactly once; forged callbacks/redirects grant nothing; reconciliation repairs a missed event

---

### Issue 5: Access Enforcement — Trial Expiry, Read-Only Mode & Export
**Labels**: `week-5`, `billing`, `access-control`, `export`

**Size**: L (5-6 hours)

**Files**:
- `src/bancaemdia/api/deps.py`
- `src/bancaemdia/domain/access.py`
- `src/bancaemdia/workers/`
- `tests/integration/test_read_only_access.py`

**Tasks**:
- [ ] Centralize a `require_write_access` dependency used by every mutating API
- [ ] Cover apostas, caixa, revisão, uploads, Telegram, coleta, titulares, settings, and future mutations
- [ ] Re-check access inside asynchronous workers before materialization
- [ ] Preserve login, owned-data reads, analytics, billing, cancellation, and the existing LGPD export after expiry
- [ ] Reject new mutation/ingestion with stable `402 account_read_only`; extension retains its outbox and Telegram explains the block
- [ ] Keep administrative `usuarios.ativo` independent from paid access
- [ ] Block mutations through replay/user CLIs, batches, and queued retries as well as ordinary endpoints
- [ ] Test RLS and export isolation for expired accounts

**Acceptance**: At the cutoff, all synchronous/asynchronous writes stop while the user can still sign in, inspect existing data, export it, and subscribe

---

### Issue 6: Titular Accounts — Models, Temporal Usage & Atomic Holder Switch
**Labels**: `week-5`, `models`, `database`, `temporal`, `transactions`

**Size**: L (6-8 hours)

**Files**:
- `src/bancaemdia/models/titular.py`
- `src/bancaemdia/models/conta_casa.py`
- `src/bancaemdia/models/uso_conta_casa.py`
- `src/bancaemdia/repositories/titular_repo.py`
- `src/bancaemdia/repositories/uso_conta_casa_repo.py`
- `src/bancaemdia/domain/titulares.py`
- `src/bancaemdia/api/v1/titulares.py`
- `alembic/versions/xxx_titulares_contas.py`
- `tests/integration/test_troca_titular.py`

**Tasks**:
- [ ] Add user-scoped `Titular` with display name and archive state; do not store bookmaker passwords, cookies, or documents
- [ ] Make `ContaCasa` a stable identity for user + holder + bookmaker with `DISPONIVEL|EM_USO|LIMITADA|ENCERRADA`
- [ ] Store usage in `[vigente_de, vigente_ate)` intervals separate from the stable account row
- [ ] Preserve existing `conta_casa_id` references and backfill legacy rows with nullable/unknown holder rather than inventing a person
- [ ] Apply RLS, FKs, indexes, and repositories to every new per-user table
- [ ] Enforce one open usage per user + bookmaker for version 1 with a named, isolated constraint/policy
- [ ] Keep APIs and domain types collection-based so the named constraint can be relaxed in the future without rewriting history
- [ ] Preview a change with bookmaker, source account, destination account, and user-selected effective timestamp
- [ ] Show which existing bets would resolve differently before applying a retroactive switch
- [ ] In one `FOR UPDATE` transaction, close X at `T` and open Y at `T`
- [ ] Require the user to choose X's resulting state (`DISPONIVEL`, `LIMITADA`, or `ENCERRADA`); never infer “limited”
- [ ] Reject gaps/overlaps that contradict version-1 policy and normalize timezone boundaries
- [ ] Persist an append-only audit event and idempotency key for preview/apply retries
- [ ] Keep bets placed before `T`, including still-open bets, attached to X

**Acceptance**: Migration preserves every existing bet/movement/account ID; two concurrent current usages are rejected today; multiple stable accounts and future explicit resolution require no schema redesign; concurrent/retried switches produce one audited interval transition, before-`T` bets stay with X, after-`T` defaults to Y, and no account state is guessed

---

### Issue 7: Bet Account Attribution — Event-Time Resolution + Review Queue
**Labels**: `week-5`, `accounts`, `materialization`, `review`

**Size**: L (5-6 hours)

**Files**:
- `src/bancaemdia/domain/account_attribution.py`
- `src/bancaemdia/domain/materializar.py`
- `src/bancaemdia/repositories/conta_casa_repo.py`
- `tests/unit/test_account_attribution.py`

**Tasks**:
- [ ] Resolve by bet occurrence time (`data_aposta`), never ingestion/capture time
- [ ] Precedence: explicit validated account reference → exactly one temporal usage → unresolved review
- [ ] Validate explicit account belongs to the same user and bookmaker under RLS
- [ ] Replace `order_by(id).limit(1)`/first-active behavior with `NONE|UNIQUE|AMBIGUOUS`
- [ ] Zero/multiple candidates retain the bet in the user's totals but place account dimension in `UNASSIGNED` and create review
- [ ] Apply the same pure resolver to manual, export, photo, private bot, house collection, and deterministic replay
- [ ] Reserve optional account reference in collection contracts for future multi-account support without reading bookmaker login names

**Acceptance**: Historical capture resolves to the account valid when the bet happened; ambiguous data is visible for review and never silently attributed to an arbitrary holder

---

### Issue 8: Titulares API — Availability Matrix, Usage History & Financials
**Labels**: `week-5`, `api`, `accounts`, `matrix`, `analytics`, `financeiro`

**Size**: L (6-8 hours)

**Files**:
- `src/bancaemdia/api/v1/titulares.py`
- `src/bancaemdia/domain/titular_matrix.py`
- `src/bancaemdia/domain/account_financials.py`
- `src/bancaemdia/repositories/titular_repo.py`
- `src/bancaemdia/repositories/account_financials_repo.py`
- `tests/integration/test_titulares_api.py`
- `tests/integration/test_account_financials.py`

**Tasks**:
- [ ] CRUD/archive holders and create/update their stable bookmaker accounts
- [ ] List holder → bookmakers with current state and complete usage history
- [ ] List bookmaker → available, current, limited, closed, and previously used holders/accounts
- [ ] Return the active interval and next valid actions; do not expose internal DB guesses
- [ ] Filter apostas and caixa by `titular_id` and `conta_casa_id`
- [ ] Paginate/search user-owned holders and enforce RLS on every direction of the matrix
- [ ] Keep responses chart/table-ready without implementing website UI
- [ ] Compute bet profit, turnover, ROI, open exposure, bet count, and win rate per stable bookmaker account
- [ ] Aggregate accounts into holder totals while retaining bookmaker breakdown
- [ ] Keep deposits, withdrawals, bonuses, and transfers separate from betting profit; expose cash balance independently
- [ ] Include `UNASSIGNED` as an explicit reconciliation bucket rather than dropping ambiguous historical bets
- [ ] Ensure account totals + unassigned reconcile to the user's canonical total at the centavo
- [ ] Support period/bookmaker/holder/account filters using bet occurrence time and current financial rules
- [ ] Keep queries available in post-trial read-only mode and protected by RLS

**Acceptance**: API answers both “X already used Betano and Betfair” and “Betfair has Y and Z available, with A in use,” with no cross-user data or bookmaker credentials; known fixtures reconcile exactly from bet → account → holder → user, with cash flow distinct from P&L and no historical result changing after a holder switch
