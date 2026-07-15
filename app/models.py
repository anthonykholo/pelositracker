from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Source trust tiers used to weight the consensus fair value. Sharp books and
# exchanges anchor the fair price; soft books contribute little. Keys are matched
# as case-insensitive substrings of the quote source name.
SOURCE_TIERS: dict[str, tuple[float, bool]] = {
    # name fragment -> (weight, is_exchange)
    "pinnacle": (1.0, False),
    "circa": (1.0, False),
    "bookmaker": (0.9, False),
    "betonline": (0.7, False),
    # exchanges: quotes already trade near a de-vigged mid
    "polymarket": (0.9, True),
    "betfair": (0.9, True),
    "smarkets": (0.8, True),
    "matchbook": (0.8, True),
    "prophetx": (0.8, True),
    "kalshi": (0.8, True),
    # soft retail books (contribute little to fair value)
    "draftkings": (0.2, False),
    "fanduel": (0.2, False),
    "betmgm": (0.2, False),
    "caesars": (0.2, False),
    "espn bet": (0.2, False),
    "bet365": (0.25, False),
    "pointsbet": (0.2, False),
    # demo feed used offline
    "demo exchange": (0.9, True),
    "demobook": (0.3, False),
}


def classify_source(name: str) -> tuple[float, bool]:
    """Return (fair-value weight, is_exchange) for a quote source name."""
    lowered = (name or "").casefold()
    for fragment, tier in SOURCE_TIERS.items():
        if fragment in lowered:
            return tier
    return (0.35, False)  # unknown book: modest default weight


@dataclass(slots=True)
class Event:
    name: str
    sport: str
    home: str
    away: str
    league: str = ""
    polymarket_slug: str | None = None
    polymarket_url: str | None = None
    polymarket_restricted: bool = False
    odds_api_sport: str | None = None
    odds_api_event_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class GameState:
    event_id: str
    home_score: float
    away_score: float
    period: str
    clock: str
    source: str
    observed_at: datetime = field(default_factory=now_utc)
    possession: str | None = None
    status: str = "in_progress"


@dataclass(slots=True)
class Quote:
    event_id: str
    market: str
    outcome: str
    probability: float
    source: str
    observed_at: datetime = field(default_factory=now_utc)
    decimal_odds: float | None = None
    bid: float | None = None
    ask: float | None = None
    liquidity: float | None = None
    market_liquidity: float | None = None
    token_id: str | None = None
    market_slug: str | None = None
    question: str | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    min_order_size: float | None = None
    tick_size: float | None = None
    accepting_orders: bool = True

    @property
    def executable_probability(self) -> float:
        return self.ask if self.ask is not None else self.probability


@dataclass(slots=True)
class Signal:
    event_id: str
    market: str
    outcome: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    action: str
    reasons: list[str]
    observed_at: datetime = field(default_factory=now_utc)
    quote_source: str = ""
    # Phase 0 auditable fields from the Rust engine.
    market_fair_prob: float = 0.0
    devig_method: str = ""
    overround: float = 1.0
    n_reference_sources: int = 0
    # Phase 2a: independent live win-probability (moneyline only), or None.
    model_live_prob: float | None = None


def as_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: as_json(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, list):
        return [as_json(item) for item in value]
    if isinstance(value, dict):
        return {key: as_json(item) for key, item in value.items()}
    return value

