"""Explainable, paper-only entry and position monitoring helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Quote, Signal


def _latest_polymarket_quotes(quotes: list[Quote]) -> dict[str, Quote]:
    latest: dict[str, Quote] = {}
    for quote in quotes:
        if quote.source.casefold() != "polymarket" or not quote.token_id:
            continue
        previous = latest.get(quote.token_id)
        if previous is None or quote.observed_at >= previous.observed_at:
            latest[quote.token_id] = quote
    return latest


def _signal_map(signals: list[Signal]) -> dict[tuple[str, str], Signal]:
    selected: dict[tuple[str, str], Signal] = {}
    for signal in signals:
        key = (signal.market, signal.outcome)
        current = selected.get(key)
        is_poly = signal.quote_source.casefold() == "polymarket"
        current_is_poly = current is not None and current.quote_source.casefold() == "polymarket"
        if current is None or (is_poly and not current_is_poly) or signal.confidence > current.confidence:
            selected[key] = signal
    return selected


def _risk_flags(quote: Quote, signal: Signal | None,
                provider_age_seconds: float | None) -> list[str]:
    flags = []
    spread = quote.ask - quote.bid if quote.ask is not None and quote.bid is not None else None
    if spread is None:
        flags.append("One side of the order book is missing; execution may be difficult.")
    elif spread > 0.04:
        flags.append(f"Wide {spread * 100:.1f}¢ bid/ask spread can erase the estimated edge.")
    if quote.ask_size is not None and quote.min_order_size and quote.ask_size < quote.min_order_size:
        flags.append("Best-ask depth is below the displayed minimum order size.")
    if quote.market_liquidity is not None and quote.market_liquidity < 500:
        flags.append("Thin total market liquidity increases slippage and exit risk.")
    if quote.liquidity is not None and quote.liquidity < 20:
        flags.append("Less than 20 shares are visible at the best bid and ask levels.")
    if quote.ask is not None and (quote.ask <= 0.05 or quote.ask >= 0.95):
        flags.append("Extreme prices leave little room for error and can move abruptly.")
    if provider_age_seconds is None:
        flags.append("Provider timestamp is unknown; freshness cannot pass.")
    elif provider_age_seconds > 90:
        flags.append(f"Provider quote is {provider_age_seconds:.0f}s old; wait for an update.")
    if signal is None or signal.n_reference_sources < 1:
        flags.append("No independent reference price is available; edge cannot be validated.")
    elif signal.confidence < 60:
        flags.append("Reference sources disagree or data quality is weak.")
    return flags


def _entry_blocker(signal: Signal | None, entry_action: str, edge: float | None,
                   required_edge: float | None, calibrated: float | None) -> str | None:
    """One-line, human reason this selection is not an entry (None when it is).

    Turns "why isn't the bot betting this?" from a mystery into a visible,
    ranked reason: no reference, edge below the floor, or display-only because no
    validated calibration artifact is installed."""
    if entry_action == "ENTRY WINDOW":
        return None
    if signal is None:
        return "No independent sportsbook reference matched yet — edge not estimable."
    if edge is not None and required_edge is not None and edge < required_edge:
        return (f"Edge {edge * 100:+.1f}% is below the required "
                f"{required_edge * 100:.1f}%.")
    if calibrated is None:
        return ("Display-only: no validated calibration artifact, so no actionable net "
                "edge (uncalibrated bots may still trade the gross edge).")
    return "Engine gates not cleared — expand details."


def market_views(quotes: list[Quote], signals: list[Signal], edge_threshold: float) -> list[dict]:
    """Return only selections with an executable Polymarket ask."""
    now = datetime.now(timezone.utc)
    signal_by_selection = _signal_map(signals)
    views = []
    for quote in _latest_polymarket_quotes(quotes).values():
        if not quote.accepting_orders or quote.ask is None:
            continue
        signal = signal_by_selection.get((quote.market, quote.outcome))
        if signal is not None and signal.n_reference_sources < 1:
            signal = None
        provider_age_seconds = (max(0.0, (now - quote.provider_timestamp).total_seconds())
                                if quote.provider_timestamp else None)
        receipt_age_seconds = max(0.0, (now - quote.received_at).total_seconds())
        spread = quote.ask - quote.bid if quote.bid is not None else None
        consensus_probability = signal.consensus_probability if signal else None
        calibrated_probability = signal.calibrated_consensus_probability if signal else None
        decision_probability = calibrated_probability
        entry_margin = signal.net_expected_value_per_share if signal else None
        # Display-only gross edge (consensus fair minus executable ask) shown when
        # the calibrated, execution-cost-adjusted net edge is unavailable, so the
        # card shows the bot's uncalibrated decision basis instead of just "—".
        gross_edge = (consensus_probability - quote.ask
                      if signal is not None and consensus_probability is not None
                      and quote.ask is not None else None)
        edge = entry_margin if entry_margin is not None else gross_edge
        edge_basis = ("net" if entry_margin is not None
                      else "gross" if gross_edge is not None else None)
        required_edge = max(edge_threshold, signal.required_edge) if signal else None
        expected_execution_cost_offset = (
            decision_probability - signal.market_probability - entry_margin
            if (decision_probability is not None and signal is not None
                and entry_margin is not None) else None
        )
        execution_premium = (
            signal.market_probability - quote.ask + expected_execution_cost_offset
            if signal is not None and expected_execution_cost_offset is not None else None
        )
        price_ceiling = (
            max(0.0, decision_probability - required_edge - execution_premium)
            if (decision_probability is not None and required_edge is not None
                and execution_premium is not None) else None
        )
        room_to_ceiling = price_ceiling - quote.ask if price_ceiling is not None else None
        net_edge_buffer = (entry_margin - required_edge
                           if entry_margin is not None and required_edge is not None else None)
        edge_buffer = edge - required_edge if edge is not None and required_edge is not None else None
        if (signal and signal.action == "PAPER_BET"
                and net_edge_buffer is not None and net_edge_buffer >= 0):
            entry_action = "ENTRY WINDOW"
        elif signal:
            entry_action = "WAIT"
        else:
            entry_action = "MARKET ONLY"
        why_no_entry = _entry_blocker(signal, entry_action, edge, required_edge,
                                      calibrated_probability)
        risks = _risk_flags(quote, signal, provider_age_seconds)
        uncertainty_low = signal.uncertainty_low if signal else None
        uncertainty_high = signal.uncertainty_high if signal else None
        views.append({
            "token_id": quote.token_id,
            "market": quote.market,
            "market_slug": quote.market_slug,
            "question": quote.question or quote.market,
            "outcome": quote.outcome,
            "buy_price": quote.ask,
            "sell_price": quote.bid,
            "spread": spread,
            "ask_size": quote.ask_size,
            "bid_size": quote.bid_size,
            "liquidity": quote.liquidity,
            "market_liquidity": quote.market_liquidity,
            "min_order_size": quote.min_order_size,
            "tick_size": quote.tick_size,
            "provider_age_seconds": provider_age_seconds,
            "receipt_age_seconds": receipt_age_seconds,
            "entry_action": entry_action,
            "model_probability": consensus_probability,
            "consensus_probability": consensus_probability,
            "calibrated_consensus_probability": calibrated_probability,
            "model_live_prob": signal.model_live_prob if signal else None,
            "independent_model_probability": (
                signal.independent_model_probability if signal else None),
            "independent_model_version": (
                signal.independent_model_version if signal else None),
            "independent_model_hash": signal.independent_model_hash if signal else None,
            "independent_calibration_version": (
                signal.independent_calibration_version if signal else None),
            "independent_calibration_hash": (
                signal.independent_calibration_hash if signal else None),
            "independent_model_sample_size": (
                signal.independent_model_sample_size if signal else 0),
            "independent_model_event_count": (
                signal.independent_model_event_count if signal else 0),
            "uncertainty_low": uncertainty_low,
            "uncertainty_high": uncertainty_high,
            "probability_net_ev_positive": (
                signal.probability_net_ev_positive if signal else None),
            "net_expected_value_per_share": (
                signal.net_expected_value_per_share if signal else None),
            "net_expected_value_total": signal.net_expected_value_total if signal else None,
            "net_ev_per_stake": signal.ev_per_stake if signal else None,
            "requested_cash": signal.requested_cash if signal else None,
            "requested_size_vwap": signal.execution_vwap if signal else None,
            "requested_effective_cost": signal.market_probability if signal else None,
            "execution_fee": signal.execution_fee if signal else None,
            "expected_execution_cost_offset": expected_execution_cost_offset,
            "paper_fillable_size": signal.fillable_size if signal else None,
            "entry_margin": entry_margin,
            "edge": edge,
            "gross_edge": gross_edge,
            "edge_basis": edge_basis,
            "why_no_entry": why_no_entry,
            "required_edge": required_edge,
            "edge_buffer": edge_buffer,
            "price_ceiling": price_ceiling,
            "room_to_ceiling": room_to_ceiling,
            "confidence": signal.confidence if signal else None,
            "reference_sources": signal.n_reference_sources if signal else 0,
            "quality_components": ({
                "data_completeness": signal.quality_data_completeness,
                "provider_freshness": signal.quality_provider_freshness,
                "identity_confidence": signal.quality_identity,
                "execution_quality": signal.quality_execution,
                "model_sample_support": signal.quality_model_sample_support,
                "calibration_support": signal.quality_calibration_support,
                "source_independence": signal.quality_source_independence,
            } if signal else None),
            "gate_results": signal.gate_results if signal else [],
            "consensus_method": signal.consensus_method if signal else None,
            "model_sample_size": signal.model_sample_size if signal else 0,
            "calibration_sample_size": signal.calibration_sample_size if signal else 0,
            "engine_version": signal.engine_version if signal else None,
            "configuration_hash": signal.configuration_hash if signal else None,
            "model_version": signal.model_version if signal else None,
            "calibration_version": signal.calibration_version if signal else None,
            "independent_model_registry_version": (
                signal.independent_model_registry_version if signal else None),
            "execution_policy_version": signal.execution_policy_version if signal else None,
            "reasons": signal.reasons if signal else ["Waiting for an independent sportsbook reference."],
            "risk_flags": risks,
        })
    priority = {"ENTRY WINDOW": 0, "WAIT": 1, "MARKET ONLY": 2}
    return sorted(views, key=lambda view: (priority[view["entry_action"]],
                                           -(view["entry_margin"] or -1), view["question"], view["outcome"]))


def position_views(positions: list[dict], quotes: list[Quote], signals: list[Signal],
                   confidence_threshold: float) -> list[dict]:
    latest = _latest_polymarket_quotes(quotes)
    signal_by_selection = _signal_map(signals)
    views = []
    for position in positions:
        quote = latest.get(position["token_id"])
        signal = signal_by_selection.get((position["market"], position["outcome"]))
        if signal is not None and signal.n_reference_sources < 1:
            signal = None
        bid = quote.bid if quote else None
        spread = (quote.ask - quote.bid if quote and quote.ask is not None and quote.bid is not None
                  else None)
        shares = float(position["shares"])
        entry = float(position["avg_entry_price"])
        cash_value = shares * bid if bid is not None else None
        pnl = shares * (bid - entry) if bid is not None else None
        roi = (bid - entry) / entry if bid is not None and entry > 0 else None
        fair = signal.calibrated_consensus_probability if signal else None
        remaining_edge = fair - bid if fair is not None and bid is not None else None
        uncertainty_low = signal.uncertainty_low if signal else None
        uncertainty_high = signal.uncertainty_high if signal else None
        conservative_hold_edge = (
            uncertainty_low - bid
            if uncertainty_low is not None and bid is not None else None
        )
        reasons = []
        if bid is None:
            action = "EXIT WATCH"
            reasons.append("No executable bid is visible, so an immediate cash-out cannot be estimated.")
        elif spread is None or spread > 0.05:
            action = "EXIT WATCH"
            reasons.append("The exit spread is wide; use a limit price and watch fill risk.")
        elif signal and uncertainty_high is not None and uncertainty_high < bid:
            action = "CONSIDER CASH"
            reasons.append(
                "The executable bid is above the 95% historical bootstrap interval."
            )
        elif roi is not None and roi >= 0.20 and (remaining_edge is None or remaining_edge < 0.02):
            action = "CONSIDER CASH"
            reasons.append("The position is up at least 20% and little validated edge remains.")
        elif roi is not None and roi <= -0.15 and (remaining_edge is None or remaining_edge <= 0):
            action = "CONSIDER CASH"
            reasons.append("The position is down at least 15% without positive validated hold edge.")
        elif (signal and conservative_hold_edge is not None
              and conservative_hold_edge >= 0.02
              and signal.confidence >= confidence_threshold):
            action = "HOLD"
            reasons.append(
                "Even the lower historical bootstrap bound remains at least 2¢ "
                "above the executable exit price."
            )
        else:
            action = "HOLD / MONITOR"
            reasons.append("No strong exit trigger is present, but the remaining edge is not decisive.")
        if signal:
            reasons.extend(signal.reasons[:2])
        else:
            reasons.append("No calibrated consensus is available; treat this as price/P&L monitoring only.")
        views.append({**position, "current_bid": bid, "current_ask": quote.ask if quote else None,
                      "spread": spread, "cash_value": cash_value, "unrealized_pnl": pnl,
                      "roi": roi, "model_probability": fair,
                      "calibrated_consensus_probability": fair,
                      "uncertainty_low": uncertainty_low,
                      "uncertainty_high": uncertainty_high,
                      "remaining_hold_edge": remaining_edge,
                      "conservative_hold_edge": conservative_hold_edge,
                      "advice": action, "reasons": reasons})
    return views
