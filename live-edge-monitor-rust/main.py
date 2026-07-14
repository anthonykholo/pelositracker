from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pydantic import BaseModel

from .engine import SignalEngine
from .models import Event, GameState, Quote, as_json
from .sources import (demo_stream, odds_api_poll, polymarket_event,
                      polymarket_market_stream, polymarket_sports_stream)
from .store import Store

load_dotenv()
store = Store()
engine = SignalEngine(float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "72")),
                      float(os.getenv("SIGNAL_EDGE_THRESHOLD", "0.035")),
                      float(os.getenv("MAX_DATA_AGE_SECONDS", "20")))
tasks: dict[str, list[asyncio.Task]] = {}


async def on_state(state: GameState):
    store.add_state(state)
    recompute(state.event_id)


async def on_quotes(quotes: list[Quote]):
    store.add_quotes(quotes)
    if quotes:
        recompute(quotes[0].event_id)


def recompute(event_id: str):
    event = store.events[event_id]
    store.set_signals(event_id, engine.evaluate(event_id, store.quotes[event_id],
                                                store.states[event_id], event.away))


@asynccontextmanager
async def lifespan(_: FastAPI):
    sports_task = asyncio.create_task(polymarket_sports_stream(lambda: list(store.events.values()), on_state))
    yield
    sports_task.cancel()
    for group in tasks.values():
        for task in group:
            task.cancel()


app = FastAPI(title="Live Sports Signal Monitor", version="0.1.0", lifespan=lifespan)


class EventIn(BaseModel):
    name: str
    sport: str
    home: str
    away: str
    league: str = ""
    polymarket_slug: str | None = None
    odds_api_sport: str | None = None
    odds_api_event_id: str | None = None
    demo: bool = False


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
    return {"event": as_json(event), "latest_state": as_json(store.states[event_id][-1]) if store.states[event_id] else None,
            "signals": as_json(store.signals[event_id]), "state_points": len(store.states[event_id]),
            "quote_points": len(store.quotes[event_id])}


@app.get("/api/events/{event_id}")
async def get_event(event_id: str):
    return event_view(event_id)


@app.post("/api/events", status_code=201)
async def add_event(payload: EventIn):
    if payload.polymarket_slug:
        try:
            poly = await polymarket_event(payload.polymarket_slug)
        except Exception as exc:
            raise HTTPException(400, f"Could not resolve Polymarket slug: {exc}") from exc
        if not poly.get("active") or poly.get("closed"):
            raise HTTPException(400, "Polymarket event is not active")
    event = store.add_event(Event(**payload.model_dump(exclude={"demo"})))
    group = []
    if payload.demo:
        group.append(asyncio.create_task(demo_stream(event, on_state, on_quotes)))
    if event.polymarket_slug:
        group.append(asyncio.create_task(polymarket_market_stream(event, on_quotes)))
    if event.odds_api_sport:
        group.append(asyncio.create_task(odds_api_poll(event, on_quotes)))
    tasks[event.id] = group
    return event_view(event.id)


@app.delete("/api/events/{event_id}", status_code=204)
async def delete_event(event_id: str):
    if event_id not in store.events:
        raise HTTPException(404, "event not found")
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
