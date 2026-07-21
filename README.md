# Live Edge Monitor

An auditable, paper-only sports-market research system. It records Polymarket
books, independent sportsbook source-family prices, and provider game state;
then produces reproducible `WATCH` or `PAPER_BET` policy output through a Rust
engine and FastAPI dashboard.

> The project cannot connect a wallet, sign an order, deposit funds, or place a
> real wager. Signal quality is data reliability—not win probability or advice.

## Safety behavior

The system fails closed. A selection remains `WATCH` when any required input is
missing or unsafe, including provider time, identity confidence, valid game
state, independent source families, complete executable depth, market status,
fee metadata, calibration evidence, or risk capacity.

No calibration artifact and no independently validated sport model are shipped.
Therefore a default installation is display/research-only and will not emit an
eligible paper fill. See [model support](docs/model-support.md).

## Data and decision pipeline

1. A Polymarket link is resolved into event, market, outcome, and token identity.
2. Complete books are fetched with the bulk `/books` endpoint and maintained from
   verified WebSocket snapshots/deltas. A hash/timestamp gap forces resnapshot.
3. The Odds API, when configured, contributes bookmaker update timestamps and
   quota telemetry. The undocumented Action Network and Pinnacle guest adapters
   are disabled unless explicitly enabled and credentialed.
4. Provider time, receipt time, and processing time remain distinct. Unknown
   provider time never falls back to local receipt time for policy eligibility.
5. One de-vigged probability per independent source family is aggregated with
   the reviewed artifact's consensus method. Equal-family logit pooling is the
   fail-safe default; the target venue is excluded.
6. Decimal execution walks full ask depth for the configured paper notional and
   includes the market fee curve. Incomplete fills are rejected by default.
7. The Rust boundary receives an explicit UTC `as_of`; identical canonical input
   produces the same decision hash in live evaluation and replay.
8. Decision-time, close-time, fill, and settlement marks are stored separately.
   CLV compares the recorded paper fill with the last valid executable close; it
   is never reconstructed from settlement-time consensus.

## Quick start on Windows

Requirements: Python 3.10–3.15, Rust, and Microsoft C++ Build Tools.

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
build-rust.cmd
start.cmd
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Local development defaults
to `admin` / `admin`; production startup rejects those credentials. `start.cmd`
creates `.env` from `env.example` and runs one feed-owning worker.

## Core configuration

```env
THE_ODDS_API_KEY=
ODDS_POLL_SECONDS=45
ODDS_REGIONS=us
ODDS_MARKETS=h2h,spreads,totals
ODDS_BOOKMAKERS=

MAX_DATA_AGE_SECONDS=120
SIGNAL_CONFIDENCE_THRESHOLD=0
SIGNAL_EDGE_THRESHOLD=0
SIGNAL_KELLY_FRACTION=0.25

ENABLE_ACTION_NETWORK=false
ENABLE_PINNACLE_GUEST=false
PINNACLE_GUEST_API_KEY=
ENABLE_INDEPENDENT_MODELS=false
INDEPENDENT_MODEL_ARTIFACT=
CALIBRATION_ARTIFACT=

DATABASE_URL=
LEDGER_DB=ledger.db
HISTORY_DB=history.db
APP_ENV=development
WEB_CONCURRENCY=1
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
```

Polymarket public market data requires no key. The Odds API integration starts
only when `THE_ODDS_API_KEY` is set. Its `x-requests-*` headers are retained in
`/api/runtime`. Keep the 45-second poll within `MAX_DATA_AGE_SECONDS` and your
provider quota.

SQLite is the local default. `DATABASE_URL` selects PostgreSQL for every store.
All stores use component-scoped, versioned migrations with checksum drift
protection; the same migrations can be applied repeatedly.

Authentication uses Argon2 password hashes and individually revocable,
expiring sessions. State-changing requests require a double-submit CSRF token.
Production requires non-default credentials and one worker until distributed
feed ownership is implemented. Webhooks require HTTPS, an explicit host
allowlist, public DNS results, and no redirects.

## Registering an event

Paste the full event URL shown in Polymarket, for example:

```text
https://polymarket.com/event/example-event-slug
```

The slug is the text after `/event/`. The dashboard resolves active CLOB tokens
and lists only selections with an executable ask. Visibility does not imply a
user is allowed to trade in their jurisdiction.

Manual API registration is also available:

```json
{
  "name": "Away at Home",
  "sport": "basketball",
  "league": "nba",
  "home": "Home",
  "away": "Away",
  "polymarket_slug": "exact-event-slug",
  "odds_api_sport": "basketball_nba",
  "odds_api_event_id": "provider-event-id"
}
```

