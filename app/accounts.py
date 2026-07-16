"""Paper-trading accounts — fake-money bots that auto-follow the engine.

Each account runs a Strategy (edge floor, confidence floor, allowed line types,
and a stake-sizing rule) over the live signal stream. When a signal clears the
account's own bar, the account "places" a paper bet: it buys shares at the
executable price and deducts the stake from its fake bankroll. When the game
finalizes, moneyline / spread / total bets are graded from the final score and
the bankroll is credited. Running several accounts side by side is a live
strategy-calibration harness (compare ROI, win rate, exposure).

Realized only — open bets are held at cost (no mark-to-market), and player
props can't be graded from the score so they're voided (refunded) at settle.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields

from .lines import is_spread_market, is_total_market, quote_line_side
from .models import Event, Signal

_MONEYLINE = {"moneyline", "h2h", "winner", "match_winner"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name           TEXT PRIMARY KEY,
    strategy       TEXT NOT NULL,
    start_bankroll REAL NOT NULL,
    bankroll       REAL NOT NULL,
    created_ts     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_bets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account     TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    event_name  TEXT,
    market      TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stake       REAL NOT NULL,
    shares      REAL NOT NULL,
    model_prob  REAL,
    edge        REAL,
    placed_ts   REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open|win|loss|push|void
    result      REAL,                            -- 1 win, 0 loss, NULL otherwise
    pnl         REAL,
    settled_ts  REAL,
    UNIQUE(account, event_id, market, outcome)
);
"""


@dataclass
class Strategy:
    name: str
    blurb: str = ""
    edge_threshold: float = 0.03
    confidence_threshold: float = 0.0
    min_sources: int = 2
    markets: tuple = ("moneyline", "spread", "total")
    sizing: str = "kelly"            # kelly | flat | flat_pct
    kelly_multiplier: float = 1.0
    flat_stake: float = 100.0
    flat_pct: float = 0.02
    max_stake_pct: float = 0.10
    start_bankroll: float = 10_000.0
    webhook_url: str = ""

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
    Strategy("Engine Kelly", "Full fractional Kelly at the engine's edge floor.",
             edge_threshold=0.03, kelly_multiplier=1.0),
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


def qualifies(strategy: Strategy, signal: Signal) -> bool:
    return (signal.n_reference_sources >= strategy.min_sources
            and signal.edge >= strategy.edge_threshold
            and (signal.confidence or 0) >= strategy.confidence_threshold
            and market_allowed(strategy, signal.market))


def stake_for(strategy: Strategy, signal: Signal, bankroll: float) -> float:
    if strategy.sizing == "flat":
        stake = strategy.flat_stake
    elif strategy.sizing == "flat_pct":
        stake = strategy.flat_pct * bankroll
    else:  # kelly
        stake = (signal.kelly_fraction or 0.0) * strategy.kelly_multiplier * bankroll
    stake = min(stake, strategy.max_stake_pct * bankroll, bankroll)
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


