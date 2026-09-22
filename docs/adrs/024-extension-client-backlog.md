# Browser Extension — Centralized Responsibility Map

## Status

This ADR is an index, not a second backlog. All extension work is reviewed and tracked through central issues in `pradyumna-001/bancaemdia-api`, even when the implementation pull request belongs to `wfcgit-hub/bancaemdia-extension`.

GitHub Issues remain disabled in the extension repository. No issue body is defined here, and no API/client pair should be recreated as two separate issues.

## Repository Boundary

| Component | Owns | Must not own |
|-----------|------|--------------|
| `bancaemdia-extension` | Passive browser capture, local sanitization, exact-host routing, browser permissions, durable local outbox, pairing client, versioned envelope submission, reproducible MV3 build | Bookmaker financial parsing, canonical ticket identity, settlement, balances, holder attribution, matching, consolidation, or financial totals |
| `bancaemdia-api` | Installation authentication, canonical contracts, server-side parsing/validation, idempotency, replay, account resolution, reconciliation, materialization, catalog truth, and financial state | Browser session data, credentials, remote page automation, or extension-only copies of financial rules |

The extension observes only the authenticated user's own bet-history responses caused by their navigation. It never places bets, clicks bookmaker controls, collects credentials, copies cookies/tokens, infers a login identity, bypasses site protections, or broadens access through wildcard domains.

## Central Issue Map

### ADR 021 — Collection Integrity & Extension Contract

| Central issue | Extension responsibility |
|---------------|--------------------------|
| **Issue 1 — Extension Client Foundation** | Complete the preserved-history repository as strict TypeScript/MV3; separate service worker, isolated content script, page bridge, storage, contracts, and adapters; port passive fetch/XHR observation; sanitize before persistence; provide redacted diagnostics; build and test a deterministic ZIP + SHA-256 artifact without sensitive browser permissions or remote code |
| **Issue 2 — Extension Pairing** | Generate an opaque installation identity; exchange a single-use code; keep the credential outside page-world code; authenticate only to the approved TLS origin; support rotation, revocation, disconnect, and re-pair without losing queued captures |
| **Issue 3 — Collection Contract v2** | Consume the API-owned pinned contract; persist captures in IndexedDB before transmission; deliver bounded batches with stable IDs; retain every item until its own ACK; retry safely across suspension/restart/offline states; quarantine permanent failures; expose metadata-only queue diagnostics |
| **Issue 4 — Casa × Telegram Matching** | No financial matching logic. Supply exact-host provenance, stable capture identity, and contract fixtures needed by the backend candidate engine |
| **Issue 5 — Casa × Telegram Consolidation** | No consolidation or financial mutation. Preserve delivery/provenance so the API can maintain one financial fact and two source contexts |
| **Issue 6 — Historical Reconciliation CLI** | No historical rewrite. Keep old sanitized fixture/envelope versions replayable when the reviewed API report needs them |
| **Issue 7 — Collection Integrity Test Matrix** | Contribute client fixtures for restart, retry, duplicate observation, partial ACK, token rotation/revocation, schema drift, and cross-version replay; extension and API suites must prove the same end-to-end invariants |

### ADR 022 — Bookmaker Coverage

| Central responsibility | Extension responsibility |
|------------------------|--------------------------|
| **Bookmaker catalog & host permissions** | Consume the signed/versioned technical catalog; declare exact `optional_host_permissions` as a reviewed build-time upper bound; request one host only after explicit user action; allow the runtime catalog to narrow or revoke but never grant an undeclared host; require a new build and browser grant for a new domain |
| **Adapter SDK & reader harness** | Implement typed exact-host adapters for fetch/XHR/WebSocket matching and sanitization; fail closed on ambiguous host, unknown schema, unsafe fixture, or oversized payload; keep canonical business parsing in the API |
| **Existing six bookmakers** | Port Betano, Superbet, BetMGM, Betfair, KTO/Kambi, and Esportiva/Altenar transport adapters; validate each current exact hostname with a fresh sanitized capture and backend replay; isolate one adapter's failure from all others |
| **bet365** | Passively observe only the minimum relevant WebSocket frames; preserve deterministic order and bounded connection-local sequence metadata; do not alter socket behavior or transmit credentials, outgoing actions, or unrelated live-score/odds traffic |
| **Coverage campaign & release gate** | Follow the user-assisted, secret-free capture protocol; record exact hostname, redirect, versions, date, and sanitization evidence; deliver domains in reviewed platform-based batches; account for every domain the user can open and log into without treating inaccessible or assumed-similar domains as supported |

## Client-Wide Safety Requirements

- Never request `<all_urls>`, cookies, browsing history, or remote executable code.
- The runtime catalog may only narrow permissions declared in the installed manifest; it cannot expand them.
- New exact domains require a reviewed extension build, version bump, explicit browser permission, sanitized fixture, backend reader test, and end-to-end replay.
- Fixtures and diagnostics must be scanned for cookies, bearer tokens, email, phone, CPF, account numbers, high-entropy session values, and unrelated personal data before entering Git history.
- Unknown schemas and protocol drift pause only the affected adapter and never create guessed financial data.
- Locally queued captures survive temporary permission removal, offline periods, token expiry, browser restart, and API retry without silent deletion.
- Regulatory status is informational and independent from technical support; technical accessibility never manufactures legal authorization.

## Cross-Repository Workflow

1. Open and review one central issue in `bancaemdia-api` using the structure defined by ADR 021 or ADR 022.
2. API and extension pull requests both reference that same issue as `pradyumna-001/bancaemdia-api#N`.
3. The API publishes canonical contracts/fixtures first when a client behavior depends on them; the extension pins the reviewed version/hash.
4. The central issue closes only when all applicable API, extension, fixture, security, and end-to-end acceptance criteria pass.
5. A client-only failure or schema drift remains visible on the same central issue; it does not create a shadow issue in the extension repository.

## Review Gate

Approval confirms that the dedicated extension repository remains private and independently built, while planning stays centralized in `bancaemdia-api`; ADRs 021 and 022 contain the only issue bodies that include extension responsibilities; client implementation may span a separate pull request without duplicating its product issue; and every extension release remains passive, least-privilege, exact-host, sanitized, versioned, reproducible, and revocable.
