# Live Edge Monitor

A paper-only live sports market monitor with a Rust scoring engine and a FastAPI dashboard. It combines live game state, Polymarket order-book data, and optional sportsbook prices to produce explainable `WATCH` or `PAPER BET` signals.

> This project never places wagers. Signal quality is not a predicted win rate, and the output is not financial advice.

## Features

- Public Polymarket market and sports WebSocket streams.
- Paste a complete Polymarket event or mobile share link; no manual slug is required.
- Actionable-only selection view based on accepting-orders status and executable asks.
- Optional multi-sportsbook polling through TheOddsAPI.
- Rust-native consensus, momentum, freshness, spread, edge, and confidence calculations.
- Paper-signal safety gates with plain-language explanations.
- Responsive dashboard with persistent explanation panels.
- Event removal that cancels feed tasks and clears in-memory event data.
- Credential-free simulation mode for testing the complete pipeline.
- Durable paper positions with entry price, shares, cash-out value, P/L, and explainable hold/cash monitoring.

## Quick start on Windows

Requirements: Python 3.10 through 3.15, Rust, and Microsoft C++ Build Tools.

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
build-rust.cmd
start.cmd
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765), then select **Launch live demo**.

`start.cmd` starts the server from the correct project directory. You only need to rerun `build-rust.cmd` after changing `native_engine/src/lib.rs`.

## Configuration

The settings file is optional. To customize it, copy `env.example` to `.env` and set any values you want to override:

```env
THE_ODDS_API_KEY=
ODDS_POLL_SECONDS=45
ODDS_REGIONS=us
ODDS_MARKETS=h2h,spreads,totals
ODDS_BOOKMAKERS=
SIGNAL_CONFIDENCE_THRESHOLD=72
SIGNAL_EDGE_THRESHOLD=0.035
MAX_DATA_AGE_SECONDS=20
```

The Polymarket public feeds do not require a key. The Odds API integration remains disabled until `THE_ODDS_API_KEY` is set. The default request uses American prices from U.S. bookmakers. Keep the key only in `.env`; that file is excluded from Git.

## Registering an event

The dashboard only needs a Polymarket event link:

```text
https://polymarket.com/event/example-event-slug
```

The app resolves the slug, event title, active markets, CLOB tokens, order books, and—when possible—the matching The Odds API event. It lists only selections currently accepting orders with an executable ask. Public market visibility does not imply that trading is available or legal for every user or location.

The API also accepts URL-first registration:

```json
{
  "polymarket_url": "https://polymarket.com/event/example-event-slug"
}
```

The older manual fields remain available through `POST /api/events` when needed.

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

For The Odds API, get valid sport keys from `GET /v4/sports` and event IDs from `GET /v4/sports/{sport}/events`. Supplying an event ID is recommended because it prevents team-name mismatches and limits the response to one event. Polling three markets in one region generally costs three usage credits per successful request, so choose `ODDS_POLL_SECONDS` according to your plan.

## Dashboard guide

- **WATCH**: one or more safety gates failed.
- **PAPER BET**: every configured gate passed; no wager is placed.
- **Model probability**: consensus probability plus a deliberately capped recent-scoring adjustment.
- **Estimated edge**: model probability minus the best executable market probability.
- **Signal quality**: a 0–100 quality score based on freshness, source agreement/count, spread, and edge strength—not a win probability.
- **Entry ceiling**: model fair probability minus the configured minimum edge. An ask below the ceiling has enough modeled margin to pass that gate.
- **HOLD / CONSIDER CASH / EXIT WATCH**: paper-position heuristics using the executable bid, spread, P/L, model fair value, remaining edge, and data quality. They are not personalized financial advice or guaranteed outcomes.
- **Cash value**: shares multiplied by the current best bid, before fees, slippage, or failed fills.

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

The entry and exit statuses are transparent heuristics, not a trained sport-specific model or instruction to trade. Before considering real-money use, train and walk-forward test by sport, league, market, and game phase; calibrate probabilities; and account for latency, slippage, liquidity, limits, fees, rules, suspended books, and rejected fills.

For production, use a licensed low-latency play-by-play provider as the authoritative game-state source. The public Polymarket sports feed explicitly may be delayed or incomplete.
