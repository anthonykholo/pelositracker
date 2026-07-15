"""Durable paper-bet ledger — the 'truth loop'.

Every time a signal fires PAPER_BET we record one row per (event, market,
outcome) at its entry price. When the event locks we snapshot the closing
consensus fair value and compute CLV (closing_fair_prob - entry_executable),
which is the primary, settlement-free measure of whether the edge was real.
Moneyline bets are additionally settled from the final score so calibration
metrics (Brier, log-loss) can be computed offline in backtest.py.

CLV needs only market data; it is available for every market. Settlement
(win/loss) is only derived for moneyline here, because spreads/totals/props
need data the system does not yet ingest.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Iterable

from .models import Event, Signal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            TEXT NOT NULL,
    event_name          TEXT,
    sport               TEXT,
    market              TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    quote_source        TEXT,
    entry_ts            REAL NOT NULL,
    entry_executable    REAL NOT NULL,
    entry_fair_prob     REAL NOT NULL,
    entry_edge          REAL NOT NULL,
    confidence          REAL,
    devig_method        TEXT,
    overround           REAL,
    n_reference_sources INTEGER,
    closing_fair_prob   REAL,
    clv                 REAL,
    closing_ts          REAL,
    settled_result      REAL,
    settled_ts          REAL,
    UNIQUE(event_id, market, outcome)
);
CREATE TABLE IF NOT EXISTS closing_lines (
    event_id          TEXT NOT NULL,
    market            TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    closing_fair_prob REAL NOT NULL,
    closing_ts        REAL NOT NULL,
    UNIQUE(event_id, market, outcome)
);
CREATE TABLE IF NOT EXISTS positions (
    event_id        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    shares          REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    created_ts      REAL NOT NULL,
    updated_ts      REAL NOT NULL,
    PRIMARY KEY(event_id, token_id)
);
"""

_MONEYLINE_MARKETS = {"moneyline", "h2h", "winner"}


def _now() -> float:
    return time.time()


class Ledger:
    """Thread-safe SQLite append log for paper bets and closing lines."""

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("LEDGER_DB", "ledger.db")
        # A single shared connection guarded by a lock; the app writes at ~1/s.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_signals(self, event: Event, signals: Iterable[Signal]) -> int:
        """Log the entry snapshot of every PAPER_BET, once per selection."""
        now = _now()
        rows = [
            (
                event.id, event.name, event.sport, s.market, s.outcome, s.quote_source,
                now, s.market_probability, s.market_fair_prob, s.edge, s.confidence,
                s.devig_method, s.overround, s.n_reference_sources,
            )
            for s in signals
            if s.action == "PAPER_BET"
        ]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO bets
                   (event_id, event_name, sport, market, outcome, quote_source,
                    entry_ts, entry_executable, entry_fair_prob, entry_edge,
                    confidence, devig_method, overround, n_reference_sources)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    def snapshot_closing(self, event_id: str, fair_by_selection: dict[tuple[str, str], float]) -> None:
        """Record the closing consensus fair and compute CLV for open bets."""
        if not fair_by_selection:
            return
        now = _now()
        with self._lock:
            for (market, outcome), fair in fair_by_selection.items():
                self._conn.execute(
                    """INSERT INTO closing_lines (event_id, market, outcome, closing_fair_prob, closing_ts)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(event_id, market, outcome)
                       DO UPDATE SET closing_fair_prob=excluded.closing_fair_prob,
                                     closing_ts=excluded.closing_ts""",
                    (event_id, market, outcome, fair, now),
                )
                # CLV = closing fair prob - the price we entered at. Only set once.
                self._conn.execute(
                    """UPDATE bets
                       SET closing_fair_prob=?, clv=? - entry_executable, closing_ts=?
                       WHERE event_id=? AND market=? AND outcome=? AND closing_fair_prob IS NULL""",
                    (fair, fair, now, event_id, market, outcome),
                )
            self._conn.commit()

    def settle_moneyline(self, event_id: str, winner_labels: set[str]) -> None:
        """Settle moneyline-style bets from the final result (win=1, loss=0)."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """SELECT id, market, outcome FROM bets
                   WHERE event_id=? AND settled_result IS NULL""",
                (event_id,),
            )
            updates = [
                (1.0 if row["outcome"] in winner_labels else 0.0, now, row["id"])
                for row in cur.fetchall()
                if row["market"].lower() in _MONEYLINE_MARKETS
            ]
            if updates:
                self._conn.executemany(
                    "UPDATE bets SET settled_result=?, settled_ts=? WHERE id=?", updates
                )
                self._conn.commit()

    def all_bets(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute("SELECT * FROM bets ORDER BY entry_ts")]

    def event_bets(self, event_id: str) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM bets WHERE event_id=? ORDER BY entry_ts", (event_id,)
                )
            ]

    def upsert_position(self, event_id: str, token_id: str, market: str, outcome: str,
                        shares: float, avg_entry_price: float) -> dict:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO positions
                   (event_id, token_id, market, outcome, shares, avg_entry_price, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id, token_id) DO UPDATE SET
                     market=excluded.market, outcome=excluded.outcome, shares=excluded.shares,
                     avg_entry_price=excluded.avg_entry_price, updated_ts=excluded.updated_ts""",
                (event_id, token_id, market, outcome, shares, avg_entry_price, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM positions WHERE event_id=? AND token_id=?", (event_id, token_id)
            ).fetchone()
            return dict(row)

    def event_positions(self, event_id: str) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                "SELECT * FROM positions WHERE event_id=? ORDER BY updated_ts DESC", (event_id,)
            )]

    def delete_position(self, event_id: str, token_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM positions WHERE event_id=? AND token_id=?", (event_id, token_id)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_event_positions(self, event_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM positions WHERE event_id=?", (event_id,))
            self._conn.commit()
