from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Form
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .engine import SignalEngine
from . import __version__, backtest
from .accounts import AccountBook, DEFAULT_STRATEGIES
from .advice import market_views, position_views
from .history import HistoryDB
from .ledger import Ledger
from .lines import pregame_priors
from .models import Event, GameState, Quote, as_json
from .sources import (extract_polymarket_slug, infer_polymarket_event,
                      match_odds_api_event, odds_api_poll, polymarket_event,
                      polymarket_market_stream, polymarket_sports_events,
                      polymarket_sports_stream, sports_game_status)
from .actionnetwork import action_network_poll
from .pinnacle import pinnacle_poll
from .store import Store

load_dotenv()
store = Store()
ledger: Ledger | None = None
account_book: AccountBook | None = None
history_db: HistoryDB | None = None
engine = SignalEngine(float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "0.0")),
                      float(os.getenv("SIGNAL_EDGE_THRESHOLD", "0.0")),
                      float(os.getenv("MAX_DATA_AGE_SECONDS", "20")),
                      kelly_fraction=float(os.getenv("SIGNAL_KELLY_FRACTION", "0.25")),
                      edge_z=float(os.getenv("SIGNAL_EDGE_Z", "1.0")))
tasks: dict[str, list[asyncio.Task]] = {}
_finalized: set[str] = set()
_pregame: dict[str, dict] = {}  # event_id -> {"spread": home point, "total": line}, captured near tip
_subscribers: set[asyncio.Queue] = set()  # SSE clients for real-time dashboard pushes
_sports_status: dict[str, dict] = {}  # latest public Polymarket sport_result by event slug
_config_state = {"auto_monitor": False}

_auth_users_env = os.getenv("AUTHORIZED_USERS")
if _auth_users_env:
    AUTHORIZED_USERS = {}
    for pair in _auth_users_env.split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            AUTHORIZED_USERS[u.strip()] = p.strip()
else:
    AUTHORIZED_USERS = {
        os.getenv("ADMIN_USERNAME", "admin"): os.getenv("ADMIN_PASSWORD", "admin")
    }

AUTH_TOKEN = secrets.token_urlsafe(32)

async def verify_auth(request: Request):
    if request.cookies.get("auth_token") != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

def _notify_subscribers() -> None:
    """Wake every SSE client that a snapshot changed (coalesced per client)."""
    for queue in list(_subscribers):
        if queue.empty():
            try:
                queue.put_nowait(1)
            except asyncio.QueueFull:
                pass
