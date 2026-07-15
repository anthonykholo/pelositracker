"""Turn a sport + period + clock into fraction-of-game-remaining.

The live win-probability model needs to know how much game is left. This is
sport-specific: regulation structure differs, and most feeds report a clock
that counts DOWN within the current period (NBA/NFL/NHL). Sports whose clock
counts up or whose structure we cannot parse reliably (e.g. soccer) return
None, so the caller simply skips the live model rather than using a wrong
fraction. Soccer up-count + Skellam handling is deferred to Phase 2b.
"""
from __future__ import annotations

import re

# sport key fragment -> (regulation periods, seconds per period), down-counting clock
_REGULATION: dict[str, tuple[int, int]] = {
    "basketball": (4, 12 * 60),
    "nba": (4, 12 * 60),
    "wnba": (4, 10 * 60),
    "ncaab": (2, 20 * 60),
    "football": (4, 15 * 60),
    "nfl": (4, 15 * 60),
    "ncaaf": (4, 15 * 60),
    "hockey": (3, 20 * 60),
    "nhl": (3, 20 * 60),
}


def _regulation_for(sport: str) -> tuple[int, int] | None:
    key = (sport or "").strip().casefold()
    if key in _REGULATION:
        return _REGULATION[key]
    for fragment, value in _REGULATION.items():
        if fragment in key:
            return value
    return None


def _period_index(period: str) -> int:
    """Extract the current period number from labels like 'Q4', '2H', '3', 'OT'."""
    text = (period or "").strip().casefold()
    if not text:
        return 1
    if "ot" in text or "overtime" in text:
        return 99  # overtime: treat as past regulation (near-zero time)
    match = re.search(r"\d+", text)
    return int(match.group()) if match else 1


def _clock_seconds(clock: str) -> float | None:
    """Parse a down-count clock 'mm:ss' or 'ss' into seconds; None if blank."""
    text = (clock or "").strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            minutes = float(parts[0])
            seconds = float(parts[1])
        except (ValueError, IndexError):
            return None
        return minutes * 60 + seconds
    try:
        return float(text)
    except ValueError:
        return None


def game_progress(sport: str, period: str, clock: str) -> tuple[float, float] | tuple[None, None]:
    """Return (seconds_remaining, fraction_remaining) in [0, 1], or (None, None).

    Assumes a down-counting clock within each period (correct for
    basketball/football/hockey). Unknown sports return (None, None).
    """
    regulation = _regulation_for(sport)
    if regulation is None:
        return (None, None)
    n_periods, period_len = regulation
    index = _period_index(period)
    within = _clock_seconds(clock)
    if within is None:
        within = float(period_len)  # assume the period just started
    within = max(0.0, min(within, float(period_len)))
    periods_after = max(0, n_periods - index)
    seconds_remaining = periods_after * period_len + within
    total = n_periods * period_len
    fraction = max(0.0, min(seconds_remaining / total, 1.0))
    return (seconds_remaining, fraction)
