# Expansion Roadmap — Decisions Before New GitHub Issues

## Status

**Proposed** — this document and the Week 5–9 issue drafts are being submitted by pull request. Merging the proposal approves the scope; it does not silently create or start any issue.

## Scope

This expansion covers backend, the private Telegram intake bot, calculator APIs, advanced analytics, billing, and the browser extension integration. It contains **no website frontend work**.

The extension is a requested capture client, not part of the website frontend. Its code belongs in the dedicated private repository [`wfcgit-hub/bancaemdia-extension`](https://github.com/wfcgit-hub/bancaemdia-extension); its canonical API contract, parsers, financial effects, matching, and audit trail remain in `bancaemdia-api`.

## Confirmed Product Decisions

### Billing

- These decisions supersede the placeholder payment-provider references in ADR 013, `HIGH_LEVEL_PLAN.md`, and `TECHNICAL_REVIEW.md`; those older passages remain historical planning context, not implementation instructions.
- Every user receives one seven-day free trial without entering a card.
- New users start the trial at account creation. Users who already exist when billing launches receive seven full days from the rollout timestamp.
- When access expires, the account becomes read-only: sign-in, reads, analytics, billing, and export remain available; new mutations and ingestion are blocked.
- Price is deliberately undecided. No issue may hardcode a price or invent Free/Pro/Team tiers.
- Mercado Pago is the primary candidate for a Brazilian individual account (CPF). Integration begins only after written business classification and production eligibility are confirmed.
- Asaas is the provider-neutral fallback if Mercado Pago rejects or cannot confirm the operation.
- The product is described truthfully as subscription software for recordkeeping and analytics. It never receives wagers, deposits, prizes, or customer funds.

### Holders and bookmaker accounts

- A `Titular` is a person whose bookmaker account the user may operate. No password, cookie, bookmaker credential, or unnecessary document is stored.
- A stable `ContaCasa` belongs to a holder and bookmaker. Temporal usage is stored separately so profit can be calculated by account and then by holder.
- The user chooses the effective instant of a change from holder X to holder Y and the resulting state of X's account; the system must never assume that X was limited.
- Version 1 permits several historical/available accounts but only one account in use per bookmaker at an instant.
- The model and contracts use explicit account identities and collections so a later issue can permit simultaneous accounts without redesigning historical data.
- Attribution uses the bet occurrence time, not capture time. An open bet remains attached to the account used when it was placed.
- No code may choose the first active account on ambiguity. Zero or multiple candidates produce an explicit review item.

### Telegram bot

- The bot is private and receives one bet at a time by photo or forwarded photo.
- It reuses the canonical extraction and materialization services; it is not the old partner-channel/liquidation bot.
- An incomplete extraction becomes a durable draft. The bot summarizes what it understood and asks only for missing or ambiguous fields; it does not ask the user to resend the image.
- The user can correct, cancel, or continue the draft later. Nothing is presented as saved before a successful idempotent commit.

### Calculators

The first calculator set contains exactly:

1. implied probability;
2. fair/no-vig market probability;
3. RTP;
4. surebet;
5. dutching;
6. stake splitter;
7. live hedge;
8. target profit;
9. bankroll percentage.

All money/odds arithmetic uses `Decimal`, explicit validation, and deterministic cent allocation.

The line calculator is a separate research gate. An input such as `Over 4.5 @ 1.86` does not, by itself, identify the true distribution for `Over 6.5`. No Poisson or other model is selected in advance. The discovery issue must define supported markets, simultaneous line data, margin removal, push/Asian-line behavior, calibration metrics, and rejected cases with the owner before an implementation issue is written.

### Analytics

Issue #30 already owns basic dashboard aggregates and chart-ready output. New work adds only missing backend semantics: betting profit separated from deposits/withdrawals, odds bands, weekday × period heatmap, sport/competition breakdowns, profit factor, stake quartiles, and holder/account dimensions.

Authenticated per-user responses must not use a shared `Cache-Control: public` policy. Caching must be private or explicitly tenant-partitioned.

### Extension and collection

- The extension is one Manifest V3 repository, not one repository per bookmaker.
- Capture remains passive: it observes responses caused by the user's own navigation and never clicks, places bets, initiates bookmaker requests, stores cookies, or reads passwords.
- The extension transports raw bookmaker data. Financial parsing and normalization stay in the API.
- The API owns versioned JSON schemas. The extension pins a compatible copy/hash and tests N/N-1 compatibility; a third contracts repository is unnecessary now.
- One token exists per installation/profile, with pair, rotate, and revoke lifecycle. Multiple devices are supported.
- Payload delivery uses a durable local outbox, bounded batches, stable item identities, and acknowledgement per item. A successful response must not erase a newer capture that arrived during the request.
- The initial distribution must work as a deterministic ZIP with checksum. Chrome Web Store eligibility is investigated separately and is not a launch dependency.
- With a large catalog, permissions are optional and granted only for exact domains the user enables. `<all_urls>` is forbidden. The signed runtime catalog can narrow a build's declared hosts but cannot grant a host absent from that build's `optional_host_permissions`; adding a new exact domain therefore requires a reviewed extension release and a new explicit user grant.

### Meaning of “all bookmakers”

Technical support and regulatory status are independent fields.

The coverage catalog is seeded from current national administrative and judicial lists, available state/DF registries, and manual domains that the owner can actually open and authenticate to. A manually added domain may be technically supported while its regulatory status remains `UNKNOWN` or `UNVERIFIED`; the product must not call it regulated without an authoritative source.

A brand/domain counts as supported only after:

- the exact hostname is recorded;
- the owner supplies a real sanitized capture while logged in;
- transport/platform is measured rather than guessed;
- the backend reader has fixtures and regression tests;
- relevant open, settled, canceled, cashout, simple, and multiple states are validated where the bookmaker exposes them;
- schema drift fails explicitly instead of silently materializing incorrect money.

Unknown platforms are handled by small follow-up issues grouped by real technical family (normally three to five brands), generated from the living coverage matrix. The coverage gate remains open until every domain the owner can open and log into is accounted for.

## Existing-Issue Boundaries

- #21 already receives `/coleta`; this roadmap adds token lifecycle, a real versioned contract, cross-origin matching, and safe delivery rather than recreating the endpoint.
- #26 imports a complete Telegram export; it is not the private real-time bot.
- #15 and #19 remain the canonical extraction and materialization pipeline.
- #30 remains the base dashboard endpoint; advanced analytics must extend its canonical output rather than duplicate it. Its proposed `Cache-Control: public` requirement is superseded immediately by private/non-shared caching and must be amended after this PR merges, before #30 can be implemented or accepted.
- #36 tests duplicates inside each origin; Casa × Telegram consolidation is a separate invariant.
- #43 already owns the LGPD export; billing only guarantees it remains available in read-only mode.

## Issue Granularity

The expansion is organized as 30 reviewable issues. API and extension work share one issue when they produce a single end-to-end capability, while tests, documentation, operational gates, and ordinary setup remain tasks or acceptance criteria unless they have an independent deliverable or materially different risk.

An issue remains separate when it owns a distinct transactional boundary, security boundary, research decision, migration, or independently reviewable release gate. In particular, Telegram photo extraction remains separate from confirmation because extraction may only create or update a draft, while confirmation is the sole idempotent transaction allowed to materialize financial data. This mirrors the existing separation between extraction (#15) and materialization (#19).

## Review and Creation Protocol

1. The administrator reviews this PR, the issue bodies in ADRs 019–023, and the extension responsibility map in ADR 024.
2. Requested changes are made in the PR, preserving a readable review trail.
3. Merge means the proposed roadmap is accepted.
4. Immediately after merge, amend existing issue #30 to remove public caching from authenticated financial responses; this safety correction precedes its implementation or acceptance.
5. Only after merge are the new milestones/issues created in the central `bancaemdia-api` tracker.
6. Extension implementation PRs may close central issues using cross-repository references such as `pradyumna-001/bancaemdia-api#N`.
7. No issue begins merely because its draft exists in this proposal.
