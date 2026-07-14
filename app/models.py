from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Event:
    name: str
    sport: str
    home: str
    away: str
    league: str = ""
    polymarket_slug: str | None = None
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

