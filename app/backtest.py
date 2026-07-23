"""Offline evaluation metrics over paper observations and fills.

No single metric establishes an edge. Reports keep calibrated consensus,
executable-price baselines, close marks, fill economics, calibration, and
event-block uncertainty separate so the UI cannot turn one favorable number
into a profitability claim.
"""
from __future__ import annotations

import json
import math
import random

_EPS = 1e-6


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def clv_summary(bets: list[dict]) -> dict:
    """CLV = closing_fair_prob - entry_executable, over bets with a close."""
    clvs = [b["clv"] for b in bets if b.get("clv") is not None]
    if not clvs:
        return {"n": 0, "mean_clv": None, "median_clv": None, "beat_close_rate": None}
    ordered = sorted(clvs)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": len(clvs),
        "mean_clv": _mean(clvs),
        "median_clv": median,
        "beat_close_rate": sum(1 for c in clvs if c > 0) / len(clvs),
    }


def _settled(bets: list[dict]) -> list[dict]:
    return [b for b in bets if b.get("settled_result") is not None]


def _scored(bets: list[dict], prob_key: str) -> list[dict]:
    """Settled rows that actually carry ``prob_key``.

    A missing calibrated probability stays missing: it is never backfilled with
    the uncalibrated fair value, so a metric reported as "calibrated" is only ever
    computed over rows that truly had a calibrated probability.
    """
    return [b for b in bets
            if b.get("settled_result") is not None and b.get(prob_key) is not None]


def _probability(row: dict, key: str) -> float:
    value = row.get(key)
    if value is None:
        raise ValueError(f"missing probability field: {key}")
    return float(value)


def brier_score(bets: list[dict], prob_key: str = "entry_fair_prob") -> float | None:
    rows = _scored(bets, prob_key)
    if not rows:
        return None
    return _mean([(_probability(b, prob_key) - b["settled_result"]) ** 2 for b in rows])


def log_loss(bets: list[dict], prob_key: str = "entry_fair_prob") -> float | None:
    rows = _scored(bets, prob_key)
    if not rows:
        return None
    total = 0.0
    for b in rows:
        p = min(max(_probability(b, prob_key), _EPS), 1 - _EPS)
        y = b["settled_result"]
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(rows)


def reliability_bins(bets: list[dict], prob_key: str = "entry_fair_prob", n_bins: int = 10) -> list[dict]:
    """Group settled bets by predicted probability for a reliability diagram."""
    rows = _scored(bets, prob_key)
    bins: list[dict] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        # include the right edge in the final bin
        members = [
            b for b in rows
            if lo <= _probability(b, prob_key) < hi
            or (i == n_bins - 1 and _probability(b, prob_key) == hi)
        ]
        if not members:
            continue
        bins.append({
            "lo": lo,
            "hi": hi,
            "count": len(members),
            "mean_predicted": _mean([_probability(b, prob_key) for b in members]),
            "empirical_rate": _mean([b["settled_result"] for b in members]),
        })
    return bins


def expected_calibration_error(bins: list[dict]) -> float | None:
    """Count-weighted mean gap between predicted probability and outcome rate."""
    total = sum(b["count"] for b in bins)
    if not total:
        return None
    return sum(b["count"] * abs(b["mean_predicted"] - b["empirical_rate"]) for b in bins) / total


def brier_decomposition(bets: list[dict], prob_key: str = "entry_calibrated_prob",
                        n_bins: int = 10) -> dict[str, float] | None:
    """Murphy reliability/resolution/uncertainty decomposition by bins."""
    rows = _scored(bets, prob_key)
    if not rows:
        return None
    bins = reliability_bins(rows, prob_key, n_bins)
    count = len(rows)
    base_rate = sum(float(row["settled_result"]) for row in rows) / count
    reliability = sum(
        bucket["count"] / count
        * (bucket["mean_predicted"] - bucket["empirical_rate"]) ** 2
        for bucket in bins
    )
    resolution = sum(
        bucket["count"] / count * (bucket["empirical_rate"] - base_rate) ** 2
        for bucket in bins
    )
    uncertainty = base_rate * (1.0 - base_rate)
    return {
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "reconstructed_brier": reliability - resolution + uncertainty,
    }