_FINAL_STATUSES = {"final", "ended", "closed", "complete", "finished"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _require_safe_id(value: str | None, field: str) -> None:
    # These are interpolated into outbound API paths; reject path/query injection.
    if value is not None and not _SAFE_ID.match(value):
        raise HTTPException(400, f"invalid {field}")


async def on_state(state: GameState):
    store.add_state(state)
    if history_db is not None:
        await asyncio.to_thread(history_db.log_state, state)
    await record(state.event_id)
    if str(state.status).lower() in _FINAL_STATUSES:
        await finalize_event(state.event_id)


async def on_sports_status(slug: str, payload: dict) -> None:
    """Cache authoritative live/final state for discovery without paid polling."""
    snapshot = dict(payload)
    snapshot["_received_at"] = datetime.now(timezone.utc).isoformat()
    _sports_status[slug] = snapshot
    normalized = sports_game_status(snapshot)
    if not _discover_cache.get("data") or normalized is None:
        return
    if normalized == "final":
        _discover_cache["data"] = [game for game in _discover_cache["data"]
                                   if game.get("slug") != slug]
        return
    for game in _discover_cache["data"]:
        if game.get("slug") == slug:
            game["status"] = normalized
            game["status_source"] = "polymarket-live-feed"


async def on_quotes(quotes: list[Quote]):
    store.add_quotes(quotes)
    if history_db is not None and quotes:
        await asyncio.to_thread(history_db.log_quotes, quotes)
    if quotes:
        await record(quotes[0].event_id)


def recompute(event_id: str) -> list:
    event = store.events.get(event_id)
    if event is None:  # event removed between emit and callback
        return []
    quotes = store.quotes[event_id]
    prior = _pregame.setdefault(event_id, {"spread": None, "total": None})
    if prior["spread"] is None or prior["total"] is None:
        spread, total = pregame_priors(quotes, event.home, event.away)
        prior["spread"] = prior["spread"] if prior["spread"] is not None else spread
        prior["total"] = prior["total"] if prior["total"] is not None else total
    signals = engine.evaluate(event_id, quotes, store.states[event_id], event.away,
                              sport=event.sport, home_outcome=event.home,
                              pregame_spread=prior["spread"], pregame_total=prior["total"])
    store.set_signals(event_id, signals)
    return signals


async def record(event_id: str) -> None:
    signals = recompute(event_id)
    _notify_subscribers()  # push the fresh snapshot to the dashboard immediately
    event = store.events.get(event_id)
    # Ledger commits fsync to disk; keep that off the event loop.
    if ledger is not None and event is not None and signals:
        await asyncio.to_thread(ledger.record_signals, event, signals)
    if account_book is not None and event is not None and signals:
        placed_bets = await asyncio.to_thread(account_book.place, event, signals)
        if placed_bets:
            from .notify import notify_webhook
            for p in placed_bets:
                if p.get("webhook_url"):
                    asyncio.create_task(notify_webhook(p["webhook_url"], p))


def _winner_labels(event: Event, home_score: float, away_score: float) -> set[str]:
    if home_score > away_score:
        return {"home", event.home}
    if away_score > home_score:
        return {"away", event.away}
    return {"draw", "Draw"}  # a tie settles the Draw outcome, not nothing


async def finalize_event(event_id: str) -> None:
    """Cancel the event's feeds, snapshot the closing consensus (CLV), settle."""
    if event_id in _finalized:
        return
    _finalized.add(event_id)
    for task in tasks.pop(event_id, []):  # stop paid pollers / streams for a dead game
        task.cancel()
    if ledger is None:
        return
    event = store.events.get(event_id)
    signals = store.signals.get(event_id) or []
    fair_by_selection = {
        (s.market, s.outcome): (s.market_fair_prob or s.model_probability)
        for s in signals
        if (s.market_fair_prob or s.model_probability)
    }
    states = store.states.get(event_id) or []
    winners = _winner_labels(event, states[-1].home_score, states[-1].away_score) \
        if (event and states) else set()

    def _writes():
        ledger.snapshot_closing(event_id, fair_by_selection)
        if winners:
            ledger.settle_moneyline(event_id, winners)
        if account_book is not None and event is not None and states:
            account_book.settle(event, states[-1].home_score, states[-1].away_score)
        if history_db is not None and event is not None:
            prior = _pregame.get(event_id, {})
            final_state = states[-1] if states else None
            history_db.log_outcome(event, prior.get("spread"), prior.get("total"), final_state)

    await asyncio.to_thread(_writes)


async def auto_monitor_loop():
    while True:
        try:
            if _config_state["auto_monitor"]:
                games = await polymarket_sports_events(live_statuses=_sports_status)
                for game in games:
                    if game.get("status") == "live":
                        slug = game.get("slug")
                        if slug and not any(e.polymarket_slug == slug for e in store.events.values()):
                            try:
                                await add_event(EventIn(polymarket_url=f"https://polymarket.com/event/{slug}"))
                            except Exception:
                                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ledger, account_book, history_db
    ledger = Ledger()
    account_book = AccountBook()
    history_db = HistoryDB()
    account_book.seed(DEFAULT_STRATEGIES)
    sports_task = asyncio.create_task(polymarket_sports_stream(
        lambda: list(store.events.values()), on_state, on_sports_status))
    auto_task = asyncio.create_task(auto_monitor_loop())
    yield
    sports_task.cancel()
    auto_task.cancel()
    for group in tasks.values():
        for task in group:
            task.cancel()
    ledger.close()
    account_book.close()
    history_db.close()


app = FastAPI(title="Live Sports Signal Monitor", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class EventIn(BaseModel):
    polymarket_url: str | None = None
    name: str | None = None
    sport: str | None = None
    home: str | None = None
    away: str | None = None
    league: str = ""
    polymarket_slug: str | None = None
    odds_api_sport: str | None = None
    odds_api_event_id: str | None = None


class PositionIn(BaseModel):
    token_id: str
    market: str
    outcome: str
    shares: float = Field(gt=0, le=1_000_000)
    avg_entry_price: float = Field(gt=0, lt=1)


class StrategyIn(BaseModel):
    name: str
    edge_threshold: float = 0.03
    sizing: str = "kelly"
    kelly_multiplier: float = 1.0
    flat_stake: float = 100.0
    start_bankroll: float = 10000.0
    webhook_url: str = ""


class ConfigIn(BaseModel):
    auto_monitor: bool


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/watch")
async def watch():
    return FileResponse(Path(__file__).parent / "static" / "watch.html")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    for auth_u, auth_p in AUTHORIZED_USERS.items():
        if secrets.compare_digest(username, auth_u) and secrets.compare_digest(password, auth_p):
            response.set_cookie(key="auth_token", value=AUTH_TOKEN, httponly=True, samesite="strict")
            return {"status": "ok"}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("auth_token")
    return {"status": "ok"}


@app.get("/api/config")
async def config():
    return {"confidence_threshold": engine.confidence_threshold, "edge_threshold": engine.edge_threshold,
            "max_age_seconds": engine.max_age_seconds, "auto_monitor": _config_state["auto_monitor"]}


@app.post("/api/config", dependencies=[Depends(verify_auth)])
async def update_config(payload: ConfigIn):
    _config_state["auto_monitor"] = payload.auto_monitor
    return await config()


_discover_cache: dict = {"at": 0.0, "data": []}


@app.get("/api/discover", dependencies=[Depends(verify_auth)])
async def discover():
    """Browse live/upcoming Polymarket sports games to add without a link."""
    now = time.monotonic()
    if _discover_cache["data"] and now - _discover_cache["at"] < 45:
        return _discover_cache["data"]  # cache so browsing doesn't hammer Gamma
    try:
        games = await polymarket_sports_events(live_statuses=_sports_status)
    except Exception as exc:
        raise HTTPException(502, f"Could not reach Polymarket: {exc}") from exc
    _discover_cache.update(at=now, data=games)
    return games


def _sort_events_by_edge():
    events = list(store.events.values())
    def max_edge(event):
        signals = store.signals.get(event.id, [])
        return max((s.edge for s in signals if s.edge is not None), default=0.0)
    events.sort(key=max_edge, reverse=True)
    return events

@app.get("/api/events", dependencies=[Depends(verify_auth)])
async def list_events():
    return [event_view(event.id) for event in _sort_events_by_edge()]

def _events_snapshot_sse() -> str:
    payload = json.dumps([event_view(event.id) for event in _sort_events_by_edge()], default=str)
    return f"data: {payload}\n\n"


@app.get("/api/stream", dependencies=[Depends(verify_auth)])
async def stream():
    """Server-Sent Events: push the events snapshot the instant data changes."""
    async def generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        _subscribers.add(queue)
        try:
            yield _events_snapshot_sse()  # initial state
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                    yield _events_snapshot_sse()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # keep the connection warm
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def event_view(event_id: str):
    event = store.events.get(event_id)
    if not event:
        raise HTTPException(404, "event not found")
    signals = store.signals[event_id]
    positions = ledger.event_positions(event_id) if ledger is not None else []
    return {"event": as_json(event),
            "latest_state": as_json(store.states[event_id][-1]) if store.states[event_id] else None,
            "signals": as_json(signals),
            "actionable_markets": market_views(store.quotes[event_id], signals, engine.edge_threshold),
            "positions": position_views(positions, store.quotes[event_id], signals,
                                          engine.confidence_threshold),
            "state_points": len(store.states[event_id]),
            "quote_points": len(store.quotes[event_id])}


@app.get("/api/events/{event_id}/history", dependencies=[Depends(verify_auth)])
async def get_event_history_api(event_id: str):
    if history_db is None:
        raise HTTPException(503, "History database not available")
    return history_db.get_event_history(event_id)


@app.get("/api/events/{event_id}", dependencies=[Depends(verify_auth)])
async def get_event(event_id: str):
    return event_view(event_id)


@app.post("/api/events", status_code=201, dependencies=[Depends(verify_auth)])
async def add_event(payload: EventIn):
    values = payload.model_dump()
    link_or_slug = payload.polymarket_url or payload.polymarket_slug
    if link_or_slug:
        try:
            slug = extract_polymarket_slug(link_or_slug)
        except Exception as exc:
            raise HTTPException(400, f"Could not parse Polymarket link: {exc}") from exc
        _require_safe_id(slug, "polymarket slug")
        try:
            poly = await polymarket_event(slug)
        except Exception as exc:
            raise HTTPException(400, f"Could not resolve Polymarket link: {exc}") from exc
        if not poly.get("active") or poly.get("closed"):
            raise HTTPException(400, "Polymarket event is not active")
        actionable = [market for market in poly.get("markets", [])
                      if market.get("active", True) and not market.get("closed", False)
                      and market.get("enableOrderBook", True) and market.get("acceptingOrders", False)
                      and market.get("clobTokenIds")]
        if not actionable:
            raise HTTPException(400, "This event has no markets currently accepting orders")
        inferred = infer_polymarket_event(poly)
        values.update({
            "polymarket_slug": slug,
            "polymarket_url": f"https://polymarket.com/event/{slug}",
            "polymarket_restricted": bool(poly.get("restricted", False)),
            "name": payload.name or inferred["name"],
            "sport": payload.sport or inferred["sport"],
            "home": payload.home or inferred["home"],
            "away": payload.away or inferred["away"],
            "odds_api_sport": payload.odds_api_sport or inferred["odds_api_sport"],
        })
        # We now defer match_odds_api_event to the background polling task so the POST returns instantly.
    required = ("name", "sport", "home", "away")
    missing = [field for field in required if not values.get(field)]
    if missing:
        raise HTTPException(400, f"Missing required fields: {', '.join(missing)}")
    _require_safe_id(values.get("odds_api_sport"), "odds_api_sport")
    _require_safe_id(values.get("odds_api_event_id"), "odds_api_event_id")
    event = store.add_event(Event(**values))
    group = []
    if event.polymarket_slug:
        group.append(asyncio.create_task(polymarket_market_stream(event, on_quotes)))
    if event.odds_api_sport:
        group.append(asyncio.create_task(odds_api_poll(event, on_quotes)))
        group.append(asyncio.create_task(action_network_poll(event, on_quotes)))
        group.append(asyncio.create_task(pinnacle_poll(event, on_quotes)))
    tasks[event.id] = group
    _notify_subscribers()
    return event_view(event.id)


@app.put("/api/events/{event_id}/positions", dependencies=[Depends(verify_auth)])
async def save_position(event_id: str, payload: PositionIn):
    if event_id not in store.events:
        raise HTTPException(404, "event not found")
    if ledger is None:
        raise HTTPException(503, "position ledger is not ready")
    valid_tokens = {quote.token_id for quote in store.quotes[event_id]
                    if quote.source.casefold() == "polymarket" and quote.token_id}
    if payload.token_id not in valid_tokens:
        raise HTTPException(400, "That selection is not available for this event")
    ledger.upsert_position(event_id, payload.token_id, payload.market, payload.outcome,
                           payload.shares, payload.avg_entry_price)
    _notify_subscribers()
    return event_view(event_id)


@app.delete("/api/events/{event_id}/positions/{token_id}", status_code=204, dependencies=[Depends(verify_auth)])
async def remove_position(event_id: str, token_id: str):
    if ledger is None or not ledger.delete_position(event_id, token_id):
        raise HTTPException(404, "position not found")
    _notify_subscribers()


@app.get("/api/metrics", dependencies=[Depends(verify_auth)])
async def metrics():
    if ledger is None:
        return {"n_bets": 0, "n_settled": 0}
    return backtest.summary(ledger.all_bets())


@app.get("/api/leaderboard", dependencies=[Depends(verify_auth)])
async def get_leaderboard():
    if account_book is None:
        return []
    return account_book.leaderboard()


@app.post("/api/accounts", status_code=201, dependencies=[Depends(verify_auth)])
async def create_account(payload: StrategyIn):
    if account_book is None:
        raise HTTPException(503, "Account book is not initialized")
    from .accounts import Strategy
    strat = Strategy(
        name=payload.name,
        blurb="Custom bot created via UI.",
        edge_threshold=payload.edge_threshold,
        sizing=payload.sizing,
        kelly_multiplier=payload.kelly_multiplier,
        flat_stake=payload.flat_stake,
        start_bankroll=payload.start_bankroll,
        webhook_url=payload.webhook_url
    )
    account_book.seed([strat])
    return {"status": "ok"}


@app.get("/api/accounts/{name}/bets", dependencies=[Depends(verify_auth)])
async def get_account_bets(name: str):
    if account_book is None:
        return []
    return account_book.account_bets(name)


@app.get("/api/bets", dependencies=[Depends(verify_auth)])
async def bets(event_id: str | None = None):
    if ledger is None:
        return []
    return ledger.event_bets(event_id) if event_id else ledger.all_bets()


@app.delete("/api/events/{event_id}", status_code=204, dependencies=[Depends(verify_auth)])
async def delete_event(event_id: str):
    if event_id not in store.events:
        raise HTTPException(404, "event not found")
    await finalize_event(event_id)
    if ledger is not None:
        ledger.delete_event_positions(event_id)
    for task in tasks.pop(event_id, []):
        task.cancel()
    _finalized.discard(event_id)
    _pregame.pop(event_id, None)
    del store.events[event_id]
    store.states.pop(event_id, None)
    store.quotes.pop(event_id, None)
    store.signals.pop(event_id, None)
    _notify_subscribers()


