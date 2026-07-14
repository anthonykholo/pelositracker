# Live Edge Monitor

An API-first, paper-only live sports monitoring prototype. Register an event, stream score and market updates, and receive explainable signals gated by freshness, source agreement, spread, estimated edge, and a configurable signal-quality threshold.

## What it does

- Streams public Polymarket order-book updates and its sports-result WebSocket.
- Optionally polls TheOddsAPI for normalized sportsbook moneyline/spread/total prices.
- Includes a one-click simulation so the complete pipeline works without API credentials.
- Removes binary-market vig source by source, blends a deliberately capped recent-scoring adjustment, then compares the estimate with the best executable price.
- Emits `PAPER_BET` only when every safety gate passes. It never places a wager.

## Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`, then choose **Launch live demo**. Environment values are read by the process; PowerShell users can set them with `$env:THE_ODDS_API_KEY="..."` before starting. The app intentionally does not load or expose secret values to the browser.

## Registering a real event

Use the dashboard form or `POST /api/events`. For Polymarket, provide the exact event slug. For TheOddsAPI, provide its sport key and preferably its event ID so the poller does not need to scan a slate.

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

## Important model limits

The displayed confidence is a **signal-quality score**, not a calibrated probability of winning. The initial momentum feature is a transparent capped scoring-run heuristic; it must be replaced by sport-specific models trained and walk-forward tested on timestamped historical data before any real-money use. Avoid survivorship and look-ahead bias, include latency and fill/slippage, and calibrate separately by sport, league, market, and game phase.

For production, use a licensed low-latency play-by-play feed such as Sportradar as the authoritative game-state source. Consumer-site scraping is not implemented because it is fragile, can violate terms, and offers no latency or correctness guarantee.

## Tests

```powershell
pytest -q
```
