from datetime import datetime, timezone

from app.advice import market_views, position_views
from app.models import Quote, Signal


NOW = datetime.now(timezone.utc)


def poly_quote():
    return Quote("event", "moneyline", "Knicks", .55, "Polymarket", NOW,
                 bid=.54, ask=.56, liquidity=5000, token_id="token",
                 question="Knicks vs Celtics", bid_size=100, ask_size=80,
                 min_order_size=5, tick_size=.01)


def signal(model=.62, action="PAPER_BET", confidence=82):
    return Signal("event", "moneyline", "Knicks", model, .56, model - .56,
                  confidence, action, ["two sharp references agree"], NOW,
                  quote_source="Polymarket", market_fair_prob=model,
                  devig_method="exchange", n_reference_sources=2)


def test_actionable_market_has_executable_prices_and_entry_ceiling():
    views = market_views([poly_quote()], [signal()], edge_threshold=.035)
    assert len(views) == 1
    assert views[0]["entry_action"] == "ENTRY WINDOW"
    assert views[0]["buy_price"] == .56
    assert views[0]["sell_price"] == .54
    assert abs(views[0]["price_ceiling"] - .585) < 1e-9


def test_market_without_an_ask_is_not_shown_as_placeable():
    quote = poly_quote()
    quote.ask = None
    assert market_views([quote], [signal()], edge_threshold=.035) == []


def test_position_advice_can_hold_or_consider_cash():
    position = {"event_id": "event", "token_id": "token", "market": "moneyline",
                "outcome": "Knicks", "shares": 20.0, "avg_entry_price": .45}
    hold = position_views([position], [poly_quote()], [signal(model=.62)], 72)[0]
    assert hold["advice"] == "HOLD"
    cash = position_views([position], [poly_quote()], [signal(model=.50)], 72)[0]
    assert cash["advice"] == "CONSIDER CASH"
    assert cash["cash_value"] == 10.8
