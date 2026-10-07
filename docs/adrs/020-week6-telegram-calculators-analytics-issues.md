# Week 6: Telegram Intake, Calculators & Advanced Analytics — Milestones & GitHub Issues

## Milestone: **Week 6 — Telegram Intake + Calculator APIs + Advanced Analytics**

**Target Date**: 7 days from Week 5 completion
**Depends on**: Week 5 account-holder foundations complete; existing extraction/materialization pipeline (#15 and #19); Week 3 Issue 7 / GitHub #30 dashboard aggregates available
**Scope Boundary**: Backend and Telegram integration only — no website frontend, templates, pages, CSS, or JavaScript
**Success Criteria**:
- [ ] A user can link a private Telegram chat to exactly one Banca em Dia account through a short-lived, single-use code
- [ ] Production receives Telegram updates by authenticated webhook; local development can use polling without changing domain behavior
- [ ] Every inbound update and outbound bot message is durable, retryable, observable, and idempotent
- [ ] A photo creates one resumable bet draft; it never creates a financial bet before explicit user confirmation
- [ ] When extraction is incomplete, the bot asks only for the missing information and never asks the user to resend the photo
- [ ] Replayed updates, repeated confirmations, worker restarts, and Telegram outages never duplicate a bet or lose a reply
- [ ] The four selected calculator APIs use one pure `Decimal` domain core, deterministic cent allocation, and explicit validation
- The line-calculator research (#104) is archived for post-launch by owner decision on 2026-10-06; it is excluded from this milestone. Historical criteria below remain preserved; see [archive](../archive/post-launch/line-calculator.md).
- [ ] Advanced analytics adds only capabilities absent from GitHub #30 and keeps deposits/withdrawals separate from betting profit
- [ ] Authenticated financial responses are never publicly cacheable
- [ ] RLS, privacy, rate-limit, contract, and end-to-end tests pass in CI
- [ ] No website frontend code is added by any issue in this milestone

---

## GitHub Issues (9 issues)

### Issue 1: Telegram Account Link — One-Time Code, Revocation & Tenant Binding
**Labels**: `week-6`, `telegram`, `auth`, `security`
**Size**: M (3-4 hours)

**Files**:
- `src/bancaemdia/models/telegram_link.py`
- `src/bancaemdia/repositories/telegram_link.py`
- `src/bancaemdia/services/telegram_link.py`
- `src/bancaemdia/api/v1/telegram.py`
- `alembic/versions/xxx_telegram_account_link.py`
- `tests/integration/test_telegram_link.py`

**Tasks**:
- [ ] Add tenant-owned `TelegramLink` with `usuario_id`, `telegram_user_id`, `telegram_chat_id`, `linked_at`, `revoked_at`, and last-use timestamps
- [ ] Add `TelegramLinkCode` with an 8-character human-readable code, 30-minute expiry, single-use state, failed-attempt counter, and issuer `usuario_id`
- [ ] Store only a keyed hash of the code; never persist or log the plaintext code, bot token, or Telegram identifiers in structured log fields
- [ ] Add authenticated `POST /api/v1/telegram/link-codes`:
  - Invalidates any still-open code for the same user
  - Returns plaintext only once with `expires_at`
  - Is rate-limited per user and collision-safe
- [ ] Support `/vincular <codigo>` in a private Telegram chat:
  - Reject groups, channels, expired codes, used codes, and excessive attempts
  - Consume the code and create/re-activate the link in one transaction
  - Never trust a forwarded message's author as the Banca em Dia identity
- [ ] Add authenticated `GET /api/v1/telegram/link` and `DELETE /api/v1/telegram/link` for status and revocation
- [ ] Enforce one active Banca em Dia user per Telegram identity and prevent cross-tenant reads through RLS
- [ ] Audit link, re-link, failed redemption, and revocation without recording secrets
- [ ] Return neutral errors so an attacker cannot discover whether a code or account exists

**Acceptance**: A fresh code links one private chat exactly once; the same, expired, brute-forced, group-sent, or cross-tenant code cannot create a link, and revocation immediately blocks new intake

---

### Issue 2: Telegram Transport — Authenticated Webhook, Durable Inbox & Transactional Outbox
**Labels**: `week-6`, `telegram`, `webhook`, `reliability`
**Size**: L (5-6 hours)

**Files**:
- `src/bancaemdia/models/telegram_message.py`
- `src/bancaemdia/integrations/telegram/client.py`
- `src/bancaemdia/integrations/telegram/webhook.py`
- `src/bancaemdia/workers/telegram.py`
- `alembic/versions/xxx_telegram_inbox_outbox.py`
- `tests/integration/test_telegram_transport.py`

**Tasks**:
- [ ] Add a durable `TelegramInbox` record with unique `update_id`, normalized event type, sender/chat/message identifiers, minimal encrypted/raw payload, status, attempts, and timestamps
- [ ] Add a tenant-owned `TelegramOutbox` record with deterministic idempotency key, destination chat, message payload, status, next-attempt time, attempts, and last error
- [ ] Implement `POST /api/v1/integrations/telegram/webhook`:
  - Validate `X-Telegram-Bot-Api-Secret-Token` with constant-time comparison
  - Enforce body-size and content-type limits
  - Persist the inbox row before returning success
  - Return quickly and defer extraction/business work to a worker
- [ ] Provide polling only for local development; webhook and polling modes must be mutually exclusive and feed the same inbox contract
- [ ] Process inbox rows with row locking, bounded exponential backoff, poison-message/DLQ state, and crash-safe retry
- [ ] Deliver outbox rows only after their originating database transaction commits; a Telegram outage must not lose the response
- [ ] Treat Telegram `update_id` as transport deduplication and domain idempotency keys as the protection for business effects
- [ ] Redact bot tokens, codes, photo bytes, captions, Telegram names, and raw payloads from logs and traces
- [ ] Add metrics for inbox age/depth, outbox age/depth, retries, DLQ count, webhook rejection, and Telegram API latency/error rate

**Acceptance**: Replaying the same update 10 times stores one inbox item and produces one business effect; stopping Telegram delivery or killing the worker preserves queued work and sends the reply once after recovery

---

### Issue 3: Telegram Bet Draft — Resumable Conversation for Missing Fields
**Labels**: `week-6`, `telegram`, `domain`, `conversation`
**Size**: L (5-6 hours)

**Files**:
- `src/bancaemdia/models/rascunho_aposta.py`
- `src/bancaemdia/domain/rascunho_aposta.py`
- `src/bancaemdia/repositories/rascunho_aposta.py`
- `src/bancaemdia/services/telegram_conversation.py`
- `alembic/versions/xxx_bet_draft.py`
- `tests/unit/test_telegram_conversation.py`

**Tasks**:
- [ ] Add tenant-owned `RascunhoAposta` with UUID, Telegram origin, media reference/hash, extracted fields, per-field confidence/source, missing-field list, status, version, and timestamps
- [ ] Define states: `AWAITING_EXTRACTION`, `AWAITING_INFORMATION`, `AWAITING_CONFIRMATION`, `CONFIRMED`, `CANCELLED`, `FAILED`
- [ ] Allow at most one active draft per linked private chat; a new photo while one is pending must offer continue/cancel instead of silently replacing data
- [ ] Reuse the canonical bet validation rules to calculate missing/invalid fields; do not maintain a second, weaker list inside the bot
- [ ] Generate a concise summary of everything already understood and ask only for fields that are still missing or ambiguous
- [ ] Never ask the user to resend the photo; accept one or several missing values in a reply and patch only the fields supplied
- [ ] Support `/continuar`, `/corrigir <campo> <valor>`, and `/cancelar`; returning later must restore the same active draft
- [ ] Preserve correction history and optimistic versioning so two replies cannot overwrite each other silently
- [ ] Resolve the default `conta_casa_id` from the account valid at game time (`data_jogo` / `comeca_em`) when exactly one exists; explicit multi-account references preserve the account that placed the bet; ask which account when resolution is absent or ambiguous
- [ ] Keep the resolver interface cardinality-safe for future simultaneous accounts, while this milestone enforces the current one-account-in-use rule
- [ ] Keep incomplete drafts outside all balances, profit, turnover, ROI, and dashboard aggregates

**Acceptance**: Given an extracted draft missing house and stake, the bot summarizes known values, asks only for house/stake, accepts the reply without a new photo, survives a restart, and still has created zero financial bets

---

### Issue 4: Telegram Photo Intake — Extraction into One Draft, Never Directly into Finance
**Labels**: `week-6`, `telegram`, `extraction`, `ai`
**Size**: L (5-6 hours)

**Files**:
- `src/bancaemdia/integrations/telegram/media.py`
- `src/bancaemdia/services/telegram_photo_intake.py`
- `src/bancaemdia/workers/telegram_extraction.py`
- `tests/integration/test_telegram_photo_intake.py`
- `tests/fixtures/telegram/`

**Tasks**:
- [ ] Accept one photo per private message, including a photo forwarded by the linked user; the linked sender remains the tenant owner
- [ ] For the first version, explicitly reject albums, multiple photos in one intake, and text-only bet creation with a clear supported-flow message
- [ ] Select the largest Telegram photo variant, download through Telegram `file_id`, validate MIME/size, hash the actual bytes, and store it through the existing protected media path
- [ ] Preserve source metadata (`update_id`, chat/message IDs, forward metadata, `file_unique_id`, content hash) without treating it as trusted bet data
- [ ] Reuse the Week 2 OCR → Haiku → Sonnet extraction ladder and its cache; do not create a Telegram-specific extractor or prompt contract
- [ ] Map extraction output into `RascunhoAposta`, including field-level source/confidence, but never call the financial materializer from the extraction worker
- [ ] Use origin-aware deduplication so the same update/photo retry resumes the existing draft instead of starting another one
- [ ] If no field can be read, keep the draft and ask for the required values; do not request a new upload
- [ ] If the image appears to contain more than one coupon/bet, keep it pending and ask the user which single bet is intended; never silently materialize several
- [ ] After extraction, route to either missing-field conversation or confirmation summary through the outbox
- [ ] Record extraction failure/cost/latency using existing observability without exposing image contents

**Acceptance**: A direct or forwarded single-bet photo produces one deduplicated draft and a persisted bot response; incomplete/unreadable input enters the question flow, while no path creates an `Aposta` before confirmation

---

### Issue 5: Telegram Confirmation — Idempotent Draft Materialization & Saved-Data Reply
**Labels**: `week-6`, `telegram`, `materialization`, `idempotency`
**Size**: L (5-6 hours)

**Files**:
- `src/bancaemdia/services/telegram_confirmation.py`
- `src/bancaemdia/workers/telegram_materialization.py`
- `src/bancaemdia/domain/materializar.py`
- `tests/integration/test_telegram_confirmation.py`

**Tasks**:
- [ ] Render an explicit confirmation summary from the latest persisted draft version, including house/account, event, market/selection, odds, stake, dates, and any uncertainty still requiring correction
- [ ] Accept confirmation only when canonical validation reports no missing or invalid required field
- [ ] Treat ambiguous text as a correction/question, never as implicit confirmation; support explicit confirm, correct, and cancel actions
- [ ] Materialize in one transaction using deterministic key `telegram_draft:<draft_uuid>` and a unique database constraint
- [ ] In that transaction:
  - Lock and re-read the draft version
  - Resolve canonical entities and the account valid at `data_aposta`
  - Append the source event and create/update the bet through the existing materializer
  - Mark the draft `CONFIRMED`
  - Enqueue the success response in the outbox
- [ ] Use origin `telegram_bot` (or the reviewed canonical equivalent) without colliding with Telegram-export message keys
- [ ] Build the success message from committed/saved values, not from extraction memory, so the user sees exactly what entered the ledger
- [ ] If materialization fails, keep the draft resumable and do not send a success reply
- [ ] Replaying the confirmation, retrying after timeout, or receiving two simultaneous confirmations must return the already-created bet without a second financial effect
- [ ] Emit trace links from update → draft → event → bet → outbox while redacting content

**Acceptance**: Ten concurrent/replayed confirmations for one complete draft commit exactly one bet and one financial effect; the eventual Telegram reply matches the row read back from the database

---

### Issue 6: Telegram Bot Hardening — Privacy, Abuse Controls, E2E & Runbook
**Labels**: `week-6`, `telegram`, `testing`, `security`
**Size**: L (5-6 hours)

**Files**:
- `tests/e2e/test_telegram_bot.py`
- `tests/security/test_telegram_security.py`
- `tests/fixtures/telegram/`
- `docs/runbooks/telegram-bot.md`
- `docs/privacy/telegram-data.md`

**Tasks**:
- [ ] Add end-to-end scenarios with a fake Telegram API and real PostgreSQL/Redis:
  - Link → photo → complete extraction → confirm → one saved bet
  - Link → incomplete extraction → provide only missing values → confirm
  - Forwarded photo, unreadable photo, multiple-bet ambiguity, correction, cancellation, and resume after restart
  - Duplicate update/photo/confirmation and concurrent replies
  - Telegram API outage, extraction timeout, worker crash, and outbox recovery
- [ ] Add per-chat, per-user, and global rate limits for link attempts, photos, corrections, and confirmations
- [ ] Reject group/channel intake, unlinked chats, forged webhook secrets, oversized files/bodies, unsupported MIME, and stale callback actions
- [ ] Define configurable retention for raw Telegram payloads and media, implement scheduled purge, and retain only the minimum audit/idempotency metadata afterward
- [ ] Verify RLS isolation for links, drafts, inbox/outbox tenant views, media, and materialized bets
- [ ] Verify secrets and personal content are absent from logs, traces, metrics labels, error reports, and DLQ summaries
- [ ] Add alert thresholds for backlog age, DLQ growth, extraction failure, delivery failure, and suspicious link attempts
- [ ] Write setup/rotation/recovery procedures for bot token, webhook secret, webhook registration, polling mode, queue replay, and unlink/privacy requests
- [ ] Document user-facing constraints: private chat, one bet/photo at a time, no album/text-only intake in v1, explicit confirmation required

**Acceptance**: The complete and missing-field E2E journeys pass; replay/failure tests prove zero duplicate bets and zero lost durable messages, and the security suite finds no public or cross-tenant Telegram data

---

### Issue 7: Selected Calculator APIs — Four Operations on One Decimal Core
**Labels**: `week-6`, `calculators`, `domain`, `probability`, `allocation`, `risk`, `api`, `testing`

**Product decision (2026-09-28)**: The owner narrowed the original nine operations to four.
This revision supersedes the original Issue 7 checklist; Issue 8's line-calculator research remains separate.

**Files**:
- `src/bancaemdia/domain/calculators/core.py`
- `src/bancaemdia/domain/calculators/probability.py`
- `src/bancaemdia/domain/calculators/allocation.py`
- `src/bancaemdia/domain/calculators/planning.py`
- `src/bancaemdia/api/v1/schemas/calculators.py`
- `src/bancaemdia/api/v1/calculators.py`
- `tests/unit/calculators/test_calculators.py`
- `tests/contract/test_calculators_api.py`

**Tasks**:
- [ ] Keep pure typed Decimal calculations, bounded validation, exact cent allocation and
      explicit method, precision, rounding, assumptions and warnings.
- [ ] `POST /api/v1/calculadoras/mercado-justo`: require all mutually exclusive outcomes,
      report raw implied probabilities, proportional no-vig probabilities and odds, and overround.
      Explicitly state that these are not true-probability predictions or neighboring-line prices.
- [ ] `POST /api/v1/calculadoras/distribuir-entre-resultados`: combine dutching and surebet
      into one inverse-odds allocation; show stake, return and net profit for every outcome,
      minimum profit and ROI, and flag arbitrage only if every rounded scenario is profitable.
- [ ] `POST /api/v1/calculadoras/cobertura-ao-vivo`: support two-way cash stakes and the
      equalized-profit objective, including commission on winning odds profit and both rounded
      scenarios. Reject unsupported freebet, multi-way and Asian-push contracts.
- [ ] `POST /api/v1/calculadoras/percentual-banca`: support direct and inverse stake sizing.
- [ ] Remove standalone implied-probability, RTP, stake-split and target-profit operations,
      and remove the `protect_stake` hedge objective.
- [ ] Update OpenAPI, examples, API reference, tests and product documentation.

**Acceptance**: Exactly four authenticated stateless calculator routes are exposed. Fair
probabilities sum to one at the declared precision. Allocated stakes reconcile to the requested
total, and a profit guarantee requires positive profit in every rounded outcome. Both hedge
scenario profits are reproducible from the reported inputs. Unsupported or incomplete requests
are rejected explicitly.

---

### Issue 8: Line Calculator Discovery — Market Model, Dataset, Calibration & Go/No-Go
**Labels**: `week-6`, `calculators`, `research`, `decision`
**Size**: L (6-8 hours)

**Files**:
**Archived for post-launch — #104, owner decision 2026-10-06.** The following research outline is historical, not an active implementation or release prerequisite.

- `docs/discovery/line-calculator.md`
- `docs/adrs/xxx-line-calculator-model-decision.md`
- `research/line-calculator/README.md`
- `research/line-calculator/schema.json`
- `research/line-calculator/fixtures/` (sanitized, non-sensitive samples)

**Tasks**:
- [ ] Treat this as a decision/research issue only: do not create an API endpoint, production model, or implementation issue in this milestone
- [ ] Write the exact product question using the motivating example: given market `shots`, quoted `over 4.5 @ 1.86`, what additional information is required to estimate a fair `over 6.5` price?
- [ ] Prove and document that one quoted line alone may be underdetermined; the system must return `unsupported/insufficient_data` rather than invent a curve
- [ ] Define candidate supported market families separately (shots, goals, corners, cards, player props, etc.); approval for one family must not silently authorize another
- [ ] Define “fair” precisely, including vig removal, source market completeness, push probability, integer/half/quarter Asian lines, and over/under complement rules
- [ ] Design a sanitized dataset contract with, when available:
  - Simultaneous prices for several neighboring lines and both sides
  - Sport, league, event/player, market family, bookmaker, and timestamp
  - Pre-match/live state and live clock/state variables
  - Closing/result data used only where scientifically valid
- [ ] Gather a representative user-provided sample jointly with the product owner and record consent/provenance without credentials or personal account data
- [ ] Compare owner-approved distributional and non-parametric approaches empirically; explicitly reject a Poisson-only shortcut and do not privilege Poisson as the default or baseline
- [ ] Use temporal/event-separated train-validation-test splits to prevent prices or outcomes from the same event leaking across sets
- [ ] Define evaluation gates: calibration curve/error, log loss/Brier score where applicable, price/line error, coverage, stability by market/league, and baseline comparison
- [ ] Test sensitivity to bookmaker margin, stale/non-simultaneous prices, sparse neighboring lines, live-state drift, and market-definition mismatch
- [ ] Produce a proposed input/output/error contract with confidence/coverage and explicit unsupported cases
- [ ] End with an ADR containing per-market Go/No-Go, approved dataset version/hash, chosen method, limitations, and required monitoring
- [ ] Only after owner/admin approval of both model and dataset may a separate implementation issue be proposed

**Acceptance**: The review produces a reproducible dataset/schema, benchmark report, calibration evidence, explicit unsupported cases, and an approved ADR; absent that approval, no line-calculator implementation issue exists

---

### Issue 9: Advanced Analytics Backend — Cashflow-Safe Series, Missing Insights & Private Caching
**Labels**: `week-6`, `analytics`, `api`, `data-integrity`
**Size**: L (6-8 hours)

**Files**:
- `src/bancaemdia/api/v1/painel.py`
- `src/bancaemdia/domain/analytics.py`
- `src/bancaemdia/models/meta_desempenho.py`
- `alembic/versions/xxx_advanced_analytics.py`
- `tests/integration/test_advanced_analytics.py`
- `tests/contract/test_analytics_api.py`

**Tasks**:
- [ ] Preserve GitHub #30 as the canonical base: extend its backing view/schema/output where required and do not create parallel definitions of summary, by-house, by-tipster, by-market, generic period, bankroll evolution, export, or raw chart data
- [ ] Correct GitHub #30 caching before adding data:
  - Replace `Cache-Control: public, max-age=30, stale-while-revalidate=60`
  - Authenticated financial/dashboard responses use `Cache-Control: private, no-store`
  - Add `Vary: Authorization, Cookie` and tests proving shared caches cannot reuse one tenant's response for another
- [ ] Add `GET /api/v1/painel/analises` with the existing period/filter semantics and only the missing analytics below
- [ ] Extend GitHub #30's existing bankroll-evolution output with reconciled cashflow components, rather than adding a second competing series, using separate lines for:
  - Cumulative betting profit (settled bets only)
  - Cumulative deposits
  - Cumulative withdrawals
  - Resulting bankroll balance
- [ ] Enforce at every time point: `balance = initial_balance + deposits - withdrawals + betting_profit`; deposits/withdrawals must never be labeled or counted as profit
- [ ] Add odds-band distribution with bet count, turnover, profit, ROI, and hit rate; use reviewed stable bands and an explicit `unknown` bucket
- [ ] Add weekday × period-of-day heatmap with count, profit, ROI, and hit rate; derive bucket in the user's configured timezone, never server-local time
- [ ] Add by-sport performance with an explicit `unknown` bucket; do not infer a sport in the analytics query when canonical data is absent
- [ ] Add average odds and profit factor using the canonical financial inclusion rules; no-loss profit factor returns `null`, not infinity
- [ ] Add adaptive stake quartiles based on observed face value; identical boundaries collapse rather than producing fake empty quartiles, and freebets use face value for bucket placement
- [ ] Add group progression (`profit / configured_group_capital`) only for groups with capital; return `null` plus reason when capital is absent or zero
- [ ] Add tenant-owned performance goals (`MetaDesempenho`) and backend CRUD/progress for reviewed metrics, date interval, target, baseline, status, and archival; no goal UI in this issue
- [ ] Make every bucket reconcilable to the same filtered population, with explicit `unknown/not_applicable` counts rather than silently dropping rows
- [ ] Use replica/materialized-view strategy only where it preserves the reconciliation identities; document refresh timestamp and staleness in the response
- [ ] Add fixtures covering freebet, pending, cancelled, severe-review, deposit, withdrawal, cashout, missing sport/odds, timezone boundary, identical stakes, and absent group capital

**Acceptance**: Contract tests prove tenants cannot share cached responses; deposits/withdrawals never change betting profit; every series/bucket reconciles to canonical totals; and all new insights are absent from, rather than duplicates of, GitHub #30

---

## Dependency Graph

```mermaid
graph TD
    W5[Week 5 Complete] --> 1[Telegram Account Link]
    W5 --> 2[Telegram Inbox/Outbox]
    1 --> 3[Conversational Draft]
    2 --> 3
    2 --> 4[Photo to Draft]
    3 --> 4
    3 --> 5[Confirmation/Materialization]
    4 --> 5
    5 --> 6[Telegram Hardening/E2E]

    W5 --> 7[Four Selected Calculator APIs]
    W5 --> 8[Line Calculator Discovery]

    W3[Week 3 Issue 7 / GitHub #30] --> 9[Advanced Analytics]
    W5 --> 9
```

---

## Suggested Execution Order (Day by Day)

| Day | Issues | Notes |
|-----|--------|-------|
| 1 | 1, 2, 7, 8 | Link/transport foundations, standard calculator core/APIs, and independent line-model discovery |
| 2 | 3, 7 | Draft state machine in parallel with standard calculators |
| 3 | 4, 7 | Photo extraction into the established draft contract + standard calculator completion |
| 4 | 5, 7 | Idempotent Telegram materialization + calculator contract and invariant tests |
| 5 | 6, 9 | Telegram hardening/E2E + cashflow-safe advanced analytics and cache correction |
| 6 | 6, 9 | Security, reconciliation, failure-path, and analytics verification |
| 7 | 8, 9 | Dataset/model Go/No-Go and final analytics validation; no line implementation without approval |

---

## Review Gate Before Creating GitHub Issues

This document is the proposal the administrator reviews in a pull request. It does **not** authorize automatic issue creation. After the PR is approved and merged, create the 9 GitHub issues from the reviewed text; changes requested in review must be reflected here first.
