from datetime import datetime, timedelta, timezone

import pytest

from app.calibration import BetaCoefficients
from app.engine import SignalEngine
from app.models import Quote


NOW = datetime.now(timezone.utc)


_SignalEngine = SignalEngine


class SignalEngine(_SignalEngine):
    """Algorithm tests install an explicit fixture calibration allowlist."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_fixture_policies = True
        self.calibrated_markets = {"moneyline", "spread", "total"}


def q(source, outcome, p, bid=None, ask=None):
    return Quote("e", "moneyline", outcome, p, source, NOW,
                 bid=p - .01 if bid is None else bid,
                 ask=p + .01 if ask is None else ask)


def test_single_source_has_no_independent_reference():
    """One book cannot price against itself, so no edge is estimable."""
    engine = SignalEngine(confidence_threshold=0, edge_threshold=.03)
    result = engine.evaluate("e", [q("Pinnacle", "home", .50), q("Pinnacle", "away", .50)], [], as_of=NOW)
    assert result[0].action == "WATCH"
    assert all(x.n_reference_sources == 0 for x in result)
    assert any("no independent fair" in reason for reason in result[0].reasons)


def test_soft_book_lagging_consensus_is_display_only_without_fill_depth():
    """A price gap is not actionable when expected paper dollars are unknown."""
    engine = SignalEngine(confidence_threshold=50, edge_threshold=.02)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        # DraftKings lags: home buyable at 0.55.
        q("DraftKings", "home", .545, bid=.54, ask=.55),
        q("DraftKings", "away", .455, bid=.45, ask=.46),
    ]
    result = engine.evaluate("e", quotes, [], as_of=NOW)
    home = next(x for x in result if x.outcome == "home")
    assert home.quote_source == "DraftKings"      # cheapest executable
    assert home.n_reference_sources == 2          # leave-one-out excludes DK
    assert home.edge > .02
    assert home.action == "WATCH"
    assert any("net expected dollar value unavailable" in reason for reason in home.reasons)
    assert .58 < home.market_fair_prob < .62       # ~0.60 sharp consensus


def test_stale_duplicate_quotes_collapse_to_freshest_without_changing_decisions():
    """The live store retains a long tail of superseded quotes per selection.
    The engine keeps only the freshest valid quote per selection, so replaying
    the same selections with an older, differently-priced history prepended must
    yield byte-identical decisions to evaluating the latest snapshot alone."""
    engine = SignalEngine(confidence_threshold=50, edge_threshold=.02)
    latest = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        q("DraftKings", "home", .545, bid=.54, ask=.55),
        q("DraftKings", "away", .455, bid=.45, ask=.46),
    ]
    old = NOW - timedelta(minutes=5)
    stale = [
        Quote("e", "moneyline", outcome, price, source, old,
              bid=price - .01, ask=price + .01)
        for source in ("Pinnacle", "Betfair", "DraftKings")
        for outcome, price in (("home", .30), ("away", .70))  # very different prices
    ]

    def decisions(signals):
        return {
            (s.market, s.outcome, s.quote_source): (
                round(s.market_fair_prob, 9), round(s.edge, 9), s.action)
            for s in signals
        }

    baseline = engine.evaluate("e", latest, [], as_of=NOW)
    with_history = engine.evaluate("e", stale + latest, [], as_of=NOW)
    assert decisions(baseline) == decisions(with_history)
    # And the fresh selection actually survived (sanity: not an empty match).
    assert any(s.outcome == "home" for s in with_history)


def test_wide_spread_blocks_signal():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=-1)
    quotes = [Quote("e", "moneyline", side, .5, src, NOW, bid=.40, ask=.51)
              for src in ("Pinnacle", "Betfair") for side in ("home", "away")]
    result = engine.evaluate("e", quotes, [], as_of=NOW)
    assert all(x.action == "WATCH" for x in result)
    assert any("wide executable spread" in reason for reason in result[0].reasons)


def test_traditional_book_pair_is_devigged_with_shin():
    """A booksum > 1 (vig-laden) traditional pair is de-vigged via Shin."""
    engine = SignalEngine(confidence_threshold=0, edge_threshold=-1)
    # booksum = 1.10 (10% overround) on each unknown traditional book.
    quotes = [q(src, side, p)
              for src in ("BookA", "BookB")
              for side, p in (("home", .66), ("away", .44))]
    result = engine.evaluate("e", quotes, [], as_of=NOW)
    home = next(x for x in result if x.outcome == "home")
    assert home.devig_method == "shin"
    assert 0.0 < home.market_fair_prob < 1.0
    assert home.overround > 1.05                    # vig detected


def test_alternate_lines_are_grouped_and_devigged_separately():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        Quote("e", "spread", "Home -1.5", .60, "Pinnacle", NOW),
        Quote("e", "spread", "Away +1.5", .50, "Pinnacle", NOW),
        Quote("e", "spread", "Home -2.5", .55, "Pinnacle", NOW),
        Quote("e", "spread", "Away +2.5", .55, "Pinnacle", NOW),
        Quote("e", "spread", "Home -1.5", .49, "Polymarket", NOW, bid=.48, ask=.50),
        Quote("e", "spread", "Away +1.5", .51, "Polymarket", NOW, bid=.50, ask=.52),
    ]
    result = engine.evaluate("e", quotes, [], home_outcome="Home", away_outcome="Away", as_of=NOW)
    home = next(x for x in result if x.outcome == "Home -1.5")
    assert home.quote_source == "Polymarket"
    assert home.n_reference_sources == 1
    assert .52 < home.market_fair_prob < .58
    assert home.edge > .02


def test_exchange_quote_without_an_ask_is_never_actionable():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW, bid=.54, ask=None),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW, bid=.45, ask=.47),
    ]
    home = next(x for x in engine.evaluate("e", quotes, [], as_of=NOW) if x.outcome == "home")
    assert home.edge > 0
    assert home.action == "WATCH"
    assert any("no executable ask" in reason for reason in home.reasons)


def test_same_book_from_two_adapters_counts_as_one_reference():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("pinnacle", "home", .60), q("pinnacle", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW,
              bid=.53, ask=.55, ask_size=100),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW,
              bid=.45, ask=.47, ask_size=100),
    ]
    home = next(x for x in engine.evaluate("e", quotes, [], as_of=NOW) if x.outcome == "home")
    assert home.n_reference_sources == 1
    assert home.action == "WATCH"
    assert any("fewer than 2 independent" in reason for reason in home.reasons)


def test_incomplete_three_way_book_is_excluded_from_devig():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        # Incomplete Action price set: home/away without Draw must not be
        # normalized as though this were a binary market.
        q("Bookmaker", "home", .55), q("Bookmaker", "away", .30),
        q("Pinnacle", "home", .50), q("Pinnacle", "away", .25),
        q("Pinnacle", "draw", .30),
        Quote("e", "moneyline", "home", .44, "Polymarket", NOW,
              bid=.43, ask=.45, ask_size=100),
        Quote("e", "moneyline", "away", .29, "Polymarket", NOW,
              bid=.28, ask=.30, ask_size=100),
        Quote("e", "moneyline", "draw", .24, "Polymarket", NOW,
              bid=.23, ask=.25, ask_size=100),
    ]
    home = next(x for x in engine.evaluate("e", quotes, [], as_of=NOW) if x.outcome == "home")
    assert home.n_reference_sources == 1
    assert home.action == "WATCH"


def test_stale_opposing_leg_makes_the_whole_book_stale():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0,
                          max_age_seconds=120)
    old = NOW - timedelta(seconds=121)
    quotes = [
        q("Pinnacle", "home", .60),
        Quote("e", "moneyline", "away", .40, "Pinnacle", old),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW,
              bid=.53, ask=.55, ask_size=100),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW,
              bid=.45, ask=.47, ask_size=100),
    ]
    home = next(x for x in engine.evaluate("e", quotes, [], as_of=NOW) if x.outcome == "home")
    assert home.n_reference_sources == 1


def test_unknown_exchange_depth_blocks_paper_execution():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW,
              bid=.53, ask=.55, liquidity=None, ask_size=None),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW,
              bid=.45, ask=.47, liquidity=None, ask_size=None),
    ]
    home = next(x for x in engine.evaluate("e", quotes, [], as_of=NOW) if x.outcome == "home")
    assert home.edge > 0
    assert home.fillable_size is None
    assert home.action == "WATCH"
    assert any("order-book depth unavailable" in reason for reason in home.reasons)


def test_as_of_is_required_and_identical_inputs_have_identical_hashes():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
              q("Circa", "home", .60), q("Circa", "away", .40)]
    with pytest.raises(ValueError, match="as_of"):
        engine.evaluate("e", quotes, [])
    first = engine.evaluate("e", quotes, [], as_of=NOW)
    second = engine.evaluate("e", quotes, [], as_of=NOW)
    assert [signal.decision_hash for signal in first] == [signal.decision_hash for signal in second]
    assert [signal.decision_id for signal in first] == [signal.decision_id for signal in second]
    assert all(signal.engine_version and signal.configuration_hash for signal in first)
    assert all(signal.input_snapshot_json for signal in first)
    assert [signal.action for signal in first] == [signal.action for signal in second]


def test_missing_calibration_artifact_blocks_action():
    engine = _SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
              q("Circa", "home", .60), q("Circa", "away", .40),
              Quote("e", "moneyline", "home", .50, "DraftKings", NOW,
                    bid=.49, ask=.51)]
    signal = next(value for value in engine.evaluate("e", quotes, [], as_of=NOW)
                  if value.outcome == "home")
    assert signal.action == "WATCH"
    assert signal.quality_calibration == 0
    assert any("calibration artifact unavailable" in reason for reason in signal.reasons)


def test_native_contract_rejects_undersized_policy_override():
    engine = _SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    engine.model_policy_overrides["moneyline"] = {
        **engine._legacy_test_policy("moneyline"),
        "sample_size": 999,
        "model_sample_size": 999,
    }
    quotes = [
        q("Pinnacle", "home", .70), q("Pinnacle", "away", .30),
        q("Circa", "home", .70), q("Circa", "away", .30),
        Quote(
            "e", "moneyline", "home", .50, "Polymarket", NOW,
            bid=.49, ask=.50, ask_size=1000, depth_complete=True,
            ask_levels=((.50, 1000.0),), fee_rate=0.0,
            tick_size=.01, min_order_size=1.0,
        ),
    ]

    signal = next(value for value in engine.evaluate("e", quotes, [], as_of=NOW)
                  if value.outcome == "home")

    assert signal.action == "WATCH"
    assert any("sample is below 1,000" in reason for reason in signal.reasons)


def test_beta_policy_exposes_canonical_probabilities_and_bootstrap_ev_gate():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    policy = engine._legacy_test_policy("moneyline")
    policy.update({
        "calibration_method": "beta",
        "beta_coefficients": [1.0, 1.0, -0.3],
        "beta_bootstrap_coefficients": (
            [[1.0, 1.0, -0.3]] * 100 + [[1.0, 1.0, -1.0]] * 100
        ),
        "min_probability_positive": 0.9,
        "min_expected_value_dollars": 0.0,
        "sample_size": 1500,
    })
    engine.model_policy_overrides["moneyline"] = policy
    quotes = [
        q("Pinnacle", "home", .65), q("Pinnacle", "away", .35),
        q("Betfair", "home", .65), q("Betfair", "away", .35),
        Quote(
            "e", "moneyline", "home", .55, "Polymarket", NOW,
            bid=.55, ask=.56, ask_size=1000, depth_complete=True,
            ask_levels=((.56, 1000.0),), fee_rate=0.0,
            tick_size=.01, min_order_size=1.0,
        ),
        Quote(
            "e", "moneyline", "away", .45, "Polymarket", NOW,
            bid=.44, ask=.45, ask_size=1000, depth_complete=True,
            ask_levels=((.45, 1000.0),), fee_rate=0.0,
            tick_size=.01, min_order_size=1.0,
        ),
    ]
    home = next(value for value in engine.evaluate("e", quotes, [], as_of=NOW)
                if value.outcome == "home")
    assert home.consensus_probability == pytest.approx(.65, abs=.01)
    assert home.calibrated_consensus_probability is not None
    assert home.calibrated_consensus_probability == pytest.approx(
        BetaCoefficients(1.0, 1.0, -0.3).calibrate(home.consensus_probability),
        abs=1e-12,
    )
    assert home.calibrated_consensus_probability < home.consensus_probability
    assert home.uncertainty_low is not None and home.uncertainty_high is not None
    assert home.probability_net_ev_positive == pytest.approx(.5)
    assert home.net_expected_value_per_share is not None
    assert home.net_expected_value_total is not None
    assert home.action == "WATCH"
    probability_gate = next(
        gate for gate in home.gate_results
        if gate["code"] == "probability_net_ev_positive"
    )
    assert probability_gate["status"] == "fail"
    assert probability_gate["passed"] is False
    assert probability_gate["threshold"] == .9


def test_pipeline_bootstrap_propagates_consensus_choice_into_uncertainty():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    policy = engine._legacy_test_policy("moneyline")

    def draw(source, execution_cost_offset):
        return {
            "pipeline": f"sharp-{source}",
            "devig_method": "proportional",
            "consensus_method": "sharp_source",
            "beta_coefficients": [1.0, 1.0, 0.0],
            "execution_cost_offset": execution_cost_offset,
            "sharp_source_family": source,
            "consensus_intercept": 0.0,
            "family_coefficients": {},
            "missing_family_coefficients": {},
        }

    policy.update({
        "devig_method": "proportional",
        "uncertainty_draws": (
            [draw("pinnacle", .01)] * 100 + [draw("betfair", .02)] * 100
        ),
        "min_probability_positive": .9,
    })
    engine.model_policy_overrides["moneyline"] = policy
    quotes = [
        q("Pinnacle", "home", .70), q("Pinnacle", "away", .30),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        Quote(
            "e", "moneyline", "home", .63, "Polymarket", NOW,
            bid=.63, ask=.64, ask_size=1000, depth_complete=True,
            ask_levels=((.64, 1000.0),), fee_rate=0.0,
            tick_size=.01, min_order_size=1.0,
        ),
        Quote(
            "e", "moneyline", "away", .37, "Polymarket", NOW,
            bid=.35, ask=.36, ask_size=1000, depth_complete=True,
            ask_levels=((.36, 1000.0),), fee_rate=0.0,
            tick_size=.01, min_order_size=1.0,
        ),
    ]
    home = next(value for value in engine.evaluate("e", quotes, [], as_of=NOW)
                if value.outcome == "home")
    assert home.uncertainty_low == pytest.approx(.60, abs=.01)
    assert home.uncertainty_high == pytest.approx(.70, abs=.01)
    assert home.probability_net_ev_positive == pytest.approx(.5)
    assert home.net_expected_value_per_share == pytest.approx(-.005, abs=.01)
    assert home.kelly_fraction == 0
    assert home.action == "WATCH"
