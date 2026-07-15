from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import unquote, urlparse

import httpx
import websockets

from .models import Event, GameState, Quote


logger = logging.getLogger(__name__)

_SPORT_KEYS = {
    "nba": ("basketball", "basketball_nba"),
    "wnba": ("basketball", "basketball_wnba"),
    "ncaab": ("basketball", "basketball_ncaab"),
    "nfl": ("football", "americanfootball_nfl"),
    "ncaaf": ("football", "americanfootball_ncaaf"),
    "mlb": ("baseball", "baseball_mlb"),
    "nhl": ("hockey", "icehockey_nhl"),
    "epl": ("soccer", "soccer_epl"),
    "premier league": ("soccer", "soccer_epl"),
    "mls": ("soccer", "soccer_usa_mls"),
}


def extract_polymarket_slug(value: str) -> str:
    """Accept a full Polymarket link (including mobile query strings) or a bare slug."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Paste a Polymarket event link")
    parsed = urlparse(value if "://" in value else f"https://polymarket.com/event/{value}")
    if parsed.hostname and not (parsed.hostname == "polymarket.com" or
                                parsed.hostname.endswith(".polymarket.com")):
        raise ValueError("The link must be from polymarket.com")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if "event" in parts:
        index = parts.index("event")
        if index + 1 < len(parts):
            return parts[index + 1]
    if len(parts) == 1:
        return parts[0]
    raise ValueError("Could not find an event slug in that Polymarket link")


def infer_polymarket_event(data: dict) -> dict[str, str | None]:
    """Infer useful sports fields while keeping URL-only registration possible."""
    title = str(data.get("title") or data.get("question") or "Polymarket event").strip()
    searchable = [title, str(data.get("category", "")), str(data.get("seriesSlug", ""))]
    for series in data.get("series") or []:
        searchable.extend([str(series.get("slug", "")), str(series.get("title", ""))])
    for tag in data.get("tags") or []:
        searchable.extend([str(tag.get("slug", "")), str(tag.get("label", ""))])
    haystack = " ".join(searchable).casefold()
    sport, odds_sport = "prediction-market", None
    for marker, values in _SPORT_KEYS.items():
        if re.search(rf"\b{re.escape(marker)}\b", haystack):
            sport, odds_sport = values
            break

    away, home = "Outcome A", "Outcome B"
    matchup = re.split(r"\s+(?:vs\.?|at)\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(matchup) == 2:
        away, home = matchup[0].strip(), matchup[1].strip()
    return {"name": title, "sport": sport, "away": away, "home": home,
            "odds_api_sport": odds_sport}


def canonical_market(value: str) -> str:
    lowered = (value or "").strip().casefold()
    if lowered in {"h2h", "moneyline", "winner", "match_winner"}:
        return "moneyline"
    if lowered in {"spreads", "spread", "handicap"}:
        return "spread"
    if lowered in {"totals", "total", "over_under"}:
        return "total"
    return value or "market"


def parse_jsonish(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _book_depth(levels: list, best_price: float | None) -> float:
    """Total resting size at the best price level of one side of the book."""
    if best_price is None or not levels:
        return 0.0
    total = 0.0
    for level in levels:
        price = _to_float(level.get("price"))
        size = _to_float(level.get("size"))
        if price is not None and size is not None and abs(price - best_price) < 1e-9:
            total += size
    return total


def _best_level_size(change: dict) -> float | None:
    """Best-of-book size from a price_change / best_bid_ask message, if present."""
    total = 0.0
    seen = False
    for key in ("best_bid_size", "best_ask_size"):
        size = _to_float(change.get(key))
        if size is not None:
            total += size
            seen = True
    return total if seen else None


async def polymarket_event(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"https://gamma-api.polymarket.com/events/slug/{slug}")
        response.raise_for_status()
        return response.json()


async def match_odds_api_event(sport_key: str | None, title: str) -> dict | None:
    """Match a Polymarket sports title to The Odds API's quota-free events list."""
    key = os.getenv("THE_ODDS_API_KEY")
    if not key or not sport_key:
        return None
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params={"apiKey": key, "dateFormat": "iso"})
        response.raise_for_status()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold())
    ranked = []
    for game in response.json():
        home = str(game.get("home_team", ""))
        away = str(game.get("away_team", ""))
        names = [re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip() for name in (home, away)]
        score = sum(2 if name and name in normalized_title else
                    1 if name.split() and name.split()[-1] in normalized_title.split() else 0
                    for name in names)
        if score:
            ranked.append((score, game))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked[0][0] >= 2 else None


