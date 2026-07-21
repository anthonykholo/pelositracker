# Architecture

## Supported topology

One FastAPI process owns provider subscriptions, normalization, decisions, SSE
clients, and finalization. Durable observations and paper records use
PostgreSQL in production or SQLite for local research. `WEB_CONCURRENCY` must
be `1`; production startup rejects any other value because feed ownership and
event locks are process-local.

## Components

- `app/main.py`: lifespan, authenticated API, task supervision, and orchestration.
- `app/sources.py`: Polymarket and The Odds API transport normalization.
- `app/domain/`, `app/identity.py`, `app/gameclock.py`: time, identity, gate,
  and league-state contracts.
- `app/orderbook.py`, `app/execution.py`: verified book state and deterministic
  Decimal paper fills.
- `app/engine.py` and `native_engine/`: canonical JSON boundary and pure
  explicit-`as_of` policy calculation.
- `app/model_training.py`, `app/calibration.py`, `app/model_registry.py`: offline nested chronological
  model selection, beta calibration, event-block artifact construction, and
  strict consensus/independent-model artifact loading. Training never runs in
  the web process and no independent model artifact ships.
- `app/history.py`, `app/ledger.py`: immutable evidence and decision/order/fill/
  close/settlement marks.
- `app/static/`: static HTML, CSS, local vendored libraries, and event-driven JS.

The current safe boundary is deliberately paper-only. There is no wallet,
private-key, signing, exchange authentication, or real order-routing component.

## Concurrency boundary

Per-event locks serialize quote evaluation against finalization. Database work
in feed callbacks and finalization runs in the bounded asyncio thread executor.
Provider and notification tasks are owned by the lifespan and drained on
shutdown. Horizontal collectors require a future durable lease/message-stream
design and are not supported by this release.
