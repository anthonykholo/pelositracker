import threading
import time
import json
from typing import Iterable

from .database import Database
from .models import Event, GameState, Quote
from .identity import CanonicalEvent, MappingDecision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_outcomes (
    event_id TEXT PRIMARY KEY,
    name TEXT,
    sport TEXT,
    home TEXT,
    away TEXT,
    league TEXT,
    polymarket_slug TEXT,
    pregame_spread DOUBLE PRECISION,
    pregame_total DOUBLE PRECISION,
    final_home_score DOUBLE PRECISION,
    final_away_score DOUBLE PRECISION,
    final_status TEXT,
    settled_ts DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS quotes_history (
    id SERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT,
    probability DOUBLE PRECISION NOT NULL,
    ask DOUBLE PRECISION,
    bid DOUBLE PRECISION,
    liquidity DOUBLE PRECISION,
    observed_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_event ON quotes_history(event_id, observed_at);

CREATE TABLE IF NOT EXISTS states_history (
    id SERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    home_score DOUBLE PRECISION NOT NULL,
    away_score DOUBLE PRECISION NOT NULL,
    period TEXT,
    clock TEXT,
    status TEXT,
    observed_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_states_event ON states_history(event_id, observed_at);

CREATE TABLE IF NOT EXISTS canonical_events (
    canonical_event_id TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    league TEXT NOT NULL,
    starts_at DOUBLE PRECISION,
    home_participant_id TEXT NOT NULL,
    away_participant_id TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_participants (
    participant_id TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    UNIQUE(sport, canonical_name)
);
CREATE TABLE IF NOT EXISTS canonical_markets (
    market_id TEXT PRIMARY KEY,
    canonical_event_id TEXT NOT NULL,
    market_type TEXT NOT NULL,
    line_value TEXT,
    period_scope TEXT NOT NULL,
    UNIQUE(canonical_event_id, market_type, line_value, period_scope)
);
CREATE TABLE IF NOT EXISTS canonical_outcomes (
    outcome_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    outcome_key TEXT NOT NULL,
    UNIQUE(market_id, outcome_key)
);
CREATE TABLE IF NOT EXISTS provider_mappings (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_object_type TEXT NOT NULL,
    provider_object_id TEXT NOT NULL,
    canonical_id TEXT,
    status TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL,
    decided_at DOUBLE PRECISION NOT NULL,
    UNIQUE(provider, provider_object_type, provider_object_id)
);
"""

def _now() -> float:
    return time.time()

class HistoryDB:
    def __init__(self, path: str | None = None):
        self._db = Database.open(
            path, sqlite_envs=("HISTORY_DB",), sqlite_default="history.db"
        )
        self.path = self._db.target
        self.backend = self._db.backend
        self._conn = self._db.connection
        self._lock = threading.Lock()
        with self._lock:
            self._db.initialize(_SCHEMA, component="history", version=1)
            self._db.migrate_columns("history", 2, {
                "quotes_history": {
                    "provider_timestamp": "DOUBLE PRECISION",
                    "received_at": "DOUBLE PRECISION",
                    "processed_at": "DOUBLE PRECISION",
                    "source_family": "TEXT",
                    "decimal_odds": "DOUBLE PRECISION",
                    "bid_size": "DOUBLE PRECISION",
                    "ask_size": "DOUBLE PRECISION",
                    "market_liquidity": "DOUBLE PRECISION",
                    "token_id": "TEXT",
                    "market_slug": "TEXT",
                    "min_order_size": "DOUBLE PRECISION",
                    "tick_size": "DOUBLE PRECISION",
                    "accepting_orders": "INTEGER",
                    "book_hash": "TEXT",
                    "sequence": "BIGINT",
                    "depth_complete": "INTEGER",
                    "fee_rate": "DOUBLE PRECISION",
                    "fee_schedule_id": "TEXT",
                    "quarantined": "INTEGER",
                    "quarantine_reason": "TEXT",
                    "bid_levels_json": "TEXT",
                    "ask_levels_json": "TEXT",
                },
                "states_history": {
                    "source": "TEXT",
                    "possession": "TEXT",
                    "provider_timestamp": "DOUBLE PRECISION",
                    "received_at": "DOUBLE PRECISION",
                    "processed_at": "DOUBLE PRECISION",
                    "quarantined": "INTEGER",
                    "quarantine_reason": "TEXT",
                },
            })
            self._db.migrate_columns("history", 3, {
                "quotes_history": {
                    "internal_quote_id": "TEXT",
                    "provider_source_id": "TEXT",
                    "provider_event_id": "TEXT",
                    "canonical_event_id": "TEXT",
                    "provider_market_id": "TEXT",
                    "condition_id": "TEXT",
                    "market_scope": "TEXT",
                    "line_value": "DOUBLE PRECISION",
                    "outcome_id": "TEXT",
                    "outcome_label": "TEXT",
                    "active": "INTEGER",
                    "resolved": "INTEGER",
                    "restricted": "INTEGER",
                    "raw_payload_hash": "TEXT",
                    "normalization_version": "TEXT",
                    "mapping_decision_id": "TEXT",
                },
                "states_history": {
                    "provider_event_id": "TEXT",
                    "canonical_event_id": "TEXT",
                    "league_id": "TEXT",
                    "sport_id": "TEXT",
                    "home_team_id": "TEXT",
                    "away_team_id": "TEXT",
                    "regulation_period": "INTEGER",
                    "overtime_number": "INTEGER",
                    "normalized_seconds_remaining": "DOUBLE PRECISION",
                    "clock_direction": "TEXT",
                    "live": "INTEGER",
                    "ended": "INTEGER",
                    "sequence": "BIGINT",
                    "state_hash": "TEXT",
                    "state_schema_version": "TEXT",
                },
            })
            self._db.migrate_columns("history", 4, {
                "quotes_history": {"negative_risk": "INTEGER"},
                "states_history": {"finished_timestamp": "DOUBLE PRECISION"},
            })
            self._db.migrate_columns("history", 5, {
                "provider_mappings": {
                    "orientation": "TEXT",
                    "algorithm_version": "TEXT",
                    "decision_threshold": "DOUBLE PRECISION",
                    "human_override": "INTEGER",
                    "evidence_json": "TEXT",
                },
            })

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def log_quotes(self, quotes: Iterable[Quote]) -> None:
        with self._lock:
            rows = []
            for q in quotes:
                rows.append((
                    q.event_id, q.market, q.outcome, q.source,
                    q.probability, q.ask, q.bid, q.liquidity,
                    q.observed_at.timestamp(),
                    q.provider_timestamp.timestamp() if q.provider_timestamp else None,
                    q.received_at.timestamp(), q.processed_at.timestamp(), q.source_family,
                    q.decimal_odds, q.bid_size, q.ask_size, q.market_liquidity,
                    q.token_id, q.market_slug, q.min_order_size, q.tick_size,
                    int(q.accepting_orders), q.book_hash, q.sequence,
                    int(q.depth_complete), q.fee_rate, q.fee_schedule_id,
                    int(q.quarantined), q.quarantine_reason,
                    json.dumps(q.bid_levels, separators=(",", ":")),
                    json.dumps(q.ask_levels, separators=(",", ":")),
                    q.internal_quote_id, q.provider_source_id, q.provider_event_id,
                    q.canonical_event_id, q.provider_market_id, q.condition_id,
                    q.market_scope, q.line, q.outcome_id, q.outcome_label,
                    int(q.active), int(q.resolved), int(q.restricted),
                    q.raw_payload_hash, q.normalization_version, q.mapping_decision_id,
                    int(q.negative_risk) if q.negative_risk is not None else None,
                ))

            if not rows:
                return

            with self._db.transaction() as cur:
                self._db.execute_many(
                    cur,
                    """INSERT INTO quotes_history
                       (event_id, market, outcome, source, probability, ask, bid, liquidity,
                        observed_at, provider_timestamp, received_at, processed_at,
                        source_family, decimal_odds, bid_size, ask_size, market_liquidity,
                        token_id, market_slug, min_order_size, tick_size, accepting_orders,
                        book_hash, sequence, depth_complete, fee_rate, fee_schedule_id,
                        quarantined, quarantine_reason, bid_levels_json, ask_levels_json,
                        internal_quote_id, provider_source_id, provider_event_id,
                        canonical_event_id, provider_market_id, condition_id, market_scope,
                        line_value, outcome_id, outcome_label, active, resolved, restricted,
                        raw_payload_hash, normalization_version, mapping_decision_id,
                        negative_risk)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows,
                )

    def log_state(self, state: GameState) -> None:
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO states_history
                       (event_id, home_score, away_score, period, clock, status, observed_at,
                        source, possession, provider_timestamp, received_at, processed_at,
                        quarantined, quarantine_reason, provider_event_id,
                        canonical_event_id, league_id, sport_id, home_team_id,
                        away_team_id, regulation_period, overtime_number,
                        normalized_seconds_remaining, clock_direction, live, ended,
                        sequence, state_hash, state_schema_version, finished_timestamp)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (state.event_id, state.home_score, state.away_score, 
                     state.period, state.clock, state.status, state.observed_at.timestamp(),
                     state.source, state.possession,
                     state.provider_timestamp.timestamp() if state.provider_timestamp else None,
                     state.received_at.timestamp(), state.processed_at.timestamp(),
                     int(state.quarantined), state.quarantine_reason,
                     state.provider_event_id, state.canonical_event_id, state.league_id,
                     state.sport_id, state.home_team_id, state.away_team_id,
                     state.regulation_period, state.overtime_number,
                     state.normalized_seconds_remaining, state.clock_direction,
                     int(state.live) if state.live is not None else None,
                     int(state.ended) if state.ended is not None else None,
                     state.sequence, state.state_hash, state.state_schema_version,
                     state.finished_timestamp.timestamp() if state.finished_timestamp else None)
                )

    def log_outcome(self, event: Event, pregame_spread: float | None, pregame_total: float | None, final_state: GameState | None) -> None:
        now = _now()
        home_score = final_state.home_score if final_state else None
        away_score = final_state.away_score if final_state else None
        status = final_state.status if final_state else None
        
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO event_outcomes 
                       (event_id, name, sport, home, away, league, polymarket_slug, 
                        pregame_spread, pregame_total, final_home_score, final_away_score, final_status, settled_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(event_id) DO UPDATE SET
                         name=EXCLUDED.name, sport=EXCLUDED.sport, home=EXCLUDED.home, away=EXCLUDED.away,
                         league=EXCLUDED.league, polymarket_slug=EXCLUDED.polymarket_slug,
                         pregame_spread=EXCLUDED.pregame_spread, pregame_total=EXCLUDED.pregame_total,
                         final_home_score=EXCLUDED.final_home_score, final_away_score=EXCLUDED.final_away_score,
                         final_status=EXCLUDED.final_status, settled_ts=EXCLUDED.settled_ts""",
                    (event.id, event.name, event.sport, event.home, event.away, event.league, event.polymarket_slug,
                     pregame_spread, pregame_total, home_score, away_score, status, now)
                )

    def log_event_identity(self, canonical: CanonicalEvent,
                           mapping: MappingDecision | None = None) -> None:
        now = _now()
        with self._lock:
            with self._db.transaction() as cur:
                for participant in (canonical.home, canonical.away):
                    self._db.execute(
                        cur,
                        """INSERT INTO canonical_participants
                           (participant_id, sport, canonical_name) VALUES (%s,%s,%s)
                           ON CONFLICT(participant_id) DO NOTHING""",
                        (participant.participant_id, participant.sport,
                         participant.canonical_name),
                    )
                self._db.execute(
                    cur,
                    """INSERT INTO canonical_events
                       (canonical_event_id, sport, league, starts_at,
                        home_participant_id, away_participant_id, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(canonical_event_id) DO NOTHING""",
                    (canonical.canonical_event_id, canonical.sport, canonical.league,
                     canonical.starts_at.timestamp() if canonical.starts_at else None,
                     canonical.home.participant_id, canonical.away.participant_id, now),
                )
                if mapping is not None:
                    self._db.execute(
                        cur,
                        """INSERT INTO provider_mappings
                           (provider, provider_object_type, provider_object_id,
                            canonical_id, status, confidence, reason, decided_at,
                            orientation, algorithm_version, decision_threshold,
                            human_override, evidence_json)
                           VALUES (%s,'event',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(provider, provider_object_type, provider_object_id)
                           DO UPDATE SET canonical_id=EXCLUDED.canonical_id,
                             status=EXCLUDED.status, confidence=EXCLUDED.confidence,
                             reason=EXCLUDED.reason, decided_at=EXCLUDED.decided_at,
                             orientation=EXCLUDED.orientation,
                             algorithm_version=EXCLUDED.algorithm_version,
                             decision_threshold=EXCLUDED.decision_threshold,
                             human_override=EXCLUDED.human_override,
                             evidence_json=EXCLUDED.evidence_json""",
                        (mapping.provider, mapping.provider_object_id, mapping.canonical_id,
                         mapping.status.value, mapping.confidence, mapping.reason, now,
                         mapping.orientation, mapping.algorithm_version, mapping.threshold,
                         int(mapping.human_override), mapping.evidence_json),
                    )

    def get_event_history(
        self, event_id: str, *, after_ts: float | None = None,
        limit: int | None = None,
    ) -> dict:
        """Fetch chronological quotes and states for an event for charting.

        ``after_ts`` restricts the window to observations at or after a
        timestamp (for incremental fetches), and ``limit`` bounds each series to
        its most-recent N points. Both default to ``None`` (return the full
        history, unchanged) but let a caller cap peak memory so it scales with
        the requested page size rather than the total rows an event accumulated.
        """
        quotes_rows = self._history_series(
            "SELECT market, outcome, probability, observed_at FROM quotes_history",
            event_id, after_ts=after_ts, limit=limit,
        )
        states_rows = self._history_series(
            "SELECT home_score, away_score, status, observed_at FROM states_history",
            event_id, after_ts=after_ts, limit=limit,
        )
        return {"quotes": quotes_rows, "states": states_rows}

    def _history_series(
        self, select: str, event_id: str, *,
        after_ts: float | None, limit: int | None,
    ) -> list[dict]:
        where = "event_id=%s"
        params: list = [event_id]
        if after_ts is not None:
            where += " AND observed_at >= %s"
            params.append(after_ts)
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                if limit is not None:
                    # Take the most recent N at the database, then restore
                    # ascending order for the chart, so a long event never
                    # hydrates its entire history into Python.
                    params.append(limit)
                    self._db.execute(
                        cur,
                        f"{select} WHERE {where} ORDER BY observed_at DESC LIMIT %s",
                        tuple(params),
                    )
                    return [dict(r) for r in reversed(cur.fetchall())]
                self._db.execute(
                    cur, f"{select} WHERE {where} ORDER BY observed_at ASC",
                    tuple(params),
                )
                return [dict(r) for r in cur.fetchall()]
