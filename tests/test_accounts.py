import pytest

from app.accounts import AccountBook, Strategy, qualification_failures, qualifies, stake_for
from app.models import Event, Quote, Signal


def valid_signal(**overrides):
    values = dict(
        event_id="e", market="moneyline", outcome="Home",
        model_probability=.60, market_probability=.50, edge=.10,
        confidence=90, action="PAPER_BET", reasons=[],
        quote_source="Polymarket", n_reference_sources=2,
        required_edge=.04, kelly_fraction=.05,
        consensus_probability=.60, calibrated_consensus_probability=.60,
        probability_net_ev_positive=.99, net_expected_value_total=5.0,
        token_id="token-home",
    )
    values.update(overrides)
    return Signal(**values)


def executable_quote(event: Event, token_id: str, market: str, outcome: str,
                     *, ask: float = .50, bid: float = .48) -> Quote:
    return Quote(
        event.id, market, outcome, ask, "Polymarket", token_id=token_id,
        ask=ask, bid=bid, ask_levels=((ask, 10_000.0),),
        bid_levels=((bid, 10_000.0),), depth_complete=True, fee_rate=0.0,
    )


@pytest.mark.parametrize("changes, expected", [
    ({"action": "WATCH"}, "engine gates"),
    ({"quote_source": "Pinnacle"}, "Polymarket"),
    ({"market_probability": 0}, "invalid executable"),
    ({"n_reference_sources": 1}, "too few"),
    ({"edge": .03, "required_edge": .04}, "risk-adjusted"),
])
def test_bot_rejects_every_hard_engine_or_execution_failure(changes, expected):
    strategy = Strategy("test", edge_threshold=0)
    signal = valid_signal(**changes)
    assert not qualifies(strategy, signal)
    assert any(expected in reason for reason in qualification_failures(strategy, signal))


def _uncalibrated_gates(reference_ok: bool = True):
    """Gate results the Rust engine emits with no calibration artifact: real
    gates evaluated, calibration/policy gates left ``unknown``."""
    return [
        {"code": "provider_freshness", "passed": True, "status": "pass"},
        {"code": "reference_source_support", "passed": reference_ok,
         "status": "pass" if reference_ok else "fail"},
        {"code": "market_identity", "passed": True, "status": "pass"},
        {"code": "market_status", "passed": True, "status": "pass"},
        {"code": "executable_fill", "passed": True, "status": "pass"},
        {"code": "net_edge", "passed": True, "status": "pass"},
        {"code": "signal_quality", "passed": True, "status": "pass"},
        {"code": "calibration_support", "passed": None, "status": "unknown"},
        {"code": "uncertainty_support", "passed": None, "status": "unknown"},
    ]


def test_uncalibrated_watch_signal_trades_only_with_opt_in():
    strategy = Strategy("test", edge_threshold=0)
    signal = valid_signal(action="WATCH", calibrated_consensus_probability=None,
                          gate_results=_uncalibrated_gates())
    # Default: the engine's honest WATCH verdict stands; no paper bet.
    assert not qualifies(strategy, signal)
    assert any("engine gates" in reason
               for reason in qualification_failures(strategy, signal))
    # Opt-in: a fundamentally sound, uncalibrated gross-gap edge is tradeable.
    assert qualifies(strategy, signal, allow_uncalibrated=True)


def test_uncalibrated_opt_in_still_blocks_a_real_gate_failure():
    strategy = Strategy("test", edge_threshold=0)
    signal = valid_signal(action="WATCH", calibrated_consensus_probability=None,
                          gate_results=_uncalibrated_gates(reference_ok=False))
    assert not qualifies(strategy, signal, allow_uncalibrated=True)


def test_uncalibrated_opt_in_never_admits_a_signal_with_no_gate_record():
    strategy = Strategy("test", edge_threshold=0)
    signal = valid_signal(action="WATCH", calibrated_consensus_probability=None,
                          gate_results=[])
    assert not qualifies(strategy, signal, allow_uncalibrated=True)


