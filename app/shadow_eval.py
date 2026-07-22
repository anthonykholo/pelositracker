"""Shadow evaluation of model-backed paper bets (Stage 3 of the bot lifecycle).

A prediction model is only trustworthy once its probabilities are shown to be
calibrated and to beat the price you could have taken. This module answers that
question for the model-backed bots (e.g. the tennis in-play model) by mapping
their settled paper bets into the row shape the proven ``backtest`` metrics
already expect, then scoring the model against the executable-price baseline.

It deliberately makes no profitability claim. Two honest limitations apply and
are reported alongside the numbers:

* **Selection bias.** Only bets that cleared every strategy/execution gate are
  present, so this is a conditional-on-trading view, not the full opportunity
  set. Logging every model decision (not just placed bets) is the next step.
* **Outcome coverage.** Only win/loss-graded bets contribute to calibration;
  open, cashed-out, pushed, and void bets are excluded because they have no
  binary settled result.
"""
from __future__ import annotations

from typing import Iterable

from . import backtest


def _eval_rows(bets: Iterable[dict], sport: str | None) -> list[dict]:
    """Map account_bets rows to the generic shape ``backtest`` scores.

    ``model_prob`` is the model's decision probability, ``entry_price`` is the
    all-in executable price actually paid, and ``result`` is 1.0 win / 0.0 loss
    / None (open, cashed-out, push, void) as written by ``AccountBook.settle``.
    """
    rows: list[dict] = []
    wanted = sport.casefold() if sport else None
    for bet in bets:
        if wanted is not None and (bet.get("sport") or "").casefold() != wanted:
            continue
        model_prob = bet.get("model_prob")
        executable = bet.get("entry_price")
        if model_prob is None or executable is None:
            continue
        if not 0.0 < float(model_prob) < 1.0 or not 0.0 < float(executable) < 1.0:
            continue
        result = bet.get("result")
        rows.append({
            "event_id": bet.get("event_id"),
            "sport": bet.get("sport"),
            # backtest keys: the model's probability and the price-as-predictor.
            "entry_calibrated_prob": float(model_prob),
            "entry_fair_prob": float(model_prob),
            "entry_executable": float(executable),
            "settled_result": (float(result) if result is not None else None),
            "filled_shares": bet.get("shares"),
            "filled_cash": bet.get("stake"),
        })
    return rows


def model_eval_report(bets: Iterable[dict], sport: str | None = "tennis") -> dict:
    """Calibration + market-baseline report for model-backed paper bets.

    ``beats_market`` is True only when the model is *both* better calibrated
    (lower Brier) and sharper (lower log loss) than taking the price as the
    forecast. Neither number, alone or together, establishes profitability.
    """
    rows = _eval_rows(bets, sport)
    settled = backtest._settled(rows)
    model_key = "entry_calibrated_prob"
    bins = backtest.reliability_bins(rows, model_key)

    model_brier = backtest.brier_score(rows, model_key)
    model_log_loss = backtest.log_loss(rows, model_key)
    market_brier = backtest.brier_score(rows, "entry_executable")
    market_log_loss = backtest.log_loss(rows, "entry_executable")

    beats_market = None
    if None not in (model_brier, model_log_loss, market_brier, market_log_loss):
        beats_market = model_brier < market_brier and model_log_loss < market_log_loss

    return {
        "sport": sport,
        "model_name": "tennis_in_play" if sport == "tennis" else "model",
        "n_evaluated_bets": len(rows),
        "n_settled": len(settled),
        "model": {
            "brier": model_brier,
            "log_loss": model_log_loss,
            "ece": backtest.expected_calibration_error(bins),
            "calibration": backtest.calibration_intercept_slope(rows, model_key),
            "brier_decomposition": backtest.brier_decomposition(rows, model_key),
        },
        "market_baseline": {
            # The executable price treated as a forecast — the bar the model
            # must clear to be worth anything beyond copying the market.
            "brier": market_brier,
            "log_loss": market_log_loss,
        },
        "beats_market": beats_market,
        "reliability": bins,
        "statistical_claim_supported": False,
        "caveats": [
            "conditional on trading: only bets that cleared every gate are scored",
            "only win/loss-graded bets contribute; open/cashed-out/push/void excluded",
            "the model is uncalibrated; a well-anchored model can still lose after costs",
            "no closing-line comparison yet; CLV requires per-bet close marks",
        ],
    }
