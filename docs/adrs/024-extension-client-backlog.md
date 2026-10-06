# Extension boundary and independent worldwide backlog

## Current decision — 2026-09-29

This supersedes the previous centralized responsibility map. Extension issues are enabled and implementation is tracked in [bancaemdia-extension](https://github.com/wfcgit-hub/bancaemdia-extension/issues). A new backlog was created from the actual repository audit; old API issues were not migrated or renumbered. Closed API #106 is historical context only.

The API owns installation authentication (#107), canonical collection contract and ACKs (#108), integrity (#112), signed technical catalog (#113), reader harness (#114), six-reader regression (#115), bet365 stateful reconstruction (#116), worldwide reader campaign (#117) and combined API E2E (#118). The extension owns passive capture, permissions, adapters, sanitization before persistence, pairing client, local outbox, delivery, build and distribution.

| API dependency | Client issue |
|---|---|
| #107 pairing authentication | [extension #6](https://github.com/wfcgit-hub/bancaemdia-extension/issues/6) |
| #108 schema/envelopes/ACK | [extension #7](https://github.com/wfcgit-hub/bancaemdia-extension/issues/7), [#8](https://github.com/wfcgit-hub/bancaemdia-extension/issues/8) |
| #113 signed projection | [extension #10](https://github.com/wfcgit-hub/bancaemdia-extension/issues/10), [#15](https://github.com/wfcgit-hub/bancaemdia-extension/issues/15) |
| #114–115 reader regressions | [extension #11](https://github.com/wfcgit-hub/bancaemdia-extension/issues/11), [#12](https://github.com/wfcgit-hub/bancaemdia-extension/issues/12) |
| #116 bet365 reconstruction | [extension #13](https://github.com/wfcgit-hub/bancaemdia-extension/issues/13) |
| #117 worldwide readers | [extension #14](https://github.com/wfcgit-hub/bancaemdia-extension/issues/14) |
| #118 E2E | [extension #17](https://github.com/wfcgit-hub/bancaemdia-extension/issues/17) |

Each repository has separate PRs and acceptance evidence. Client-only changes do not close API work. The existing collection contract is v1; publish the canonical v2 artifact before the client pins a source commit/hash. No local invented client schema can replace it.

## Worldwide technical scope

Every item in the user's forthcoming list is a real implementation target, with additional lists supported incrementally. Preserve brand, region, exact final hostname, aliases, redirects, evidence and original input. Do not infer protocol sharing from branding. Brazilian authorization lists are informational sources among many; regulation and accessibility do not remove a technical target.

The API catalog must accept worldwide candidates and represent unknown jurisdiction/regulatory evidence without classifying it as technical rejection. Keep inaccessible/geo-blocked/unknown-protocol targets visible with evidence, cause, next action and review date. Backend readers own financial interpretation, canonical identity and state; the extension supplies sanitized passive captures.

Client validation, API reader validation and end-to-end validation are separate gates. Real sanitized captures and observed lifecycle states are required before support claims; synthetic fixtures and catalog names are not sufficient. Ordinary bookmaker navigation is user-controlled; no password, cookie, token, raw HAR or browser profile should be requested.

## Permissions and rollout

Chrome permits broad optional host declarations, but the selected client policy uses reviewed exact optional HTTPS hosts. A new host therefore needs a client build and browser consent. A signed API catalog can narrow the compiled/granted set, never grant permission or deliver remote executable code. Source: [Chrome permissions](https://developer.chrome.com/docs/extensions/reference/api/permissions), checked 2026-09-29.

## Remaining API-specific work

Update implementation/acceptance of #113 and #117 to keep worldwide candidates nominally traceable and Brazilian regulatory ingestion optional. Publish #107/#108/#113 artifacts with version/hash metadata. These remain API deliverables; this documentation PR does not implement or close them.