def _polymarket_token_meta(data: dict) -> dict[str, dict]:
    token_meta = {}
    for market in data.get("markets", []):
        if (not market.get("active", True) or market.get("closed", False) or
                not market.get("enableOrderBook", True) or
                not market.get("acceptingOrders", False)):
            continue
        outcomes = parse_jsonish(market.get("outcomes"))
        tokens = parse_jsonish(market.get("clobTokenIds"))
        question = str(market.get("question") or market.get("groupItemTitle") or "Market")
        market_type = canonical_market(str(market.get("sportsMarketType") or question))
        for token, outcome in zip(tokens, outcomes):
            token_meta[str(token)] = {
                "market": market_type,
                "outcome": str(outcome),
                "question": question,
                "market_slug": str(market.get("slug") or ""),
                "liquidity": _to_float(market.get("liquidityNum") or market.get("liquidity")),
                "min_order_size": _to_float(market.get("orderMinSize")),
                "tick_size": _to_float(market.get("orderPriceMinTickSize")),
                "accepting_orders": True,
            }
    return token_meta


def _quote_from_book(event: Event, token: str, meta: dict, book: dict) -> Quote | None:
    bids, asks = book.get("bids", []), book.get("asks", [])
    bid_prices = [price for level in bids if (price := _to_float(level.get("price"))) is not None]
    ask_prices = [price for level in asks if (price := _to_float(level.get("price"))) is not None]
    bid = max(bid_prices, default=None)
    ask = min(ask_prices, default=None)
    probability = ((bid + ask) / 2 if bid is not None and ask is not None
                   else ask if ask is not None else bid)
    if probability is None:
        return None
    bid_size = _book_depth(bids, bid)
    ask_size = _book_depth(asks, ask)
    return Quote(
        event.id, meta["market"], meta["outcome"], probability, "Polymarket",
        bid=bid, ask=ask, liquidity=bid_size + ask_size,
        market_liquidity=meta.get("liquidity"), token_id=token,
        market_slug=meta.get("market_slug"), question=meta.get("question"),
        bid_size=bid_size, ask_size=ask_size,
        min_order_size=_to_float(book.get("min_order_size")) or meta.get("min_order_size"),
        tick_size=_to_float(book.get("tick_size")) or meta.get("tick_size"),
        accepting_orders=meta.get("accepting_orders", True),
    )


async def _initial_polymarket_quotes(event: Event, token_meta: dict[str, dict]) -> list[Quote]:
    semaphore = asyncio.Semaphore(10)
    async with httpx.AsyncClient(timeout=15) as client:
        async def fetch(token: str) -> Quote | None:
            async with semaphore:
                response = await client.get("https://clob.polymarket.com/book",
                                            params={"token_id": token})
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return _quote_from_book(event, token, token_meta[token], response.json())
        return [quote for quote in await asyncio.gather(*(fetch(token) for token in token_meta))
                if quote is not None]


