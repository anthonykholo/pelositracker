"""Thin Python adapter around the Rust signal engine."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any

from .calibration import CalibrationArtifact
from .model_registry import IndependentModelArtifact
from .gameclock import clock_seconds, game_progress
from .domain.time import ensure_utc
from .lines import is_spread_market, is_total_market, quote_line_side
from .models import GameState, Quote, Signal, canonical_source, classify_source
from .execution import BookLevel, simulate_buy


ENGINE_VERSION = "live-edge-engine-0.6.0"
REQUEST_SCHEMA_VERSION = "decision-request-v4"
SOURCE_MAPPING_VERSION = "canonical-source-family-v1"
DEFAULT_MODEL_VERSION = "equal-family-logit-consensus-v2-display-only"
EXECUTION_POLICY_VERSION = "paper-depth-v1"

try:
    from ._native_engine import evaluate_json
except ImportError as exc:  # pragma: no cover - exercised only before native build
    raise ImportError(
        "The Rust engine is not built. Run: .\\.venv\\Scripts\\python.exe -m "
        "maturin develop --release"
    ) from exc


class SignalEngine:
    """Python-facing configuration wrapper for the Rust recommendation engine."""

    def __init__(self, confidence_threshold: float = 72, edge_threshold: float = 0.035,
                 max_age_seconds: float = 20, kelly_fraction: float = 0.25,
                 edge_z: float = 1.0, enable_independent_model: bool = False):
        self.confidence_threshold = confidence_threshold
        self.edge_threshold = edge_threshold
        self.max_age_seconds = max_age_seconds
        self.kelly_fraction = kelly_fraction
        # ``edge_z`` remains an accepted constructor argument for old callers,
        # but dispersion-based pseudo-standard-errors are intentionally retired.
        self.enable_independent_model = enable_independent_model
        self.paper_notional = 100.0
        self.allow_fixture_policies = False
        self.calibrated_markets: set[str] = set()
        self.calibration_artifact: CalibrationArtifact | None = None
        self.model_policy_overrides: dict[str, dict[str, Any]] = {}
        self.model_version = DEFAULT_MODEL_VERSION
        self.calibration_version = "unavailable"
        self.independent_model_artifact: IndependentModelArtifact | None = None
        self.independent_model_registry_version = "unavailable"

    def install_calibration(self, artifact: CalibrationArtifact) -> None:
        """Install a validated artifact without making legacy v1 files actionable."""
        self.calibration_artifact = artifact
        self.model_version = artifact.model_version
        self.calibration_version = (
            f"{artifact.artifact_version}:{artifact.calibration_method}:"
            f"{artifact.model_hash[:12] or 'legacy-display-only'}"
        )

    def install_independent_models(self, artifact: IndependentModelArtifact) -> None:
        """Install reviewed display-only sport-model policies."""
        self.independent_model_artifact = artifact
        self.independent_model_registry_version = (
            f"{artifact.artifact_version}:{artifact.artifact_hash[:12]}"
        )

    @staticmethod
    def _canonical_model_market(market: str) -> str:
        key = market.strip().casefold()
        if is_spread_market(key):
            return "spread"
        if is_total_market(key):
            return "total"
        if key in {"moneyline", "h2h", "winner", "match_winner"}:
            return "moneyline"
        return key

    def _independent_model_policies(
        self, quotes: list[Quote], sport: str, league: str
    ) -> list[dict[str, Any]]:
        artifact = self.independent_model_artifact
        if not self.enable_independent_model or artifact is None:
            return []
        policies = []
        markets = sorted({self._canonical_model_market(quote.market) for quote in quotes})
        for market in markets:
            policy = artifact.policy_for(sport, league, market)
            if policy is not None:
                payload = policy.to_engine_dict()
                payload["registry_artifact_hash"] = artifact.artifact_hash
                policies.append(payload)
        return policies

    @staticmethod
    def _legacy_test_policy(market: str) -> dict[str, Any]:
        """Compatibility for deterministic unit/replay fixtures only.

        Production configuration never populates ``calibrated_markets``. This
        explicit override keeps old replay fixtures useful without turning a
        version-1 display allowlist into deployable statistical evidence.
        """
        return {
            "market": market.strip().casefold(),
            "devig_method": "shin",
            "consensus_method": "equal_family_logit",
            "calibration_method": "identity",
            "beta_coefficients": [1.0, 1.0, 0.0],
            "beta_bootstrap_coefficients": [[1.0, 1.0, 0.0]] * 200,
            "execution_cost_offsets": [0.0] * 200,
            "min_probability_positive": 0.95,
            "min_expected_value_dollars": 0.0,
            "sample_size": 1000,
            "model_sample_size": 1000,
            "sharp_source_family": None,
            "consensus_intercept": 0.0,
            "family_coefficients": {},
            "missing_family_coefficients": {},
            "policy_source": "explicit-test-override",
        }

    def _model_policies(self, quotes: list[Quote], sport: str, league: str) -> list[dict]:
        policies: dict[str, dict[str, Any]] = {
            market.strip().casefold(): dict(policy)
            for market, policy in self.model_policy_overrides.items()
        }
        artifact = self.calibration_artifact
        if artifact is not None and artifact.eligible_for_action:
            for market in sorted({quote.market.strip().casefold() for quote in quotes}):
                policy = artifact.policy_for(sport, league, market)
                if policy is not None:
                    policies[market] = policy.to_engine_dict()
                    policies[market]["policy_source"] = "versioned-artifact"
        if self.allow_fixture_policies:
            for market in self.calibrated_markets:
                key = market.strip().casefold()
                policies.setdefault(key, self._legacy_test_policy(key))
        return [policies[key] for key in sorted(policies)]

    def evaluate(self, event_id: str, quotes: list[Quote], states: list[GameState],
                 away_outcome: str = "away", sport: str = "", league: str = "",
                 home_outcome: str = "",
                 pregame_spread: float | None = None,
                 pregame_total: float | None = None,
                 *, as_of: datetime | None = None,
                 canonical_event_id: str | None = None) -> list[Signal]:
        if as_of is None:
            raise ValueError("as_of is required for deterministic evaluation")
        as_of = ensure_utc(as_of)
        model_policies = self._model_policies(quotes, sport, league)
        independent_model_policies = self._independent_model_policies(
            quotes, sport, league
        )
        quote_payloads = [
            self._quote_payload(q, home_outcome, away_outcome, self.paper_notional)
            for q in quotes
        ]
        configuration = {
            "confidence_threshold": self.confidence_threshold,
            "edge_threshold": self.edge_threshold,
            "max_age_seconds": self.max_age_seconds,
            "kelly_fraction": self.kelly_fraction,
            "paper_notional": self.paper_notional,
            "enable_independent_model": bool(independent_model_policies),
            "model_policies": model_policies,
            "independent_model_policies": independent_model_policies,
        }
        canonical_configuration = json.dumps(
            configuration, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        configuration_hash = hashlib.sha256(
            canonical_configuration.encode("utf-8")
        ).hexdigest()
        request = {
            "request_schema_version": REQUEST_SCHEMA_VERSION,
            "as_of": as_of.timestamp(),
            "event_id": event_id,
            "canonical_event_id": canonical_event_id,
            **configuration,
            "away_outcome": away_outcome,
            "sport": sport or None,
            "league": league or None,
            "pregame_spread": pregame_spread,
            "pregame_total": pregame_total,
            "lineage": {
                "engine_version": ENGINE_VERSION,
                "configuration_hash": configuration_hash,
                "source_mapping_version": SOURCE_MAPPING_VERSION,
                "model_version": self.model_version,
                "calibration_version": self.calibration_version,
                "independent_model_registry_version": (
                    self.independent_model_registry_version
                ),
                "execution_policy_version": EXECUTION_POLICY_VERSION,
            },
            "quotes": quote_payloads,
            "states": [
                self._state_payload(s, sport, league)
                for s in states
            ],
        }
        canonical_request = json.dumps(request, separators=(",", ":"), sort_keys=True,
                                       allow_nan=False)
        decision_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        results = json.loads(evaluate_json(canonical_request))
        signals = []
        for result in results:
            audit = next((payload for payload in quote_payloads
                          if payload["market"] == result["market"]
                          and payload["outcome"] == result["outcome"]
                          and payload["source"] == result["quote_source"]), {})
            decision_id = hashlib.sha256(
                f"{decision_hash}:{result['market']}:{result['outcome']}".encode("utf-8")
            ).hexdigest()
            signals.append(Signal(
                **result,
                observed_at=as_of,
                decision_hash=decision_hash,
                requested_cash=audit.get("requested_cash"),
                filled_cash=audit.get("filled_cash"),
                filled_shares=audit.get("filled_shares"),
                execution_fee=audit.get("execution_fee"),
                execution_vwap=audit.get("execution_vwap"),
                execution_complete=bool(audit.get("execution_complete")),
                decision_id=decision_id,
                engine_version=ENGINE_VERSION,
                configuration_hash=configuration_hash,
                source_mapping_version=SOURCE_MAPPING_VERSION,
                model_version=self.model_version,
                calibration_version=self.calibration_version,
                independent_model_registry_version=(
                    self.independent_model_registry_version
                ),
                execution_policy_version=EXECUTION_POLICY_VERSION,
                input_snapshot_json=canonical_request,
                token_id=audit.get("token_id"),
                order_book_snapshot_id=audit.get("book_hash"),
            ))
        return signals

    @staticmethod
    def _state_payload(s: GameState, sport: str, league: str) -> dict:
        regulation_seconds, fraction_remaining = game_progress(
            sport, s.period, s.clock, league
        )
        overtime_number = 0 if regulation_seconds is not None else s.overtime_number
        seconds_remaining = regulation_seconds
        if seconds_remaining is None and overtime_number is not None and overtime_number > 0:
            seconds_remaining = (
                s.normalized_seconds_remaining
                if s.normalized_seconds_remaining is not None
                else clock_seconds(s.clock)
            )
        possession = (s.possession or "").strip().casefold()
        home_identity = (s.home_team_id or "").strip().casefold()
        away_identity = (s.away_team_id or "").strip().casefold()
        possession_home = None
        if possession in {"home", "h"} or (home_identity and possession == home_identity):
            possession_home = 1.0
        elif possession in {"away", "a"} or (away_identity and possession == away_identity):
            possession_home = 0.0
        independent_inputs_valid = (
            seconds_remaining is not None
            and overtime_number is not None
            and possession_home is not None
        )
        return {
            "home_score": s.home_score,
            "away_score": s.away_score,
            "observed_at": s.observed_at.timestamp(),
            "provider_timestamp": (s.provider_timestamp.timestamp()
                                   if s.provider_timestamp else None),
            "received_at": s.received_at.timestamp(),
            "processed_at": s.processed_at.timestamp(),
            "source": s.source,
            "fraction_remaining": fraction_remaining,
            "seconds_remaining": seconds_remaining,
            "overtime_number": overtime_number,
            "timestamp_trusted": s.timestamp_trusted,
            "state_valid": not s.quarantined and independent_inputs_valid,
            "possession": s.possession,
            "possession_home": possession_home,
        }

    @staticmethod
    def _quote_payload(q: Quote, home_outcome: str = "", away_outcome: str = "",
                       paper_notional: float = 100.0) -> dict:
        weight, is_exchange = classify_source(q.source)
        executable_ask = q.ask
        executable_size = q.ask_size
        execution_complete = not is_exchange
        fee_metadata_known = not is_exchange
        requested_cash = None
        filled_cash = None
        filled_shares = None
        execution_fee = None
        execution_vwap = None
        if is_exchange and q.depth_complete:
            levels = [BookLevel.create(price, size) for price, size in q.ask_levels]
            simulation = simulate_buy(
                levels, cash=paper_notional, fee_rate=q.fee_rate,
                tick_size=q.tick_size, min_order_size=q.min_order_size,
                active=q.active, resolved=q.resolved, restricted=q.restricted,
                accepting_orders=q.accepting_orders, depth_complete=q.depth_complete,
                identity_ambiguous=q.quarantined,
            )
            execution_complete = simulation.complete
            fee_metadata_known = q.fee_rate is not None
            requested_cash = float(simulation.requested_cash)
            filled_cash = float(simulation.filled_cash)
            filled_shares = float(simulation.filled_shares)
            execution_fee = float(simulation.fee)
            execution_vwap = float(simulation.vwap) if simulation.vwap is not None else None
            if simulation.complete and simulation.effective_probability is not None:
                executable_ask = float(simulation.effective_probability)
                executable_size = float(simulation.filled_shares)
        point, side = quote_line_side(q.market, q.outcome, home_outcome, away_outcome)
        market_key, outcome_key = SignalEngine._comparison_keys(
            q.market, q.outcome, home_outcome, away_outcome, point, side
        )
        return {
            "market": q.market,
            "outcome": q.outcome,
            "comparison_market": market_key,
            "comparison_outcome": outcome_key,
            "comparison_source": SignalEngine._source_key(q.source),
            "probability": q.probability,
            "source": q.source,
            "observed_at": q.observed_at.timestamp(),
            "provider_timestamp": (q.provider_timestamp.timestamp()
                                   if q.provider_timestamp else None),
            "received_at": q.received_at.timestamp(),
            "processed_at": q.processed_at.timestamp(),
            "timestamp_trusted": q.timestamp_trusted and not q.quarantined,
            "identity_valid": not q.quarantined,
            "bid": q.bid,
            "ask": executable_ask,
            "source_weight": weight,
            "is_exchange": is_exchange,
            "decimal_odds": q.decimal_odds,
            "liquidity": q.liquidity,
            "ask_size": executable_size,
            "depth_complete": q.depth_complete and execution_complete,
            "fee_metadata_known": fee_metadata_known,
            "accepting_orders": (q.accepting_orders and q.active
                                 and not q.resolved and not q.restricted),
            "requested_cash": requested_cash,
            "filled_cash": filled_cash,
            "filled_shares": filled_shares,
            "execution_fee": execution_fee,
            "execution_vwap": execution_vwap,
            "execution_complete": execution_complete,
            "token_id": q.token_id,
            "book_hash": q.book_hash,
            "point": point,
            "side": side,
        }

    @staticmethod
    def _source_key(source: str) -> str:
        """Canonicalize one underlying book across direct and aggregator feeds."""
        return canonical_source(source)

    @staticmethod
    def _comparison_keys(market: str, outcome: str, home: str, away: str,
                         point: float | None, side: str | None) -> tuple[str, str]:
        """Return stable cross-provider keys without changing display labels.

        The line is part of the market identity. Grouping every alternate
        spread/total together makes de-vigging treat many unrelated two-way
        books as one giant market and can inflate overround above 500%.
        """
        market_key = (market or "market").strip().casefold()
        outcome_key = (outcome or "").strip().casefold()
        if is_spread_market(market_key) and point is not None and side in {"home", "away"}:
            home_line = point if side == "home" else -point
            if abs(home_line) < 1e-9:
                home_line = 0.0
            return f"spread:{home_line:g}", side
        if is_total_market(market_key) and point is not None and side in {"over", "under"}:
            return f"total:{point:g}", side
        if point is not None and side in {"over", "under"}:
            return f"{market_key}:{point:g}", side
        if outcome_key in {"home", (home or "").strip().casefold()}:
            outcome_key = "home"
        elif outcome_key in {"away", (away or "").strip().casefold()}:
            outcome_key = "away"
        return market_key, outcome_key

