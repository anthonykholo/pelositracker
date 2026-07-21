"""Explicit league clock normalization and game-state validation.

Unknown formats return `None`; they are never converted into plausible game time.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class LeagueRule:
    key: str
    period_count: int
    period_seconds: int
    clock_direction: str
    period_aliases: tuple[str, ...] = ()


LEAGUE_RULES: dict[str, LeagueRule] = {
    "nba": LeagueRule("nba", 4, 12 * 60, "down", ("basketball_nba",)),
    "wnba": LeagueRule("wnba", 4, 10 * 60, "down", ("basketball_wnba",)),
    "ncaab": LeagueRule("ncaab", 2, 20 * 60, "down", ("basketball_ncaab",)),
    "nfl": LeagueRule("nfl", 4, 15 * 60, "down", ("americanfootball_nfl",)),
    "ncaaf": LeagueRule("ncaaf", 4, 15 * 60, "down", ("americanfootball_ncaaf",)),
    "nhl": LeagueRule("nhl", 3, 20 * 60, "down", ("icehockey_nhl",)),
    "mls": LeagueRule("mls", 2, 45 * 60, "up", ("soccer_usa_mls",)),
    "epl": LeagueRule("epl", 2, 45 * 60, "up", ("soccer_epl",)),
}

_OVERTIME = re.compile(r"^(?:\d*ot\d*|overtime|shootout|so)$")


def league_rule(sport: str, league: str = "") -> LeagueRule | None:
    candidates = ((league or "").strip().casefold(), (sport or "").strip().casefold())
    for candidate in candidates:
        if candidate in LEAGUE_RULES:
            return LEAGUE_RULES[candidate]
        for rule in LEAGUE_RULES.values():
            if candidate in rule.period_aliases:
                return rule
    # Generic sport names do not determine league period length (for example,
    # NBA and WNBA differ). Missing league identity is therefore unknown.
    return None


def _period_index(period: str) -> int | None:
    text = (period or "").strip().casefold()
    if not text:
        return None
    tokens = [token for token in re.split(r"[^a-z0-9]+", text) if token]
    if any(_OVERTIME.fullmatch(token) for token in tokens):
        return 99
    if text in {"first half", "1h", "h1"}:
        return 1
    if text in {"second half", "2h", "h2"}:
        return 2
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def clock_seconds(clock: str) -> float | None:
    text = (clock or "").strip()
    if not text:
        return None
    try:
        if ":" in text:
            parts = text.split(":")
            if len(parts) != 2:
                return None
            minutes, seconds = (float(part) for part in parts)
            if minutes < 0 or seconds < 0 or seconds >= 60:
                return None
            return minutes * 60 + seconds
        value = float(text)
        return value if value >= 0 else None
    except ValueError:
        return None


def game_progress(
    sport: str,
    period: str,
    clock: str,
    league: str = "",
) -> tuple[float, float] | tuple[None, None]:
    """Return regulation seconds/fraction remaining, or unknown.

    Overtime has no comparable regulation fraction and therefore returns unknown.
    Missing periods, missing clocks, malformed clocks, and impossible values also
    return unknown rather than inventing a period boundary.
    """
    rule = league_rule(sport, league)
    index = _period_index(period)
    clock_value = clock_seconds(clock)
    if rule is None or index is None or clock_value is None:
        return (None, None)
    if index < 1 or index > rule.period_count:
        return (None, None)

    total = rule.period_count * rule.period_seconds
    if rule.clock_direction == "down":
        if clock_value > rule.period_seconds:
            return (None, None)
        remaining = (rule.period_count - index) * rule.period_seconds + clock_value
    else:
        # Soccer-style feeds may report a whole-match minute in the second half
        # or a within-half minute. Normalize either form conservatively.
        elapsed = clock_value
        if index > 1 and elapsed <= rule.period_seconds:
            elapsed += (index - 1) * rule.period_seconds
        if elapsed > total:
            return (None, None)
        remaining = total - elapsed
    return (remaining, max(0.0, min(remaining / total, 1.0)))


@dataclass(frozen=True, slots=True)
class StateValidation:
    valid: bool
    reason: str | None = None


def validate_state_transition(
    *,
    sport: str,
    period: str,
    clock: str,
    home_score: float,
    away_score: float,
    previous_period: str | None = None,
    previous_clock: str | None = None,
    previous_home_score: float | None = None,
    previous_away_score: float | None = None,
    league: str = "",
) -> StateValidation:
    if home_score < 0 or away_score < 0:
        return StateValidation(False, "negative score")
    if previous_home_score is not None and home_score < previous_home_score:
        return StateValidation(False, "home score regressed")
    if previous_away_score is not None and away_score < previous_away_score:
        return StateValidation(False, "away score regressed")
    remaining, _ = game_progress(sport, period, clock, league)
    if remaining is None:
        return StateValidation(False, "unknown or invalid game clock")
    if previous_period is not None and previous_clock is not None:
        prior_remaining, _ = game_progress(sport, previous_period, previous_clock, league)
        if prior_remaining is not None and remaining > prior_remaining + 2:
            return StateValidation(False, "game clock regressed")
    return StateValidation(True)
