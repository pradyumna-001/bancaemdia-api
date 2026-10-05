# ADR 025: Line calculator market/data decision

Date: 2026-09-28

Status: **Proposed — No-Go pending dataset and model approval**

Issue: [#104](https://github.com/pradyumna-001/bancaemdia-api/issues/104)

## Context

The owner wants a data-based calculator that estimates a fair price at a different betting line, with the broadest defensible market coverage, possibly including throw-ins. One quote cannot identify the line curve. A static public sample was audited, but it has only four multi-line groups in soccer and no outcome labels; see the [research audit](../../research/line-calculator/README.md). A [free-tier live provider probe](../../research/line-calculator/live-coverage-2026-09-28.md) found current goal/corner/total ladders, but no historical odds or settled outcomes. No owner-approved 5,000–10,000-event historical dataset has been supplied. No model or calibration benchmark has run.

## Decision now

Do not implement or advertise a cross-line fair-odd estimate, including for the “other” market bucket. Reject underdetermined requests. Prepare a paid-source pilot only after review of actual provider export, full alternate lines, both sides, results, license and cost. The Odds API is the first documented low-cost pilot candidate; OpticOdds should be compared if it can demonstrate broader market history (especially throw-ins). The owner's willingness to evaluate a paid source is not dataset or expenditure approval.

| Candidate family | Evidence currently available | Go/No-Go |
|---|---|---|
| Soccer match/team goals | Current EU/UK event probes have multi-line paired groups; no historical benchmark | **No-Go** |
| Soccer match/team corners | Current US event probe has paired match/team/first-half ladders; no historical benchmark | **No-Go** |
| Soccer match/team cards, shots, fouls, offsides | No sufficient paired, settled sample | **No-Go** |
| Soccer throw-ins/laterals | Coverage of candidate providers unverified; no public ladder | **No-Go** |
| Soccer player shots, shots on target, goals, assists, fouls, cards, saves | Public sample has at most two paired multi-line groups; sampled current player shots had no complete pairs | **No-Go** |
| NBA/MLB/NHL match and team totals | Current probe has paired multi-line groups; no historical benchmark or outcome mapping | **No-Go** |
| Other sports, player props and “other” markets | Main player lines often paired, sampled alternate player lines lacked both sides; no historical benchmark | **No-Go** |
| Live variants of every above family | No synchronized clock/state/line history | **No-Go** |

The future method remains **undecided**. Compare monotone no-vig interpolation, non-parametric/ordinal survival approaches and appropriate distributional candidates on owner-approved, event-disjoint temporal data; do not select Poisson by assumption. For half lines, require simultaneous complete over/under quotes. For integer and quarter lines, require settlement and push-mass evidence. Approval for one market family does not approve another.

## Approval record (not yet populated)

| Field | Current value |
|---|---|
| Owner-approved provider/license | Pending |
| Owner-provided sanitized sample and consent | Pending |
| Immutable dataset version and SHA-256 | Pending |
| Independent event / paired-ladder counts per family | Pending |
| Temporal event-disjoint benchmark report | Pending |
| Calibration and sensitivity evidence | Pending |
| Selected method and supported range per family | Pending |
| Owner/admin approval of method **and** dataset | Pending |

## Required approval criteria and monitoring

Pre-register metric thresholds and target range before locked-test evaluation. The report must show calibration, log loss/Brier when applicable, held-out price error, coverage/abstention, event-bootstrap uncertainty, bookmaker/league stability, sensitivity to margin and quote age, and unsupported definitions. After approval, monitor calibration/drift by family and league, quote age, margin, impossible or nonmonotone curves, abstention frequency, source outages, license changes and model/dataset version. Suspend a family when its validation assumptions cease to hold. A separate implementation issue may be proposed **only after** explicit owner/admin approval of both dataset and model.

## Consequences

The current research artifacts make source evaluation reproducible and avoid giving bettors false precision. They do **not** meet issue #104's final acceptance criterion of an approved ADR and empirical calibration. This ADR remains No-Go until the missing evidence is supplied and reviewed.
