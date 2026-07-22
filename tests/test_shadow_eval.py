"""Tests for shadow evaluation of model-backed paper bets."""
from __future__ import annotations

from app import shadow_eval


def _bet(model_prob, entry_price, result, sport="tennis", event_id="e1"):
    return {
        "event_id": event_id,
        "sport": sport,
        "model_prob": model_prob,
        "entry_price": entry_price,
        "result": result,          # 1.0 win, 0.0 loss, None otherwise
        "shares": 100.0,
        "stake": 50.0,
    }


def test_empty_report_has_no_metrics_and_no_verdict():
    report = shadow_eval.model_eval_report([], sport="tennis")
    assert report["n_evaluated_bets"] == 0
    assert report["n_settled"] == 0
    assert report["model"]["brier"] is None
    assert report["beats_market"] is None
    assert report["statistical_claim_supported"] is False


def test_sport_filter_scores_only_requested_sport():
    bets = [
        _bet(0.9, 0.5, 1.0, sport="tennis", event_id="t1"),
        _bet(0.1, 0.5, 0.0, sport="tennis", event_id="t2"),
        _bet(0.9, 0.5, 0.0, sport="basketball", event_id="b1"),
    ]
    report = shadow_eval.model_eval_report(bets, sport="tennis")
    assert report["n_evaluated_bets"] == 2
    assert report["n_settled"] == 2


def test_open_and_void_bets_excluded_from_calibration():
    bets = [
        _bet(0.8, 0.6, 1.0),        # graded win
        _bet(0.3, 0.6, 0.0),        # graded loss
        _bet(0.7, 0.6, None),       # open / cashed-out / push / void
    ]
    report = shadow_eval.model_eval_report(bets, sport="tennis")
    # All three map to rows; only the two graded ones score.
    assert report["n_evaluated_bets"] == 3
    assert report["n_settled"] == 2


def test_invalid_probabilities_are_dropped():
    bets = [
        _bet(0.0, 0.6, 1.0),        # degenerate model prob
        _bet(0.6, 1.0, 0.0),        # degenerate executable price
        _bet(0.8, 0.6, 1.0),        # valid
    ]
    report = shadow_eval.model_eval_report(bets, sport="tennis")
    assert report["n_evaluated_bets"] == 1


def test_well_calibrated_model_beats_an_uninformative_market():
    # Model is confident and correct; the price is a coin flip on every bet.
    bets = [_bet(0.9, 0.5, 1.0, event_id=f"w{i}") for i in range(10)]
    bets += [_bet(0.1, 0.5, 0.0, event_id=f"l{i}") for i in range(10)]
    report = shadow_eval.model_eval_report(bets, sport="tennis")
    assert report["model"]["brier"] < report["market_baseline"]["brier"]
    assert report["model"]["log_loss"] < report["market_baseline"]["log_loss"]
    assert report["beats_market"] is True


def test_miscalibrated_model_does_not_beat_a_sharp_market():
    # Model is confidently wrong; the price nails every outcome.
    bets = [_bet(0.1, 0.9, 1.0, event_id=f"w{i}") for i in range(10)]
    bets += [_bet(0.9, 0.1, 0.0, event_id=f"l{i}") for i in range(10)]
    report = shadow_eval.model_eval_report(bets, sport="tennis")
    assert report["model"]["brier"] > report["market_baseline"]["brier"]
    assert report["beats_market"] is False


def test_bets_for_eval_filters_by_sport(tmp_path):
    from app.accounts import AccountBook

    book = AccountBook(str(tmp_path / "acc.db"))
    with book._db.transaction() as cur:
        for sport, event in (("tennis", "t1"), ("basketball", "b1")):
            book._db.execute(
                cur,
                """INSERT INTO account_bets
                   (account, event_id, event_name, market, outcome, entry_price,
                    stake, shares, model_prob, edge, placed_ts, status, sport)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s)""",
                ("Bot", event, "A vs B", "moneyline", "home", 0.6, 50.0, 80.0,
                 0.65, 0.05, 1.0, sport),
            )
    assert len(book.bets_for_eval(sport="tennis")) == 1
    assert len(book.bets_for_eval()) == 2
    book.close()
