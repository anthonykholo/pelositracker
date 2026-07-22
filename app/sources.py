from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import unquote, urlparse

import httpx
import websockets

from .models import Event, GameState, Quote
from .matching import closest_start, team_match_score
from .domain.time import parse_provider_timestamp
from .orderbook import BookGapError, OrderBookState
from .execution import D
from .resilience import RetryBackoff
from .telemetry import runtime_telemetry


logger = logging.getLogger(__name__)
_odds_quota: dict[str, str] = {}


def _provider_time(value: object) -> datetime | None:
    try:
        return parse_provider_timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None

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
    # Tennis and esports are recognized as sports for labeling, correlation
    # grouping, and per-sport exposure caps. No usable reference-book feed
    # exists for them here (The Odds API tennis is per-tournament with no
    # evergreen key and has no esports; Action Network/Pinnacle do not cover
    # them either), so odds_api_sport is None and they stay single-source
    # unless an independent price feed or model is added.
    "tennis": ("tennis", None),
    "atp": ("tennis", None),
    "wta": ("tennis", None),
    "esports": ("esports", None),
    "counter-strike": ("esports", None),
    "cs2": ("esports", None),
    "league of legends": ("esports", None),
    "dota": ("esports", None),
    "valorant": ("esports", None),
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
    game_start = next((str(market.get("gameStartTime")) for market in data.get("markets", [])
                       if market.get("gameStartTime")), None)
    return {"name": title, "sport": sport, "away": away, "home": home,
            "odds_api_sport": odds_sport, "game_start": game_start}


def canonical_market(value: str) -> str:
    lowered = (value or "").strip().casefold()
    if lowered in {"h2h", "moneyline", "winner", "match_winner"}:
        return "moneyline"
    if lowered in {"spreads", "spread", "handicap"}:
        return "spread"
    if lowered in {"totals", "total", "over_under"}:
        return "total"
    return value or "market"


def _format_line(value: float) -> str:
    """Render provider line numbers the same way across every quote adapter."""
    return f"{value:g}"


def _format_signed_line(value: float) -> str:
    return f"{0.0 if abs(value) < 1e-9 else value:+g}"


def _polymarket_outcome_labels(market_type: str, outcomes: list, line: float | None) -> list[str]:
    """Expand Gamma's line metadata into comparable selection labels.

    Polymarket keeps the handicap/total in ``market.line`` while its token
    outcomes are just team names or ``Over``/``Under``. Sportsbook adapters put
    the line in the outcome label. Without this normalization the two feeds can
    never join, so every Polymarket spread/total reports zero references.
    """
    labels = [str(outcome).strip() for outcome in outcomes]
    if line is None:
        return labels
    if market_type == "total":
        return [f"{label} {_format_line(abs(line))}" for label in labels]
    if market_type == "spread" and labels:
        # Gamma's first outcome is the team named by the spread market and
        # ``line`` is that team's handicap. The opposing outcome gets -line.
        normalized = [f"{labels[0]} {_format_signed_line(line)}"]
        normalized.extend(f"{label} {_format_signed_line(-line)}" for label in labels[1:])
        return normalized
    return labels