def _single_source_watch_signal(event, **overrides):
    """A tennis-style signal the engine could not price (no reference books)."""
    values = dict(
        event_id=event.id, market="moneyline", outcome=event.home,
        action="WATCH", n_reference_sources=0, edge=0.0, required_edge=0.0,
        market_probability=0.50, model_probability=0.0,
        calibrated_consensus_probability=None, consensus_probability=0.0,
        gate_results=[{"code": "reference_source_support",
                       "passed": False, "status": "fail"}],
        token_id="token-home", fillable_size=1000,
    )
    values.update(overrides)
    return valid_signal(**values)


def test_model_backed_probability_trades_a_single_source_watch_signal(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    strategy = Strategy("tennis", sizing="flat", flat_stake=50.0, start_bankroll=1000.0,
                        edge_threshold=0.0, max_stake_pct=1.0, max_event_exposure_pct=1.0,
                        max_sport_exposure_pct=1.0, max_correlated_exposure_pct=1.0,
                        max_total_exposure_pct=1.0)
    book.seed([strategy])
    event = Event("Alcaraz vs Sinner", "tennis", "Alcaraz", "Sinner", id="tennis-1")
    signal = _single_source_watch_signal(event)
    quote = executable_quote(event, "token-home", "moneyline", "Alcaraz", ask=.50, bid=.48)
    try:
        # No model probability: the engine's WATCH stands, nothing is placed.
        assert book.place(event, [signal], [quote]) == []
        # An independent model says 60% vs a 50% ask -> a real 10% edge, traded.
        placed = book.place(event, [signal], [quote],
                            model_probabilities={"token-home": 0.60})
        rows = book.account_bets("tennis")
    finally:
        book.close()
    assert len(placed) == 1
    assert placed[0]["edge"] == pytest.approx(0.10, abs=1e-6)
    assert rows[0]["model_prob"] == pytest.approx(0.60)
    assert rows[0]["edge"] == pytest.approx(0.10, abs=1e-6)


def test_model_backed_bet_still_respects_the_edge_floor(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    strategy = Strategy("tennis", sizing="flat", flat_stake=50.0, start_bankroll=1000.0,
                        edge_threshold=0.05, max_stake_pct=1.0, max_event_exposure_pct=1.0,
                        max_total_exposure_pct=1.0)
    book.seed([strategy])
    event = Event("Alcaraz vs Sinner", "tennis", "Alcaraz", "Sinner", id="tennis-2")
    signal = _single_source_watch_signal(event)
    quote = executable_quote(event, "token-home", "moneyline", "Alcaraz", ask=.50, bid=.48)
    try:
        # Model edge of only 2% is below the 5% strategy floor -> no bet.
        placed = book.place(event, [signal], [quote],
                            model_probabilities={"token-home": 0.52})
    finally:
        book.close()
    assert placed == []


def test_validated_polymarket_signal_qualifies_and_depth_caps_stake():
    strategy = Strategy("flat", edge_threshold=.05, sizing="flat", flat_stake=100)
    signal = valid_signal(fillable_size=40)  # 40 shares * $0.50 = $20 available
    assert qualifies(strategy, signal)
    assert stake_for(strategy, signal, 1000) == pytest.approx(20)


def test_unknown_depth_does_not_become_a_zero_dollar_stake():
    strategy = Strategy("flat", edge_threshold=.05, sizing="flat", flat_stake=100)
    signal = valid_signal(fillable_size=None)
    assert qualifies(strategy, signal)
    assert stake_for(strategy, signal, 1000) == pytest.approx(100)


def test_sport_and_correlated_group_caps_are_durable_and_enforced(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    strategy = Strategy(
        "risk-test", sizing="flat", flat_stake=100.0, start_bankroll=1000.0,
        edge_threshold=0.0, max_stake_pct=1.0, max_event_exposure_pct=1.0,
        max_sport_exposure_pct=.08, max_correlated_exposure_pct=.05,
        max_total_exposure_pct=1.0,
    )
    book.seed([strategy])
    first = Event("A vs B", "basketball", "A", "B", id="event-1")
    second = Event("C vs D", "basketball", "C", "D", id="event-2")
    moneyline = valid_signal(
        event_id=first.id, outcome="A", decision_id="decision-1",
        fillable_size=1000,
    )
    spread = valid_signal(
        event_id=first.id, market="spread", outcome="A -1.5",
        decision_id="decision-2", fillable_size=1000, token_id="token-spread",
    )
    other_event = valid_signal(
        event_id=second.id, outcome="C", decision_id="decision-3",
        fillable_size=1000, token_id="token-other",
    )
    try:
        placed_first = book.place(first, [moneyline, spread], [
            executable_quote(first, "token-home", "moneyline", "A"),
            executable_quote(first, "token-spread", "spread", "A -1.5"),
        ])
        placed_second = book.place(second, [other_event], [
            executable_quote(second, "token-other", "moneyline", "C"),
        ])
        rows = book.account_bets("risk-test")
    finally:
        book.close()

    assert sum(item["stake"] for item in placed_first) == pytest.approx(50.0)
    assert sum(item["stake"] for item in placed_second) == pytest.approx(30.0)
    assert sum(row["stake"] for row in rows) == pytest.approx(80.0)
    assert {row["sport"] for row in rows} == {"basketball"}
    assert all(row["correlation_group"] and row["decision_id"] for row in rows)


def test_ungradeable_prop_is_rejected_before_it_can_be_voided(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    event = Event("A vs B", "basketball", "A", "B", id="event")
    strategy = Strategy("bot", sizing="flat", flat_stake=100, start_bankroll=1_000)
    prop = valid_signal(
        event_id=event.id, market="player points", outcome="Player over 20.5",
        token_id="token-prop",
    )
    quote = executable_quote(
        event, "token-prop", "player points", "Player over 20.5"
    )
    try:
        book.seed([strategy])
        assert book.place(event, [prop], [quote], as_of=1_000) == []
        assert book.account_bets("bot") == []
    finally:
        book.close()


@pytest.mark.parametrize("signal_changes, quote_changes", [
    ({}, {"ask": .58}),
    ({"order_book_snapshot_id": "decision-book"}, {"book_hash": "new-book"}),
])
def test_entry_rechecks_actual_depth_price_and_book_identity(
        tmp_path, signal_changes, quote_changes):
    book = AccountBook(str(tmp_path / "accounts.db"))
    event = Event("A vs B", "basketball", "A", "B", id="event")
    strategy = Strategy("bot", sizing="flat", flat_stake=100, start_bankroll=1_000)
    signal = valid_signal(event_id=event.id, outcome="A", **signal_changes)
    ask = quote_changes.get("ask", .50)
    quote = executable_quote(event, "token-home", "moneyline", "A", ask=ask)
    for key, value in quote_changes.items():
        if key != "ask":
            setattr(quote, key, value)
    try:
        book.seed([strategy])
        assert book.place(event, [signal], [quote], as_of=1_000) == []
        assert book.account_bets("bot") == []
    finally:
        book.close()


def test_marks_include_spread_and_both_fees_in_liquidation_value(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    event = Event("A vs B", "basketball", "A", "B", id="event")
    strategy = Strategy("bot", sizing="flat", flat_stake=100, start_bankroll=1_000)
    signal = valid_signal(event_id=event.id, outcome="A")
    entry = executable_quote(event, "token-home", "moneyline", "A", ask=.50, bid=.49)
    entry.fee_rate = .03
    mark = executable_quote(event, "token-home", "moneyline", "A", ask=.51, bid=.49)
    mark.fee_rate = .03
    try:
        book.seed([strategy])
        book.place(event, [signal], [entry], as_of=1_000)
        book.mark_and_cash_out(event, [mark], [signal], as_of=1_121)
        bet = book.account_bets("bot")[0]
        marks = book.bet_marks("bot", bet["id"])
    finally:
        book.close()

    assert bet["entry_fee"] > 0
    assert bet["last_mark_value"] < bet["stake"]
    assert bet["last_mark_pnl"] < 0
    assert marks[0]["exit_fee"] > 0
    assert marks[0]["decision_action"] == "MARK_ONLY"


def test_cashout_ignores_a_penny_move_then_takes_meaningful_model_reversal_profit(
        tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    event = Event("A vs B", "basketball", "A", "B", id="event")
    strategy = Strategy(
        "bot", sizing="flat", flat_stake=100, start_bankroll=1_000,
        cash_out_enabled=True,
    )
    entry_signal = valid_signal(event_id=event.id, outcome="A")
    reversed_signal = valid_signal(
        event_id=event.id, outcome="A", consensus_probability=.50,
        calibrated_consensus_probability=.50, model_probability=.50,
        independent_model_probability=.90,
    )
    entry = executable_quote(event, "token-home", "moneyline", "A", ask=.50, bid=.48)
    penny = executable_quote(event, "token-home", "moneyline", "A", ask=.52, bid=.51)
    meaningful = executable_quote(event, "token-home", "moneyline", "A", ask=.57, bid=.56)
    try:
        book.seed([strategy])
        book.place(event, [entry_signal], [entry], as_of=1_000)
        assert book.mark_and_cash_out(
            event, [penny], [reversed_signal], as_of=1_121
        ) == []
        assert book.account_bets("bot")[0]["status"] == "open"

        exits = book.mark_and_cash_out(
            event, [meaningful], [reversed_signal], as_of=1_166
        )
        bet = book.account_bets("bot")[0]
        bankroll = book.leaderboard()[0]["bankroll"]
        assert book.mark_and_cash_out(
            event, [meaningful], [reversed_signal], as_of=1_200
        ) == []
    finally:
        book.close()

    assert len(exits) == 1
    assert bet["status"] == "cashed_out"
    assert bet["pnl"] == pytest.approx(12.0)
    assert bankroll == pytest.approx(1_012.0)
    assert "meaningful net profit" in bet["exit_reason"]


def test_cashout_toggle_off_marks_but_never_closes(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    event = Event("A vs B", "basketball", "A", "B", id="event")
    strategy = Strategy("bot", sizing="flat", flat_stake=100, start_bankroll=1_000)
    signal = valid_signal(event_id=event.id, outcome="A")
    entry = executable_quote(event, "token-home", "moneyline", "A", ask=.50, bid=.48)
    surge = executable_quote(event, "token-home", "moneyline", "A", ask=.76, bid=.75)
    try:
        book.seed([strategy])
        book.place(event, [signal], [entry], as_of=1_000)
        exits = book.mark_and_cash_out(event, [surge], [signal], as_of=1_500)
        bet = book.account_bets("bot")[0]
        marks = book.bet_marks("bot", bet["id"])
        assert book.set_cash_out("bot", True)
        assert book.leaderboard()[0]["cash_out_enabled"] is True
    finally:
        book.close()

    assert exits == []
    assert bet["status"] == "open"
    assert bet["last_mark_pnl"] == pytest.approx(50.0)
    assert marks[0]["decision_action"] == "MARK_ONLY"


def test_missing_sell_depth_is_recorded_as_unpriced_and_cannot_exit(tmp_path):
    book = AccountBook(str(tmp_path / "accounts.db"))
    event = Event("A vs B", "basketball", "A", "B", id="event")
    strategy = Strategy(
        "bot", sizing="flat", flat_stake=100, start_bankroll=1_000,
        cash_out_enabled=True,
    )
    signal = valid_signal(event_id=event.id, outcome="A")
    entry = executable_quote(event, "token-home", "moneyline", "A")
    unavailable = executable_quote(event, "token-home", "moneyline", "A", bid=.70)
    unavailable.bid_levels = ()
    try:
        book.seed([strategy])
        book.place(event, [signal], [entry], as_of=1_000)
        assert book.mark_and_cash_out(
            event, [unavailable], [signal], as_of=1_500
        ) == []
        bet = book.account_bets("bot")[0]
        marks = book.bet_marks("bot", bet["id"])
        board = book.leaderboard()[0]
    finally:
        book.close()

    assert bet["last_mark_value"] is None
    assert marks[0]["decision_action"] == "UNPRICED"
    assert "bid depth" in marks[0]["execution_reason"]
    assert board["equity"] is None
    assert board["unpriced_open_positions"] == 1
