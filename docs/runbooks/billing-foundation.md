# Billing foundation (reused from #133)

Supersedes the original cardless rollout: the owner changed the decision on 2026-09-27. A card is required and Stripe confirmation starts exactly 168 hours. Signup/backfill creates only a durable reservation. `trial_confirmed=false` gives no trial entitlement after rollout; public status hides the reservation timestamps. A signed event plus a fresh Stripe subscription with a confirmed customer card fixes both bounds once. Cancellation/email changes/resubscription cannot reset them.

The original c90 migration is retained; d91 migrates it without losing grants. It is intended for the unpublished foundation, not an already launched cardless product. Rollout remains off; `SELECT billing_activate_rollout()` is an explicit administrative launch action and must not be run in production without owner authorization. Checkout also stays disabled unless BILLING_ENABLED and a validated test credential are provided.

One product, independently versioned currency/cadence prices, integer minor units (the historical column name `amount_cents` is retained). No price seed. Runtime currency allowlist must reflect actual account support. A zero-decimal currency is not multiplied by 100; no implicit FX conversion or guessed price. Published financial terms are immutable, and only one published interval may overlap per product/currency/cadence. Subscription keeps its agreed Price until explicit migration. Public API uses `amount_minor`.

RLS, durable trial identity and append-only field-name audit are inherited from #133 and the minimal audit/export dependency extracted from #130. The original PR branches are retained. `usuarios.ativo` remains administrative. Export excludes provider references; deleting an account cannot orphan an active external subscription.

See [Stripe operations](billing-stripe.md) and [ADR 025](../adrs/025-billing-provider.md). Production eligibility and live sandbox evidence remain separate gates.
