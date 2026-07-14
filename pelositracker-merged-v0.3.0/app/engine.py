"""Thin Python adapter around the Rust signal engine."""

from __future__ import annotations

import json

from .models import GameState, Quote, Signal

try:
    from ._native_engine import evaluate_json
except ImportError as exc:  # pragma: no cover - exercised only before native build
    raise ImportError(
        "The Rust engine is not built. Run: .\\.venv\\Scripts\\python.exe -m "
        "maturin develop --release"
    ) from exc


class SignalEngine:
    """Python-facing configuration wrapper for the Rust recommendation engine."""

    def __init__(self, confidence_threshold: float = 72, edge_threshold: float = 0.035,
                 max_age_seconds: float = 20):
        self.confidence_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.max_age_seconds = max_age_seconds

    def evaluate(self, event_id: str, quotes: list[Quote], states: list[GameState],
                 away_outcome: str = "away") -> list[Signal]:
        request = {
            "event_id": event_id,
            "confidence_threshold": self.confidence_threshold,
            "edge_threshold": self.edge_threshold,
            "max_age_seconds": self.max_age_seconds,
            "away_outcome": away_outcome,
            "quotes": [
                {
                    "market": q.market,
                    "outcome": q.outcome,
                    "probability": q.probability,
                    "source": q.source,
                    "observed_at": q.observed_at.timestamp(),
                    "bid": q.bid,
                    "ask": q.ask,
                }
                for q in quotes
            ],
            "states": [
                {
                    "home_score": s.home_score,
                    "away_score": s.away_score,
                    "observed_at": s.observed_at.timestamp(),
                }
                for s in states
            ],
        }
        results = json.loads(evaluate_json(json.dumps(request, separators=(",", ":"))))
        return [Signal(**result) for result in results]