class AccountBook:
    """Thread-safe SQLite store of paper accounts and their bets."""

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("ACCOUNTS_DB", os.getenv("LEDGER_DB", "ledger.db"))
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def seed(self, strategies: list[Strategy]) -> None:
        """Create any missing preset accounts (idempotent)."""
        now = _now()
        with self._lock:
            for strat in strategies:
                self._conn.execute(
                    """INSERT OR IGNORE INTO accounts (name, strategy, start_bankroll, bankroll, created_ts)
                       VALUES (?,?,?,?,?)""",
                    (strat.name, strat.to_json(), strat.start_bankroll, strat.start_bankroll, now),
                )
            self._conn.commit()

    def place(self, event: Event, signals: list[Signal]) -> list[dict]:
        """Let every account bet the signals that clear its own bar (once each)."""
        now = _now()
        placed_bets = []
        with self._lock:
            accounts = self._conn.execute("SELECT name, strategy, bankroll FROM accounts").fetchall()
            for account in accounts:
                strategy = Strategy.from_json(account["strategy"])
                bankroll = account["bankroll"]
                for signal in signals:
                    if signal.market_probability <= 0 or not qualifies(strategy, signal):
                        continue
                    stake = stake_for(strategy, signal, bankroll)
                    if stake < 1.0:  # dust or out of funds
                        continue
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO account_bets
                           (account, event_id, event_name, market, outcome, entry_price, stake,
                            shares, model_prob, edge, placed_ts, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                        (account["name"], event.id, event.name, signal.market, signal.outcome,
                         signal.market_probability, stake, stake / signal.market_probability,
                         signal.model_probability, signal.edge, now),
                    )
                    if cur.rowcount:
                        bankroll -= stake
                        placed_bets.append({
                            "bot_name": account["name"],
                            "webhook_url": strategy.webhook_url,
                            "event_name": event.name,
                            "market": signal.market,
                            "outcome": signal.outcome,
                            "action": "PAPER_BET",
                            "stake": stake,
                            "entry_price": signal.market_probability,
                            "edge": signal.edge,
                        })
                        self._conn.execute("UPDATE accounts SET bankroll=? WHERE name=?",
                                           (bankroll, account["name"]))
            if placed_bets:
                self._conn.commit()
        return placed_bets

    def settle(self, event: Event, home_score: float, away_score: float) -> int:
        """Grade every open bet on the event and credit each account's bankroll."""
        now = _now()
        settled = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM account_bets WHERE event_id=? AND status='open'", (event.id,)
            ).fetchall()
            credits: dict[str, float] = {}
            updates = []
            for row in rows:
                verdict = grade(row["market"], row["outcome"], event.home, event.away,
                                home_score, away_score) or "void"
                if verdict == "win":
                    payout, pnl, result = row["shares"], row["shares"] - row["stake"], 1.0
                elif verdict == "loss":
                    payout, pnl, result = 0.0, -row["stake"], 0.0
                else:  # push or void -> refund the stake
                    payout, pnl, result = row["stake"], 0.0, None
                credits[row["account"]] = credits.get(row["account"], 0.0) + payout
                updates.append((verdict, result, pnl, now, row["id"]))
                settled += 1
            if updates:
                self._conn.executemany(
                    "UPDATE account_bets SET status=?, result=?, pnl=?, settled_ts=? WHERE id=?", updates
                )
                for name, credit in credits.items():
                    self._conn.execute("UPDATE accounts SET bankroll=bankroll+? WHERE name=?",
                                       (credit, name))
                self._conn.commit()
        return settled

    def leaderboard(self) -> list[dict]:
        with self._lock:
            accounts = self._conn.execute("SELECT * FROM accounts").fetchall()
            board = []
            for account in accounts:
                agg = self._conn.execute(
                    """SELECT
                         COUNT(*) AS n_bets,
                         SUM(status='open') AS n_open,
                         SUM(status IN ('win','loss','push','void')) AS n_settled,
                         SUM(status='win') AS wins,
                         SUM(status='loss') AS losses,
                         COALESCE(SUM(CASE WHEN status='open' THEN stake ELSE 0 END), 0) AS open_stake,
                         COALESCE(SUM(pnl), 0) AS realized_pnl
                       FROM account_bets WHERE account=?""",
                    (account["name"],),
                ).fetchone()
                start = account["start_bankroll"]
                equity = account["bankroll"] + agg["open_stake"]
                decided = (agg["wins"] or 0) + (agg["losses"] or 0)
                board.append({
                    "name": account["name"],
                    "strategy": Strategy.from_json(account["strategy"]).blurb,
                    "bankroll": account["bankroll"],
                    "start_bankroll": start,
                    "equity": equity,
                    "roi": (equity - start) / start if start else 0.0,
                    "realized_pnl": agg["realized_pnl"] or 0.0,
                    "n_bets": agg["n_bets"] or 0,
                    "n_open": agg["n_open"] or 0,
                    "n_settled": agg["n_settled"] or 0,
                    "wins": agg["wins"] or 0,
                    "losses": agg["losses"] or 0,
                    "win_rate": (agg["wins"] / decided) if decided else None,
                    "open_stake": agg["open_stake"] or 0.0,
                })
            board.sort(key=lambda a: a["equity"], reverse=True)
            return board

    def account_bets(self, name: str, limit: int = 100) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM account_bets WHERE account=? ORDER BY placed_ts DESC LIMIT ?",
                (name, limit))]

    def reset(self, strategies: list[Strategy]) -> None:
        """Wipe all bets and restore every account to its starting bankroll."""
        with self._lock:
            self._conn.execute("DELETE FROM account_bets")
            self._conn.execute("DELETE FROM accounts")
            self._conn.commit()
        self.seed(strategies)
