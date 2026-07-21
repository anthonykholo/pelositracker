from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any

from .domain.time import parse_provider_timestamp


MIN_POLICY_SAMPLE = 1000
MIN_BOOTSTRAP_DRAWS = 200


def _key(value: str | None, default: str = "*") -> str:
    normalized = str(value or default).strip().casefold()
    return normalized or default


def _finite(value: Any, label: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def _timestamp(value: Any, label: str) -> datetime:
    parsed = parse_provider_timestamp(value)
    if parsed is None:
        raise ValueError(f"{label} must be a valid UTC timestamp")
    return parsed


@dataclass(frozen=True, slots=True)
class BetaCoefficients:
    a: float
    b: float
    c: float

    @classmethod
    def from_value(cls, value: dict[str, Any] | list[float] | tuple[float, ...]) -> "BetaCoefficients":
        if isinstance(value, dict):
            coefficients = cls(
                _finite(value["a"], "beta a"),
                _finite(value["b"], "beta b"),
                _finite(value["c"], "beta c"),
            )
        else:
            if len(value) != 3:
                raise ValueError("beta coefficient draw must contain a, b, and c")
            coefficients = cls(*(_finite(item, "beta coefficient") for item in value))
        coefficients.validate()
        return coefficients

    def validate(self) -> None:
        if self.a < 0 or self.b < 0:
            raise ValueError("beta calibration must be monotone (a and b non-negative)")

    def calibrate(self, probability: float) -> float:
        if not math.isfinite(probability) or not 0 < probability < 1:
            raise ValueError("probability must be finite and strictly between zero and one")
        clipped = min(max(probability, 1e-9), 1 - 1e-9)
        value = self.a * math.log(clipped) - self.b * math.log1p(-clipped) + self.c
        return min(max(_sigmoid(value), 1e-9), 1 - 1e-9)

    def as_list(self) -> list[float]:
        return [self.a, self.b, self.c]


@dataclass(frozen=True, slots=True)
class CalibrationPolicy:
    sport: str
    league: str
    market: str
    sample_size: int
    model_sample_size: int
    devig_method: str
    consensus_method: str
    calibration_method: str
    beta_coefficients: BetaCoefficients
    beta_bootstrap_coefficients: tuple[BetaCoefficients, ...]
    execution_cost_offsets: tuple[float, ...]
    uncertainty_draws: tuple[dict[str, Any], ...]
    candidate_metrics: dict[str, dict[str, dict[str, float | int]]]
    min_probability_positive: float
    min_expected_value_dollars: float
    sharp_source_family: str | None = None
    consensus_intercept: float = 0.0
    family_coefficients: tuple[tuple[str, float], ...] = ()
    missing_family_coefficients: tuple[tuple[str, float], ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CalibrationPolicy":
        calibration_method = _key(payload.get("calibration_method"), "identity")
        coefficients = BetaCoefficients.from_value(
            payload.get("beta_coefficients", {"a": 1.0, "b": 1.0, "c": 0.0})
        )
        draws = tuple(
            BetaCoefficients.from_value(value)
            for value in payload.get("beta_bootstrap_coefficients", ())
        )
        family_coefficients = tuple(sorted(
            (_key(name), _finite(value, "family coefficient"))
            for name, value in dict(payload.get("family_coefficients", {})).items()
        ))
        missing_coefficients = tuple(sorted(
            (_key(name), _finite(value, "missing-family coefficient"))
            for name, value in dict(payload.get("missing_family_coefficients", {})).items()
        ))
        raw_uncertainty_draws = payload.get("uncertainty_draws", ())
        uncertainty_draws: list[dict[str, Any]] = []
        for value in raw_uncertainty_draws:
            draw = dict(value)
            draw_coefficients = BetaCoefficients.from_value(draw["beta_coefficients"])
            draw_devig = _key(draw.get("devig_method"))
            draw_consensus = _key(draw.get("consensus_method"))
            draw_family_coefficients = {
                _key(name): _finite(coefficient, "draw family coefficient")
                for name, coefficient in dict(draw.get("family_coefficients", {})).items()
            }
            draw_missing_coefficients = {
                _key(name): _finite(coefficient, "draw missing-family coefficient")
                for name, coefficient in dict(
                    draw.get("missing_family_coefficients", {})
                ).items()
            }
            uncertainty_draws.append({
                "pipeline": str(draw.get("pipeline", "historical-bootstrap")),
                "devig_method": draw_devig,
                "consensus_method": draw_consensus,
                "beta_coefficients": draw_coefficients.as_list(),
                "execution_cost_offset": _finite(
                    draw["execution_cost_offset"], "draw execution cost offset"
                ),
                "sharp_source_family": (
                    _key(draw.get("sharp_source_family"))
                    if draw.get("sharp_source_family") else None
                ),
                "consensus_intercept": _finite(
                    draw.get("consensus_intercept", 0.0), "draw consensus intercept"
                ),
                "family_coefficients": draw_family_coefficients,
                "missing_family_coefficients": draw_missing_coefficients,
            })
        if not uncertainty_draws:
            for draw_coefficients, cost_offset in zip(
                draws,
                payload.get("execution_cost_offsets", ()),
                strict=True,
            ):
                uncertainty_draws.append({
                    "pipeline": "legacy-aligned-bootstrap",
                    "devig_method": _key(payload.get("devig_method")),
                    "consensus_method": _key(payload.get("consensus_method")),
                    "beta_coefficients": draw_coefficients.as_list(),
                    "execution_cost_offset": _finite(
                        cost_offset, "execution cost offset"
                    ),
                    "sharp_source_family": (
                        _key(payload.get("sharp_source_family"))
                        if payload.get("sharp_source_family") else None
                    ),
                    "consensus_intercept": _finite(
                        payload.get("consensus_intercept", 0.0), "intercept"
                    ),
                    "family_coefficients": dict(family_coefficients),
                    "missing_family_coefficients": dict(missing_coefficients),
                })
        policy = cls(
            sport=_key(payload.get("sport")),
            league=_key(payload.get("league")),
            market=_key(payload.get("market")),
            sample_size=int(payload["sample_size"]),
            model_sample_size=int(payload.get("model_sample_size", payload["sample_size"])),
            devig_method=_key(payload.get("devig_method")),
            consensus_method=_key(payload.get("consensus_method")),
            calibration_method=calibration_method,
            beta_coefficients=coefficients,
            beta_bootstrap_coefficients=draws,
            execution_cost_offsets=tuple(
                _finite(value, "execution cost offset")
                for value in payload.get("execution_cost_offsets", ())
            ),
            uncertainty_draws=tuple(uncertainty_draws),
            candidate_metrics=dict(payload.get("candidate_metrics", {})),
            min_probability_positive=_finite(
                payload.get("min_probability_positive", 0.95),
                "minimum positive-EV probability",
            ),
            min_expected_value_dollars=_finite(
                payload.get("min_expected_value_dollars", 1.0),
                "minimum expected value dollars",
            ),
            sharp_source_family=(
                _key(payload.get("sharp_source_family"))
                if payload.get("sharp_source_family") else None
            ),
            consensus_intercept=_finite(payload.get("consensus_intercept", 0.0), "intercept"),
            family_coefficients=family_coefficients,
            missing_family_coefficients=missing_coefficients,
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.sample_size < MIN_POLICY_SAMPLE:
            raise ValueError("calibration sample is too small for policy eligibility")
        if self.model_sample_size < MIN_POLICY_SAMPLE:
            raise ValueError("model-selection sample is too small for policy eligibility")
        if self.devig_method not in {"shin", "proportional"}:
            raise ValueError("unsupported selected de-vig method")
        if self.consensus_method not in {
            "equal_family_logit", "sharp_source", "stacked_logit"
        }:
            raise ValueError("unsupported selected consensus method")
        if self.calibration_method not in {"identity", "beta"}:
            raise ValueError("unsupported calibration method")
        if len(self.beta_bootstrap_coefficients) < MIN_BOOTSTRAP_DRAWS:
            raise ValueError("event-block beta bootstrap requires at least 200 draws")
        if len(self.execution_cost_offsets) != len(self.beta_bootstrap_coefficients):
            raise ValueError("execution-cost bootstrap must align with beta bootstrap draws")
        if len(self.uncertainty_draws) < MIN_BOOTSTRAP_DRAWS:
            raise ValueError("pipeline uncertainty requires at least 200 aligned draws")
        for draw in self.uncertainty_draws:
            if draw["devig_method"] not in {"shin", "proportional"}:
                raise ValueError("uncertainty draw has unsupported de-vig method")
            if draw["consensus_method"] not in {
                "equal_family_logit", "sharp_source", "stacked_logit"
            }:
                raise ValueError("uncertainty draw has unsupported consensus method")
            if (draw["consensus_method"] == "sharp_source"
                    and not draw["sharp_source_family"]):
                raise ValueError("sharp-source uncertainty draw lacks source family")
            if (draw["consensus_method"] == "stacked_logit"
                    and not draw["family_coefficients"]):
                raise ValueError("stacked uncertainty draw lacks coefficients")
        if not 0.5 < self.min_probability_positive <= 1:
            raise ValueError("minimum positive-EV probability must be in (0.5, 1]")
        if self.min_expected_value_dollars < 0:
            raise ValueError("minimum expected value dollars cannot be negative")

        devig_metrics = self.candidate_metrics.get("devig", {})
        consensus_metrics = self.candidate_metrics.get("consensus", {})
        if self.devig_method not in devig_metrics:
            raise ValueError("selected de-vig method lacks chronological candidate metrics")
        if self.consensus_method not in consensus_metrics:
            raise ValueError("selected consensus method lacks chronological candidate metrics")
        for family in (devig_metrics, consensus_metrics):
            for name, metric in family.items():
                sample_size = int(metric.get("sample_size", 0))
                loss = _finite(metric.get("log_loss", math.nan), f"{name} log loss")
                brier = _finite(
                    metric.get("brier_score", math.nan), f"{name} Brier score"
                )
                if (sample_size < MIN_POLICY_SAMPLE or loss < 0
                        or not 0 <= brier <= 1):
                    raise ValueError(
                        "chronological candidate metrics have insufficient sample support"
                    )

        if self.consensus_method == "sharp_source" and not self.sharp_source_family:
            raise ValueError("sharp-source consensus requires a declared source family")
        if self.consensus_method == "stacked_logit" and not self.family_coefficients:
            raise ValueError("stacked consensus requires fitted family coefficients")

    def matches(self, sport: str, league: str, market: str) -> bool:
        dimensions = (
            (self.sport, _key(sport)),
            (self.league, _key(league)),
            (self.market, _key(market)),
        )
        return all(expected == "*" or expected == actual for expected, actual in dimensions)

    def specificity(self) -> int:
        return sum(value != "*" for value in (self.sport, self.league, self.market))

    def hierarchy_rank(self) -> tuple[bool, bool, bool, int]:
        """Rank market -> league -> sport fallbacks, then prefer more evidence."""
        return (
            self.market != "*",
            self.league != "*",
            self.sport != "*",
            self.sample_size,
        )

    def calibrate(self, probability: float) -> float:
        if self.calibration_method == "identity":
            if not math.isfinite(probability) or not 0 < probability < 1:
                raise ValueError("probability must be finite and strictly between zero and one")
            return probability
        return self.beta_coefficients.calibrate(probability)

    def to_engine_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "devig_method": self.devig_method,
            "consensus_method": self.consensus_method,
            "calibration_method": self.calibration_method,
            "beta_coefficients": self.beta_coefficients.as_list(),
            "beta_bootstrap_coefficients": [
                coefficients.as_list() for coefficients in self.beta_bootstrap_coefficients
            ],
            "execution_cost_offsets": list(self.execution_cost_offsets),
            "uncertainty_draws": list(self.uncertainty_draws),
            "min_probability_positive": self.min_probability_positive,
            "min_expected_value_dollars": self.min_expected_value_dollars,
            "sample_size": self.sample_size,
            "model_sample_size": self.model_sample_size,
            "sharp_source_family": self.sharp_source_family,
            "consensus_intercept": self.consensus_intercept,
            "family_coefficients": dict(self.family_coefficients),
            "missing_family_coefficients": dict(self.missing_family_coefficients),
        }


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    artifact_version: str
    model_version: str
    trained_through: datetime
    evaluated_from: datetime
    evaluated_through: datetime
    sample_size: int
    brier_score: float
    log_loss: float
    supported_markets: frozenset[str]
    calibration_method: str
    model_hash: str = ""
    model_trained_through: datetime | None = None
    validation_through: datetime | None = None
    uncertainty_method: str = "unavailable"
    policies: tuple[CalibrationPolicy, ...] = ()

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationArtifact":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        version = str(payload["artifact_version"])
        if version == "1":
            artifact = cls(
                artifact_version=version,
                model_version=str(payload["model_version"]),
                trained_through=_timestamp(payload["trained_through"], "trained_through"),
                evaluated_from=_timestamp(payload["evaluated_from"], "evaluated_from"),
                evaluated_through=_timestamp(
                    payload["evaluated_through"], "evaluated_through"
                ),
                sample_size=int(payload["sample_size"]),
                brier_score=_finite(payload["brier_score"], "Brier score"),
                log_loss=_finite(payload["log_loss"], "log loss"),
                supported_markets=frozenset(_key(value) for value in payload["supported_markets"]),
                calibration_method=_key(payload.get("calibration_method"), "identity"),
            )
        elif version == "2":
            policies = tuple(
                CalibrationPolicy.from_payload(value) for value in payload.get("segments", ())
            )
            artifact = cls(
                artifact_version=version,
                model_version=str(payload["model_version"]),
                model_hash=str(payload.get("model_hash", "")),
                model_trained_through=_timestamp(
                    payload["model_trained_through"], "model_trained_through"
                ),
                validation_through=(
                    _timestamp(payload["validation_through"], "validation_through")
                    if payload.get("validation_through") else None
                ),
                trained_through=_timestamp(
                    payload["calibration_trained_through"],
                    "calibration_trained_through",
                ),
                evaluated_from=_timestamp(payload["evaluated_from"], "evaluated_from"),
                evaluated_through=_timestamp(
                    payload["evaluated_through"], "evaluated_through"
                ),
                sample_size=int(payload["sample_size"]),
                brier_score=_finite(payload["brier_score"], "Brier score"),
                log_loss=_finite(payload["log_loss"], "log loss"),
                supported_markets=frozenset(policy.market for policy in policies),
                calibration_method="segmented",
                uncertainty_method=_key(payload.get("uncertainty_method"), "unavailable"),
                policies=policies,
            )
        else:
            raise ValueError("unsupported calibration artifact version")
        artifact.validate()
        return artifact

    @property
    def eligible_for_action(self) -> bool:
        return self.artifact_version == "2" and bool(self.policies)

    def policy_for(self, sport: str, league: str, market: str) -> CalibrationPolicy | None:
        if not self.eligible_for_action:
            return None
        candidates = [
            policy for policy in self.policies if policy.matches(sport, league, market)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda policy: (
                policy.hierarchy_rank(), policy.sport, policy.league, policy.market
            ),
            reverse=True,
        )
        return candidates[0]

    def validate(self) -> None:
        if self.trained_through >= self.evaluated_from:
            raise ValueError("calibration evaluation is not out-of-sample")
        if self.evaluated_from >= self.evaluated_through:
            raise ValueError("invalid calibration evaluation interval")
        if self.sample_size < MIN_POLICY_SAMPLE:
            raise ValueError("calibration sample is too small for policy eligibility")
        if not (0 <= self.brier_score <= 1) or self.log_loss < 0:
            raise ValueError("invalid calibration metrics")
        if not self.supported_markets:
            raise ValueError("calibration artifact supports no markets")

        if self.artifact_version == "1":
            if self.calibration_method != "identity":
                raise ValueError("legacy artifacts support identity calibration only")
            return

        if self.model_trained_through is None or self.model_trained_through >= self.trained_through:
            raise ValueError("model selection and calibration windows are not chronological")
        if self.validation_through is None:
            raise ValueError("actionable artifacts require an explicit validation interval")
        if not self.trained_through < self.validation_through < self.evaluated_from:
            raise ValueError("validation and untouched test windows are not chronological")
        if len(self.model_hash) != 64 or any(value not in "0123456789abcdef" for value in self.model_hash):
            raise ValueError("model hash must be a lower-case SHA-256 digest")
        if self.uncertainty_method != "event_block_bootstrap":
            raise ValueError("actionable artifacts require event-block bootstrap uncertainty")


def load_calibration(path: str | Path | None) -> CalibrationArtifact | None:
    return CalibrationArtifact.load(path) if path else None
