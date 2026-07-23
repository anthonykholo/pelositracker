"""Tests for joint-outcome (portfolio) Kelly sizing."""
from __future__ import annotations

import pytest

from app.portfolio import (
    Candidate,
    SolveStatus,
    enumerate_scenarios,
    joint_kelly_stakes,
    solve_joint_kelly,
)

BANKROLL = 1000.0


def _c(prob=0.6, price=0.5, group="g", cap=BANKROLL):
    return Candidate(prob=prob, price=price, group=group, cap=cap)


def test_empty_book():
    assert joint_kelly_stakes([], BANKROLL) == []


def test_single_bet_matches_full_kelly():
    # p=.6 at even odds -> Kelly fraction 0.2 -> ~$200.
    [stake] = joint_kelly_stakes([_c()], BANKROLL)
    assert stake == pytest.approx(200.0, abs=25.0)


def test_negative_edge_gets_no_stake():
    [stake] = joint_kelly_stakes([_c(prob=0.45)], BANKROLL)
    assert stake == pytest.approx(0.0, abs=1.0)


def test_perfectly_correlated_bets_share_one_budget():
    both = joint_kelly_stakes([_c(group="same"), _c(group="same")], BANKROLL)
    [single] = joint_kelly_stakes([_c()], BANKROLL)
    # Two identical, perfectly correlated bets ~= one bet's budget, not double.
    assert sum(both) == pytest.approx(single, rel=0.2)
    assert sum(both) < 1.7 * single


def test_independent_bets_diversify_more_total_less_each():
    indep = joint_kelly_stakes([_c(group="a"), _c(group="b")], BANKROLL)
    [single] = joint_kelly_stakes([_c()], BANKROLL)
    assert all(stake < single for stake in indep)      # each shrinks
    assert single < sum(indep) < 2.0 * single          # but total grows


def test_respects_per_bet_cap():
    [stake] = joint_kelly_stakes([_c(cap=30.0)], BANKROLL)
    assert stake == pytest.approx(30.0, abs=1.0)


def test_respects_total_fraction_budget():
    book = [_c(group="a"), _c(group="b"), _c(group="c")]
    stakes = joint_kelly_stakes(book, BANKROLL, max_total_fraction=0.1)
    assert sum(stakes) <= 0.1 * BANKROLL + 1e-6


def test_half_kelly_is_half_of_full():
    [full] = joint_kelly_stakes([_c()], BANKROLL, kelly_multiplier=1.0)
    [half] = joint_kelly_stakes([_c()], BANKROLL, kelly_multiplier=0.5)
    assert half == pytest.approx(0.5 * full, rel=0.1)


def test_single_bet_is_exact_analytical_kelly():
    # p=.6 at even odds -> f* = (0.6*2 - 1)/1 = 0.2 -> exactly $200 (no solver).
    [stake] = joint_kelly_stakes([_c(prob=0.6, price=0.5)], BANKROLL)
    assert stake == pytest.approx(200.0, abs=1e-9)


def test_stakes_are_deterministic_bit_for_bit():
    book = [_c(group="a"), _c(group="b"), _c(prob=0.55, price=0.5, group="c")]
    assert joint_kelly_stakes(book, BANKROLL) == joint_kelly_stakes(book, BANKROLL)


def test_permuting_candidates_permutes_the_output():
    a = _c(prob=0.62, price=0.50, group="a", cap=400.0)
    b = _c(prob=0.55, price=0.45, group="b", cap=400.0)
    forward = joint_kelly_stakes([a, b], BANKROLL)
    reverse = joint_kelly_stakes([b, a], BANKROLL)
    assert reverse[0] == pytest.approx(forward[1])
    assert reverse[1] == pytest.approx(forward[0])


def test_every_scenario_stays_strictly_solvent():
    book = [_c(prob=0.62, group="a"), _c(prob=0.6, group="b")]
    fractions = solve_joint_kelly(book, BANKROLL).fractions
    for scenario in enumerate_scenarios(book):
        wealth = 1.0 + sum(r * fractions[i] for i, r in enumerate(scenario.returns))
        assert wealth > 0.0


def test_same_group_bets_are_comonotonic_not_independent():
    # In a comonotonic group the lower-probability bet only ever wins when the
    # higher-probability bet also wins -- they are never independent draws.
    scenarios = enumerate_scenarios([_c(prob=0.6, group="same"),
                                     _c(prob=0.4, group="same")])
    assert len(scenarios) == 3  # both win / only the 0.6 bet / neither
    for scenario in scenarios:
        higher_wins, lower_wins = scenario.returns[0] > 0, scenario.returns[1] > 0
        assert not (lower_wins and not higher_wins)


def test_independent_groups_form_a_product_space():
    scenarios = enumerate_scenarios([_c(prob=0.6, group="a"), _c(prob=0.6, group="b")])
    assert len(scenarios) == 4
    assert sum(s.probability for s in scenarios) == pytest.approx(1.0)
    both_win = sum(s.probability for s in scenarios
                   if s.returns[0] > 0 and s.returns[1] > 0)
    assert both_win == pytest.approx(0.36)  # 0.6 * 0.6, i.e. genuinely independent


def test_negative_edge_candidate_gets_zero_inside_a_book():
    good = _c(prob=0.62, price=0.5, group="a")
    bad = _c(prob=0.40, price=0.5, group="b")  # E[log-growth] contribution < 0
    stakes = joint_kelly_stakes([good, bad], BANKROLL)
    assert stakes[0] > 0.0
    assert stakes[1] == 0.0


def test_total_and_per_position_caps_hold_for_a_book():
    book = [_c(group="a", cap=50.0), _c(group="b", cap=50.0), _c(group="c", cap=50.0)]
    stakes = joint_kelly_stakes(book, BANKROLL, max_total_fraction=0.08)
    assert all(0.0 <= stake <= 50.0 + 1e-9 for stake in stakes)
    assert sum(stakes) <= 0.08 * BANKROLL + 1e-6


def test_solver_reports_a_converged_kkt_gap():
    result = solve_joint_kelly([_c(group="a"), _c(group="b")], BANKROLL)
    assert result.status is SolveStatus.OPTIMAL
    assert result.kkt_residual is not None and result.kkt_residual <= 1e-6


def test_state_space_blow_up_fails_closed_to_watch():
    # 40 independent singleton groups -> 2**40 scenarios, far past the cap.
    book = [Candidate(prob=0.55, price=0.5, group=f"g{i}", cap=BANKROLL)
            for i in range(40)]
    result = solve_joint_kelly(book, BANKROLL)
    assert result.status is SolveStatus.WATCH
    assert result.reason == "portfolio_state_space_too_large"
    assert result.stakes == [0.0] * len(book)


def test_matches_cvxpy_oracle_when_available():
    cp = pytest.importorskip("cvxpy")
    import numpy as np

    book = [_c(prob=0.62, price=0.50, group="a"),
            _c(prob=0.58, price=0.52, group="b")]
    scenarios = enumerate_scenarios(book)
    returns = np.array([s.returns for s in scenarios])
    pi = np.array([s.probability for s in scenarios])
    x = cp.Variable(len(book), nonneg=True)
    problem = cp.Problem(
        cp.Maximize(pi @ cp.log(1.0 + returns @ x)),
        [cp.sum(x) <= 1.0, x <= 1.0, 1.0 + returns @ x >= 1e-6],
    )
    problem.solve()
    ours = solve_joint_kelly(book, BANKROLL).fractions
    assert np.allclose(ours, x.value, atol=1e-4)
