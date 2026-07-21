# Changelog

## 0.6.0 — Independent-model evidence registry

- Added a fail-closed exact sport/league/market registry for independently
  validated model artifacts; `ENABLE_INDEPENDENT_MODELS` alone can no longer
  expose the legacy score/clock benchmark.
- Required chronological train/validation/untouched-test windows, model/data/
  calibration hashes, minimum test observations/events, same-row proper-score
  wins against consensus, pregame, and Stern baselines, time/lead calibration
  slices, event-block support, search control, and explicit review approval.
- Restricted the future runtime contract to a fitted NBA moneyline logistic
  model requiring pregame, score, time, possession, overtime, and phase
  features; the legacy Brownian score/clock formula remains benchmark-only.
- Persisted independent-model and calibration lineage in ledger v6 and added
  same-row evaluation without changing paper actions.
- Shipped no fitted model artifact. Every sport model therefore remains
  unavailable and no predictive-edge claim is made.

## 0.5.0 — Chronological calibration and paper risk controls

- Added a v2 fitted-artifact contract with event-grouped nested chronological
  folds, de-vig/consensus candidate metrics, monotone beta-or-identity
  calibration, model hashes, and aligned event-block pipeline/calibration/
  execution-cost uncertainty draws.
- Replaced dispersion-based pseudo-confidence with explicit calibrated
  probability intervals, probability of positive net EV, net EV after
  executable cost, and machine-readable policy gates.
- Added per-decision, event, sport, transparent correlated-group, and aggregate
  paper exposure caps plus decision lineage.
- Persisted Milestone E decision/fill fields in forward-only ledger v5 and
  accounts v2 migrations and expanded evaluation with scoring decomposition,
  execution, drawdown, concentration, and event-block summaries.
- Kept v1 artifacts and every unsupported sport model display-only. No fitted
  artifact, real-order capability, or statistical edge claim is shipped.

## Unreleased -- Auditable paper-research remediation

- Added explicit provider/receipt/processing/as-of timestamps and fail-closed freshness gates.
- Added canonical event/market identity decisions, quarantine states, versioned migration records, and lossless quote/state history.
- Made replay deterministic and tied decisions to canonical hashes and declared engine/config/model/calibration/execution versions.
- Added full-depth paper execution with fee, status, minimum-size, tick, partial-fill, portfolio, and closing-mark controls.
- Replaced subjective source weights with one observation per canonical source family and disabled actionable signals without chronological calibration support.
- Added Argon2id authentication, per-session revocation, CSRF protection, rate limits, SSRF-safe notifications, strict CSP, security headers, and local pinned frontend assets.
- Added provider supervision, readiness/runtime diagnostics, a single-worker production guard, non-root container execution, and broader CI checks.
- Preserved the paper-only boundary: no wallets, signing, exchange credentials, or real order routing were added.

## Unreleased — Live-status and signal audit

- Replaced the five-hour schedule guess with fresh Polymarket `sport_result` status for `LIVE`; schedule-only games now show `STARTED · VERIFYING`.
- Removed false matchup discovery caused by matching the letters `vs` inside participant names.
- Forced Polymarket cards to calculate edge against the exact executable Polymarket ask displayed on the website.
- Separated data quality from edge size and exposed freshness, agreement, source-coverage, and execution components.
- Changed entry ceilings to use the full risk-adjusted required edge and added raw edge / required edge / edge-buffer display.
- Removed the obsolete offline demo path so monitored signals come from live provider data.

## 0.4.0 — URL-first markets and paper positions

- Added full Polymarket event-link registration, including mobile share links.
- Automatically infers event metadata and attempts a quota-free The Odds API event match.
- Shows only active, order-accepting selections with an executable Polymarket ask.
- Added initial CLOB order-book snapshots, live depth, spread, liquidity, minimum size, and tick metadata.
- Added entry price ceilings, margin-to-ceiling guidance, and execution/data risk flags.
- Added durable user-entered paper positions with cash-out value, P/L, remaining hold edge, and explainable `HOLD`, `CONSIDER CASH`, or `EXIT WATCH` statuses.
- Preserved the market-relative Rust engine, cyberpunk HUD, and durable CLV/calibration truth loop.

## 0.3.2 — The Odds API V4

- Corrected authentication and request paths for The Odds API V4.
- Added configurable regions, markets, bookmakers, and a quota-safer polling default.
- Filtered sport-wide responses to the registered matchup and preserved spread/total points.
- Added sanitized terminal warnings and adapter tests without consuming API credits.
- Changed the default The Odds API polling interval to 45 seconds.

## 0.3.1 — Python 3.14 compatibility

- Upgraded PyO3 to 0.29 for Python 3.14 support.
- Refreshed the pinned Python dependencies and verified them on Python 3.14.
- Added a visible `env.example` so browser-based GitHub uploads do not omit the template.
- Made `.env` optional at startup and added Python 3.14 to CI.

## 0.3.0 — Merged release

- Merged the redesigned compact dashboard with the Rust-backed application.
- Preserved Rust-native scoring and the FastAPI feed architecture.
- Added persistent **Why this signal?** panels across live refreshes.
- Added event removal with task cancellation and in-memory cleanup.
- Clarified model probability, estimated edge, and signal-quality labels.
- Added accessible form labels, keyboard focus states, responsive layouts, and inline errors.
- Added refresh de-duplication and safer client-side rendering.
- Added `start.cmd`, repository cleanup rules, and GitHub Actions CI.
- Consolidated two divergent app copies into one canonical repository layout.
