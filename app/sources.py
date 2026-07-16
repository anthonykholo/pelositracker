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
    "wnba": ("basketball", "basketball_wnba"),
    "nba": ("basketball", "basketball_nba"),
    "ncaab": ("basketball", "basketball_ncaab"),
    "nfl": ("football", "americanfootball_nfl"),
    "ncaaf": ("football", "americanfootball_ncaaf"),
    "mlb": ("baseball", "baseball_mlb"),
    "nhl": ("hockey", "icehockey_nhl"),
    "ufc": ("mma", "mma_mixed_martial_arts"),
    "mma": ("mma", "mma_mixed_martial_arts"),
    "boxing": ("boxing", "boxing_boxing"),
    "premier league": ("soccer", "soccer_epl"),
    "epl": ("soccer", "soccer_epl"),
    "la liga": ("soccer", "soccer_spain_la_liga"),
    "serie a": ("soccer", "soccer_italy_serie_a"),
    "bundesliga": ("soccer", "soccer_germany_bundesliga"),
    "ligue 1": ("soccer", "soccer_france_ligue_one"),
    "champions league": ("soccer", "soccer_uefa_champs_league"),
    "world cup": ("soccer", "soccer_fifa_world_cup"),
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


_SPORTS_TAGS = (
    "nba", "nfl", "nhl", "mlb", "mls", "epl", "premier league", "la liga", "serie a",
    "bundesliga", "ligue 1", "champions league", "europa league", "soccer", "football",
    "tennis", "ufc", "mma", "boxing", "f1", "formula 1", "golf", "ncaa", "ncaab", "ncaaf",
    "cricket", "world cup", "sports", "cfb", "cbb", "wnba",
)
_MATCHUP_RE = re.compile(r"\b(?:vs\.?|at)\b", re.IGNORECASE)
_LIVE_GAME_STATUSES = {"live", "in progress", "inprogress", "playing", "halftime", "intermission"}
_FINAL_GAME_STATUSES = {"final", "ended", "closed", "complete", "completed", "finished", "cancelled", "canceled"}
_UPCOMING_GAME_STATUSES = {"scheduled", "upcoming", "not started", "pregame", "pre game", "delayed", "postponed"}


def _tag_labels(event: dict) -> list[str]:
    return [str(t.get("label", "")).casefold() for t in (event.get("tags") or [])]


def _is_sports_event(event: dict) -> bool:
    labels = _tag_labels(event)
    return any(any(kw in label for kw in _SPORTS_TAGS) for label in labels)


def _league_label(event: dict) -> str:
    for t in (event.get("tags") or []):
        label = str(t.get("label", ""))
        if label.casefold() in _SPORTS_TAGS or any(kw in label.casefold() for kw in _SPORTS_TAGS):
            if label.casefold() not in ("sports", "games", "football"):
                return label
    return "Sports"


def sports_game_status(payload) -> str | None:
    """Normalize explicit provider state without guessing from the start time."""
    if isinstance(payload, str):
        raw = payload
        live_value = None
    elif isinstance(payload, dict):
        raw = next((payload.get(key) for key in
                    ("status", "gameStatus", "game_status", "state") if payload.get(key)), "")
        live_value = payload.get("live")
    else:
        return None
    if live_value is True or str(live_value).strip().casefold() == "true":
        return "live"
    normalized = re.sub(r"[_-]+", " ", str(raw).strip().casefold())
    if normalized in _FINAL_GAME_STATUSES:
        return "final"
    if normalized in _LIVE_GAME_STATUSES:
        return "live"
    if normalized in _UPCOMING_GAME_STATUSES:
        return "upcoming"
    return None


def filter_sports_games(events: list[dict]) -> list[dict]:
    """Keep tradeable sports GAMES (team-vs-team matchups) that are accepting
    orders right now; drop futures, sub-events, and finished/idle markets. Pure
    so it can be unit-tested without the network."""
    games = []
    for event in events:
        title = str(event.get("title", ""))
        if not _MATCHUP_RE.search(title) or " - " in title:  # matchups, not names containing "vs"
            continue
        if not event.get("enableOrderBook", False) or not _is_sports_event(event):
            continue
        markets = event.get("markets") or []
        if not any(m.get("acceptingOrders") for m in markets):  # tradeable now
            continue
        slug = event.get("slug")
        if not slug:
            continue
        # Real first-pitch/tip time lives on the market (top-level startDate is
        # just when the market was created).
        game_start = next((m.get("gameStartTime") for m in markets if m.get("gameStartTime")), None)
        provider_status = next((sports_game_status(m) for m in markets if sports_game_status(m)),
                               sports_game_status(event))
        games.append({
            "slug": slug,
            "title": title,
            "league": _league_label(event),
            "game_start": game_start,
            "provider_status": provider_status,
            "restricted": bool(event.get("restricted", False)),
        })
    return games


