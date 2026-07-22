from datetime import datetime, timezone

import pytest

from app.advice import market_views, position_views
from app.models import Quote, Signal


NOW = datetime.now(timezone.utc)


def poly_quote():
    return Quote("event", "moneyline", "Knicks", .55, "Polymarket", NOW,
                 bid=.54, ask=.56, liquidity=5000, token_id="token",
                 question="Knicks vs Celtics", bid_size=100, ask_size=80,
                 min_order_size=5, tick_size=.01)


def signal(model=.62, action="PAPER_BET", confidence=82, required_edge=0.0):
    return Signal("event", "moneyline", "Knicks", model, .56, model - .56,
                  confidence, action, ["two sharp references agree"], NOW,
                  quote_source="Polymarket", market_fair_prob=model,
                  devig_method="exchange", n_reference_sources=2,
                  required_edge=required_edge, quality_freshness=90,
                  quality_agreement=80, quality_sources=67, quality_execution=95,
                  quality_data_completeness=100,
                  quality_provider_freshness=90, quality_identity=100,
                  quality_model_sample_support=100,
                  quality_calibration_support=100,
                  quality_source_independence=67,
                  consensus_probability=model,
                  calibrated_consensus_probability=model,
                  uncertainty_low=model - .02, uncertainty_high=model + .02,
                  probability_net_ev_positive=.98,
                  net_expected_value_per_share=model - .56,
                  net_expected_value_total=80 * (model - .56),
                  consensus_method="equal_family_logit",
                  calibration_sample_size=1500,
                  gate_results=[{"code": "paper_policy", "status": "pass"}])


def uncalibrated_signal(consensus=.62, action="WATCH", required_edge=0.0):
    """A signal with references but no calibration artifact (net EV unavailable)."""
    return Signal("event", "moneyline", "Knicks", consensus, .56, consensus - .56,
                  80, action, ["display-only consensus"], NOW,
                  quote_source="Polymarket", market_fair_prob=consensus,
                  n_reference_sources=2, required_edge=required_edge,
                  consensus_probability=consensus,
                  calibrated_consensus_probability=None,
                  net_expected_value_per_share=None,
                  gate_results=[])


def test_gross_edge_shown_when_calibrated_net_edge_is_unavailable():
    view = market_views([poly_quote()], [uncalibrated_signal(consensus=.62)],
                        edge_threshold=0.0)[0]
    assert view["edge_basis"] == "gross"
    assert view["gross_edge"] == pytest.approx(.06)      # consensus .62 - ask .56
    assert view["edge"] == pytest.approx(.06)
    assert view["entry_action"] == "WAIT"                # engine verdict unchanged
    assert "calibration" in view["why_no_entry"].lower()


def test_why_no_entry_flags_edge_below_the_required_floor():
    view = market_views([poly_quote()], [uncalibrated_signal(consensus=.57)],
                        edge_threshold=0.05)[0]
    assert view["edge_basis"] == "gross"                 # gross edge .01 < required .05
    assert "below the required" in view["why_no_entry"]


def test_why_no_entry_is_none_for_an_entry_window():
    view = market_views([poly_quote()], [signal()], edge_threshold=.035)[0]
    assert view["entry_action"] == "ENTRY WINDOW"
    assert view["why_no_entry"] is None


def test_actionable_market_has_executable_prices_and_entry_ceiling():
    views = market_views([poly_quote()], [signal()], edge_threshold=.035)
    assert len(views) == 1
    assert views[0]["entry_action"] == "ENTRY WINDOW"
    assert views[0]["buy_price"] == .56
    assert views[0]["sell_price"] == .54
    assert abs(views[0]["price_ceiling"] - .585) < 1e-9
    assert views[0]["edge"] == views[0]["entry_margin"]
    assert views[0]["quality_components"]["provider_freshness"] == 90


def test_entry_ceiling_uses_risk_adjusted_required_edge():
    views = market_views([poly_quote()], [signal(required_edge=.06)], edge_threshold=.035)
    assert abs(views[0]["price_ceiling"] - .56) < 1e-9
    assert views[0]["required_edge"] == .06
    assert abs(views[0]["edge_buffer"] - 0.0) < 1e-9


def test_entry_ceiling_includes_depth_fee_and_historical_execution_costs():
    value = signal(required_edge=.035)
    value.market_probability = .58
    value.net_expected_value_per_share = .02

    view = market_views([poly_quote()], [value], edge_threshold=.035)[0]

    assert view["expected_execution_cost_offset"] == pytest.approx(.02)
    assert view["requested_effective_cost"] == pytest.approx(.58)
    assert view["edge"] == pytest.approx(.02)
    assert view["price_ceiling"] == pytest.approx(.545)
    assert view["room_to_ceiling"] == pytest.approx(-.015)


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
