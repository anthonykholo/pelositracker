"""Pinnacle sharp-line feed via their public guest API.

Pinnacle is the sharpest sportsbook globally and anchors the fair-value
consensus (weight=1.0 in SOURCE_TIERS). This module polls their guest
API for moneyline, spread, and total markets with unlimited, free access.
"""
import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from .models import Event, Quote

logger = logging.getLogger(__name__)

_API_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
_HEADERS = {"x-api-key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"}

# Map from our internal odds_api_sport keys to Pinnacle league IDs
_SPORT_TO_LEAGUE: dict[str, int] = {
    "baseball_mlb": 246,
    "basketball_wnba": 578,
    "basketball_nba": 487,
    "americanfootball_nfl": 889,
    "icehockey_nhl": 1456,
    "soccer_usa_mls": 2627,
    "soccer_epl": 1980,
}


def _implied_prob(american: int | float | None) -> float:
    """Convert American odds to implied probability."""
    if american is None:
        return 0.0
    if american > 0:
        return 100.0 / (american + 100.0)
    else:
        return -american / (-american + 100.0)


def _match_pinnacle_game(event: Event, matchups: list[dict]) -> dict | None:
    """Match an Event to a Pinnacle matchup by team name."""
    target_home = event.home.lower().split()[-1]
    target_away = event.away.lower().split()[-1]

    for m in matchups:
        if m.get("type") != "matchup":
            continue
        if m.get("special"):
            continue
        parts = m.get("participants", [])
        if len(parts) != 2:
            continue
        names = [p.get("name", "").lower() for p in parts]
        combined = " ".join(names)
        if target_home in combined and target_away in combined:
            return m
    return None


def _parse_pinnacle_quotes(event: Event, matchup: dict, markets: list[dict]) -> list[Quote]:
    """Convert Pinnacle market data into Quote objects."""
    quotes: list[Quote] = []

    for mk in markets:
        mk_type = mk.get("type", "")
        prices = mk.get("prices", [])
        key = mk.get("key", "")

        # Only process full-game markets (key starts with s;0;)
        # s;0; = full game, s;1; = first half, s;3; = first period/inning
        if not key.startswith("s;0;"):
            continue

        if mk_type == "moneyline":
            for p in prices:
                desig = p.get("designation", "")
                price = p.get("price")
                if price is None:
                    continue
                if desig == "home":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="moneyline",
                        outcome=event.home,
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))
                elif desig == "away":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="moneyline",
                        outcome=event.away,
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))

        elif mk_type == "spread":
            for p in prices:
                desig = p.get("designation", "")
                price = p.get("price")
                points = p.get("points")
                if price is None or points is None:
                    continue
                if desig == "home":
                    sign = "+" if points >= 0 else ""
                    quotes.append(Quote(
                        event_id=event.id,
                        market="spread",
                        outcome=f"{event.home} {sign}{points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))
                elif desig == "away":
                    sign = "+" if points >= 0 else ""
                    quotes.append(Quote(
                        event_id=event.id,
                        market="spread",
                        outcome=f"{event.away} {sign}{points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))

        elif mk_type == "total":
            # Skip team totals
            if "tt;" in key:
                continue
            for p in prices:
                desig = p.get("designation", "")
                price = p.get("price")
                points = p.get("points")
                if price is None or points is None:
                    continue
                if desig == "over":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="total",
                        outcome=f"Over {points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))
                elif desig == "under":
                    quotes.append(Quote(
                        event_id=event.id,
                        market="total",
                        outcome=f"Under {points:g}",
                        probability=_implied_prob(price),
                        source="pinnacle",
                    ))

    return quotes


async def pinnacle_poll(event: Event, emit: Callable[[list[Quote]], Awaitable[None]]):
    """Poll Pinnacle for sharp odds on a tracked event."""
    if not event.odds_api_sport:
        return

    league_id = _SPORT_TO_LEAGUE.get(event.odds_api_sport)
    if league_id is None:
        logger.warning(f"No Pinnacle league mapping for sport: {event.odds_api_sport}")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                # Step 1: Get matchups for the league
                r = await client.get(
                    f"{_API_BASE}/leagues/{league_id}/matchups",
                    headers=_HEADERS,
                    params={"brandId": "0"},
                )
                if r.status_code != 200:
                    logger.warning(f"Pinnacle matchups status {r.status_code} for {event.name}")
                    await asyncio.sleep(60)
                    continue

                matchups = r.json()
                matched = _match_pinnacle_game(event, matchups)
                if not matched:
                    logger.debug(f"No Pinnacle match for {event.name}")
                    await asyncio.sleep(60)
                    continue

                matchup_id = matched.get("id")

                # Step 2: Get markets/odds for this matchup
                r2 = await client.get(
                    f"{_API_BASE}/matchups/{matchup_id}/markets/related/straight",
                    headers=_HEADERS,
                    params={"primaryOnly": "true"},
                )
                if r2.status_code != 200:
                    logger.warning(f"Pinnacle markets status {r2.status_code} for {event.name}")
                    await asyncio.sleep(60)
                    continue

                markets = r2.json()
                quotes = _parse_pinnacle_quotes(event, matched, markets)
                if quotes:
                    logger.info(f"Pinnacle: {len(quotes)} quotes for {event.name}")
                    await emit(quotes)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pinnacle poll error for {event.name}: {e}")

            await asyncio.sleep(45)  # Pinnacle lines move slowly; 45s is plenty