# Per-league tag slugs, queried individually so a high-volume league (e.g.
# tennis has hundreds of daily matches) can't crowd others out of a single
# volume/date-ordered page. Override with DISCOVER_LEAGUES (comma-separated).
_DEFAULT_LEAGUES = ("mlb", "nba", "wnba", "nfl", "nhl", "mls", "epl",
                    "ufc", "tennis", "golf", "boxing", "nascar",
                    "basketball", "football", "soccer", "esports")


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _game_window(games: list[dict], now: datetime,
                 live_statuses: dict[str, dict] | None = None) -> list[dict]:
    """Use explicit provider state for LIVE; time alone can only mean STARTED.

    A recently passed start time is not proof that a game is still in progress.
    This avoids labeling finals, postponements, and stale tennis markets live.
    """
    live_statuses = live_statuses or {}
    out = []
    for original in games:
        game = dict(original)
        start = _parse_iso(game.get("game_start"))
        status_payload = live_statuses.get(str(game.get("slug"))) or game.get("provider_status")
        explicit = sports_game_status(status_payload)
        if explicit == "live" and isinstance(status_payload, dict) and status_payload.get("_received_at"):
            received_at = _parse_iso(status_payload.get("_received_at"))
            if received_at and (now - received_at).total_seconds() > 180:
                explicit = None  # a stale LIVE packet must not keep a game live forever
        if explicit == "final":
            continue
        if explicit == "live":
            game["status"] = "live"
            game["status_source"] = "polymarket-live-feed"
            out.append(game)
            continue
        if start is None:
            game["status"] = "upcoming"
            game["status_source"] = "schedule-missing"
            out.append(game)
            continue
        hours = (now - start).total_seconds() / 3600.0
        if explicit == "upcoming" or -168 <= hours < 0:
            game["status"] = "upcoming"
            game["status_source"] = "provider" if explicit else "schedule"
        elif 0 <= hours <= 6:
            game["status"] = "started"
            game["status_source"] = "schedule-only"
        else:
            continue
        out.append(game)
    rank = {"live": 0, "started": 1, "upcoming": 2}
    out.sort(key=lambda g: (rank.get(g["status"], 9), _parse_iso(g.get("game_start")) or now))
    return out


