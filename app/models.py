from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any
from uuid import uuid4


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Source classification describes market mechanics only. Consensus weighting is
# equal by independent source family unless a versioned calibration artifact says
# otherwise; subjective brand weights are deliberately prohibited.
_EXCHANGE_SOURCES = {
    "polymarket", "betfair", "smarkets", "matchbook", "prophetx", "kalshi",
    "demoexchange",
}


def classify_source(name: str) -> tuple[float, bool]:
    """Return an equal consensus weight and whether the venue is an exchange."""
    source = canonical_source(name)
    return (1.0, any(fragment in source for fragment in _EXCHANGE_SOURCES))


def canonical_source(name: str) -> str:
    """Stable identity for one book across direct and aggregator adapters."""
    compact = re.sub(r"[^a-z0-9]+", "", (name or "").casefold())
    aliases = {
        "williamhill": "caesars",
        "williamhillus": "caesars",
    }
    return aliases.get(compact, compact or "unknown")


@dataclass(slots=True)
class Event:
    name: str
    sport: str
    home: str
    away: str
    league: str = ""
    polymarket_slug: str | None = None
    polymarket_url: str | None = None
    polymarket_restricted: bool = False
    odds_api_sport: str | None = None
    odds_api_event_id: str | None = None
    game_start: str | None = None
    canonical_event_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class GameState:
    event_id: str
    home_score: float
    away_score: float
    period: str
    clock: str
    source: str
    # `observed_at` is a compatibility alias. New code must use the three
    # provenance timestamps below and never substitute receipt time for provider time.
    observed_at: datetime | None = None
    possession: str | None = None
    status: str = "in_progress"
    provider_timestamp: datetime | None = None
    received_at: datetime = field(default_factory=now_utc)
    processed_at: datetime = field(default_factory=now_utc)
    timestamp_trusted: bool = False
    quarantined: bool = False
    quarantine_reason: str | None = None
    provider_event_id: str | None = None
    canonical_event_id: str | None = None
    league_id: str | None = None
    sport_id: str | None = None
    home_team_id: str | None = None
    away_team_id: str | None = None
    regulation_period: int | None = None
    overtime_number: int | None = None
    normalized_seconds_remaining: float | None = None
    clock_direction: str | None = None
    live: bool | None = None
    ended: bool | None = None
    sequence: int | None = None
    state_hash: str | None = None
    state_schema_version: str = "game-state-v2"
    finished_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.provider_timestamp is None and self.observed_at is not None:
            self.provider_timestamp = self.observed_at
        if self.observed_at is None:
            self.observed_at = self.provider_timestamp or self.received_at
        self.timestamp_trusted = self.provider_timestamp is not None


@dataclass(slots=True)
class Quote:
    event_id: str
    market: str
    outcome: str
    probability: float
    source: str
    observed_at: datetime | None = None
    decimal_odds: float | None = None
    bid: float | None = None
    ask: float | None = None
    liquidity: float | None = None
    market_liquidity: float | None = None
    token_id: str | None = None
    market_slug: str | None = None
    question: str | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    min_order_size: float | None = None
    tick_size: float | None = None
    accepting_orders: bool = True
    provider_timestamp: datetime | None = None
    received_at: datetime = field(default_factory=now_utc)
    processed_at: datetime = field(default_factory=now_utc)
    timestamp_trusted: bool = False
    quarantined: bool = False
    quarantine_reason: str | None = None
    source_family: str = ""
    book_hash: str | None = None
    sequence: int | None = None
    depth_complete: bool = False
    fee_rate: float | None = None
    fee_schedule_id: str | None = None
    bid_levels: tuple[tuple[float, float], ...] = ()
    ask_levels: tuple[tuple[float, float], ...] = ()
    internal_quote_id: str = field(default_factory=lambda: str(uuid4()))
    provider_source_id: str | None = None
    provider_event_id: str | None = None
    canonical_event_id: str | None = None
    provider_market_id: str | None = None
    condition_id: str | None = None
    market_scope: str = "unknown"
    line: float | None = None
    outcome_id: str | None = None
    outcome_label: str | None = None
    active: bool = True
    resolved: bool = False
    restricted: bool = False
    negative_risk: bool | None = None
    raw_payload_hash: str | None = None
    normalization_version: str = "quote-v2"
    mapping_decision_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider_timestamp is None and self.observed_at is not None:
            self.provider_timestamp = self.observed_at
        if self.observed_at is None:
            self.observed_at = self.provider_timestamp or self.received_at
        self.timestamp_trusted = self.provider_timestamp is not None
        if not self.source_family:
            self.source_family = canonical_source(self.source)
        if self.provider_source_id is None:
            self.provider_source_id = self.source
        if self.outcome_label is None:
            self.outcome_label = self.outcome
        if self.raw_payload_hash is None:
            self.raw_payload_hash = self.book_hash

    @property
    def executable_probability(self) -> float:
        return self.ask if self.ask is not None else self.probability


