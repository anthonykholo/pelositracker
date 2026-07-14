from datetime import datetime, timezone

from app.engine import SignalEngine
from app.models import GameState, Quote


NOW = datetime.now(timezone.utc)


def q(source, outcome, p):
    return Quote("e", "moneyline", outcome, p, source, NOW, bid=p - .01, ask=p + .01)


def test_requires_multiple_sources_and_edge():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=.03)
    result = engine.evaluate("e", [q("one", "home", .50), q("one", "away", .50)], [])
    assert result[0].action == "WATCH"
    assert any("fewer than 2" in reason for reason in result[0].reasons)


def test_momentum_can_create_paper_signal_with_good_market_quality():
    engine = SignalEngine(confidence_threshold=50, edge_threshold=.02)
    quotes = sum(([q(src, "home", p), q(src, "away", 1-p)]
                  for src, p in (("a", .50), ("b", .505), ("c", .495))), [])
    states = [GameState("e", 10, 10, "Q1", "10:00", "feed", NOW),
              GameState("e", 18, 10, "Q1", "08:00", "feed", NOW)]
    result = engine.evaluate("e", quotes, states)
    home = next(x for x in result if x.outcome == "home")
    assert home.action == "PAPER_BET"
    assert home.edge > .02


def test_wide_spread_blocks_signal():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=-1)
    quotes = [Quote("e", "moneyline", side, .5, src, NOW, bid=.40, ask=.51)
              for src in ("a", "b") for side in ("home", "away")]
    result = engine.evaluate("e", quotes, [])
    assert all(x.action == "WATCH" for x in result)
    assert any("wide executable spread" in reason for reason in result[0].reasons)


def test_real_away_team_name_gets_inverse_momentum():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=-1)
    quotes = [q(src, team, p) for src in ("a", "b")
              for team, p in (("Boston", .5), ("New York", .5))]
    states = [GameState("e", 10, 10, "Q1", "10:00", "feed", NOW),
              GameState("e", 18, 10, "Q1", "08:00", "feed", NOW)]
    result = engine.evaluate("e", quotes, states, away_outcome="New York")
    away = next(x for x in result if x.outcome == "New York")
    assert away.model_probability < .5
