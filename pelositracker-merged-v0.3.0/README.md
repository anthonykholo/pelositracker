# Live Edge Monitor

A paper-only live sports market monitor with a Rust scoring engine and a FastAPI dashboard. It combines live game state, Polymarket order-book data, and optional sportsbook prices to produce explainable `WATCH` or `PAPER BET` signals.

> This project never places wagers. Signal quality is not a predicted win rate, and the output is not financial advice.

## Features

- Public Polymarket market and sports WebSocket streams.
- Optional multi-sportsbook polling through TheOddsAPI.
- Rust-native consensus, momentum, freshness, spread, edge, and confidence calculations.
- Paper-signal safety gates with plain-language explanations.
- Responsive dashboard with persistent explanation panels.
- Event removal that cancels feed tasks and clears in-memory event data.
- Credential-free simulation mode for testing the complete pipeline.

## Quick start on Windows

Requirements: Python 3.12, Rust, and Microsoft C++ Build Tools.

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
build-rust.cmd
start.cmd
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765), then select **Launch live demo**.

`start.cmd` starts the server from the correct project directory. You only need to rerun `build-rust.cmd` after changing `native_engine/src/lib.rs`.

## Configuration

Copy `.env.example` to `.env` and set any values you want to override:

```env
THE_ODDS_API_KEY=
ODDS_POLL_SECONDS=5
SIGNAL_CONFIDENCE_THRESHOLD=72
SIGNAL_EDGE_THRESHOLD=0.035
MAX_DATA_AGE_SECONDS=20
```

The Polymarket public feeds do not require a key. TheOddsAPI integration remains disabled until `THE_ODDS_API_KEY` is set.

## Registering an event

Use the dashboard or `POST /api/events`:

```json
{
  "name": "Away at Home",
  "sport": "basketball",
  "home": "Home",
  "away": "Away",
  "polymarket_slug": "exact-polymarket-event-slug",
  "odds_api_sport": "basketball_nba",
  "odds_api_event_id": "provider-event-id"
}
```

For Polymarket, the slug is the portion after `/event/` in the event URL.

## Dashboard guide

- **WATCH**: one or more safety gates failed.
- **PAPER BET**: every configured gate passed; no wager is placed.
- **Model probability**: consensus probability plus a deliberately capped recent-scoring adjustment.
- **Estimated edge**: model probability minus the best executable market probability.
- **Signal quality**: a 0–100 quality score based on freshness, source agreement/count, spread, and edge strength—not a win probability.

## Architecture

```text
Polymarket WebSockets ─┐
TheOddsAPI polling ────┼─> Python feed adapters ─> Rust signal engine ─> FastAPI API ─> Dashboard
Demo stream ───────────┘
```

- `app/`: API, adapters, in-memory store, Python/Rust bridge, and dashboard.
- `native_engine/`: PyO3 Rust crate containing the scoring logic.
- `tests/`: engine compatibility and API lifecycle tests.
- `build-rust.cmd`: reproducible Windows native-extension build.
- `start.cmd`: local server launcher on port 8765.

## Tests

```cmd
.venv\Scripts\python.exe -m pytest -q
cargo test --manifest-path native_engine\Cargo.toml
```

## Model limitations

The current momentum component is a transparent capped heuristic, not a trained sport-specific model. Before considering real-money use, train and walk-forward test by sport, league, market, and game phase; calibrate probabilities; and account for latency, slippage, liquidity, limits, and rejected fills.

For production, use a licensed low-latency play-by-play provider as the authoritative game-state source. The public Polymarket sports feed explicitly may be delayed or incomplete.

