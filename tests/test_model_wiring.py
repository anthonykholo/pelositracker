"""Tests for the in-play model wiring in app.main (anchor capture, staleness)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.main as main_module
from app.accounts import grade
from app.main import _is_moneyline_market, _prematch_anchor, _settle_scores, _state_age_seconds
from app.models import Event, GameState, Signal


def _signal(outcome, market, prob):
    return Signal(event_id="e", market=market, outcome=outcome, model_probability=prob,
                  market_probability=0.5, edge=0.1, confidence=90, action="WATCH", reasons=[])


def _event():
    return Event("Lakers vs Celtics", "basketball", "Lakers", "Celtics", league="nba", id="e")


def test_anchor_prefers_moneyline_and_ignores_a_spread_on_the_same_side():
    event = _event()
    signals = [
        _signal("Lakers", "spread", 0.55),      # same side, wrong market
        _signal("Lakers", "moneyline", 0.62),   # the real win-prob anchor
    ]
    assert _prematch_anchor(signals, event, "home", _is_moneyline_market) == 0.62


def test_anchor_returns_none_when_only_a_non_moneyline_price_exists():
    event = _event()
    signals = [_signal("Lakers", "spread", 0.55)]
    assert _prematch_anchor(signals, event, "home", _is_moneyline_market) is None


def test_anchor_maps_away_side_and_skips_degenerate_probabilities():
    event = _event()
    signals = [
        _signal("Celtics", "moneyline", 0.0),    # degenerate, skipped
        _signal("Celtics", "moneyline", 0.41),
    ]
    assert _prematch_anchor(signals, event, "away", _is_moneyline_market) == 0.41


def test_draw_anchor_is_not_filtered_by_the_moneyline_guard():
    event = Event("A vs B", "soccer", "A", "B", league="epl", id="e")
    signals = [_signal("Draw", "1x2 draw condition", 0.27)]
    assert _prematch_anchor(signals, event, "draw", lambda _market: True) == 0.27


def _state(**kw):
    base = dict(event_id="e", home_score=10, away_score=8, period="Q2", clock="5:00",
                source="feed")
    base.update(kw)
    return GameState(**base)


def test_state_age_is_none_without_a_trusted_provider_timestamp():
    # No provider timestamp -> unknown freshness -> None (callers fail closed).
    assert _state_age_seconds(_state(), datetime.now(timezone.utc)) is None


def test_state_age_uses_the_provider_timestamp_when_trusted():
    now = datetime.now(timezone.utc)
    state = _state(provider_timestamp=now - timedelta(seconds=45))
    assert state.timestamp_trusted is True
    assert _state_age_seconds(state, now) == 45.0


def test_tennis_settles_by_set_count_and_grades_the_match_winner():
    event = Event("Alcaraz vs Sinner", "tennis", "Alcaraz", "Sinner",
                  league="atp", id="tn", polymarket_slug="alcaraz-sinner")
    main_module._sports_status["alcaraz-sinner"] = {"score": "6-3, 6-4", "period": "S2"}
    try:
        # A stale set-games GameState must NOT drive the grade; the set count does.
        home, away = _settle_scores(event, [_state(event_id="tn", home_score=3, away_score=4)])
    finally:
        main_module._sports_status.pop("alcaraz-sinner", None)
    assert (home, away) == (2.0, 0.0)
    assert grade("moneyline", "Alcaraz", "Alcaraz", "Sinner", home, away) == "win"


def test_tennis_falls_back_to_the_live_state_when_no_score_is_cached():
    event = Event("A vs B", "tennis", "A", "B", league="atp", id="none", polymarket_slug="x")
    assert _settle_scores(event, [_state(event_id="none", home_score=1, away_score=0)]) == (1, 0)


def test_non_tennis_settles_by_final_live_score():
    assert _settle_scores(_event(), [_state(home_score=101, away_score=99)]) == (101, 99)