@dataclass(slots=True)
class Signal:
    event_id: str
    market: str
    outcome: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    action: str
    reasons: list[str]
    observed_at: datetime = field(default_factory=now_utc)
    quote_source: str = ""
    # Phase 0 auditable fields from the Rust engine.
    market_fair_prob: float = 0.0
    devig_method: str = ""
    overround: float = 1.0
    n_reference_sources: int = 0
    # Phase 2a: independent live win-probability (moneyline only), or None.
    model_live_prob: float | None = None
    # Phase 4: sizing & risk-normalized gating.
    ev_per_stake: float = 0.0
    kelly_fraction: float = 0.0
    required_edge: float = 0.0
    fair_stderr: float = 0.0
    fillable_size: float | None = None
    quality_freshness: float = 0.0
    quality_agreement: float = 0.0
    quality_sources: float = 0.0
    quality_execution: float = 0.0
    quality_calibration: float = 0.0
    quality_data_completeness: float = 0.0
    quality_provider_freshness: float = 0.0
    quality_identity: float = 0.0
    quality_model_sample_support: float = 0.0
    quality_calibration_support: float = 0.0
    quality_source_independence: float = 0.0
    decision_hash: str = ""
    requested_cash: float | None = None
    filled_cash: float | None = None
    filled_shares: float | None = None
    execution_fee: float | None = None
    execution_vwap: float | None = None
    execution_complete: bool = False
    # Reproducibility lineage.  The legacy transport names above remain for
    # compatibility; these fields are canonical for persisted decisions.
    decision_id: str = ""
    engine_version: str = ""
    configuration_hash: str = ""
    source_mapping_version: str = ""
    model_version: str = ""
    calibration_version: str = ""
    independent_model_registry_version: str = ""
    execution_policy_version: str = ""
    input_snapshot_json: str = ""
    token_id: str | None = None
    order_book_snapshot_id: str | None = None
    # Canonical Milestone E outputs. The legacy transport fields above remain
    # populated for old ledger/API readers but must not be relabelled in new UI.
    consensus_probability: float = 0.0
    calibrated_consensus_probability: float | None = None
    independent_model_probability: float | None = None
    independent_model_version: str | None = None
    independent_model_hash: str | None = None
    independent_calibration_version: str | None = None
    independent_calibration_hash: str | None = None
    independent_model_sample_size: int = 0
    independent_model_event_count: int = 0
    uncertainty_low: float | None = None
    uncertainty_high: float | None = None
    probability_net_ev_positive: float | None = None
    net_expected_value_per_share: float | None = None
    net_expected_value_total: float | None = None
    consensus_method: str = ""
    model_sample_size: int = 0
    calibration_sample_size: int = 0
    gate_results: list[dict[str, Any]] = field(default_factory=list)


def as_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: as_json(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, (list, tuple)):
        return [as_json(item) for item in value]
    if isinstance(value, dict):
        return {key: as_json(item) for key, item in value.items()}
    return value

