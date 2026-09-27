# Stripe sandbox validation — 2026-09-27

Status: actual sandbox evidence, **production NO_GO**. This report contains assertions only: no account/customer identifiers, hosted URLs, payment details, secrets or personal data. All customers and amounts were explicit synthetic fixtures; no commercial price was selected. No live charge, payout, verification or production activation occurred.

## Environment

- Isolated Stripe sandbox belonging to the Brazilian account; outgoing API version `2024-06-20`.
- Real adapter and billing routes against a disposable PostgreSQL 16 database, using the ordinary non-owner application role. The local harness supplies a synthetic authenticated user; it does not validate frontend authentication/UI deployment.
- Official Stripe CLI forwards real signed events to the raw-body webhook; the actual reconciliation worker polls the database. Neither CLI payloads nor signing secrets were printed or persisted in evidence.
- Portal configuration: period-end cancellation, subscription updates disabled. Adaptive Pricing disabled in sandbox. Versioned recurring Price fixtures validated in BRL, USD, EUR and zero-decimal JPY. Hosted purchase exercised in BRL only.
- Stripe Tax settings returned HTTP 400 because the seller's country is unsupported. `STRIPE_TEST_AUTOMATIC_TAX=false` permits sandbox validation only; global tax implementation remains a launch blocker.

## Observed results

| Scenario | Evidence / result |
| --- | --- |
| Crash after remote Checkout creation | Seeded the durable pending operation without its session reference, then retried the real application service. Stripe returned exactly the original hosted session; no second trial/subscription was created. |
| Before card confirmation | API returned `AWAITING_CARD` / `READ_ONLY`, no confirmed trial. |
| Hosted Checkout | Official test card submitted to Stripe's hosted form; session completed and browser returned to a page that grants no entitlement. No card details passed through the application. |
| Trial confirmation | Real subscription had a customer-owned card payment method. API returned `TRIALING` / `FULL_WRITE`; confirmed bounds differed by exactly 604800 seconds. Initial invoice paid zero and had no charge. |
| Real signed delivery | `checkout.session.completed`, `customer.subscription.created`, `invoice.paid` reached the durable inbox and were processed as `done`. Later cancellation updates were also processed. |
| Duplicate / reverse-order replay | Five actual account events fetched into memory and delivered newest-first twice each, using fresh local test signatures: ten acknowledgements, still five unique inbox rows, unchanged entitlement. This is application replay evidence, not a claim of Stripe-native redelivery testing. |
| Forgery / raw-body change | Invalid signature and adding whitespace to otherwise valid JSON after signing both returned HTTP 400. |
| API management | Portal session created through the application; repeating cancellation succeeded and preserved the original trial bounds. |
| Customer Portal | Hosted portal displayed the zero-value trial invoice. Resuming and then canceling through its UI succeeded and retained the original trial end; no plan/price update was offered. |
| One second before trial end | Separate Billing test-clock subscription remained `trialing`; invoice amount paid was zero and customer had no charges. |
| First paid period | Advanced Billing clock through trial boundary and invoice processing. Real paid invoice/charge projected `ACTIVE`; paid period began at the trial end. Stripe charge creation timestamps use wall time, so the assertion uses absence of charges before clock advancement and Billing period boundaries. |
| Partial then full refund | Real test refunds: partial refund retained `ACTIVE`; refunding the remainder projected `PAST_DUE`, revoking the paid period. |
| Failed renewal and recovery | Official declining payment-method fixture produced `past_due`; application projected `PAST_DUE`. Replacing it with the successful fixture and paying the test invoice restored `ACTIVE` for the new period. |
| Scheduled cancellation | With an active paid period, cancellation preserved access; advancing the clock to its boundary produced `CANCELED`. |

The clock lifecycle used a separate API-created synthetic subscription with the same seven-day terms, because a clock cannot be retroactively attached to the hosted Checkout customer. Paid/refund/failure assertions exercised the real adapter and projection against actual Stripe objects. Deterministic PostgreSQL tests cover persistence and access at simulated time boundaries; the local database clock was not changed to follow Stripe's simulated future.

## Deterministic supplement and remaining gates

Focused regression after the country/tax fix: 52 tests passed against PostgreSQL, including lifecycle, exact cutoff, DST, dedup, eight retries/dead letter, missing-event repair, RLS/export and adapter/cancellation contracts. Previous full combined CI at `afdcf48` passed 1803 tests with eight documented non-billing skips; latest-head CI is recorded in the acceptance ledger.

The account API reported charging and payouts disabled and details not submitted. Required before completion: identity/capabilities and private fee/payout checks, international tax solution, commercial price/currency decisions, deployed webhook/worker monitoring, reviewed migrations, pending holders/Telegram integration and frontend work in its own repository. Sandbox success closes none of these gates. The adapter continues to reject live keys and live objects.

Owner decision after this validation: proceed without a preventive eligibility inquiry. The draft support message was discarded without sending. Written support confirmation is no longer a project gate; account requirements and production authorization are unchanged.

Follow-up: the stricter card check was re-run against the existing real hosted Checkout, without new external objects or charges. Setup confirmation was complete, customer/subscription ownership matched, trial remained exactly 604800 seconds and cancellation was preserved. Deterministic tests reject pending authentication, open Checkout, cross-customer/subscription association and live sessions before the first grant.

Combined runtime CI follow-up: [run 36347113288](https://github.com/pradyumna-001/bancaemdia-api/actions/runs/36347113288) passed 1,804 tests with eight non-billing skips and 93.41% coverage. The added Redis/Celery test executed successfully (not skipped): beat scheduled the actual billing task, a worker consumed the Redis message, PostgreSQL persisted reconciliation, and a second real delivery did not reconsume the completed inbox event. This isolated service test substitutes the Stripe boundary and does not claim deployed infrastructure validation.
