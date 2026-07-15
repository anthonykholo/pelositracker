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


def _risk_flags(quote: Quote, signal: Signal | None, age_seconds: float) -> list[str]:
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
    if age_seconds > 90:
        flags.append(f"Quote is {age_seconds:.0f}s old; wait for a fresh order-book update.")
    if signal is None or signal.n_reference_sources < 1:
        flags.append("No independent reference price is available; edge cannot be validated.")
    elif signal.confidence < 60:
        flags.append("Reference sources disagree or data quality is weak.")
    return flags


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
        age_seconds = max(0.0, (now - quote.observed_at).total_seconds())
        spread = quote.ask - quote.bid if quote.bid is not None else None
        model_probability = signal.model_probability if signal else None
        entry_margin = model_probability - quote.ask if model_probability is not None else None
        price_ceiling = max(0.0, model_probability - edge_threshold) if signal else None
        room_to_ceiling = price_ceiling - quote.ask if price_ceiling is not None else None
        if signal and signal.action == "PAPER_BET" and room_to_ceiling is not None and room_to_ceiling >= 0:
            entry_action = "ENTRY WINDOW"
        elif signal:
            entry_action = "WAIT"
        else:
            entry_action = "MARKET ONLY"
        risks = _risk_flags(quote, signal, age_seconds)
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
            "age_seconds": age_seconds,
            "entry_action": entry_action,
            "model_probability": model_probability,
            "model_live_prob": signal.model_live_prob if signal else None,
            "entry_margin": entry_margin,
            "price_ceiling": price_ceiling,
            "room_to_ceiling": room_to_ceiling,
            "confidence": signal.confidence if signal else None,
            "reference_sources": signal.n_reference_sources if signal else 0,
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
        fair = signal.model_probability if signal else None
        remaining_edge = fair - bid if fair is not None and bid is not None else None
        reasons = []
        if bid is None:
            action = "EXIT WATCH"
            reasons.append("No executable bid is visible, so an immediate cash-out cannot be estimated.")
        elif spread is None or spread > 0.05:
            action = "EXIT WATCH"
            reasons.append("The exit spread is wide; use a limit price and watch fill risk.")
        elif signal and remaining_edge is not None and remaining_edge < -0.02:
            action = "CONSIDER CASH"
            reasons.append("The executable bid is more than 2¢ above the current model fair value.")
        elif roi is not None and roi >= 0.20 and (remaining_edge is None or remaining_edge < 0.02):
            action = "CONSIDER CASH"
            reasons.append("The position is up at least 20% and little validated edge remains.")
        elif roi is not None and roi <= -0.15 and (remaining_edge is None or remaining_edge <= 0):
            action = "CONSIDER CASH"
            reasons.append("The position is down at least 15% without positive validated hold edge.")
        elif signal and remaining_edge is not None and remaining_edge >= 0.02 and signal.confidence >= confidence_threshold:
            action = "HOLD"
            reasons.append("Model fair value remains at least 2¢ above the executable exit price.")
        else:
            action = "HOLD / MONITOR"
            reasons.append("No strong exit trigger is present, but the remaining edge is not decisive.")
        if signal:
            reasons.extend(signal.reasons[:2])
        else:
            reasons.append("No independent fair-value signal is available; treat this as price/P&L monitoring only.")
        views.append({**position, "current_bid": bid, "current_ask": quote.ask if quote else None,
                      "spread": spread, "cash_value": cash_value, "unrealized_pnl": pnl,
                      "roi": roi, "model_probability": fair, "remaining_hold_edge": remaining_edge,
                      "advice": action, "reasons": reasons})
    return views
