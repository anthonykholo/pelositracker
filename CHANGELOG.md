# Changelog

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
