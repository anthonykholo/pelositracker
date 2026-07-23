"""Leakage-safe offline utilities for fitting Milestone E artifacts.

These functions consume already-normalized, settled research observations. They
do not fetch data, place orders, or promote a model automatically. Promotion is
a separate reviewed step after the untouched chronological test fold passes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable

from .calibration import BetaCoefficients
from .domain.time import ensure_utc
from .multiplicity import reality_check_pvalue, romano_wolf_pvalues


_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    event_id: str
    observed_at: datetime
    sport: str
    league: str
    market: str
    outcome: float
    candidate_probabilities: dict[str, float]
    executable_cost: float
    execution_cost_error: float
    # When the features and the settled label became available. Default to
    # observed_at (the legacy assumption) so existing rows behave unchanged; a
    # label that settles later must carry its true availability so it cannot leak
    # into an earlier fold.
    feature_available_at: datetime | None = None
    label_available_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        object.__setattr__(
            self, "feature_available_at",
            ensure_utc(self.feature_available_at)
            if self.feature_available_at is not None else self.observed_at)
        object.__setattr__(
            self, "label_available_at",
            ensure_utc(self.label_available_at)
            if self.label_available_at is not None else self.observed_at)
        if not self.event_id:
            raise ValueError("event_id is required")
        if self.outcome not in {0.0, 1.0}:
            raise ValueError("settled outcome must be binary")
        if not self.candidate_probabilities:
            raise ValueError("at least one candidate probability is required")
        for name, probability in self.candidate_probabilities.items():
            if not name or not math.isfinite(probability) or not 0 < probability < 1:
                raise ValueError("candidate probabilities must be finite and in (0, 1)")
        for value, label in (
            (self.executable_cost, "executable cost"),
            (self.execution_cost_error, "execution cost error"),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{label} must be finite")
        if not 0 < self.executable_cost < 1:
            raise ValueError("executable cost must be strictly between zero and one")

    @property
    def usable_at(self) -> datetime:
        """Earliest origin at which the row is usable to fit: both its features
        and its settled label are known (the later of the two availabilities)."""
        assert self.feature_available_at is not None and self.label_available_at is not None
        return max(self.feature_available_at, self.label_available_at)


@dataclass(frozen=True, slots=True)
class ChronologicalFolds:
    selection: tuple[EvaluationObservation, ...]
    calibration: tuple[EvaluationObservation, ...]
    validation: tuple[EvaluationObservation, ...]
    test: tuple[EvaluationObservation, ...]


@dataclass(frozen=True, slots=True)
class CandidateSpecification:
    """Auditable metadata for one already-computed out-of-fold pipeline."""

    devig_method: str
    consensus_method: str
    sharp_source_family: str | None = None
    consensus_intercept: float = 0.0
    family_coefficients: tuple[tuple[str, float], ...] = ()
    missing_family_coefficients: tuple[tuple[str, float], ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CandidateSpecification":
        return cls(
            devig_method=str(payload["devig_method"]).strip().casefold(),
            consensus_method=str(payload["consensus_method"]).strip().casefold(),
            sharp_source_family=(
                str(payload["sharp_source_family"]).strip().casefold()
                if payload.get("sharp_source_family") else None
            ),
            consensus_intercept=float(payload.get("consensus_intercept", 0.0)),
            family_coefficients=tuple(sorted(
                (str(name).strip().casefold(), float(value))
                for name, value in dict(payload.get("family_coefficients", {})).items()
            )),
            missing_family_coefficients=tuple(sorted(
                (str(name).strip().casefold(), float(value))
                for name, value in dict(
                    payload.get("missing_family_coefficients", {})
                ).items()
            )),
        )

    def validate(self) -> None:
        if self.devig_method not in {"proportional", "shin"}:
            raise ValueError("candidate has unsupported de-vig method")
        if self.consensus_method not in {
            "equal_family_logit", "sharp_source", "stacked_logit"
        }:
            raise ValueError("candidate has unsupported consensus method")
        if self.consensus_method == "sharp_source" and not self.sharp_source_family:
            raise ValueError("sharp-source candidate requires sharp_source_family")
        if self.consensus_method == "stacked_logit" and not self.family_coefficients:
            raise ValueError("stacked candidate requires fitted family coefficients")
        values = [self.consensus_intercept]
        values.extend(value for _, value in self.family_coefficients)
        values.extend(value for _, value in self.missing_family_coefficients)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("candidate coefficients must be finite")

    def as_dict(self) -> dict[str, Any]:
        return {
            "devig_method": self.devig_method,
            "consensus_method": self.consensus_method,
            "sharp_source_family": self.sharp_source_family,
            "consensus_intercept": self.consensus_intercept,
            "family_coefficients": dict(self.family_coefficients),
            "missing_family_coefficients": dict(self.missing_family_coefficients),
        }


def chronological_folds(
    observations: Iterable[EvaluationObservation],
    *,
    model_selection_through: datetime,
    calibration_through: datetime,
    validation_through: datetime,
) -> ChronologicalFolds:
    selection_end = ensure_utc(model_selection_through)
    calibration_end = ensure_utc(calibration_through)
    validation_end = ensure_utc(validation_through)
    if not selection_end < calibration_end < validation_end:
        raise ValueError("chronological fold boundaries must be strictly increasing")

    grouped_folds: dict[str, set[str]] = {}
    partitions: dict[str, list[EvaluationObservation]] = {
        "selection": [], "calibration": [], "validation": [], "test": []
    }
    # Partition by usable_at (max of feature- and label-availability), NOT by the
    # prediction-observation time, so a row whose label settles after a fold's
    # origin cannot leak its outcome into that fold's fit/selection.
    for row in sorted(observations, key=lambda value: (value.usable_at, value.event_id)):
        if row.usable_at <= selection_end:
            fold = "selection"
        elif row.usable_at <= calibration_end:
            fold = "calibration"
        elif row.usable_at <= validation_end:
            fold = "validation"
        else:
            fold = "test"
        grouped_folds.setdefault(row.event_id, set()).add(fold)
        partitions[fold].append(row)

    crossing = sorted(event_id for event_id, folds in grouped_folds.items() if len(folds) > 1)
    if crossing:
        raise ValueError(
            f"event crosses a chronological boundary: {', '.join(crossing[:3])}"
        )
    if any(not partitions[name] for name in partitions):
        raise ValueError("every chronological fold must contain observations")
    return ChronologicalFolds(**{
        name: tuple(values) for name, values in partitions.items()
    })


def _clip(probability: float) -> float:
    return min(max(probability, _EPSILON), 1 - _EPSILON)


def score_predictions(probabilities: Iterable[float], outcomes: Iterable[float]) -> dict[str, float]:
    pairs = [(_clip(float(probability)), float(outcome))
             for probability, outcome in zip(probabilities, outcomes, strict=True)]
    if not pairs:
        raise ValueError("cannot score an empty prediction set")
    brier = sum((probability - outcome) ** 2 for probability, outcome in pairs) / len(pairs)
    loss = -sum(
        outcome * math.log(probability) + (1 - outcome) * math.log1p(-probability)
        for probability, outcome in pairs
    ) / len(pairs)
    return {"sample_size": len(pairs), "brier_score": brier, "log_loss": loss}


def select_candidate(
    observations: Iterable[EvaluationObservation],
) -> tuple[str, dict[str, dict[str, float | int]]]:
    rows = tuple(observations)
    if not rows:
        raise ValueError("candidate selection requires observations")
    candidates = set.intersection(*(set(row.candidate_probabilities) for row in rows))
    if not candidates:
        raise ValueError("no candidate is present for every selection observation")
    outcomes = [row.outcome for row in rows]
    metrics: dict[str, dict[str, float | int]] = {}
    for candidate in sorted(candidates):
        metrics[candidate] = score_predictions(
            [row.candidate_probabilities[candidate] for row in rows], outcomes
        )
    selected = min(
        metrics,
        key=lambda name: (float(metrics[name]["log_loss"]),
                          float(metrics[name]["brier_score"]), name),
    )
    return selected, metrics


def _solve_three(matrix: list[list[float]], vector: list[float]) -> list[float]:
    augmented = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("beta calibration Hessian is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[row][3] for row in range(3)]


def fit_beta_calibration(
    probabilities: Iterable[float],
    outcomes: Iterable[float],
    *,
    l2: float = 1e-4,
    max_iterations: int = 60,
) -> BetaCoefficients:
    pairs = [(_clip(float(probability)), float(outcome))
             for probability, outcome in zip(probabilities, outcomes, strict=True)]
    if len(pairs) < 20:
        raise ValueError("beta calibration requires at least 20 observations")
    if any(outcome not in {0.0, 1.0} for _, outcome in pairs):
        raise ValueError("beta calibration outcomes must be binary")
    if not any(outcome == 0.0 for _, outcome in pairs) or not any(
        outcome == 1.0 for _, outcome in pairs
    ):
        raise ValueError("beta calibration requires both outcome classes")

    features = [(math.log(probability), -math.log1p(-probability), 1.0)
                for probability, _ in pairs]
    labels = [outcome for _, outcome in pairs]
    parameters = [1.0, 1.0, 0.0]
    prior = [1.0, 1.0, 0.0]

    def objective(values: list[float]) -> float:
        total = 0.0
        for feature, outcome in zip(features, labels, strict=True):
            prediction = _clip(1.0 / (1.0 + math.exp(-max(-40.0, min(40.0,
                sum(weight * value for weight, value in zip(values, feature, strict=True)))))))
            total -= outcome * math.log(prediction) + (1 - outcome) * math.log1p(-prediction)
        penalty = 0.5 * l2 * sum((value - target) ** 2
                                 for value, target in zip(values, prior, strict=True))
        return total / len(features) + penalty

    for _ in range(max_iterations):
        gradient = [l2 * (value - target)
                    for value, target in zip(parameters, prior, strict=True)]
        hessian = [[l2 if row == column else 0.0 for column in range(3)]
                   for row in range(3)]
        for feature, outcome in zip(features, labels, strict=True):
            linear = max(-40.0, min(40.0, sum(
                weight * value for weight, value in zip(parameters, feature, strict=True)
            )))
            prediction = 1.0 / (1.0 + math.exp(-linear))
            residual = prediction - outcome
            weight = prediction * (1.0 - prediction)
            for row in range(3):
                gradient[row] += residual * feature[row] / len(features)
                for column in range(3):
                    hessian[row][column] += (
                        weight * feature[row] * feature[column] / len(features)
                    )
        if max(abs(value) for value in gradient) < 1e-8:
            break
        step = _solve_three(hessian, gradient)
        before = objective(parameters)
        accepted = False
        scale = 1.0
        for _ in range(20):
            candidate = [parameters[index] - scale * step[index] for index in range(3)]
            candidate[0] = max(0.0, candidate[0])
            candidate[1] = max(0.0, candidate[1])
            if objective(candidate) <= before + 1e-12:
                parameters = candidate
                accepted = True
                break
            scale *= 0.5
        if not accepted:
            break
    return BetaCoefficients(*parameters)


def event_block_beta_bootstrap(
    observations: Iterable[EvaluationObservation],
    *,
    candidate: str,
    draws: int = 500,
    seed: int = 0,
) -> tuple[BetaCoefficients, ...]:
    if draws < 1:
        raise ValueError("bootstrap draws must be positive")
    groups: dict[str, list[EvaluationObservation]] = {}
    for row in observations:
        if candidate not in row.candidate_probabilities:
            raise ValueError(f"candidate {candidate!r} is missing from an observation")
        groups.setdefault(row.event_id, []).append(row)
    event_ids = sorted(groups)
    if len(event_ids) < 20:
        raise ValueError("event-block bootstrap requires at least 20 events")
    generator = random.Random(seed)
    results = []
    for _ in range(draws):
        sampled = [generator.choice(event_ids) for _ in event_ids]
        rows = [row for event_id in sampled for row in groups[event_id]]
        results.append(fit_beta_calibration(
            [row.candidate_probabilities[candidate] for row in rows],
            [row.outcome for row in rows],
            max_iterations=25,
        ))
    return tuple(results)


def event_block_uncertainty(
    observations: Iterable[EvaluationObservation],
    *,
    candidate: str,
    calibration_method: str,
    draws: int = 500,
    seed: int = 0,
) -> tuple[tuple[BetaCoefficients, ...], tuple[float, ...]]:
    """Fit aligned calibration/cost draws while resampling complete events."""
    if draws < 200:
        raise ValueError("actionable uncertainty requires at least 200 draws")
    if calibration_method not in {"identity", "beta"}:
        raise ValueError("unsupported calibration method")
    groups: dict[str, list[EvaluationObservation]] = {}
    for row in observations:
        if candidate not in row.candidate_probabilities:
            raise ValueError(f"candidate {candidate!r} is missing from an observation")
        groups.setdefault(row.event_id, []).append(row)
    event_ids = sorted(groups)
    if len(event_ids) < 20:
        raise ValueError("event-block uncertainty requires at least 20 events")
    generator = random.Random(seed)
    coefficient_draws: list[BetaCoefficients] = []
    execution_offsets: list[float] = []
    for _ in range(draws):
        sampled = [generator.choice(event_ids) for _ in event_ids]
        rows = [row for event_id in sampled for row in groups[event_id]]
        # Even when identity wins the validation comparison, refit beta in each
        # event resample to represent finite-sample calibration uncertainty
        # around that identity decision. The central prediction remains identity.
        coefficients = fit_beta_calibration(
            [row.candidate_probabilities[candidate] for row in rows],
            [row.outcome for row in rows],
            max_iterations=25,
        )
        coefficient_draws.append(coefficients)
        execution_offsets.append(
            sum(row.execution_cost_error for row in rows) / len(rows)
        )
    return tuple(coefficient_draws), tuple(execution_offsets)


def _event_resample(
    observations: tuple[EvaluationObservation, ...], generator: random.Random
) -> list[EvaluationObservation]:
    groups: dict[str, list[EvaluationObservation]] = {}
    for row in observations:
        groups.setdefault(row.event_id, []).append(row)
    event_ids = sorted(groups)
    if len(event_ids) < 20:
        raise ValueError("pipeline bootstrap requires at least 20 events per fold")
    sampled = [generator.choice(event_ids) for _ in event_ids]
    return [row for event_id in sampled for row in groups[event_id]]


def event_block_pipeline_uncertainty(
    folds: ChronologicalFolds,
    *,
    specifications: dict[str, CandidateSpecification],
    candidates: list[str],
    draws: int = 500,
    seed: int = 0,
) -> tuple[dict[str, Any], ...]:
    """Resample pipeline choice, calibration, and execution cost together."""
    if draws < 200:
        raise ValueError("actionable uncertainty requires at least 200 draws")
    simple = [name for name in candidates
              if specifications[name].consensus_method == "equal_family_logit"]
    generator = random.Random(seed)
    results: list[dict[str, Any]] = []
    for _ in range(draws):
        selection_rows = _event_resample(folds.selection, generator)
        selection_metrics = _candidate_metrics(selection_rows, candidates)
        selected = min(candidates, key=lambda name: (
            float(selection_metrics[name]["log_loss"]),
            float(selection_metrics[name]["brier_score"]), name,
        ))
        if specifications[selected].consensus_method == "stacked_logit":
            if not simple:
                raise ValueError("stacked candidates require an equal-family baseline")
            baseline = min(simple, key=lambda name: (
                float(selection_metrics[name]["log_loss"]),
                float(selection_metrics[name]["brier_score"]), name,
            ))
            validation_rows = _event_resample(folds.validation, generator)
            validation_metrics = _candidate_metrics(validation_rows, [selected, baseline])
            if not _strictly_better(
                validation_metrics[selected], validation_metrics[baseline]
            ):
                selected = baseline

        calibration_rows = _event_resample(folds.calibration, generator)
        coefficients = fit_beta_calibration(
            [row.candidate_probabilities[selected] for row in calibration_rows],
            [row.outcome for row in calibration_rows],
            max_iterations=25,
        )
        cost_offset = sum(
            row.execution_cost_error for row in calibration_rows
        ) / len(calibration_rows)
        results.append({
            "pipeline": selected,
            **specifications[selected].as_dict(),
            "beta_coefficients": coefficients.as_list(),
            "execution_cost_offset": cost_offset,
        })
    return tuple(results)


def _metrics_by_method(
    pipeline_metrics: dict[str, dict[str, float | int]],
    specifications: dict[str, CandidateSpecification],
    attribute: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for pipeline, metrics in pipeline_metrics.items():
        method = str(getattr(specifications[pipeline], attribute))
        current = result.get(method)
        if current is None or (
            float(metrics["log_loss"]), float(metrics["brier_score"]), pipeline
        ) < (
            float(current["log_loss"]), float(current["brier_score"]),
            str(current["pipeline"]),
        ):
            result[method] = {**metrics, "pipeline": pipeline}
    return result


def _candidate_metrics(
    rows: Iterable[EvaluationObservation], candidates: Iterable[str]
) -> dict[str, dict[str, float | int]]:
    observations = tuple(rows)
    outcomes = [row.outcome for row in observations]
    return {
        candidate: score_predictions(
            [row.candidate_probabilities[candidate] for row in observations], outcomes
        )
        for candidate in candidates
    }


def _observation_log_loss(probability: float, outcome: float) -> float:
    clipped = min(max(probability, _EPSILON), 1.0 - _EPSILON)
    return -(outcome * math.log(clipped) + (1.0 - outcome) * math.log(1.0 - clipped))


def multiplicity_report(
    observations: Iterable[EvaluationObservation], *, candidates: list[str],
    benchmark: str, draws: int = 1000, seed: int = 0,
) -> dict[str, Any]:
    """Familywise-error-controlled evidence that a *searched* candidate beats
    ``benchmark`` in out-of-sample log score, clustered by event.

    Returns White's Reality Check p-value (does any searched candidate beat the
    benchmark at all?) and the Romano-Wolf per-candidate adjusted p-values. Use
    this whenever more than one candidate was searched, so a lucky winner is not
    promoted as skill. Computes losses from the candidates' recorded
    probabilities; it does not refit."""
    rows = tuple(observations)
    if not rows:
        raise ValueError("at least one observation is required")
    searched = [name for name in candidates if name != benchmark]
    if not searched:
        raise ValueError("at least one non-benchmark candidate is required")
    event_ids = [row.event_id for row in rows]
    benchmark_loss = [
        _observation_log_loss(row.candidate_probabilities[benchmark], row.outcome)
        for row in rows
    ]
    diffs_by_candidate = {
        name: [
            benchmark_loss[i]
            - _observation_log_loss(rows[i].candidate_probabilities[name], rows[i].outcome)
            for i in range(len(rows))
        ]
        for name in searched
    }
    return {
        "benchmark": benchmark,
        "candidates_searched": len(searched),
        "reality_check_pvalue": reality_check_pvalue(
            diffs_by_candidate, event_ids, draws=draws, seed=seed),
        "romano_wolf_pvalues": romano_wolf_pvalues(
            diffs_by_candidate, event_ids, draws=draws, seed=seed),
    }


def _strictly_better(candidate: dict[str, float | int], baseline: dict[str, float | int]) -> bool:
    return (
        float(candidate["log_loss"]) <= float(baseline["log_loss"])
        and float(candidate["brier_score"]) <= float(baseline["brier_score"])
        and (
            float(candidate["log_loss"]) < float(baseline["log_loss"])
            or float(candidate["brier_score"]) < float(baseline["brier_score"])
        )
    )


def build_calibration_artifact(
    observations: Iterable[EvaluationObservation],
    *,
    specifications: dict[str, CandidateSpecification],
    model_selection_through: datetime,
    calibration_through: datetime,
    validation_through: datetime,
    model_version: str,
    sport: str,
    league: str,
    market: str,
    bootstrap_draws: int = 500,
    seed: int = 0,
    min_probability_positive: float = 0.95,
    min_expected_value_dollars: float = 1.0,
) -> dict[str, Any]:
    """Build one reviewable v2 segment from nested chronological folds.

    Candidate probabilities must already be point-in-time/out-of-fold. This
    builder will not infer them from post-event prices or refit a sport model.
    The returned payload is not installed or promoted automatically.
    """
    rows = tuple(observations)
    expected_segment = tuple(
        value.strip().casefold() for value in (sport, league, market)
    )
    if any(
        (row.sport.strip().casefold(), row.league.strip().casefold(),
         row.market.strip().casefold()) != expected_segment
        for row in rows
    ):
        raise ValueError("observations must belong to the declared artifact segment")
    for specification in specifications.values():
        specification.validate()
    if not specifications:
        raise ValueError("candidate specifications are required")
    folds = chronological_folds(
        rows,
        model_selection_through=model_selection_through,
        calibration_through=calibration_through,
        validation_through=validation_through,
    )
    fold_sizes = {
        "selection": len(folds.selection),
        "calibration": len(folds.calibration),
        "validation": len(folds.validation),
        "test": len(folds.test),
    }
    if any(size < 1000 for size in fold_sizes.values()):
        raise ValueError(
            "actionable artifacts require at least 1,000 observations in every fold"
        )
    all_rows = folds.selection + folds.calibration + folds.validation + folds.test
    missing = sorted(
        name for name in specifications
        if any(name not in row.candidate_probabilities for row in all_rows)
    )
    if missing:
        raise ValueError(
            f"declared candidate is missing from an observation: {', '.join(missing)}"
        )
    candidate_names = sorted(specifications)

    selection_metrics = _candidate_metrics(folds.selection, candidate_names)
    selected = min(candidate_names, key=lambda name: (
        float(selection_metrics[name]["log_loss"]),
        float(selection_metrics[name]["brier_score"]), name,
    ))
    validation_raw = _candidate_metrics(folds.validation, candidate_names)

    # Learned stacking is promoted only when it beats the declared simple
    # equal-family baseline on both proper scores in the later validation fold.
    if specifications[selected].consensus_method == "stacked_logit":
        simple = [name for name in candidate_names
                  if specifications[name].consensus_method == "equal_family_logit"]
        if not simple:
            raise ValueError("stacked candidates require an equal-family baseline")
        baseline = min(simple, key=lambda name: (
            float(selection_metrics[name]["log_loss"]),
            float(selection_metrics[name]["brier_score"]), name,
        ))
        if not _strictly_better(validation_raw[selected], validation_raw[baseline]):
            selected = baseline

    calibration_probabilities = [
        row.candidate_probabilities[selected] for row in folds.calibration
    ]
    calibration_outcomes = [row.outcome for row in folds.calibration]
    beta = fit_beta_calibration(calibration_probabilities, calibration_outcomes)
    validation_probabilities = [
        row.candidate_probabilities[selected] for row in folds.validation
    ]
    validation_outcomes = [row.outcome for row in folds.validation]
    identity_validation = score_predictions(validation_probabilities, validation_outcomes)
    beta_validation = score_predictions(
        [beta.calibrate(probability) for probability in validation_probabilities],
        validation_outcomes,
    )
    calibration_method = "beta" if _strictly_better(
        beta_validation, identity_validation
    ) else "identity"
    central = beta if calibration_method == "beta" else BetaCoefficients(1.0, 1.0, 0.0)

    pipeline_draws = event_block_pipeline_uncertainty(
        folds,
        specifications=specifications,
        candidates=candidate_names,
        draws=bootstrap_draws,
        seed=seed,
    )
    coefficient_draws = tuple(
        BetaCoefficients.from_value(draw["beta_coefficients"])
        for draw in pipeline_draws
    )
    execution_offsets = tuple(
        float(draw["execution_cost_offset"]) for draw in pipeline_draws
    )
    test_probabilities = [row.candidate_probabilities[selected] for row in folds.test]
    calibrated_test = [
        central.calibrate(probability) if calibration_method == "beta" else probability
        for probability in test_probabilities
    ]
    test_metrics = score_predictions(
        calibrated_test, [row.outcome for row in folds.test]
    )
    specification = specifications[selected]
    hash_material = {
        "model_version": model_version,
        "selected_pipeline": selected,
        "specifications": {name: specifications[name].as_dict()
                           for name in sorted(specifications)},
        "selection_metrics": selection_metrics,
        "boundaries": [
            model_selection_through.isoformat(), calibration_through.isoformat(),
            validation_through.isoformat(),
        ],
    }
    model_hash = hashlib.sha256(
        json.dumps(hash_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    candidate_metrics = {
        "devig": _metrics_by_method(selection_metrics, specifications, "devig_method"),
        "consensus": _metrics_by_method(
            selection_metrics, specifications, "consensus_method"
        ),
        "pipelines_selection": selection_metrics,
        "pipelines_validation": validation_raw,
        "calibration_validation": {
            "identity": identity_validation,
            "beta": beta_validation,
        },
    }
    return {
        "artifact_version": "2",
        "model_version": model_version,
        "model_hash": model_hash,
        "model_trained_through": max(row.observed_at for row in folds.selection).isoformat(),
        "calibration_trained_through": max(
            row.observed_at for row in folds.calibration
        ).isoformat(),
        "validation_through": max(row.observed_at for row in folds.validation).isoformat(),
        "evaluated_from": min(row.observed_at for row in folds.test).isoformat(),
        "evaluated_through": max(row.observed_at for row in folds.test).isoformat(),
        "sample_size": len(folds.test),
        "brier_score": test_metrics["brier_score"],
        "log_loss": test_metrics["log_loss"],
        "uncertainty_method": "event_block_bootstrap",
        "statistical_claim_supported": False,
        "segments": [{
            "sport": sport.strip().casefold(),
            "league": league.strip().casefold(),
            "market": market.strip().casefold(),
            "sample_size": len(folds.calibration),
            "model_sample_size": len(folds.selection),
            **specification.as_dict(),
            "calibration_method": calibration_method,
            "beta_coefficients": central.as_list(),
            "beta_bootstrap_coefficients": [draw.as_list() for draw in coefficient_draws],
            "execution_cost_offsets": list(execution_offsets),
            "uncertainty_draws": list(pipeline_draws),
            "candidate_metrics": candidate_metrics,
            "selected_pipeline": selected,
            "test_metrics": test_metrics,
            "min_probability_positive": min_probability_positive,
            "min_expected_value_dollars": min_expected_value_dollars,
        }],
    }


def iter_observations_jsonl(path: str | Path) -> Iterable[EvaluationObservation]:
    """Yield observations one line at a time.

    Streaming keeps peak memory close to a single parsed row rather than
    "entire file text + a full list of parsed rows", which matters when the
    offline training/evaluation input grows large. Callers that genuinely need
    the whole set materialized can wrap this in ``tuple(...)`` (see
    ``load_observations_jsonl``); callers that only make a single pass should
    iterate directly.
    """
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                yield EvaluationObservation(
                    event_id=str(payload["event_id"]),
                    observed_at=datetime.fromisoformat(
                        str(payload["observed_at"]).replace("Z", "+00:00")
                    ),
                    sport=str(payload["sport"]),
                    league=str(payload["league"]),
                    market=str(payload["market"]),
                    outcome=float(payload["outcome"]),
                    candidate_probabilities={
                        str(name): float(value)
                        for name, value in dict(payload["candidate_probabilities"]).items()
                    },
                    executable_cost=float(payload["executable_cost"]),
                    execution_cost_error=float(payload["execution_cost_error"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid observation on JSONL line {line_number}") from exc


def load_observations_jsonl(path: str | Path) -> list[EvaluationObservation]:
    return list(iter_observations_jsonl(path))


def write_artifact(payload: dict[str, Any], path: str | Path) -> None:
    """Write canonical JSON for review; runtime installation is a separate step."""
    Path(path).write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _cli_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_utc(parsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a review-only chronological calibration artifact"
    )
    parser.add_argument("observations", help="point-in-time JSONL observations")
    parser.add_argument("candidates", help="JSON object keyed by pipeline name")
    parser.add_argument("output", help="destination artifact JSON")
    parser.add_argument("--selection-through", required=True, type=_cli_datetime)
    parser.add_argument("--calibration-through", required=True, type=_cli_datetime)
    parser.add_argument("--validation-through", required=True, type=_cli_datetime)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--sport", required=True)
    parser.add_argument("--league", required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument("--bootstrap-draws", type=int, default=500)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--min-probability-positive", type=float, default=0.95)
    parser.add_argument("--min-expected-value-dollars", type=float, default=1.0)
    arguments = parser.parse_args(argv)
    candidate_payload = json.loads(Path(arguments.candidates).read_text(encoding="utf-8"))
    specifications = {
        str(name): CandidateSpecification.from_payload(payload)
        for name, payload in dict(candidate_payload).items()
    }
    artifact = build_calibration_artifact(
        iter_observations_jsonl(arguments.observations),
        specifications=specifications,
        model_selection_through=arguments.selection_through,
        calibration_through=arguments.calibration_through,
        validation_through=arguments.validation_through,
        model_version=arguments.model_version,
        sport=arguments.sport,
        league=arguments.league,
        market=arguments.market,
        bootstrap_draws=arguments.bootstrap_draws,
        seed=arguments.seed,
        min_probability_positive=arguments.min_probability_positive,
        min_expected_value_dollars=arguments.min_expected_value_dollars,
    )
    write_artifact(artifact, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