def calibration_intercept_slope(
    bets: list[dict], prob_key: str = "entry_calibrated_prob"
) -> dict[str, float] | None:
    """Fit outcome ~ intercept + slope*logit(p) without external ML dependencies."""
    rows = _scored(bets, prob_key)
    if len(rows) < 3:
        return None
    outcomes = [float(row["settled_result"]) for row in rows]
    if min(outcomes) == max(outcomes):
        return None
    logits = []
    for row in rows:
        probability = min(max(_probability(row, prob_key), _EPS), 1 - _EPS)
        logits.append(math.log(probability / (1.0 - probability)))
    intercept, slope = 0.0, 1.0
    for _ in range(50):
        gradient_intercept = gradient_slope = 0.0
        h00 = h01 = h11 = 0.0
        for logit_value, outcome in zip(logits, outcomes, strict=True):
            linear = max(-40.0, min(40.0, intercept + slope * logit_value))
            prediction = 1.0 / (1.0 + math.exp(-linear))
            residual = prediction - outcome
            weight = max(prediction * (1.0 - prediction), 1e-9)
            gradient_intercept += residual
            gradient_slope += residual * logit_value
            h00 += weight
            h01 += weight * logit_value
            h11 += weight * logit_value * logit_value
        determinant = h00 * h11 - h01 * h01
        if abs(determinant) < 1e-12:
            return None
        step_intercept = (h11 * gradient_intercept - h01 * gradient_slope) / determinant
        step_slope = (-h01 * gradient_intercept + h00 * gradient_slope) / determinant
        intercept -= step_intercept
        slope -= step_slope
        if max(abs(step_intercept), abs(step_slope)) < 1e-8:
            break
    return {"intercept": intercept, "slope": slope}


def event_block_interval(
    rows: list[dict], value_key: str, *, draws: int = 1000, seed: int = 0
) -> dict[str, float | int] | None:
    """Percentile interval for a mean, resampling whole events together."""
    groups: dict[str, list[float]] = {}
    for index, row in enumerate(rows):
        value = row.get(value_key)
        if value is None:
            continue
        event_id = str(row.get("event_id") or f"ungrouped-{index}")
        groups.setdefault(event_id, []).append(float(value))
    event_ids = sorted(groups)
    if not event_ids:
        return None
    generator = random.Random(seed)
    estimates = []
    for _ in range(draws):
        sample = [generator.choice(event_ids) for _ in event_ids]
        values = [value for event_id in sample for value in groups[event_id]]
        estimates.append(sum(values) / len(values))
    estimates.sort()

    def percentile(probability: float) -> float:
        position = probability * (len(estimates) - 1)
        lower, upper = math.floor(position), math.ceil(position)
        weight = position - lower
        return estimates[lower] * (1.0 - weight) + estimates[upper] * weight

    return {
        "events": len(event_ids),
        "draws": draws,
        "mean": sum(estimates) / len(estimates),
        "lower": percentile(.025),
        "upper": percentile(.975),
    }


def _paper_profit(row: dict) -> float | None:
    if row.get("settled_result") is None or row.get("filled_shares") is None:
        return None
    # PaperExecutionSimulator.filled_cash is the total cash paid, including
    # execution fees. Keep the fee column as separate lineage/reporting data,
    # but do not subtract it twice from settled profit. A missing cash figure
    # makes the P&L undefined: treating it as $0 would book the position as free
    # and inflate profit, so the row is invalidated instead.
    cash = row.get("filled_cash")
    if cash is None:
        return None
    shares = float(row["filled_shares"])
    return shares * float(row["settled_result"]) - float(cash)


def _completely_filled(row: dict) -> bool:
    """A submitted order whose filled cash reached the cash it requested.

    ``requested_cash`` is the all-in cash the order asked to deploy and a full
    paper fill lands ``filled_cash`` exactly on it, so cash completeness is a
    faithful proxy for a full fill without a per-order requested-share field.
    """
    requested = float(row.get("requested_cash") or 0.0)
    cash = row.get("filled_cash")
    return cash is not None and requested > 0 and float(cash) >= requested - _EPS


