"""Paper-trading accounts — fake-money bots that auto-follow the engine.

Each account runs a Strategy (edge floor, confidence floor, allowed line types,
and a stake-sizing rule) over the live signal stream. When a signal clears the
account's own bar, the account "places" a paper bet: it buys shares at the
executable price and deducts the stake from its fake bankroll. When the game
finalizes, moneyline / spread / total bets are graded from the final score and
the bankroll is credited. Running several accounts side by side is a live
strategy-calibration harness (compare ROI, win rate, exposure).

Every entry and optional cash-out is simulated against complete Polymarket CLOB
depth with the recorded fee schedule. Open positions are marked at executable
net liquidation value; unsupported markets are rejected before entry rather
than silently voided later.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime

from .database import Database
from .execution import BookLevel, simulate_buy, simulate_sell
from .lines import is_spread_market, is_total_market, quote_line_side
from .models import Event, Quote, Signal

_MONEYLINE = {"moneyline", "h2h", "winner", "match_winner"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name           TEXT PRIMARY KEY,
    strategy       TEXT NOT NULL,
    start_bankroll DOUBLE PRECISION NOT NULL,
    bankroll       DOUBLE PRECISION NOT NULL,
    created_ts     DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS account_bets (
    id          SERIAL PRIMARY KEY,
    account     TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    event_name  TEXT,
    market      TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    stake       DOUBLE PRECISION NOT NULL,
    shares      DOUBLE PRECISION NOT NULL,
    model_prob  DOUBLE PRECISION,
    edge        DOUBLE PRECISION,
    placed_ts   DOUBLE PRECISION NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open|win|loss|push|void
    result      DOUBLE PRECISION,                            -- 1 win, 0 loss, NULL otherwise
    pnl         DOUBLE PRECISION,
    settled_ts  DOUBLE PRECISION,
    UNIQUE(account, event_id, market, outcome)
);
"""

_MARKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_bet_marks (
    id                       SERIAL PRIMARY KEY,
    bet_id                   INTEGER NOT NULL,
    account                  TEXT NOT NULL,
    event_id                 TEXT NOT NULL,
    token_id                 TEXT NOT NULL,
    marked_ts                DOUBLE PRECISION NOT NULL,
    provider_ts              DOUBLE PRECISION,
    received_ts              DOUBLE PRECISION,
    bid_vwap                 DOUBLE PRECISION,
    effective_sell_price     DOUBLE PRECISION,
    gross_value              DOUBLE PRECISION,
    net_value                DOUBLE PRECISION,
    exit_fee                 DOUBLE PRECISION,
    unrealized_pnl           DOUBLE PRECISION,
    model_prob               DOUBLE PRECISION,
    hold_edge                DOUBLE PRECISION,
    book_hash                TEXT,
    decision_id              TEXT,
    decision_action          TEXT NOT NULL,
    decision_reason          TEXT NOT NULL,
    execution_reason         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_bet_marks_bet_time
    ON account_bet_marks(bet_id, marked_ts);
CREATE INDEX IF NOT EXISTS idx_account_bet_marks_event_time
    ON account_bet_marks(event_id, marked_ts);
"""


@dataclass
class Strategy:
    name: str
    blurb: str = ""
    edge_threshold: float = 0.03
    confidence_threshold: float = 0.0
    min_sources: int = 2
    markets: tuple = ("all",)
    sizing: str = "kelly"            # kelly | flat | flat_pct
    kelly_multiplier: float = 1.0
    flat_stake: float = 100.0
    flat_pct: float = 0.02
    max_stake_pct: float = 0.10
    max_event_exposure_pct: float = 0.15
    max_sport_exposure_pct: float = 0.25
    max_correlated_exposure_pct: float = 0.10
    max_total_exposure_pct: float = 0.40
    start_bankroll: float = 10_000.0
    webhook_url: str = ""
    cash_out_enabled: bool = False
    cash_out_min_hold_seconds: float = 120.0
    cash_out_min_price_move: float = 0.03
    cash_out_min_profit_dollars: float = 2.0
    cash_out_min_profit_pct: float = 0.08
    cash_out_hard_profit_pct: float = 0.20
    cash_out_trailing_activation_pct: float = 0.12
    cash_out_trailing_drawdown_pct: float = 0.35
    cash_out_model_reversal_margin: float = 0.02
    cash_out_stop_loss_pct: float = 0.18

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str) -> "Strategy":
        data = json.loads(raw)
        known = {f.name for f in fields(Strategy)}
        data = {k: v for k, v in data.items() if k in known}
        if "markets" in data:
            data["markets"] = tuple(data["markets"])
        return Strategy(**data)


# A spread of risk profiles for calibration.
DEFAULT_STRATEGIES = [
    Strategy("Engine Kelly", "Full fractional Kelly whenever the engine clears every risk gate.",
             edge_threshold=0.0, kelly_multiplier=1.0),
    Strategy("Half Kelly", "Half-Kelly stakes — smoother equity curve.",
             edge_threshold=0.03, kelly_multiplier=0.5),
    Strategy("Quarter Kelly (safe)", "Quarter-Kelly and only well-anchored signals.",
             edge_threshold=0.03, kelly_multiplier=0.25, confidence_threshold=70.0, min_sources=3),
    Strategy("Flat $100", "Fixed $100 a bet at a 3% edge floor.",
             edge_threshold=0.03, sizing="flat", flat_stake=100.0),
    Strategy("Aggressive (low bar)", "Lower 1.5% edge bar, full Kelly, bigger caps.",
             edge_threshold=0.015, kelly_multiplier=1.0, max_stake_pct=0.20),
    Strategy("Moneyline only", "High-confidence moneyline bets, flat 2% of bankroll.",
             edge_threshold=0.03, confidence_threshold=75.0, markets=("moneyline",),
             sizing="flat_pct", flat_pct=0.02),
]


def line_type(market: str) -> str:
    m = (market or "").lower()
    if m in _MONEYLINE:
        return "moneyline"
    if is_spread_market(m):
        return "spread"
    if is_total_market(m):
        return "total"
    return "prop"


def market_allowed(strategy: Strategy, market: str) -> bool:
    return "all" in strategy.markets or line_type(market) in strategy.markets


def _correlation_group(event: Event, signal: Signal) -> str:
    """Transparent, conservative grouping for paper exposure caps.

    Game-side moneylines and spreads share a home/away group; game totals share
    a total group. Props are grouped by their normalized market label. This is
    a policy grouping, not a fitted covariance estimate.
    """
    market_kind = line_type(signal.market)
    _, side = quote_line_side(
        signal.market, signal.outcome, event.home, event.away
    )
    if market_kind == "moneyline":
        outcome = signal.outcome.strip().casefold()
        if outcome in {"home", event.home.strip().casefold()}:
            side = "home"
        elif outcome in {"away", event.away.strip().casefold()}:
            side = "away"
        elif outcome == "draw":
            side = "draw"
    if market_kind in {"moneyline", "spread"} and side in {"home", "away", "draw"}:
        group = f"team-side:{side}"
    elif market_kind == "total":
        group = "game-total"
    else:
        group = f"prop:{signal.market.strip().casefold()}"
    return f"{event.id}:{group}"


def _uncalibrated_eligible(signal: Signal) -> bool:
    """True when a WATCH signal is held back *only* by a missing calibration artifact.

    Every engine gate that was actually evaluated (``pass``/``fail``) must pass;
    only the calibration/policy gates may be ``unknown`` (which is exactly what
    the Rust engine reports when no versioned artifact is installed). This lets
    an opt-in paper harness trade a fundamentally sound, uncalibrated gross-gap
    edge without loosening any freshness, source-count, identity, execution, or
    edge-floor requirement. It never fires once a real calibration policy is
    installed, because such a signal would already carry action ``PAPER_BET``
    (and a non-null calibrated probability).
    """
    if signal.calibrated_consensus_probability is not None:
        return False
    evaluated = [gate for gate in (signal.gate_results or [])
                 if gate.get("passed") is not None]
    return bool(evaluated) and all(gate.get("passed") for gate in evaluated)


def qualification_failures(strategy: Strategy, signal: Signal, *,
                           allow_uncalibrated: bool = False) -> list[str]:
    """Explain why a strategy must not paper-buy a signal."""
    failures = []
    action_ready = signal.action == "PAPER_BET" or (
        allow_uncalibrated and _uncalibrated_eligible(signal))
    if not action_ready:
        failures.append("engine gates did not clear")
    if signal.quote_source.casefold() != "polymarket":
        failures.append("not an executable Polymarket selection")
    if not 0 < signal.market_probability < 1:
        failures.append("invalid executable price")
    if signal.n_reference_sources < strategy.min_sources:
        failures.append("too few independent references")
    if signal.edge < signal.required_edge:
        failures.append("edge is below the engine's risk-adjusted requirement")
    if signal.edge < strategy.edge_threshold:
        failures.append("edge is below the strategy threshold")
    if (signal.confidence or 0) < strategy.confidence_threshold:
        failures.append("signal quality is below the strategy threshold")
    if not market_allowed(strategy, signal.market):
        failures.append("market is disabled for this strategy")
    return failures


def qualifies(strategy: Strategy, signal: Signal, *,
              allow_uncalibrated: bool = False) -> bool:
    return not qualification_failures(
        strategy, signal, allow_uncalibrated=allow_uncalibrated)


def model_backed_failures(strategy: Strategy, signal: Signal) -> list[str]:
    """Gate a selection whose decision probability comes from an independent
    model (e.g. the in-play tennis model) rather than an odds consensus.

    The model *is* the second opinion, so the engine action and reference-source
    gates do not apply. Everything else that protects execution still does: it
    must be an executable Polymarket price on a market the strategy allows. The
    actual edge (model probability minus the simulated fill) is enforced in
    :meth:`AccountBook.place` against the strategy and engine floors.
    """
    failures = []
    if signal.quote_source.casefold() != "polymarket":
        failures.append("not an executable Polymarket selection")
    if not 0 < signal.market_probability < 1:
        failures.append("invalid executable price")
    if not market_allowed(strategy, signal.market):
        failures.append("market is disabled for this strategy")
    return failures


def stake_for(strategy: Strategy, signal: Signal, bankroll: float) -> float:
    if strategy.sizing == "flat":
        stake = strategy.flat_stake
    elif strategy.sizing == "flat_pct":
        stake = strategy.flat_pct * bankroll
    else:  # kelly
        stake = (signal.kelly_fraction or 0.0) * strategy.kelly_multiplier * bankroll
    stake = min(stake, strategy.max_stake_pct * bankroll, bankroll)
    if signal.quote_source.casefold() == "polymarket" and signal.fillable_size is not None:
        # fillable_size is best-ask shares; convert it to dollars at the entry price.
        stake = min(stake, max(0.0, signal.fillable_size) * signal.market_probability)
    return max(0.0, stake)


def _matches(outcome: str, label: str) -> bool:
    return (outcome or "").strip().casefold() == (label or "").strip().casefold()


def grade(market: str, outcome: str, home: str, away: str,
          home_score: float, away_score: float) -> str | None:
    """win | loss | push | None (ungradeable from the score, e.g. a player prop)."""
    m = (market or "").lower()
    if m in _MONEYLINE:
        if home_score > away_score:
            win = _matches(outcome, "home") or _matches(outcome, home)
        elif away_score > home_score:
            win = _matches(outcome, "away") or _matches(outcome, away)
        else:
            win = _matches(outcome, "draw")
        return "win" if win else "loss"
    point, side = quote_line_side(market, outcome, home, away)
    if point is not None and is_total_market(m) and side in ("over", "under"):
        total = home_score + away_score
        if total == point:
            return "push"
        return "win" if ((total > point) == (side == "over")) else "loss"
    if point is not None and is_spread_market(m) and side in ("home", "away"):
        margin = (home_score - away_score) if side == "home" else (away_score - home_score)
        covered = margin + point
        if covered == 0:
            return "push"
        return "win" if covered > 0 else "loss"
    return None  # player prop / unmappable -> void at settle


def _now() -> float:
    return time.time()


def _timestamp(value: datetime | float | None) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value) if value is not None else _now()


def _book_levels(values: tuple[tuple[float, float], ...]) -> list[BookLevel]:
    try:
        return [BookLevel.create(price, size) for price, size in values]
    except (TypeError, ValueError):
        return []


def _latest_quotes(quotes: list[Quote]) -> dict[str, Quote]:
    latest: dict[str, Quote] = {}
    for quote in quotes:
        if quote.source.casefold() != "polymarket" or not quote.token_id:
            continue
        previous = latest.get(quote.token_id)
        if previous is None or quote.processed_at >= previous.processed_at:
            latest[quote.token_id] = quote
    return latest


def _decision_probability(signal: Signal | None) -> float | None:
    if signal is None:
        return None
    for value in (
        signal.calibrated_consensus_probability,
        signal.model_probability,
        signal.consensus_probability,
    ):
        if value is not None and 0 < value < 1:
            return float(value)
    return None


def _gradeable(event: Event, market: str, outcome: str) -> bool:
    kind = line_type(market)
    if kind == "moneyline":
        return any(_matches(outcome, label) for label in ("home", "away", "draw",
                                                           event.home, event.away))
    point, side = quote_line_side(market, outcome, event.home, event.away)
    if kind == "spread":
        return point is not None and side in {"home", "away"}
    if kind == "total":
        return point is not None and side in {"over", "under"}
    return False


def _cashout_decision(strategy: Strategy, row, *, net_value: float,
                      effective_price: float, model_prob: float | None,
                      marked_at: float, high_water: float) -> tuple[bool, str]:
    hold_seconds = max(0.0, marked_at - float(row["placed_ts"]))
    pnl = net_value - float(row["stake"])
    stake = float(row["stake"])
    return_pct = pnl / stake if stake else 0.0
    price_move = effective_price - float(row["entry_price"])
    if hold_seconds < strategy.cash_out_min_hold_seconds:
        return False, f"minimum hold not reached ({hold_seconds:.0f}s)"

    if (return_pct >= strategy.cash_out_hard_profit_pct
            and price_move >= strategy.cash_out_min_price_move):
        return True, "hard profit target reached after costs"

    activation = strategy.cash_out_trailing_activation_pct * stake
    retained = high_water * (1.0 - strategy.cash_out_trailing_drawdown_pct)
    required_profit = max(strategy.cash_out_min_profit_dollars,
                          strategy.cash_out_min_profit_pct * stake)
    if (high_water >= activation and pnl >= required_profit and pnl <= retained
            and price_move >= strategy.cash_out_min_price_move):
        return True, "trailing profit protection triggered after costs"

    if model_prob is None:
        return False, "no current calibrated decision estimate"
    hold_edge = model_prob - effective_price
    reversed_model = hold_edge <= -strategy.cash_out_model_reversal_margin
    if (reversed_model and pnl >= required_profit
            and price_move >= strategy.cash_out_min_price_move):
        return True, "calibrated estimate reversed; meaningful net profit protected"
    if reversed_model and return_pct <= -strategy.cash_out_stop_loss_pct:
        return True, "calibrated estimate reversed; cost-aware stop loss triggered"
    return False, "cash-out thresholds not met after spread and fees"


class AccountBook:
    """Thread-safe store for auditable, fake-money bot positions."""

    def __init__(self, path: str | None = None):
        self._db = Database.open(
            path,
            sqlite_envs=("ACCOUNTS_DB", "LEDGER_DB"),
            sqlite_default="ledger.db",
        )
        self.path = self._db.target
        self.backend = self._db.backend
        self._conn = self._db.connection
        self._lock = threading.Lock()
        with self._lock:
            self._db.initialize(_SCHEMA, component="accounts", version=1)
            self._db.migrate_columns("accounts", 2, {
                "account_bets": {
                    "sport": "TEXT",
                    "correlation_group": "TEXT",
                    "decision_id": "TEXT",
                },
            })
            self._db.migrate_columns("accounts", 3, {
                "accounts": {
                    "cash_out_enabled": "INTEGER NOT NULL DEFAULT 0",
                },
                "account_bets": {
                    "token_id": "TEXT",
                    "condition_id": "TEXT",
                    "provider_market_id": "TEXT",
                    "entry_vwap": "DOUBLE PRECISION",
                    "entry_fee": "DOUBLE PRECISION",
                    "entry_book_hash": "TEXT",
                    "entry_provider_ts": "DOUBLE PRECISION",
                    "entry_received_ts": "DOUBLE PRECISION",
                    "last_mark_price": "DOUBLE PRECISION",
                    "last_mark_value": "DOUBLE PRECISION",
                    "last_mark_pnl": "DOUBLE PRECISION",
                    "last_mark_ts": "DOUBLE PRECISION",
                    "high_water_pnl": "DOUBLE PRECISION",
                    "exit_price": "DOUBLE PRECISION",
                    "exit_value": "DOUBLE PRECISION",
                    "exit_fee": "DOUBLE PRECISION",
                    "exit_ts": "DOUBLE PRECISION",
                    "exit_reason": "TEXT",
                },
            })
            self._db.initialize(_MARKS_SCHEMA, component="account_marks", version=1)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def seed(self, strategies: list[Strategy]) -> None:
        """Create missing preset accounts without overwriting a user's toggle."""
        now = _now()
        with self._lock:
            with self._db.transaction() as cur:
                for strat in strategies:
                    self._db.execute(
                        cur,
                        """INSERT INTO accounts
                           (name, strategy, start_bankroll, bankroll, created_ts,
                            cash_out_enabled)
                           VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (name) DO UPDATE SET
                           strategy=EXCLUDED.strategy""",
                        (strat.name, strat.to_json(), strat.start_bankroll,
                         strat.start_bankroll, now, int(strat.cash_out_enabled)),
                    )

    def set_cash_out(self, name: str, enabled: bool) -> bool:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur, "UPDATE accounts SET cash_out_enabled=%s WHERE name=%s",
                    (int(enabled), name),
                )
                return bool(cur.rowcount)

    def open_count(self, event_id: str) -> int:
        with self._lock:
            with self._db.cursor() as cur:
                self._db.execute(
                    cur,
                    "SELECT COUNT(*) FROM account_bets WHERE event_id=%s AND status='open'",
                    (event_id,),
                )
                return int(cur.fetchone()[0])

    def place(self, event: Event, signals: list[Signal], quotes: list[Quote] | None = None,
              *, as_of: datetime | float | None = None,
              allow_uncalibrated: bool = False,
              model_probabilities: dict[str, float] | None = None) -> list[dict]:
        """Open paper positions only after an exact full-depth simulated fill.

        ``allow_uncalibrated`` (opt-in, off by default) additionally admits
        signals the engine holds at WATCH solely for a missing calibration
        artifact; every other engine gate and the full-depth fill still apply.

        ``model_probabilities`` maps a Polymarket ``token_id`` to an independent
        model's win probability (e.g. the in-play tennis model). For those
        tokens the model replaces the odds consensus as the decision basis, so
        the engine action and reference-source gates are waived, but the
        executable-price checks, full-depth fill, exposure caps, and edge floor
        still apply. This is how single-source sports (tennis) trade a real
        edge instead of a fabricated one.
        """
        now = _timestamp(as_of)
        model_probabilities = model_probabilities or {}
        quote_by_token = _latest_quotes(quotes or [])
        placed_bets = []
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT name, strategy, bankroll, start_bankroll FROM accounts",
                )
                accounts = cur.fetchall()
                for account in accounts:
                    strategy = Strategy.from_json(account["strategy"])
                    bankroll = float(account["bankroll"])
                    self._db.execute(
                        cur,
                        """SELECT COALESCE(SUM(stake),0) AS total_open,
                                  COALESCE(SUM(CASE WHEN event_id=%s THEN stake ELSE 0 END),0)
                                    AS event_open,
                                  COALESCE(SUM(CASE WHEN sport=%s THEN stake ELSE 0 END),0)
                                    AS sport_open
                           FROM account_bets WHERE account=%s AND status='open'""",
                        (event.id, event.sport.casefold(), account["name"]),
                    )
                    exposure = cur.fetchone()
                    total_open = float(exposure["total_open"] or 0)
                    event_open = float(exposure["event_open"] or 0)
                    sport_open = float(exposure["sport_open"] or 0)
                    equity_for_caps = bankroll + total_open
                    for signal in signals:
                        if not signal.token_id or not _gradeable(
                                event, signal.market, signal.outcome):
                            continue
                        quote = quote_by_token.get(signal.token_id)
                        if quote is None or signal.market_probability <= 0:
                            continue
                        model_override = model_probabilities.get(signal.token_id)
                        if model_override is not None:
                            if model_backed_failures(strategy, signal):
                                continue
                        elif not qualifies(strategy, signal,
                                           allow_uncalibrated=allow_uncalibrated):
                            continue
                        if (signal.order_book_snapshot_id
                                and signal.order_book_snapshot_id != quote.book_hash):
                            continue
                        correlation_group = _correlation_group(event, signal)
                        self._db.execute(
                            cur,
                            "SELECT COALESCE(SUM(stake),0) AS correlated_open "
                            "FROM account_bets WHERE account=%s AND status='open' "
                            "AND correlation_group=%s",
                            (account["name"], correlation_group),
                        )
                        correlated_open = float(cur.fetchone()["correlated_open"] or 0)
                        requested_stake = stake_for(strategy, signal, bankroll)
                        requested_stake = min(
                            requested_stake,
                            max(0.0, strategy.max_event_exposure_pct * equity_for_caps
                                - event_open),
                            max(0.0, strategy.max_sport_exposure_pct * equity_for_caps
                                - sport_open),
                            max(0.0, strategy.max_correlated_exposure_pct * equity_for_caps
                                - correlated_open),
                            max(0.0, strategy.max_total_exposure_pct * equity_for_caps
                                - total_open),
                        )
                        if requested_stake < 1.0:
                            continue
                        execution = simulate_buy(
                            _book_levels(quote.ask_levels),
                            cash=requested_stake,
                            fee_rate=quote.fee_rate,
                            tick_size=quote.tick_size,
                            min_order_size=quote.min_order_size,
                            active=quote.active,
                            resolved=quote.resolved,
                            restricted=quote.restricted,
                            accepting_orders=quote.accepting_orders,
                            depth_complete=quote.depth_complete,
                        )
                        if (not execution.complete or execution.effective_probability is None
                                or execution.vwap is None):
                            continue
                        stake = float(execution.filled_cash)
                        shares = float(execution.filled_shares)
                        entry_price = float(execution.effective_probability)
                        model_probability = (model_override if model_override is not None
                                             else _decision_probability(signal))
                        if model_probability is None or not 0 < model_probability < 1:
                            continue
                        actual_edge = model_probability - entry_price
                        if actual_edge < max(signal.required_edge, strategy.edge_threshold):
                            continue
                        self._db.execute(
                            cur,
                            """INSERT INTO account_bets
                               (account, event_id, event_name, market, outcome, entry_price, stake,
                                shares, model_prob, edge, placed_ts, status, sport,
                                correlation_group, decision_id, token_id, condition_id,
                                provider_market_id, entry_vwap, entry_fee, entry_book_hash,
                                entry_provider_ts, entry_received_ts)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,
                                       %s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (account, event_id, market, outcome) DO NOTHING""",
                            (account["name"], event.id, event.name, signal.market, signal.outcome,
                             entry_price, stake, shares, model_probability,
                             actual_edge, now, event.sport.casefold(), correlation_group,
                             signal.decision_id or None, quote.token_id, quote.condition_id,
                             quote.provider_market_id, float(execution.vwap),
                             float(execution.fee), quote.book_hash,
                             _timestamp(quote.provider_timestamp)
                             if quote.provider_timestamp is not None else None,
                             _timestamp(quote.received_at)),
                        )
                        if not cur.rowcount:
                            continue
                        bankroll -= stake
                        total_open += stake
                        event_open += stake
                        sport_open += stake
                        placed_bets.append({
                            "bot_name": account["name"],
                            "webhook_url": strategy.webhook_url,
                            "event_name": event.name,
                            "market": signal.market,
                            "outcome": signal.outcome,
                            "action": "PAPER_BET",
                            "stake": stake,
                            "entry_price": entry_price,
                            "entry_vwap": float(execution.vwap),
                            "entry_fee": float(execution.fee),
                            "edge": actual_edge,
                        })
                        self._db.execute(
                            cur, "UPDATE accounts SET bankroll=%s WHERE name=%s",
                            (bankroll, account["name"]),
                        )
        return placed_bets

    def mark_and_cash_out(self, event: Event, quotes: list[Quote], signals: list[Signal],
                          *, as_of: datetime | float | None = None) -> list[dict]:
        """Persist executable marks and optionally close positions exactly once."""
        now = _timestamp(as_of)
        quote_by_token = _latest_quotes(quotes)
        signal_by_token = {signal.token_id: signal for signal in signals if signal.token_id}
        exits: list[dict] = []
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT b.*, a.strategy, a.cash_out_enabled
                       FROM account_bets b JOIN accounts a ON a.name=b.account
                       WHERE b.event_id=%s AND b.status='open'""",
                    (event.id,),
                )
                rows = cur.fetchall()
                for row in rows:
                    token_id = row["token_id"]
                    if not token_id:
                        continue  # legacy rows remain settle-only; no identity is invented.
                    quote = quote_by_token.get(token_id)
                    signal = signal_by_token.get(token_id)
                    model_prob = _decision_probability(signal)
                    strategy = Strategy.from_json(row["strategy"])
                    action = "UNPRICED"
                    reason = "exact current Polymarket order book unavailable"
                    execution_reason = reason
                    result = None
                    if quote is not None:
                        result = simulate_sell(
                            _book_levels(quote.bid_levels),
                            shares=row["shares"],
                            fee_rate=quote.fee_rate,
                            tick_size=quote.tick_size,
                            min_order_size=quote.min_order_size,
                            active=quote.active,
                            resolved=quote.resolved,
                            restricted=quote.restricted,
                            accepting_orders=quote.accepting_orders,
                            depth_complete=quote.depth_complete,
                        )
                        execution_reason = result.reason

                    bid_vwap = effective = gross = net = exit_fee = pnl = hold_edge = None
                    if (result is not None and result.complete
                            and result.effective_probability is not None
                            and result.vwap is not None):
                        bid_vwap = float(result.vwap)
                        effective = float(result.effective_probability)
                        gross = float(result.gross_proceeds)
                        net = float(result.net_proceeds)
                        exit_fee = float(result.fee)
                        pnl = net - float(row["stake"])
                        hold_edge = model_prob - effective if model_prob is not None else None
                        previous_high = (float(row["high_water_pnl"])
                                         if row["high_water_pnl"] is not None else pnl)
                        high_water = max(previous_high, pnl)
                        enabled = bool(row["cash_out_enabled"])
                        should_exit, reason = _cashout_decision(
                            strategy, row, net_value=net, effective_price=effective,
                            model_prob=model_prob, marked_at=now, high_water=previous_high,
                        )
                        if not enabled:
                            action = "MARK_ONLY"
                            reason = "automatic cash-out is disabled for this bot"
                            should_exit = False
                        elif should_exit:
                            action = "CASH_OUT"
                        else:
                            action = "HOLD"
                        self._db.execute(
                            cur,
                            """UPDATE account_bets SET last_mark_price=%s, last_mark_value=%s,
                               last_mark_pnl=%s, last_mark_ts=%s, high_water_pnl=%s
                               WHERE id=%s AND status='open'""",
                            (effective, net, pnl, now, high_water, row["id"]),
                        )
                    elif result is not None:
                        reason = result.reason

                    self._db.execute(
                        cur,
                        """INSERT INTO account_bet_marks
                           (bet_id, account, event_id, token_id, marked_ts, provider_ts,
                            received_ts, bid_vwap, effective_sell_price, gross_value,
                            net_value, exit_fee, unrealized_pnl, model_prob, hold_edge,
                            book_hash, decision_id, decision_action, decision_reason,
                            execution_reason)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s)""",
                        (row["id"], row["account"], row["event_id"], token_id, now,
                         (_timestamp(quote.provider_timestamp)
                          if quote is not None and quote.provider_timestamp is not None else None),
                         (_timestamp(quote.received_at) if quote is not None else None),
                         bid_vwap, effective, gross, net, exit_fee, pnl, model_prob,
                         hold_edge, quote.book_hash if quote is not None else None,
                         signal.decision_id if signal is not None else None,
                         action, reason, execution_reason),
                    )
                    if action != "CASH_OUT" or net is None:
                        continue
                    self._db.execute(
                        cur,
                        """UPDATE account_bets SET status='cashed_out', pnl=%s,
                           settled_ts=%s, exit_price=%s, exit_value=%s, exit_fee=%s,
                           exit_ts=%s, exit_reason=%s WHERE id=%s AND status='open'""",
                        (pnl, now, effective, net, exit_fee, now, reason, row["id"]),
                    )
                    if not cur.rowcount:
                        continue
                    self._db.execute(
                        cur, "UPDATE accounts SET bankroll=bankroll+%s WHERE name=%s",
                        (net, row["account"]),
                    )
                    exits.append({
                        "bot_name": row["account"],
                        "webhook_url": strategy.webhook_url,
                        "event_name": row["event_name"],
                        "market": row["market"],
                        "outcome": row["outcome"],
                        "action": "PAPER_CASH_OUT",
                        "exit_value": net,
                        "exit_price": effective,
                        "pnl": pnl,
                        "reason": reason,
                    })
        return exits

    def settle(self, event: Event, home_score: float, away_score: float,
               *, as_of: datetime | float | None = None) -> int:
        """Grade every remaining open position and credit the fake bankroll."""
        now = _timestamp(as_of)
        settled = 0
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT * FROM account_bets WHERE event_id=%s AND status='open'",
                    (event.id,),
                )
                rows = cur.fetchall()
                credits: dict[str, float] = {}
                updates = []
                for row in rows:
                    verdict = grade(row["market"], row["outcome"], event.home, event.away,
                                    home_score, away_score) or "void"
                    if verdict == "win":
                        payout, pnl, result = row["shares"], row["shares"] - row["stake"], 1.0
                    elif verdict == "loss":
                        payout, pnl, result = 0.0, -row["stake"], 0.0
                    else:
                        payout, pnl, result = row["stake"], 0.0, None
                    credits[row["account"]] = credits.get(row["account"], 0.0) + payout
                    updates.append((verdict, result, pnl, now, row["id"]))
                    settled += 1
                if updates:
                    self._db.execute_many(
                        cur,
                        "UPDATE account_bets SET status=%s, result=%s, pnl=%s, "
                        "settled_ts=%s WHERE id=%s",
                        updates,
                    )
                    for name, credit in credits.items():
                        self._db.execute(
                            cur, "UPDATE accounts SET bankroll=bankroll+%s WHERE name=%s",
                            (credit, name),
                        )
        return settled

    def void_event(self, event_id: str, *, as_of: datetime | float | None = None) -> int:
        """Void open positions only for an authoritative provider cancellation."""
        now = _timestamp(as_of)
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT id, account, stake FROM account_bets "
                    "WHERE event_id=%s AND status='open'",
                    (event_id,),
                )
                rows = cur.fetchall()
                if not rows:
                    return 0
                credits: dict[str, float] = {}
                updates = []
                for row in rows:
                    credits[row["account"]] = credits.get(row["account"], 0.0) + row["stake"]
                    updates.append((now, row["id"]))
                self._db.execute_many(
                    cur,
                    "UPDATE account_bets SET status='void', result=NULL, pnl=0, "
                    "settled_ts=%s WHERE id=%s",
                    updates,
                )
                for name, credit in credits.items():
                    self._db.execute(
                        cur, "UPDATE accounts SET bankroll=bankroll+%s WHERE name=%s",
                        (credit, name),
                    )
                return len(rows)

    def leaderboard(self) -> list[dict]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(cur, "SELECT * FROM accounts")
                accounts = cur.fetchall()
                board = []
                for account in accounts:
                    self._db.execute(
                        cur,
                        """SELECT
                             COUNT(*) AS n_bets,
                             COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),0) AS n_open,
                             COALESCE(SUM(CASE WHEN status<>'open' THEN 1 ELSE 0 END),0) AS n_settled,
                             COALESCE(SUM(CASE WHEN status='win' THEN 1 ELSE 0 END),0) AS wins,
                             COALESCE(SUM(CASE WHEN status='loss' THEN 1 ELSE 0 END),0) AS losses,
                             COALESCE(SUM(CASE WHEN status='cashed_out' THEN 1 ELSE 0 END),0)
                               AS n_cashouts,
                             COALESCE(SUM(CASE WHEN status='open' THEN stake ELSE 0 END),0)
                               AS open_stake,
                             COALESCE(SUM(CASE WHEN status='open' AND last_mark_value IS NOT NULL
                                              THEN last_mark_value ELSE 0 END),0)
                               AS marked_open_value,
                             COALESCE(SUM(CASE WHEN status='open' AND last_mark_value IS NULL
                                              THEN 1 ELSE 0 END),0) AS unpriced_open,
                             COALESCE(SUM(CASE WHEN status='open' AND last_mark_value IS NULL
                                              THEN stake ELSE 0 END),0) AS unpriced_open_stake,
                             COALESCE(SUM(CASE WHEN status='open' AND last_mark_pnl IS NOT NULL
                                              THEN last_mark_pnl ELSE 0 END),0)
                               AS open_unrealized_pnl,
                             COALESCE(SUM(pnl),0) AS realized_pnl,
                             COALESCE(SUM(CASE WHEN status='cashed_out' THEN pnl ELSE 0 END),0)
                               AS cashout_pnl,
                             COALESCE(SUM(entry_fee),0)+COALESCE(SUM(exit_fee),0) AS fees,
                             AVG(CASE WHEN status='cashed_out' THEN exit_ts-placed_ts END)
                               AS avg_cashout_hold_seconds
                           FROM account_bets WHERE account=%s""",
                        (account["name"],),
                    )
                    agg = cur.fetchone()
                    start = float(account["start_bankroll"])
                    unpriced = int(agg["unpriced_open"] or 0)
                    known_equity = float(account["bankroll"]) + float(
                        agg["marked_open_value"] or 0)
                    equity = known_equity if not unpriced else None
                    decided = int(agg["wins"] or 0) + int(agg["losses"] or 0)
                    board.append({
                        "name": account["name"],
                        "strategy": Strategy.from_json(account["strategy"]).blurb,
                        "cash_out_enabled": bool(account["cash_out_enabled"]),
                        "bankroll": float(account["bankroll"]),
                        "start_bankroll": start,
                        "equity": equity,
                        "known_equity": known_equity,
                        "roi": ((equity - start) / start if equity is not None and start
                                else None),
                        "realized_pnl": float(agg["realized_pnl"] or 0),
                        "n_bets": int(agg["n_bets"] or 0),
                        "n_open": int(agg["n_open"] or 0),
                        "n_settled": int(agg["n_settled"] or 0),
                        "n_cashouts": int(agg["n_cashouts"] or 0),
                        "wins": int(agg["wins"] or 0),
                        "losses": int(agg["losses"] or 0),
                        "win_rate": (int(agg["wins"] or 0) / decided) if decided else None,
                        "open_stake": float(agg["open_stake"] or 0),
                        "open_unrealized_pnl": float(agg["open_unrealized_pnl"] or 0),
                        "unpriced_open_positions": unpriced,
                        "unpriced_open_stake": float(agg["unpriced_open_stake"] or 0),
                        "cashout_pnl": float(agg["cashout_pnl"] or 0),
                        "execution_fees": float(agg["fees"] or 0),
                        "avg_cashout_hold_seconds": (
                            float(agg["avg_cashout_hold_seconds"])
                            if agg["avg_cashout_hold_seconds"] is not None else None
                        ),
                    })
            board.sort(
                key=lambda item: (item["equity"] is not None,
                                  item["equity"] if item["equity"] is not None
                                  else item["known_equity"]),
                reverse=True,
            )
            return board

    def account_bets(self, name: str, limit: int = 100) -> list[dict]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT * FROM account_bets WHERE account=%s ORDER BY placed_ts DESC LIMIT %s",
                    (name, limit),
                )
                return [dict(row) for row in cur.fetchall()]

    def bets_for_eval(self, sport: str | None = None, limit: int = 10_000) -> list[dict]:
        """All bets across accounts (optionally one sport) for shadow evaluation."""
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                if sport:
                    self._db.execute(
                        cur,
                        "SELECT * FROM account_bets WHERE sport=%s "
                        "ORDER BY placed_ts DESC LIMIT %s",
                        (sport.casefold(), limit),
                    )
                else:
                    self._db.execute(
                        cur,
                        "SELECT * FROM account_bets ORDER BY placed_ts DESC LIMIT %s",
                        (limit,),
                    )
                return [dict(row) for row in cur.fetchall()]

    def bet_marks(self, name: str, bet_id: int, limit: int = 500) -> list[dict]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT * FROM account_bet_marks
                       WHERE account=%s AND bet_id=%s ORDER BY marked_ts DESC LIMIT %s""",
                    (name, bet_id, limit),
                )
                return [dict(row) for row in cur.fetchall()]

    def account_marks(self, name: str, limit: int = 5_000) -> list[dict]:
        """Latest decision/valuation rows, returned in chronological order."""
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT * FROM account_bet_marks
                       WHERE account=%s ORDER BY marked_ts DESC, id DESC LIMIT %s""",
                    (name, limit),
                )
                rows = [dict(row) for row in cur.fetchall()]
                rows.reverse()
                return rows

    def reset(self, strategies: list[Strategy]) -> None:
        """Wipe all paper observations and restore starting fake bankrolls."""
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(cur, "DELETE FROM account_bet_marks")
                self._db.execute(cur, "DELETE FROM account_bets")
                self._db.execute(cur, "DELETE FROM accounts")
        self.seed(strategies)