Provider joins require sport, league, both participants, and start-time evidence.
Ambiguous doubleheaders are quarantined, not guessed.

## Reading the dashboard

- **Consensus probability**: the selected transformation of one price per
  independent source family, excluding the target venue. It is not a sport model.
- **Calibrated consensus**: consensus transformed by a reviewed chronological
  identity/beta calibration artifact; unavailable means display-only.
- **Independent model**: a separately validated sport model, when available.
  It requires operator opt-in and a reviewed exact-segment registry artifact.
  None is enabled in the repository today.
- **Market probability**: fee/slippage-adjusted executable paper price.
- **Net EV**: calibrated probability minus depth-weighted executable cost and
  fees, per share and for the simulated fill.
- **P(net EV > 0)**: share of aligned historical event-block draws whose net EV
  is positive. It is unavailable without an eligible artifact.
- **Required edge**: configured base floor plus the declared market premium.
- **Signal quality**: a policy score over completeness, provider freshness,
  identity, execution, source independence, sample support, and calibration
  support. It is not a win probability.
- **WATCH**: at least one mandatory gate failed or is unknown.
- **PAPER_BET**: all gates passed for a simulated paper order only.
- **CLV**: last valid executable close minus recorded paper fill price.

## Repository map

- `app/domain/`: canonical time, gate, and quality contracts.
- `app/identity.py`: deterministic identity and mapping decisions.
- `app/execution.py`, `app/orderbook.py`: Decimal fills and book state machine.
- `app/`: providers, API, session security, migrations, lifecycle, and replay.
- `native_engine/`: pure explicit-`as_of` consensus/policy engine.
- `migrations/`: dialect migration-ledger snapshots.
- `tests/fixtures/providers/`: golden provider payloads.
- `docs/audit-baseline.md`: pre-remediation evidence and rollback point.

Design and operating records are in [architecture](docs/architecture.md),
[data lineage](docs/data-lineage.md), [provider support](docs/provider-support.md),
[consensus model card](docs/model-card-consensus.md),
[independent-model registry](docs/independent-model-registry.md),
[backtesting methodology](docs/backtesting-methodology.md),
[security](docs/security.md), and [operations](docs/operations.md).

## Offline calibration workflow

Milestone E artifacts are built offline; the live service never trains or
promotes itself. Export settled, point-in-time/out-of-fold observations as
JSONL and declare candidate pipeline metadata in JSON, then run:

```cmd
.venv\Scripts\python.exe -m app.model_training observations.jsonl candidates.json calibration-v2.json --selection-through 2024-06-30T23:59:59Z --calibration-through 2024-12-31T23:59:59Z --validation-through 2025-03-31T23:59:59Z --model-version consensus-2025q1 --sport basketball --league nba --market moneyline
```

The builder refuses mixed segments, event leakage, fewer than 1,000 observations
in any fold, and fewer than 200 event-block draws. Review the artifact and its
test metrics before setting `CALIBRATION_ARTIFACT`; producing a file does not
establish a statistical or profitable edge.

Independent models use a separate `INDEPENDENT_MODEL_ARTIFACT`. Its entries
must have exact sport/league/market identity, immutable model and data hashes,
chronological train/validation/test windows, at least 1,000 untouched-test
  observations from 200 events, same-row proper-score improvement over
  consensus, pregame, and Stern baselines, time/lead calibration slices, at
  least 1,000 event-block draws, search-multiplicity control, required-input
  declarations, and explicit review approval. Registry v1 is limited to a
  fitted NBA moneyline logistic contract; score/clock Brownian routines remain
  benchmark-only.
`ENABLE_INDEPENDENT_MODELS=true` without this artifact has no effect. No such
artifact ships with this repository.

## Verification

```cmd
.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp
.venv\Scripts\python.exe -m ruff check app tests
.venv\Scripts\python.exe -m mypy app/domain app/execution.py app/identity.py app/security.py app/settings.py app/calibration.py app/model_training.py app/model_registry.py
cargo fmt --manifest-path native_engine\Cargo.toml -- --check
cargo test --manifest-path native_engine\Cargo.toml
cargo clippy --manifest-path native_engine\Cargo.toml --all-targets -- -D warnings
```

CI also performs a dependency audit and applies all migrations twice against
PostgreSQL. `/api/health` is liveness; `/api/ready` checks initialized runtime
dependencies; authenticated `/api/runtime` exposes counters and provider quota.