_PROP_OU_RE = re.compile(
    r"^(?P<player>.+?):\s*(?P<stat>.+?)\s+O/U\s+(?P<line>[+-]?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
_SCOPED_MAINLINE_RE = re.compile(
    r"\b(?:(?:1st|first|2nd|second|3rd|third|4th|fourth)\s+"
    r"(?:half|quarter|period|inning)|(?:first|1st)\s+(?:five|5)\s+innings|[12]h)\b",
    re.IGNORECASE,
)
_PROP_STAT_ALIASES = {
    "three pointers": "threes",
    "3 pointers": "threes",
    "three point field goals": "threes",
}


def _polymarket_selections(market: dict, market_type: str, outcomes: list,
                           line: float | None) -> list[tuple[str, str]]:
    """Build comparison-safe display identities for one Gamma market.

    Gamma uses both conventional multi-outcome books and separate Yes/No
    conditions.  The latter must be translated before joining to sportsbook
    selections; otherwise unrelated ``Yes`` tokens collapse into one market.
    """
    question = str(market.get("question") or market.get("groupItemTitle") or "Market").strip()
    group = str(market.get("groupItemTitle") or "").strip()
    labels = [str(outcome).strip() for outcome in outcomes]
    binary = labels and all(label.casefold() in {"yes", "no"} for label in labels)

    # Soccer 1X2 is represented as three independent binary conditions.  The
    # affirmative tokens map to the normal home/away/draw market.  A negative
    # token means "any of the other outcomes", so it intentionally remains a
    # unique condition and can never be compared to one sportsbook leg.
    if market_type == "moneyline" and binary and group:
        affirmative = "Draw" if group.casefold().startswith("draw") else group
        return [
            ("moneyline", affirmative)
            if label.casefold() == "yes"
            else ("moneyline condition", f"Not {affirmative}")
            for label in labels
        ]

    # Real Gamma player props use Yes/No plus a line, while sportsbook feeds
    # use Over/Under.  Only translate an anchored ``PLAYER: STAT O/U N`` shape
    # whose printed number agrees with Gamma's numeric line.
    if binary and line is not None and market_type not in {"moneyline", "spread", "total", "market"}:
        match = next(
            (candidate for raw in (group, question) if raw
             and (candidate := _PROP_OU_RE.match(raw)) is not None),
            None,
        )
        if match and abs(float(match.group("line")) - line) < 1e-9:
            player = match.group("player").strip()
            stat = market_type.replace("_", " ").strip().casefold()
            stat = _PROP_STAT_ALIASES.get(stat, stat)
            prop_market = f"{player} — {stat}"
            return [
                (prop_market,
                 f"{'Over' if label.casefold() == 'yes' else 'Under'} {_format_line(line)}")
                for label in labels
            ]

    expanded = _polymarket_outcome_labels(market_type, labels, line)

    # Do not let first-half/period/inning mainlines borrow full-game prices.
    if market_type in {"moneyline", "spread", "total"} and _SCOPED_MAINLINE_RE.search(question):
        return [(question, label) for label in expanded]

    # Every unrecognized binary condition gets its own market identity.  This
    # is deliberately MARKET ONLY until a verified external mapping exists.
    if binary:
        return [(question, label) for label in labels]
    return [(market_type, label) for label in expanded]


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


def _visible_liquidity(bid_size: float | None, ask_size: float | None) -> float | None:
    """Return known top-of-book depth without turning unknown depth into zero."""
    if bid_size is None and ask_size is None:
        return None
    return (bid_size or 0.0) + (ask_size or 0.0)


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


def _matchup_separator(title: str) -> re.Match | None:
    """Return a team-v-team separator, excluding question prose using "at"."""
    match = _MATCHUP_RE.search(title)
    if match is None:
        return None
    if match.group().casefold() == "at" and (
        "?" in title or re.match(r"^\s*(who|what|which|when|where|how|will)\b",
                                  title, re.IGNORECASE)
    ):
        return None
    return match
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
        if _matchup_separator(title) is None or " - " in title:
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
        inferred = infer_polymarket_event(event)
        provider_status = next((sports_game_status(m) for m in markets if sports_game_status(m)),
                               sports_game_status(event))
        games.append({
            "slug": slug,
            "title": title,
            "league": _league_label(event),
            "game_start": game_start,
            "provider_status": provider_status,
            "restricted": bool(event.get("restricted", False)),
            "reference_adapter": bool(inferred.get("odds_api_sport")),
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


async def match_odds_api_event(sport_key: str | None, title: str,
                               game_start: str | None = None) -> dict | None:
    """Match a Polymarket sports title to The Odds API's quota-free events list."""
    key = os.getenv("THE_ODDS_API_KEY")
    if not key or not sport_key:
        return None
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params={"apiKey": key, "dateFormat": "iso"})
        response.raise_for_status()
    participants = [part.strip() for part in _MATCHUP_RE.split(title, maxsplit=1)]
    if len(participants) != 2 or not all(participants):
        return None
    matched = []
    for game in response.json():
        home = str(game.get("home_team", ""))
        away = str(game.get("away_team", ""))
        direct = (team_match_score(participants[0], home),
                  team_match_score(participants[1], away))
        reverse = (team_match_score(participants[0], away),
                   team_match_score(participants[1], home))
        if any(all(score is not None for score in orientation)
               for orientation in (direct, reverse)):
            matched.append(game)
    if not matched:
        return None
    if game_start is None:
        return matched[0] if len(matched) == 1 else None
    return closest_start(matched, game_start, lambda game: game.get("commence_time"))


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
        selections = _polymarket_selections(
            market, market_type, outcomes, _to_float(market.get("line"))
        )
        for token, (selection_market, outcome) in zip(tokens, selections):
            token_meta[str(token)] = {
                "market": selection_market,
                "outcome": str(outcome),
                "question": question,
                "market_slug": str(market.get("slug") or ""),
                "liquidity": _to_float(market.get("liquidityNum") or market.get("liquidity")),
                "min_order_size": _to_float(market.get("orderMinSize")),
                "tick_size": _to_float(market.get("orderPriceMinTickSize")),
                "accepting_orders": True,
                "fee_rate": (0.0 if market.get("feesEnabled") is False else
                             _to_float(market.get("feeRate") or market.get("takerFee"))),
                "fee_schedule_id": str(market.get("feeScheduleId") or "") or None,
                "provider_event_id": str(data.get("id") or data.get("slug") or "") or None,
                "provider_market_id": str(market.get("id") or market.get("slug") or "") or None,
                "condition_id": str(market.get("conditionId") or "") or None,
                "market_scope": str(market.get("marketScope") or "unknown").casefold(),
                "line": _to_float(market.get("line")),
                "active": bool(market.get("active", True)),
                "resolved": bool(market.get("closed", False)),
                "restricted": bool(data.get("restricted", False) or market.get("restricted", False)),
                "negative_risk": (bool(market.get("negRisk"))
                                  if market.get("negRisk") is not None else None),
            }
    return token_meta


def _quote_from_book(event: Event, token: str, meta: dict, book: dict) -> Quote | None:
    received_at = datetime.now(timezone.utc)
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
        provider_timestamp=_provider_time(book.get("timestamp")),
        received_at=received_at, processed_at=datetime.now(timezone.utc),
        book_hash=str(book.get("hash")) if book.get("hash") else None,
        depth_complete=True,
        fee_rate=(_to_float(book.get("fee_rate"))
                  if book.get("fee_rate") is not None else meta.get("fee_rate")),
        fee_schedule_id=(str(book.get("fee_schedule_id"))
                         if book.get("fee_schedule_id") else meta.get("fee_schedule_id")),
        bid_levels=tuple((float(level["price"]), float(level["size"])) for level in bids
                         if _to_float(level.get("price")) is not None
                         and _to_float(level.get("size")) is not None),
        ask_levels=tuple((float(level["price"]), float(level["size"])) for level in asks
                         if _to_float(level.get("price")) is not None
                         and _to_float(level.get("size")) is not None),
        provider_event_id=meta.get("provider_event_id"),
        canonical_event_id=event.canonical_event_id,
        provider_market_id=meta.get("provider_market_id"),
        condition_id=meta.get("condition_id"), market_scope=meta.get("market_scope", "unknown"),
        line=meta.get("line"), outcome_id=token, active=meta.get("active", True),
        resolved=meta.get("resolved", False), restricted=meta.get("restricted", False),
        negative_risk=meta.get("negative_risk"), raw_payload_hash=(
            hashlib.sha256(json.dumps(book, sort_keys=True, separators=(",", ":"))
                           .encode("utf-8")).hexdigest()
        ),
    )


def _quote_from_ws_change(event: Event, token: str, meta: dict, message: dict,
                          change: dict, previous: Quote | None = None) -> Quote | None:
    """Merge one incremental CLOB packet with the last known top of book.

    ``price_change`` and ``best_bid_ask`` packets commonly omit depth.  A
    missing field means unknown/unchanged—not zero shares.  Keeping that
    distinction prevents paper sizing from being incorrectly capped at $0.
    """
    bid = _to_float(change.get("best_bid"))
    ask = _to_float(change.get("best_ask"))
    bid_size = _to_float(change.get("best_bid_size"))
    ask_size = _to_float(change.get("best_ask_size"))

    if message.get("event_type") == "book":
        bids, asks = message.get("bids", []), message.get("asks", [])
        bid_prices = [price for level in bids
                      if (price := _to_float(level.get("price"))) is not None]
        ask_prices = [price for level in asks
                      if (price := _to_float(level.get("price"))) is not None]
        bid = max(bid_prices, default=None)
        ask = min(ask_prices, default=None)
        bid_size = _book_depth(bids, bid)
        ask_size = _book_depth(asks, ask)
    else:
        if bid is None and previous is not None:
            bid = previous.bid
        if ask is None and previous is not None:
            ask = previous.ask

        changed_price = _to_float(change.get("price"))
        changed_size = _to_float(change.get("size"))
        changed_side = str(change.get("side") or "").strip().casefold()
        if changed_price is not None and changed_size is not None:
            if changed_side in {"buy", "bid"} and bid is not None and abs(changed_price - bid) < 1e-9:
                bid_size = changed_size
            elif changed_side in {"sell", "ask"} and ask is not None and abs(changed_price - ask) < 1e-9:
                ask_size = changed_size

        if bid_size is None and previous is not None and bid == previous.bid:
            bid_size = previous.bid_size
        if ask_size is None and previous is not None and ask == previous.ask:
            ask_size = previous.ask_size

    probability = ((bid + ask) / 2 if bid is not None and ask is not None
                   else ask if ask is not None else bid)
    if probability is None:
        return None
    received_at = datetime.now(timezone.utc)
    return Quote(
        event.id, meta["market"], meta["outcome"], probability, "Polymarket",
        bid=bid, ask=ask, liquidity=_visible_liquidity(bid_size, ask_size),
        market_liquidity=meta.get("liquidity"), token_id=token,
        market_slug=meta.get("market_slug"), question=meta.get("question"),
        bid_size=bid_size, ask_size=ask_size,
        min_order_size=meta.get("min_order_size"), tick_size=meta.get("tick_size"),
        accepting_orders=meta.get("accepting_orders", False),
        provider_timestamp=_provider_time(message.get("timestamp")),
        received_at=received_at, processed_at=datetime.now(timezone.utc),
        book_hash=str(message.get("hash")) if message.get("hash") else None,
        depth_complete=message.get("event_type") == "book",
    )


async def _initial_polymarket_quotes(event: Event, token_meta: dict[str, dict]) -> list[Quote]:
    """Fetch complete snapshots through the documented bulk endpoint (max 500)."""
    tokens = list(token_meta)
    quotes: list[Quote] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for start in range(0, len(tokens), 500):
            batch = tokens[start:start + 500]
            response = await client.post(
                "https://clob.polymarket.com/books",
                json=[{"token_id": token} for token in batch],
            )
            response.raise_for_status()
            payload = response.json()
            books = payload if isinstance(payload, list) else payload.get("data", [])
            for book in books:
                token = str(book.get("asset_id") or book.get("token_id") or "")
                if token not in token_meta:
                    continue
                quote = _quote_from_book(event, token, token_meta[token], book)
                if quote is not None:
                    quotes.append(quote)
    return quotes


async def polymarket_market_stream(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    if not event.polymarket_slug:
        return
    backoff = RetryBackoff(base_seconds=1, cap_seconds=60)
    while True:
        try:
            data = await polymarket_event(event.polymarket_slug)
            token_meta = _polymarket_token_meta(data)
            if not token_meta:
                await asyncio.sleep(30)
                continue
            initial = await _initial_polymarket_quotes(event, token_meta)
            latest_quotes = {quote.token_id: quote for quote in initial if quote.token_id}
            book_states: dict[str, OrderBookState] = {}
            for quote in initial:
                if not quote.token_id:
                    continue
                state = OrderBookState(quote.token_id)
                state.bids = {D(price): D(size) for price, size in quote.bid_levels}
                state.asks = {D(price): D(size) for price, size in quote.ask_levels}
                state.book_hash = quote.book_hash
                state.timestamp_ms = (int(quote.provider_timestamp.timestamp() * 1000)
                                      if quote.provider_timestamp else None)
                state.synchronized = bool(state.book_hash and state.timestamp_ms)
                book_states[quote.token_id] = state
            if initial:
                await emit(initial)
                runtime_telemetry.increment("polymarket_quotes", len(initial))
            async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market") as ws:
                backoff.reset()
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
                        event_type = message.get("event_type")
                        if event_type == "new_market":
                            raise BookGapError("market universe changed; resnapshot required")
                        if event_type == "market_resolved":
                            token = str(message.get("asset_id", ""))
                            if token in token_meta:
                                token_meta[token]["accepting_orders"] = False
                                token_meta[token]["active"] = False
                                token_meta[token]["resolved"] = True
                            continue
                        if event_type == "tick_size_change":
                            token = str(message.get("asset_id", ""))
                            tick = _to_float(message.get("new_tick_size") or message.get("tick_size"))
                            if token in token_meta and tick is not None:
                                token_meta[token]["tick_size"] = tick
                            continue
                        if event_type not in {"book", "best_bid_ask", "price_change"}:
                            continue
                        changes = message.get("price_changes") or [message]
                        for change in changes:
                            token = str(change.get("asset_id", ""))
                            if token not in token_meta:
                                continue
                            meta = token_meta[token]
                            state = book_states.setdefault(token, OrderBookState(token))
                            if event_type == "book":
                                state.apply_snapshot(message)
                            elif event_type == "price_change":
                                state.apply_change(message, change)
                            else:
                                best_bid = state.best_bid()
                                best_ask = state.best_ask()
                                offered_bid = _to_float(change.get("best_bid"))
                                offered_ask = _to_float(change.get("best_ask"))
                                if (not state.synchronized or best_bid is None or best_ask is None
                                        or offered_bid != float(best_bid.price)
                                        or offered_ask != float(best_ask.price)):
                                    state.synchronized = False
                                    raise BookGapError("top-of-book event cannot be reconciled")
                            snapshot = {
                                "asset_id": token,
                                "timestamp": str(state.timestamp_ms) if state.timestamp_ms else None,
                                "hash": state.book_hash,
                                "bids": [{"price": str(price), "size": str(size)}
                                         for price, size in state.bids.items()],
                                "asks": [{"price": str(price), "size": str(size)}
                                         for price, size in state.asks.items()],
                                "fee_rate": meta.get("fee_rate"),
                                "fee_schedule_id": meta.get("fee_schedule_id"),
                            }
                            quote = _quote_from_book(event, token, meta, snapshot)
                            if quote is not None:
                                latest_quotes[token] = quote
                                quotes.append(quote)
                    if quotes:
                        await emit(quotes)
                        runtime_telemetry.increment("polymarket_quotes", len(quotes))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_telemetry.increment(f"polymarket_error_{type(exc).__name__}")
            runtime_telemetry.increment("polymarket_reconnects")
            await asyncio.sleep(backoff.next_delay())


async def polymarket_sports_stream(events: Callable[[], list[Event]],
                                    emit: Callable[[GameState], Awaitable[None]],
                                    status_emit: Callable[[str, dict], Awaitable[None]] | None = None):
    backoff = RetryBackoff(base_seconds=1, cap_seconds=60)
    while True:
        try:
            async with websockets.connect("wss://sports-api.polymarket.com/ws") as ws:
                backoff.reset()
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
                    state = _game_state_from_sports_payload(matched, data)
                    if state is not None:
                        await emit(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_telemetry.increment(f"sports_feed_error_{type(exc).__name__}")
            runtime_telemetry.increment("sports_feed_reconnects")
            await asyncio.sleep(backoff.next_delay())


def _game_state_from_sports_payload(event: Event, data: dict) -> GameState | None:
    """Parse a sports update while retaining uncertainty about score orientation."""
    score = str(data.get("score", "")).split("-")
    if len(score) != 2:
        return None
    first, second = _to_float(score[0]), _to_float(score[1])
    if first is None or second is None:
        return None

    provider_home = str(data.get("homeTeam") or data.get("home_team") or "").strip()
    provider_away = str(data.get("awayTeam") or data.get("away_team") or "").strip()
    orientation_verified = bool(
        provider_home and provider_away
        and team_match_score(event.home, provider_home) is not None
        and team_match_score(event.away, provider_away) is not None
    )
    received_at = datetime.now(timezone.utc)
    provider_timestamp = _provider_time(
        data.get("timestamp") or data.get("updatedAt") or data.get("last_updated")
        or data.get("last_update")
    )
    return GameState(
        event.id,
        first,
        second,
        str(data.get("period", "")),
        str(data.get("clock") or data.get("elapsed") or ""),
        "Polymarket sports",
        possession=data.get("turn"),
        status=str(data.get("status", "in_progress")),
        provider_timestamp=provider_timestamp,
        received_at=received_at,
        processed_at=datetime.now(timezone.utc),
        quarantined=not orientation_verified,
        quarantine_reason=None if orientation_verified else "score orientation not verified",
        provider_event_id=str(data.get("gameId") or data.get("game_id") or "") or None,
        canonical_event_id=event.canonical_event_id,
        league_id=str(data.get("leagueAbbreviation") or event.league or "") or None,
        sport_id=event.sport or None,
        home_team_id=provider_home or None,
        away_team_id=provider_away or None,
        live=(bool(data.get("live")) if data.get("live") is not None else None),
        ended=(bool(data.get("ended")) if data.get("ended") is not None else None),
        sequence=(int(data["sequence"]) if str(data.get("sequence", "")).isdigit() else None),
        state_hash=hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        finished_timestamp=_provider_time(data.get("finished_timestamp")),
    )


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
        "regions": os.getenv("ODDS_REGIONS", "us,eu,uk"),
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
                provider_timestamp = _provider_time(
                    market.get("last_update") or bookmaker.get("last_update")
                    or game.get("last_update")
                )
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
                        
                        odds_home = str(game.get("home_team", ""))
                        odds_away = str(game.get("away_team", ""))
                        if odds_home and outcome_label.startswith(odds_home):
                            outcome_label = outcome_label.replace(odds_home, event.home, 1)
                        elif odds_away and outcome_label.startswith(odds_away):
                            outcome_label = outcome_label.replace(odds_away, event.away, 1)
                    quotes.append(Quote(
                        event.id,
                        market_key,
                        outcome_label,
                        american_probability(price),
                        source,
                        decimal_odds=(price / 100 + 1 if price > 0 else 100 / -price + 1),
                        provider_timestamp=provider_timestamp,
                        received_at=datetime.now(timezone.utc),
                        processed_at=datetime.now(timezone.utc),
                        source_family=str(bookmaker.get("key") or source),
                        provider_source_id=str(bookmaker.get("key") or source),
                        provider_event_id=str(game.get("id") or "") or None,
                        canonical_event_id=event.canonical_event_id,
                        provider_market_id=str(provider_key),
                        market_scope=("player_prop" if prop else "full_game"),
                        line=_to_float(outcome.get("point")),
                        outcome_id=str(outcome.get("name") or outcome_label),
                        raw_payload_hash=hashlib.sha256(
                            json.dumps(outcome, sort_keys=True, separators=(",", ":"))
                            .encode("utf-8")
                        ).hexdigest(),
                    ))
    return quotes


async def odds_api_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    key = os.getenv("THE_ODDS_API_KEY")
    if not key or not event.odds_api_sport:
        return

    # Lower interval = fresher sportsbook lines but more (paid) API credits.
    # Polymarket streams in real time regardless; this only paces The Odds API.
    interval = max(5.0, float(os.getenv("ODDS_POLL_SECONDS", "45")))
    backoff = RetryBackoff(base_seconds=5, cap_seconds=180)
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            failed = False
            try:
                if not event.odds_api_event_id:
                    matched = await match_odds_api_event(
                        event.odds_api_sport, event.name, event.game_start
                    )
                    if matched:
                        event.odds_api_event_id = str(matched["id"])
                    else:
                        logger.warning("Could not resolve Odds API event ID for %s; retrying",
                                       event.name)
                        await asyncio.sleep(interval)
                        continue
                url, params = odds_api_request(event, key)
                response = await client.get(url, params=params)
                response.raise_for_status()
                for header in ("x-requests-remaining", "x-requests-used",
                               "x-requests-last"):
                    if header in response.headers:
                        _odds_quota[header] = response.headers[header]
                quotes = odds_api_quotes(event, response.json())
                if quotes:
                    await emit(quotes)
                    runtime_telemetry.increment("odds_api_quotes", len(quotes))
                backoff.reset()
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                failed = True
                runtime_telemetry.increment(f"odds_api_http_{exc.response.status_code}")
                logger.warning("The Odds API returned HTTP %s for %s",
                               exc.response.status_code, event.name)
            except Exception as exc:
                failed = True
                runtime_telemetry.increment(f"odds_api_error_{type(exc).__name__}")
                logger.warning("The Odds API poll failed for %s (%s)", event.name,
                               type(exc).__name__)
            await asyncio.sleep(max(interval, backoff.next_delay()) if failed else interval)