async def polymarket_market_stream(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    if not event.polymarket_slug:
        return
    while True:
        try:
            data = await polymarket_event(event.polymarket_slug)
            token_meta = _polymarket_token_meta(data)
            if not token_meta:
                await asyncio.sleep(30)
                continue
            initial = await _initial_polymarket_quotes(event, token_meta)
            if initial:
                await emit(initial)
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
                            meta = token_meta[token]
                            bid = float(change["best_bid"]) if change.get("best_bid") else None
                            ask = float(change["best_ask"]) if change.get("best_ask") else None
                            bid_size = _to_float(change.get("best_bid_size"))
                            ask_size = _to_float(change.get("best_ask_size"))
                            if message.get("event_type") == "book":
                                bids, asks = message.get("bids", []), message.get("asks", [])
                                bid = max((float(x["price"]) for x in bids), default=None)
                                ask = min((float(x["price"]) for x in asks), default=None)
                                bid_size = _book_depth(bids, bid)
                                ask_size = _book_depth(asks, ask)
                            probability = ((bid + ask) / 2 if bid is not None and ask is not None
                                           else ask if ask is not None else bid)
                            if probability is not None:
                                quotes.append(Quote(
                                    event.id, meta["market"], meta["outcome"], probability,
                                    "Polymarket", bid=bid, ask=ask,
                                    liquidity=(bid_size or 0.0) + (ask_size or 0.0),
                                    market_liquidity=meta.get("liquidity"), token_id=token,
                                    market_slug=meta.get("market_slug"), question=meta.get("question"),
                                    bid_size=bid_size, ask_size=ask_size,
                                    min_order_size=meta.get("min_order_size"),
                                    tick_size=meta.get("tick_size"), accepting_orders=True,
                                ))
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


def odds_api_request(event: Event, key: str) -> tuple[str, dict[str, str]]:
    """Build an authenticated The Odds API V4 request without exposing the key in logs."""
    root = f"https://api.the-odds-api.com/v4/sports/{event.odds_api_sport}"
    if event.odds_api_event_id:
        url = f"{root}/events/{event.odds_api_event_id}/odds"
    else:
        url = f"{root}/odds"
    params = {
        "apiKey": key,
        "regions": os.getenv("ODDS_REGIONS", "us"),
        "markets": os.getenv("ODDS_MARKETS", "h2h,spreads,totals"),
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    bookmakers = os.getenv("ODDS_BOOKMAKERS", "").strip()
    if bookmakers:
        params["bookmakers"] = bookmakers
    return url, params


def _same_matchup(event: Event, game: dict) -> bool:
    home = str(game.get("home_team", "")).strip().casefold()
    away = str(game.get("away_team", "")).strip().casefold()
    return home == event.home.strip().casefold() and away == event.away.strip().casefold()


def _outcome_label(market: str, outcome: dict) -> str:
    name = str(outcome.get("name", ""))
    point = outcome.get("point")
    if point is None:
        return name
    point = float(point)
    if market == "spreads":
        return f"{name} {point:+g}"
    if market == "totals":
        return f"{name} {point:g}"
    return name


def odds_api_quotes(event: Event, payload: dict | list[dict]) -> list[Quote]:
    games = [payload] if isinstance(payload, dict) else payload
    quotes = []
    for game in games:
        if event.odds_api_event_id:
            if str(game.get("id")) != event.odds_api_event_id:
                continue
        elif not _same_matchup(event, game):
            continue
        for bookmaker in game.get("bookmakers", []):
            source = bookmaker.get("title") or bookmaker.get("key", "sportsbook")
            for market in bookmaker.get("markets", []):
                provider_key = market.get("key", "h2h")
                market_key = canonical_market(provider_key)
                for outcome in market.get("outcomes", []):
                    price = float(outcome["price"])
                    if price == 0:
                        continue
                    quotes.append(Quote(
                        event.id,
                        market_key,
                        _outcome_label(provider_key, outcome),
                        american_probability(price),
                        source,
                        decimal_odds=(price / 100 + 1 if price > 0 else 100 / -price + 1),
                    ))
    return quotes


async def odds_api_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    key = os.getenv("THE_ODDS_API_KEY")
    if not key or not event.odds_api_sport:
        return
    interval = max(1.0, float(os.getenv("ODDS_POLL_SECONDS", "45")))
    url, params = odds_api_request(event, key)
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                quotes = odds_api_quotes(event, response.json())
                if quotes:
                    await emit(quotes)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                logger.warning("The Odds API returned HTTP %s for %s",
                               exc.response.status_code, event.name)
            except Exception as exc:
                logger.warning("The Odds API poll failed for %s (%s)", event.name,
                               type(exc).__name__)
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