def execution_summary(bets: list[dict]) -> dict[str, float | int | None]:
    submitted = [row for row in bets if row.get("requested_cash") is not None]
    any_fill = [row for row in submitted if float(row.get("filled_shares") or 0.0) > 0]
    complete = [row for row in any_fill if _completely_filled(row)]
    profits = [value for row in bets if (value := _paper_profit(row)) is not None]
    requested_total = sum(float(row.get("requested_cash") or 0.0) for row in submitted)
    turnover = sum(float(row.get("filled_cash") or 0.0) for row in any_fill)
    fees = sum(float(row.get("execution_fee") or 0.0) for row in any_fill)
    return {
        "submitted": len(submitted),
        # A partial sliver is not a fill: ``fill_rate`` counts only orders that
        # filled completely. The any-fill count and the cash-weighted ratio are
        # reported separately so a tiny partial can never masquerade as a fill.
        "filled": len(complete),
        "orders_with_partial_fill": len(any_fill) - len(complete),
        "fill_rate": len(complete) / len(submitted) if submitted else None,
        "any_fill_rate": len(any_fill) / len(submitted) if submitted else None,
        "cash_fill_ratio": turnover / requested_total if requested_total else None,
        "turnover": turnover,
        "fees": fees,
        "net_paper_return": sum(profits) if profits else None,
        "return_on_turnover": sum(profits) / turnover if profits and turnover else None,
    }


def _pnl_timestamp(row: dict) -> float | None:
    """Settlement time used to order the P&L path, or None if unknowable.

    Falls back to entry time, but never to epoch 0: a row with no usable
    timestamp must not silently sort to the front of the drawdown path.
    """
    for key in ("settled_ts", "entry_ts"):
        value = row.get(key)
        if value is not None:
            return float(value)
    return None


def portfolio_summary(bets: list[dict]) -> dict[str, float | dict | None]:
    pnl_rows = [(row, profit) for row in bets
                if (profit := _paper_profit(row)) is not None]
    timestamps = [_pnl_timestamp(row) for row, _ in pnl_rows]
    # Fail closed: if any realized-P&L row cannot be placed on the timeline the
    # drawdown path is undefined -- report None rather than a misordered number.
    if pnl_rows and all(ts is not None for ts in timestamps):
        # Net simultaneous settlements so the path is independent of the order in
        # which same-instant rows happen to be stored (deterministic re-runs).
        net_by_time: dict[float, float] = {}
        for ts, (_, profit) in zip(timestamps, pnl_rows):
            assert ts is not None  # guarded by the all(...) check above
            net_by_time[ts] = net_by_time.get(ts, 0.0) + profit
        equity = peak = max_drawdown = 0.0
        for ts in sorted(net_by_time):
            equity += net_by_time[ts]
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        drawdown: float | None = max_drawdown
    else:
        drawdown = None
    turnover_by_sport: dict[str, float] = {}
    turnover_by_event: dict[str, float] = {}
    total = 0.0
    for row in bets:
        amount = float(row.get("filled_cash") or 0.0)
        total += amount
        sport = str(row.get("sport") or "unknown")
        event = str(row.get("event_id") or "unknown")
        turnover_by_sport[sport] = turnover_by_sport.get(sport, 0.0) + amount
        turnover_by_event[event] = turnover_by_event.get(event, 0.0) + amount
    def concentration(values: dict[str, float]) -> float | None:
        return max(values.values()) / total if values and total else None
    return {
        "max_drawdown_dollars": drawdown,
        "largest_sport_turnover_share": concentration(turnover_by_sport),
        "largest_event_turnover_share": concentration(turnover_by_event),
        "turnover_by_sport": turnover_by_sport,
    }


