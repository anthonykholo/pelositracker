"""Fail-closed registry for independently validated sport models.

The repository ships no artifact.  This module only defines the evidence and
runtime contract that a separately reviewed model must satisfy before its
probability may be displayed.  Independent output remains a cross-check and
never bypasses consensus, execution, uncertainty, or portfolio gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .calibration import BetaCoefficients
from .domain.time import parse_provider_timestamp


MIN_TEST_SAMPLE = 1000
MIN_TEST_EVENTS = 200
MIN_BOOTSTRAP_DRAWS = 1000
MIN_COMPARISON_SUPPORT = 0.95
MIN_SLICE_SAMPLE = 100
FEATURE_SCHEMA_VERSION = "independent-features-v1"
STATE_SCHEMA_VERSION = "game-state-v2"
_SUPPORTED_MODEL = "basketball_game_state_logit_v1"
_SUPPORTED_LEAGUES = {"nba"}
_SUPPORTED_MARKETS = {"moneyline"}
_ALLOWED_INPUTS = {
    "home_score",
    "away_score",
    "seconds_remaining",
    "possession_home",
    "overtime_number",
    "provider_timestamp",
    "pregame_spread",
}
_COEFFICIENTS = {
    "intercept",
    "score_differential",
    "pregame_home_margin",
    "time_remaining_fraction",
    "score_time_interaction",
    "pregame_time_interaction",
    "home_possession",
    "overtime",
    "late_game",
}
_MODEL_PARAMETERS = _COEFFICIENTS | {
    "late_game_threshold",
    "regulation_seconds",
    "overtime_period_seconds",
}


def _key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a JSON number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a JSON integer")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    parsed = parse_provider_timestamp(value)
    if parsed is None:
        raise ValueError(f"{label} must be a valid UTC timestamp")
    return parsed


def _digest(value: Any, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be a lower-case SHA-256 digest")
    return digest


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    sample_size: int
    brier_score: float
    log_loss: float
    probability_brier_better: float
    probability_log_loss_better: float

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BaselineComparison":
        comparison = cls(
            sample_size=_integer(payload["sample_size"], "baseline sample size"),
            brier_score=_finite(payload["brier_score"], "baseline Brier score"),
            log_loss=_finite(payload["log_loss"], "baseline log loss"),
            probability_brier_better=_finite(
                payload["probability_brier_better"], "Brier bootstrap support"
            ),
            probability_log_loss_better=_finite(
                payload["probability_log_loss_better"], "log-loss bootstrap support"
            ),
        )
        comparison.validate()
        return comparison

    def validate(self) -> None:
        if self.sample_size < MIN_TEST_SAMPLE:
            raise ValueError("baseline comparison requires at least 1,000 observations")
        if not 0 <= self.brier_score <= 1 or self.log_loss < 0:
            raise ValueError("baseline proper scores are invalid")
        if not 0 <= self.probability_brier_better <= 1 or not (
            0 <= self.probability_log_loss_better <= 1
        ):
            raise ValueError("baseline bootstrap support must be a probability")


@dataclass(frozen=True, slots=True)
class CalibrationSlice:
    sample_size: int
    brier_score: float
    log_loss: float
    calibration_intercept: float
    calibration_slope: float

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CalibrationSlice":
        metric = cls(
            sample_size=_integer(payload["sample_size"], "slice sample size"),
            brier_score=_finite(payload["brier_score"], "slice Brier score"),
            log_loss=_finite(payload["log_loss"], "slice log loss"),
            calibration_intercept=_finite(
                payload["calibration_intercept"], "slice calibration intercept"
            ),
            calibration_slope=_finite(
                payload["calibration_slope"], "slice calibration slope"
            ),
        )
        metric.validate()
        return metric

    def validate(self) -> None:
        if self.sample_size < MIN_SLICE_SAMPLE:
            raise ValueError("calibration slices require at least 100 observations")
        if not 0 <= self.brier_score <= 1 or self.log_loss < 0:
            raise ValueError("calibration slice proper scores are invalid")
        if self.calibration_slope <= 0:
            raise ValueError("calibration slice slope must be positive")


@dataclass(frozen=True, slots=True)
class IndependentModelPolicy:
    model_id: str
    model_version: str
    model_hash: str
    training_data_hash: str
    sport: str
    league: str
    market: str
    model_type: str
    feature_schema_version: str
    state_schema_version: str
    required_inputs: frozenset[str]
    parameters: tuple[tuple[str, float], ...]
    calibration_method: str
    calibration_version: str
    calibration_hash: str
    beta_coefficients: BetaCoefficients
    trained_through: datetime
    validation_from: datetime
    validation_through: datetime
    evaluated_from: datetime
    evaluated_through: datetime
    test_sample_size: int
    test_event_count: int
    brier_score: float
    log_loss: float
    evaluation_method: str
    bootstrap_draws: int
    model_selection_candidates: int
    multiple_comparison_method: str
    comparisons: tuple[tuple[str, BaselineComparison], ...]
    calibration_slices: tuple[
        tuple[str, tuple[tuple[str, CalibrationSlice], ...]], ...
    ]
    missing_feature_behavior: str
    known_limitations: tuple[str, ...]
    approved_for_display: bool
    reviewed_by: str
    reviewed_at: datetime

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "IndependentModelPolicy":
        approved = payload["approved_for_display"]
        if not isinstance(approved, bool):
            raise ValueError("approved_for_display must be a JSON boolean")
        container_types = {
            "required_inputs": list,
            "parameters": dict,
            "comparisons": dict,
            "calibration_slices": dict,
            "known_limitations": list,
        }
        for name, expected_type in container_types.items():
            if not isinstance(payload[name], expected_type):
                raise ValueError(f"{name} has the wrong JSON type")
        if not all(isinstance(value, str) for value in payload["required_inputs"]):
            raise ValueError("required_inputs must contain only strings")
        if not all(isinstance(value, str) for value in payload["known_limitations"]):
            raise ValueError("known_limitations must contain only strings")
        policy = cls(
            model_id=str(payload["model_id"]).strip(),
            model_version=str(payload["model_version"]).strip(),
            model_hash=_digest(payload["model_hash"], "model hash"),
            training_data_hash=_digest(payload["training_data_hash"], "training data hash"),
            sport=_key(payload["sport"]),
            league=_key(payload["league"]),
            market=_key(payload["market"]),
            model_type=_key(payload["model_type"]),
            feature_schema_version=str(payload["feature_schema_version"]),
            state_schema_version=str(payload["state_schema_version"]),
            required_inputs=frozenset(_key(value) for value in payload["required_inputs"]),
            parameters=tuple(sorted(
                (_key(name), _finite(value, f"model parameter {name}"))
                for name, value in dict(payload["parameters"]).items()
            )),
            calibration_method=_key(payload["calibration_method"]),
            calibration_version=str(payload["calibration_version"]).strip(),
            calibration_hash=_digest(
                payload["calibration_hash"], "independent calibration hash"
            ),
            beta_coefficients=BetaCoefficients.from_value(payload["beta_coefficients"]),
            trained_through=_timestamp(payload["trained_through"], "trained_through"),
            validation_from=_timestamp(payload["validation_from"], "validation_from"),
            validation_through=_timestamp(
                payload["validation_through"], "validation_through"
            ),
            evaluated_from=_timestamp(payload["evaluated_from"], "evaluated_from"),
            evaluated_through=_timestamp(payload["evaluated_through"], "evaluated_through"),
            test_sample_size=_integer(payload["test_sample_size"], "test sample size"),
            test_event_count=_integer(payload["test_event_count"], "test event count"),
            brier_score=_finite(payload["brier_score"], "model Brier score"),
            log_loss=_finite(payload["log_loss"], "model log loss"),
            evaluation_method=_key(payload["evaluation_method"]),
            bootstrap_draws=_integer(payload["bootstrap_draws"], "bootstrap draws"),
            model_selection_candidates=_integer(
                payload["model_selection_candidates"], "model selection candidates"
            ),
            multiple_comparison_method=_key(payload["multiple_comparison_method"]),
            comparisons=tuple(sorted(
                (_key(name), BaselineComparison.from_payload(dict(value)))
                for name, value in dict(payload["comparisons"]).items()
            )),
            calibration_slices=tuple(sorted(
                (
                    _key(dimension),
                    tuple(sorted(
                        (_key(label), CalibrationSlice.from_payload(dict(metric)))
                        for label, metric in dict(groups).items()
                    )),
                )
                for dimension, groups in dict(payload["calibration_slices"]).items()
            )),
            missing_feature_behavior=_key(payload["missing_feature_behavior"]),
            known_limitations=tuple(
                str(value).strip() for value in payload["known_limitations"]
                if str(value).strip()
            ),
            approved_for_display=approved,
            reviewed_by=str(payload["reviewed_by"]).strip(),
            reviewed_at=_timestamp(payload["reviewed_at"], "reviewed_at"),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if not self.model_id or not self.model_version:
            raise ValueError("model ID and version are required")
        if any(not value or value == "*" for value in (self.sport, self.league, self.market)):
            raise ValueError("independent models require an exact sport, league, and market")
        if (
            self.sport != "basketball"
            or self.league not in _SUPPORTED_LEAGUES
            or self.model_type != _SUPPORTED_MODEL
        ):
            raise ValueError("unsupported independent model type or sport")
        if self.market not in _SUPPORTED_MARKETS:
            raise ValueError("unsupported independent model market")
        if self.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise ValueError("unsupported independent feature schema")
        if self.state_schema_version != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported game-state schema")
        if not self.required_inputs <= _ALLOWED_INPUTS:
            raise ValueError("artifact declares an unsupported model input")
        common = {
            "home_score",
            "away_score",
            "seconds_remaining",
            "possession_home",
            "overtime_number",
            "provider_timestamp",
            "pregame_spread",
        }
        for required in common:
            if required not in self.required_inputs:
                raise ValueError(f"required model input is missing: {required}")
        parameters = dict(self.parameters)
        if parameters.keys() != _MODEL_PARAMETERS:
            raise ValueError("independent model parameter schema is not exact")
        missing_coefficients = _COEFFICIENTS - parameters.keys()
        if missing_coefficients:
            raise ValueError(
                f"independent model coefficients are missing: {sorted(missing_coefficients)}"
            )
        if any(abs(parameters[name]) > 100 for name in _COEFFICIENTS):
            raise ValueError("independent model coefficients must be in [-100, 100]")
        if parameters.get("regulation_seconds") != 48 * 60:
            raise ValueError("NBA regulation_seconds must equal 2,880")
        if parameters.get("overtime_period_seconds") != 5 * 60:
            raise ValueError("NBA overtime_period_seconds must equal 300")
        late_game_threshold = parameters.get("late_game_threshold")
        if late_game_threshold is None or not 0 < late_game_threshold < 1:
            raise ValueError("late_game_threshold must be in (0, 1)")
        if self.calibration_method not in {"identity", "beta"}:
            raise ValueError("unsupported independent-model calibration")
        if not self.calibration_version:
            raise ValueError("independent calibration version is required")
        if self.calibration_method == "identity" and self.beta_coefficients != BetaCoefficients(
            1.0, 1.0, 0.0
        ):
            raise ValueError("identity calibration must use identity coefficients")
        if not (
            self.trained_through < self.validation_from <= self.validation_through
            < self.evaluated_from <= self.evaluated_through < self.reviewed_at
        ):
            raise ValueError("model train, validation, test, and review windows are not chronological")
        if self.test_sample_size < MIN_TEST_SAMPLE:
            raise ValueError("independent models require at least 1,000 test observations")
        if self.test_event_count < MIN_TEST_EVENTS:
            raise ValueError("independent models require at least 200 test events")
        if not 0 <= self.brier_score <= 1 or self.log_loss < 0:
            raise ValueError("independent-model proper scores are invalid")
        if self.evaluation_method != "event_block_bootstrap":
            raise ValueError("independent models require event-block bootstrap evaluation")
        if self.bootstrap_draws < MIN_BOOTSTRAP_DRAWS:
            raise ValueError("independent models require at least 1,000 event-block draws")
        if self.model_selection_candidates < 1:
            raise ValueError("model selection candidate count must be positive")
        allowed_controls = {"predeclared_single_model", "holm", "white_reality_check"}
        if self.multiple_comparison_method not in allowed_controls:
            raise ValueError("unsupported multiple-comparison control")
        if (self.model_selection_candidates > 1
                and self.multiple_comparison_method == "predeclared_single_model"):
            raise ValueError("multiple model search requires a multiple-comparison control")
        comparisons = dict(self.comparisons)
        required_baselines = {
            "equal_family_logit",
            "pregame_market",
            "stern_brownian_benchmark",
        }
        if comparisons.keys() != required_baselines:
            raise ValueError("independent model baseline comparison schema is not exact")
        for required_baseline in sorted(required_baselines):
            comparison = comparisons.get(required_baseline)
            if comparison is None:
                raise ValueError(f"missing required baseline comparison: {required_baseline}")
            if comparison.sample_size != self.test_sample_size:
                raise ValueError("baseline comparisons must use the same untouched test rows")
            if not (
                self.brier_score < comparison.brier_score
                and self.log_loss < comparison.log_loss
            ):
                raise ValueError("independent model must beat every declared baseline")
            if (
                comparison.probability_brier_better < MIN_COMPARISON_SUPPORT
                or comparison.probability_log_loss_better < MIN_COMPARISON_SUPPORT
            ):
                raise ValueError("independent model lacks required bootstrap support")
        slices = {dimension: dict(groups) for dimension, groups in self.calibration_slices}
        if slices.keys() != {"time_remaining", "lead_bucket"}:
            raise ValueError("independent model calibration-slice schema is not exact")
        for dimension in ("time_remaining", "lead_bucket"):
            dimension_slices = slices.get(dimension, {})
            if len(dimension_slices) < 2:
                raise ValueError(
                    f"independent model requires at least two {dimension} calibration slices"
                )
            if sum(metric.sample_size for metric in dimension_slices.values()) != (
                self.test_sample_size
            ):
                raise ValueError(
                    f"{dimension} calibration slices must partition the untouched test rows"
                )
        if self.missing_feature_behavior != "omit_output":
            raise ValueError("missing independent-model features must omit output")
        if not self.known_limitations:
            raise ValueError("independent model must declare known limitations")
        if not self.approved_for_display or not self.reviewed_by:
            raise ValueError("independent model requires explicit review approval")

    def predict_home(
        self,
        *,
        home_score: float,
        away_score: float,
        seconds_remaining: float,
        possession_home: float,
        overtime_number: int,
        pregame_spread: float,
    ) -> float:
        """Pure-Python reference for the fitted NBA moneyline kernel."""
        values = (
            home_score,
            away_score,
            seconds_remaining,
            possession_home,
            float(overtime_number),
            pregame_spread,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("independent-model inputs must be finite")
        if possession_home not in {0.0, 1.0} or overtime_number < 0:
            raise ValueError("possession and overtime inputs are invalid")
        parameters = dict(self.parameters)
        period_seconds = (
            parameters["overtime_period_seconds"]
            if overtime_number > 0
            else parameters["regulation_seconds"]
        )
        if not 0 <= seconds_remaining <= period_seconds:
            raise ValueError("seconds_remaining is outside the modeled period")
        time_fraction = seconds_remaining / period_seconds
        lead = home_score - away_score
        pregame_home_margin = -pregame_spread
        late_game = float(time_fraction <= parameters["late_game_threshold"])
        linear = (
            parameters["intercept"]
            + parameters["score_differential"] * lead
            + parameters["pregame_home_margin"] * pregame_home_margin
            + parameters["time_remaining_fraction"] * time_fraction
            + parameters["score_time_interaction"] * lead * time_fraction
            + parameters["pregame_time_interaction"]
            * pregame_home_margin
            * time_fraction
            + parameters["home_possession"] * possession_home
            + parameters["overtime"] * float(overtime_number > 0)
            + parameters["late_game"] * late_game
        )
        raw = min(max(_sigmoid(linear), 1e-9), 1 - 1e-9)
        return self.beta_coefficients.calibrate(raw)

    def to_engine_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_hash": self.model_hash,
            "training_data_hash": self.training_data_hash,
            "sport": self.sport,
            "league": self.league,
            "market": self.market,
            "model_type": self.model_type,
            "feature_schema_version": self.feature_schema_version,
            "state_schema_version": self.state_schema_version,
            "required_inputs": sorted(self.required_inputs),
            "parameters": dict(self.parameters),
            "calibration_method": self.calibration_method,
            "calibration_version": self.calibration_version,
            "calibration_hash": self.calibration_hash,
            "beta_coefficients": self.beta_coefficients.as_list(),
            "test_sample_size": self.test_sample_size,
            "test_event_count": self.test_event_count,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "evaluation_method": self.evaluation_method,
            "bootstrap_draws": self.bootstrap_draws,
            "model_selection_candidates": self.model_selection_candidates,
            "multiple_comparison_method": self.multiple_comparison_method,
            "comparisons": {
                name: {
                    "sample_size": comparison.sample_size,
                    "brier_score": comparison.brier_score,
                    "log_loss": comparison.log_loss,
                    "probability_brier_better": comparison.probability_brier_better,
                    "probability_log_loss_better": comparison.probability_log_loss_better,
                }
                for name, comparison in self.comparisons
            },
            "calibration_slices": {
                dimension: {
                    label: {
                        "sample_size": metric.sample_size,
                        "brier_score": metric.brier_score,
                        "log_loss": metric.log_loss,
                        "calibration_intercept": metric.calibration_intercept,
                        "calibration_slope": metric.calibration_slope,
                    }
                    for label, metric in groups
                }
                for dimension, groups in self.calibration_slices
            },
            "missing_feature_behavior": self.missing_feature_behavior,
            "known_limitations": list(self.known_limitations),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat(),
            "evidence_passed": True,
        }


@dataclass(frozen=True, slots=True)
class IndependentModelArtifact:
    artifact_version: str
    artifact_hash: str
    policies: tuple[IndependentModelPolicy, ...]

    @classmethod
    def load(cls, path: str | Path) -> "IndependentModelArtifact":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("independent-model artifact must contain a JSON model list")
        if payload.get("artifact_version") != "1":
            raise ValueError("unsupported independent-model artifact version")
        policies = tuple(
            IndependentModelPolicy.from_payload(dict(value))
            for value in payload.get("models", ())
        )
        if not policies:
            raise ValueError("independent-model artifact contains no approved models")
        identities = [(policy.sport, policy.league, policy.market) for policy in policies]
        if len(set(identities)) != len(identities):
            raise ValueError("independent-model artifact contains duplicate exact segments")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return cls(
            artifact_version="1",
            artifact_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            policies=policies,
        )

    @property
    def eligible_for_display(self) -> bool:
        return bool(self.policies)

    def policy_for(self, sport: str, league: str, market: str) -> IndependentModelPolicy | None:
        identity = (_key(sport), _key(league), _key(market))
        return next(
            (
                policy for policy in self.policies
                if (policy.sport, policy.league, policy.market) == identity
            ),
            None,
        )


def load_independent_models(path: str | Path | None) -> IndependentModelArtifact | None:
    return IndependentModelArtifact.load(path) if path else None
