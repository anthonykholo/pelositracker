from __future__ import annotations

import asyncio
import json
import os
import random
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx
import websockets

from .models import Event, GameState, Quote


def parse_jsonish(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


async def polymarket_event(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"https://gamma-api.polymarket.com/events/slug/{slug}")
        response.raise_for_status()
        return response.json()


async def polymarket_market_stream(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    if not event.polymarket_slug:
        return
    while True:
        try:
            data = await polymarket_event(event.polymarket_slug)
            token_meta = {}
            for market in data.get("markets", []):
                outcomes = parse_jsonish(market.get("outcomes"))
                tokens = parse_jsonish(market.get("clobTokenIds"))
                for token, outcome in zip(tokens, outcomes):
                    market_type = market.get("sportsMarketType") or market.get("question", "moneyline")
                    token_meta[str(token)] = (market_type, str(outcome))
            if not token_meta:
                await asyncio.sleep(30)
                continue
            async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market") as ws:
                await ws.send(json.dumps({"type": "market", "assets_ids": list(token_meta),
                                          "custom_feature_enabled": True}))
                async for raw in ws:
                    if raw == "PING":
                        await ws.send("PONG")
                        continue
                    payload = json.loads(raw)
                    messages = payload if isinstance(payload, list) else [payload]
                    quotes = []
                    for message in messages:
                        if message.get("event_type") not in {"book", "best_bid_ask", "price_change"}:
                            continue
                        changes = message.get("price_changes") or [message]
                        for change in changes:
                            token = str(change.get("asset_id", ""))
                            if token not in token_meta:
                                continue
                            market, outcome = token_meta[token]
                            bid = float(change["best_bid"]) if change.get("best_bid") else None
                            ask = float(change["best_ask"]) if change.get("best_ask") else None
                            if message.get("event_type") == "book":
                                bids, asks = message.get("bids", []), message.get("asks", [])
                                bid = max((float(x["price"]) for x in bids), default=None)
                                ask = min((float(x["price"]) for x in asks), default=None)
                            probability = ((bid + ask) / 2 if bid is not None and ask is not None
                                           else ask if ask is not None else bid)
                            if probability is not None:
                                quotes.append(Quote(event.id, market, outcome, probability,
                                                    "Polymarket", bid=bid, ask=ask))
                    if quotes:
                        await emit(quotes)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(3)


async def polymarket_sports_stream(events: Callable[[], list[Event]],
                                    emit: Callable[[GameState], Awaitable[None]]):
    while True:
        try:
            async with websockets.connect("wss://sports-api.polymarket.com/ws") as ws:
                async for raw in ws:
                    if raw == "ping":
                        await ws.send("pong")
                        continue
                    data = json.loads(raw)
                    slug = data.get("slug")
                    matched = next((e for e in events() if e.polymarket_slug == slug), None)
                    if not matched:
                        continue
                    score = str(data.get("score", "0-0")).split("-")
                    if len(score) != 2:
                        continue
                    await emit(GameState(matched.id, float(score[0]), float(score[1]),
                                         str(data.get("period", "")), str(data.get("elapsed", "")),
                                         "Polymarket sports", possession=data.get("turn"),
                                         status=str(data.get("status", "in_progress"))))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(3)


def american_probability(price: float) -> float:
    return 100 / (price + 100) if price > 0 else (-price) / ((-price) + 100)


async def odds_api_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    key = os.getenv("THE_ODDS_API_KEY")
    if not key or not event.odds_api_sport:
        return
    interval = max(1.0, float(os.getenv("ODDS_POLL_SECONDS", "5")))
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                params = {"sport_key": event.odds_api_sport, "markets": "h2h,spreads,totals"}
                if event.odds_api_event_id:
                    params["event_id"] = event.odds_api_event_id
                response = await client.get("https://api.theoddsapi.com/odds/", params=params,
                                            headers={"x-api-key": key})
                response.raise_for_status()
                quotes = []
                for game in response.json():
                    if event.odds_api_event_id and str(game.get("id")) != event.odds_api_event_id:
                        continue
                    for bookmaker in game.get("bookmakers", []):
                        source = bookmaker.get("title") or bookmaker.get("key", "sportsbook")
                        for market in bookmaker.get("markets", []):
                            for outcome in market.get("outcomes", []):
                                price = float(outcome["price"])
                                quotes.append(Quote(event.id, market.get("key", "h2h"),
                                                    outcome.get("name", ""), american_probability(price),
                                                    source, decimal_odds=(price / 100 + 1 if price > 0 else
                                                    100 / -price + 1)))
                if quotes:
                    await emit(quotes)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)


async def demo_stream(event: Event, emit_state, emit_quotes):
    home = away = 0
    probability = 0.52
    tick = 0
    while True:
        tick += 1
        if tick % 3 == 0:
            if random.random() < probability:
                home += random.choice([1, 2, 3])
                probability = min(0.88, probability + random.uniform(0.015, 0.04))
            else:
                away += random.choice([1, 2, 3])
                probability = max(0.12, probability - random.uniform(0.015, 0.04))
        await emit_state(GameState(event.id, home, away, f"Q{min(4, 1 + tick // 20)}",
                                   f"{max(0, 12 - tick % 12):02d}:00", "Demo feed"))
        quotes = []
        for source, noise in (("DemoBook A", -0.025), ("DemoBook B", 0.005), ("Demo exchange", 0.02)):
            p = max(0.02, min(0.98, probability + noise + random.uniform(-0.008, 0.008)))
            quotes.extend([Quote(event.id, "moneyline", "home", p, source, ask=p + 0.01, bid=p - 0.01),
                           Quote(event.id, "moneyline", "away", 1 - p, source,
                                 ask=1 - p + 0.01, bid=1 - p - 0.01)])
        await emit_quotes(quotes)
        await asyncio.sleep(1)