def eligibility_coverage(decisions: list[dict] | None, selected_bets: int) -> dict:
    if decisions is None:
        return {
            "all_opportunities": None,
            "selected_signals": selected_bets,
            "rejection_gates": {},
            "note": "all-opportunity coverage requires decision-mark export",
        }
    rejection_gates: dict[str, int] = {}
    paper_decisions = 0
    for decision in decisions:
        if decision.get("policy_action") == "PAPER_BET":
            paper_decisions += 1
        raw_gates = decision.get("gate_results_json") or "[]"
        try:
            gates = json.loads(raw_gates) if isinstance(raw_gates, str) else raw_gates
        except (TypeError, json.JSONDecodeError):
            gates = []
        for gate in gates if isinstance(gates, list) else []:
            if isinstance(gate, dict) and gate.get("passed") is False:
                code = str(gate.get("code") or "unknown")
                rejection_gates[code] = rejection_gates.get(code, 0) + 1
    return {
        "all_opportunities": len(decisions),
        "paper_eligible_decisions": paper_decisions,
        "selected_signals": selected_bets,
        "selection_rate": paper_decisions / len(decisions) if decisions else None,
        "rejection_gates": dict(sorted(rejection_gates.items())),
        "note": "counts are decision-time opportunities; outcome coverage is reported separately",
    }


def summary(bets: list[dict], decisions: list[dict] | None = None) -> dict:
    """Execution-aware report with proper scores and event-block uncertainty."""
    settled = _settled(bets)
    model_key = "entry_calibrated_prob"
    bins = reliability_bins(bets, model_key)
    executable_clv_rows = []
    consensus_clv_rows = []
    independent_rows = [
        row for row in bets if row.get("entry_independent_prob") is not None
    ]
    for row in bets:
        if row.get("closing_executable") is not None:
            executable_clv_rows.append({
                **row,
                "clv": float(row["closing_executable"]) - float(row["entry_executable"]),
            })
        if row.get("closing_fair_prob") is not None:
            consensus_clv_rows.append({
                **row,
                "clv": float(row["closing_fair_prob"]) - float(row["entry_executable"]),
            })
    return {
        "n_bets": len(bets),
        "n_settled": len(settled),
        "clv": clv_summary(bets),
        "clv_variants": {
            "target_executable_close_minus_entry_vwap": clv_summary(executable_clv_rows),
            "reference_consensus_close_minus_entry_vwap": clv_summary(consensus_clv_rows),
            "target_mid_close_minus_entry_vwap": {"n": 0, "unavailable": True},
        },
        "model": {
            "name": "calibrated_consensus",
            # How many settled rows actually carried a calibrated probability, so a
            # metric computed over a subset is never mistaken for full coverage.
            "n_scored": len(_scored(bets, model_key)),
            "brier": brier_score(bets, model_key),
            "log_loss": log_loss(bets, model_key),
            "ece": expected_calibration_error(bins),
            "calibration": calibration_intercept_slope(bets, model_key),
            "brier_decomposition": brier_decomposition(bets, model_key),
        },
        "market_baseline": {
            # The price you could have taken, as a prediction — the bar to beat.
            "brier": brier_score(bets, "entry_executable"),
            "log_loss": log_loss(bets, "entry_executable"),
        },
        "independent_model": {
            "n_settled": len(_settled(independent_rows)),
            "brier": brier_score(independent_rows, "entry_independent_prob"),
            "log_loss": log_loss(independent_rows, "entry_independent_prob"),
            "same_rows_calibrated_consensus": {
                "brier": brier_score(independent_rows, model_key),
                "log_loss": log_loss(independent_rows, model_key),
            },
            "note": (
                "cross-check only; inclusion here does not make the independent "
                "model part of the paper action rule"
            ),
        },
        "reliability": bins,
        "execution": execution_summary(bets),
        "portfolio": portfolio_summary(bets),
        "bootstrap": {
            "mean_executable_clv": event_block_interval(
                executable_clv_rows, "clv", seed=17
            ),
        },
        "eligibility_coverage": eligibility_coverage(decisions, len(bets)),
        "statistical_claim_supported": False,
    }
