from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .engine import SignalEngine
from . import __version__, backtest
from .advice import market_views, position_views
from .ledger import Ledger
from .models import Event, GameState, Quote, as_json
from .sources import (demo_stream, extract_polymarket_slug, infer_polymarket_event,
                      match_odds_api_event, odds_api_poll, polymarket_event,
                      polymarket_market_stream, polymarket_sports_stream)
from .store import Store

load_dotenv()
store = Store()
ledger: Ledger | None = None
engine = SignalEngine(float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "72")),
                      float(os.getenv("SIGNAL_EDGE_THRESHOLD", "0.035")),
                      float(os.getenv("MAX_DATA_AGE_SECONDS", "20")))
tasks: dict[str, list[asyncio.Task]] = {}
_FINAL_STATUSES = {"final", "ended", "closed", "complete", "finished"}


async def on_state(state: GameState):
    store.add_state(state)
    recompute(state.event_id)
    if str(state.status).lower() in _FINAL_STATUSES:
        finalize_event(state.event_id)


async def on_quotes(quotes: list[Quote]):
    store.add_quotes(quotes)
    if quotes:
        recompute(quotes[0].event_id)


def recompute(event_id: str):
    event = store.events[event_id]
    signals = engine.evaluate(event_id, store.quotes[event_id],
                              store.states[event_id], event.away, sport=event.sport)
    store.set_signals(event_id, signals)
    if ledger is not None:
        ledger.record_signals(event, signals)


def finalize_event(event_id: str) -> None:
    """Snapshot the closing consensus (for CLV) and settle moneylines."""
    if ledger is None:
        return
    event = store.events.get(event_id)
    signals = store.signals.get(event_id) or []
    fair_by_selection = {
        (s.market, s.outcome): (s.market_fair_prob or s.model_probability)
        for s in signals
        if (s.market_fair_prob or s.model_probability)
    }
    ledger.snapshot_closing(event_id, fair_by_selection)
    states = store.states.get(event_id) or []
    if event and states:
        last = states[-1]
        winners: set[str] = set()
        if last.home_score > last.away_score:
            winners = {"home", event.home}
        elif last.away_score > last.home_score:
            winners = {"away", event.away}
        if winners:
            ledger.settle_moneyline(event_id, winners)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ledger
    ledger = Ledger()
    sports_task = asyncio.create_task(polymarket_sports_stream(lambda: list(store.events.values()), on_state))
    yield
    sports_task.cancel()
    for group in tasks.values():
        for task in group:
            task.cancel()
    ledger.close()


app = FastAPI(title="Live Sports Signal Monitor", version=__version__, lifespan=lifespan)


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
    demo: bool = False


class PositionIn(BaseModel):
    token_id: str
    market: str
    outcome: str
    shares: float = Field(gt=0, le=1_000_000)
    avg_entry_price: float = Field(gt=0, lt=1)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config")
async def config():
    return {"confidence_threshold": engine.confidence_threshold, "edge_threshold": engine.edge_threshold,
            "max_age_seconds": engine.max_age_seconds, "auto_betting": False}


@app.get("/api/events")
async def list_events():
    return [event_view(event.id) for event in store.events.values()]


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


@app.get("/api/events/{event_id}")
async def get_event(event_id: str):
    return event_view(event_id)


@app.post("/api/events", status_code=201)
async def add_event(payload: EventIn):
    values = payload.model_dump(exclude={"demo"})
    link_or_slug = payload.polymarket_url or payload.polymarket_slug
    if link_or_slug:
        try:
            slug = extract_polymarket_slug(link_or_slug)
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
        if values.get("odds_api_sport") and not values.get("odds_api_event_id"):
            try:
                matched = await match_odds_api_event(values["odds_api_sport"], values["name"])
            except Exception:
                matched = None
            if matched:
                values.update({"odds_api_event_id": str(matched["id"]),
                               "home": str(matched["home_team"]),
                               "away": str(matched["away_team"])})
    required = ("name", "sport", "home", "away")
    missing = [field for field in required if not values.get(field)]
    if missing:
        raise HTTPException(400, f"Missing required fields: {', '.join(missing)}")
    event = store.add_event(Event(**values))
    group = []
    if payload.demo:
        group.append(asyncio.create_task(demo_stream(event, on_state, on_quotes)))
    if event.polymarket_slug:
        group.append(asyncio.create_task(polymarket_market_stream(event, on_quotes)))
    if event.odds_api_sport:
        group.append(asyncio.create_task(odds_api_poll(event, on_quotes)))
    tasks[event.id] = group
    return event_view(event.id)


@app.put("/api/events/{event_id}/positions")
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
    return event_view(event_id)


@app.delete("/api/events/{event_id}/positions/{token_id}", status_code=204)
async def remove_position(event_id: str, token_id: str):
    if ledger is None or not ledger.delete_position(event_id, token_id):
        raise HTTPException(404, "position not found")


@app.get("/api/metrics")
async def metrics():
    if ledger is None:
        return {"n_bets": 0, "n_settled": 0}
    return backtest.summary(ledger.all_bets())


@app.get("/api/bets")
async def bets(event_id: str | None = None):
    if ledger is None:
        return []
    return ledger.event_bets(event_id) if event_id else ledger.all_bets()


@app.delete("/api/events/{event_id}", status_code=204)
async def delete_event(event_id: str):
    if event_id not in store.events:
        raise HTTPException(404, "event not found")
    finalize_event(event_id)
    if ledger is not None:
        ledger.delete_event_positions(event_id)
    for task in tasks.pop(event_id, []):
        task.cancel()
    del store.events[event_id]
    store.states.pop(event_id, None)
    store.quotes.pop(event_id, None)
    store.signals.pop(event_id, None)


@app.post("/api/demo", status_code=201)
async def add_demo():
    return await add_event(EventIn(name="Demo: Harbor Hawks vs Metro Foxes", sport="basketball",
                                   league="demo", home="Harbor Hawks", away="Metro Foxes", demo=True))
