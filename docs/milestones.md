# Remediation milestones

The milestones are implemented sequentially. Each milestone must leave the paper-only invariant intact and add regression evidence before the next one begins.

## A — Contracts and fail-closed inputs

Typed settings, canonical vocabulary, timestamp provenance, league clock rules, state validation, disabled undocumented providers, and golden provider fixtures.

## B — Identity, migrations, history, and replay

Versioned schema migrations; canonical event/participant/market/outcome identities; mapping decisions and quarantine; lossless observation history; explicit `as_of`; deterministic decision hashes and replay parity.

## C — Executable books and paper lifecycle

Bulk books, snapshot/delta state machine, gap recovery, Decimal depth walking, fee lineage, partial-fill policy, paper order/position lifecycle, decision/close/settlement marks, and corrected CLV.

## D — Security and operations

Per-session authentication, CSRF/rate limits/security headers, webhook SSRF controls, strict static assets, tracked tasks, single-owner guards, readiness/health, reproducible container, and expanded CI.

## E — Consensus, calibration, uncertainty, and risk

Implemented in engine/tooling: chronological de-vig/consensus comparison,
monotone beta/identity artifacts, event-block uncertainty, net-EV gates,
per-decision/event/sport/correlated/aggregate exposure caps, persisted lineage,
and outcome-level explainability. No fitted artifact ships, so the default
remains display-only `WATCH` and no edge claim is made.

## F — Validated models only

Promotion boundary implemented: exact-segment versioned out-of-sample evidence,
required-input contracts, baseline comparisons, event-block support,
multiplicity control, review approval, separate persistence/evaluation lineage,
and a second Rust-side validation gate. No artifact ships, so no sport model is
enabled and unsupported combinations remain unavailable.