async def polymarket_sports_events(limit_per_league: int = 100,
                                   live_statuses: dict[str, dict] | None = None) -> list[dict]:
    """List live/upcoming Polymarket sports games across leagues for discovery.

    Gamma can't order by real game time, so per league we pull both creation
    orderings (ascending surfaces soonest/in-progress games, descending the
    freshly-listed ones) and then sort/filter by the market's gameStartTime.
    """
    leagues = [s.strip() for s in os.getenv("DISCOVER_LEAGUES", "").split(",") if s.strip()] \
        or list(_DEFAULT_LEAGUES)
    async with httpx.AsyncClient(timeout=20) as client:
        async def league_events(slug: str, ascending: bool) -> list[dict]:
            try:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"closed": "false", "active": "true", "limit": str(limit_per_league),
                            "tag_slug": slug, "order": "startDate",
                            "ascending": "true" if ascending else "false"},
                )
                response.raise_for_status()
                return response.json()
            except Exception:
                return []  # a bad/off-season slug shouldn't sink the whole list
        tasks = [league_events(slug, asc) for slug in leagues for asc in (True, False)]
        batches = await asyncio.gather(*tasks)
    seen_events: set[str] = set()
    events = []
    for event in (e for batch in batches for e in batch):
        key = str(event.get("id") or event.get("slug"))
        if key not in seen_events:
            seen_events.add(key)
            events.append(event)
    ranked = _game_window(filter_sports_games(events), datetime.now(timezone.utc), live_statuses)
    return ranked[:80]  # live first, then soonest — keep the picker manageable


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
                                    emit: Callable[[GameState], Awaitable[None]],
                                    status_emit: Callable[[str, dict], Awaitable[None]] | None = None):
    while True:
        try:
            async with websockets.connect("wss://sports-api.polymarket.com/ws") as ws:
                async for raw in ws:
                    if raw == "ping":
                        await ws.send("pong")
                        continue
                    data = json.loads(raw)
                    slug = data.get("slug")
                    if slug and status_emit is not None:
                        await status_emit(str(slug), data)
                    matched = next((e for e in events() if e.polymarket_slug == slug), None)
                    if not matched:
                        continue
                    score = str(data.get("score", "0-0")).split("-")
                    if len(score) != 2:
                        continue
                    home_score, away_score = _to_float(score[0]), _to_float(score[1])
                    if home_score is None or away_score is None:
                        continue  # skip one bad message, don't drop the shared socket
                    await emit(GameState(matched.id, home_score, away_score,
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
    markets = os.getenv("ODDS_MARKETS", "h2h,spreads,totals")
    if event.odds_api_event_id:
        url = f"{root}/events/{event.odds_api_event_id}/odds"
        # Player props are only served by the per-event endpoint. Opt-in and
        # sport-specific (an unsupported market key makes The Odds API 422), so
        # off by default; set ODDS_PLAYER_MARKETS per sport to enable.
        props = os.getenv("ODDS_PLAYER_MARKETS", "").strip()
        if props:
            markets = f"{markets},{props}"
    else:
        url = f"{root}/odds"
    params = {
        "apiKey": key,
        "regions": os.getenv("ODDS_REGIONS", "us"),
        "markets": markets,
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


def is_player_prop(provider_key: str) -> bool:
    key = (provider_key or "").casefold()
    return key.startswith(("player_", "batter_", "pitcher_")) and not key.endswith("_alternate")


def _prop_market_outcome(provider_key: str, outcome: dict) -> tuple[str, str] | None:
    """(market, outcome) for a player prop, keyed per (player, stat, line) so the
    engine de-vigs each player's Over/Under and never mixes players or lines."""
    player = str(outcome.get("description", "")).strip()
    point = outcome.get("point")
    side = str(outcome.get("name", "")).strip()  # Over / Under
    if not player or point is None or not side:
        return None
    stat = provider_key.casefold()
    for prefix in ("player_", "batter_", "pitcher_"):
        if stat.startswith(prefix):
            stat = stat[len(prefix):]
            break
    return f"{player} — {stat.replace('_', ' ')}", f"{side} {float(point):g}"


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
                prop = is_player_prop(provider_key)
                for outcome in market.get("outcomes", []):
                    price = float(outcome["price"])
                    if price == 0:
                        continue
                    if prop:
                        resolved = _prop_market_outcome(provider_key, outcome)
                        if resolved is None:
                            continue
                        market_key, outcome_label = resolved
                    else:
                        market_key = canonical_market(provider_key)
                        outcome_label = _outcome_label(provider_key, outcome)
                    quotes.append(Quote(
                        event.id,
                        market_key,
                        outcome_label,
                        american_probability(price),
                        source,
                        decimal_odds=(price / 100 + 1 if price > 0 else 100 / -price + 1),
                    ))
    return quotes


async def odds_api_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    key = os.getenv("THE_ODDS_API_KEY")
    if not key or not event.odds_api_sport:
        return
        
    if not event.odds_api_event_id:
        try:
            matched = await match_odds_api_event(event.odds_api_sport, event.name)
            if matched:
                event.odds_api_event_id = str(matched["id"])
                event.home = str(matched["home_team"])
                event.away = str(matched["away_team"])
        except Exception as exc:
            logger.warning("Failed to match Odds API event: %s", exc)
            
    if not event.odds_api_event_id:
        logger.warning("Could not resolve Odds API event ID for %s", event.name)
        return

    # Lower interval = fresher sportsbook lines but more (paid) API credits.
    # Polymarket streams in real time regardless; this only paces The Odds API.
    interval = max(1.0, float(os.getenv("ODDS_POLL_SECONDS", "20")))
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



