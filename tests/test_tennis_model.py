import pytest

from app.tennis_model import (
    game_prob_from_prematch,
    match_win_prob,
    parse_tennis_score,
    set_complete,
)


def test_even_players_are_a_coin_flip_at_the_start():
    g = game_prob_from_prematch(0.5)
    assert g == pytest.approx(0.5, abs=1e-3)
    assert match_win_prob(0, 0, 0, 0, g) == pytest.approx(0.5, abs=1e-3)


@pytest.mark.parametrize("p0", [0.35, 0.5, 0.62, 0.8, 0.93])
def test_prematch_anchor_round_trips(p0):
    g = game_prob_from_prematch(p0)
    assert match_win_prob(0, 0, 0, 0, g) == pytest.approx(p0, abs=2e-3)


def test_five_set_anchor_round_trips():
    g = game_prob_from_prematch(0.7, best_of=5)
    assert match_win_prob(0, 0, 0, 0, g, best_of=5) == pytest.approx(0.7, abs=2e-3)


def test_leading_improves_win_probability():
    g = game_prob_from_prematch(0.5)
    level = match_win_prob(0, 0, 0, 0, g)
    up_a_break = match_win_prob(0, 0, 3, 1, g)
    up_a_set = match_win_prob(1, 0, 0, 0, g)
    assert up_a_break > level
    assert up_a_set > up_a_break


def test_winning_the_last_set_settles_the_match():
    g = game_prob_from_prematch(0.5)
    assert match_win_prob(2, 0, 0, 0, g) == 1.0
    assert match_win_prob(0, 2, 0, 0, g) == 1.0 - 1.0  # opponent has clinched


def test_higher_strength_monotonic():
    weak = match_win_prob(0, 0, 0, 0, game_prob_from_prematch(0.4))
    strong = match_win_prob(0, 0, 0, 0, game_prob_from_prematch(0.6))
    assert strong > weak


def test_underdog_serving_out_a_set_can_lead_the_market():
    # Anchored 30% underdog who is up a set and a break is now favored to win it.
    g = game_prob_from_prematch(0.30)
    assert match_win_prob(1, 0, 5, 3, g) > 0.5


def test_set_complete_rules():
    assert set_complete(6, 4)
    assert set_complete(7, 5)
    assert set_complete(7, 6)
    assert not set_complete(6, 5)
    assert not set_complete(5, 3)


def test_parse_current_set_only():
    assert parse_tennis_score("1-1", "S1") == (0, 0, 1, 1)


def test_parse_completed_first_set_then_current():
    assert parse_tennis_score("6-3, 3-3", "S2") == (1, 0, 3, 3)


def test_parse_two_completed_sets_split():
    assert parse_tennis_score("5-7, 3-5", "S2") == (0, 1, 3, 5)


def test_parse_tiebreak_pins_current_set_to_six_all():
    assert parse_tennis_score("0-0", "TB2") == (0, 0, 6, 6)


def test_parse_rejects_non_tennis_scores():
    assert parse_tennis_score("", "S1") is None
    assert parse_tennis_score("abc", "S1") is None
