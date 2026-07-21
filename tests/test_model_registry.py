import json
from datetime import datetime, timezone

import pytest

from app.model_registry import IndependentModelArtifact
from app.engine import SignalEngine
from app.models import GameState, Quote


def model_payload(**overrides):
    model = {
        "model_id": "nba-moneyline-game-state-logit",
        "model_version": "nba-moneyline-2026q1",
        "model_hash": "a" * 64,
        "training_data_hash": "b" * 64,
        "sport": "basketball",
        "league": "nba",
        "market": "moneyline",
        "model_type": "basketball_game_state_logit_v1",
        "feature_schema_version": "independent-features-v1",
        "state_schema_version": "game-state-v2",
        "required_inputs": [
            "home_score", "away_score", "seconds_remaining",
            "possession_home", "overtime_number", "provider_timestamp",
            "pregame_spread",
        ],
        "parameters": {
            "intercept": 0.0,
            "score_differential": 0.22,
            "pregame_home_margin": 0.08,
            "time_remaining_fraction": -0.1,
            "score_time_interaction": -0.05,
            "pregame_time_interaction": 0.02,
            "home_possession": 0.08,
            "overtime": 0.0,
            "late_game": 0.1,
            "late_game_threshold": 0.25,
            "regulation_seconds": 2880,
            "overtime_period_seconds": 300,
        },
        "calibration_method": "identity",
        "calibration_version": "nba-moneyline-cal-2026q1",
        "calibration_hash": "c" * 64,
        "beta_coefficients": [1.0, 1.0, 0.0],
        "trained_through": "2025-06-30T23:59:59Z",
        "validation_from": "2025-07-01T00:00:00Z",
        "validation_through": "2025-09-30T23:59:59Z",
        "evaluated_from": "2025-10-01T00:00:00Z",
        "evaluated_through": "2026-03-31T23:59:59Z",
        "test_sample_size": 1500,
        "test_event_count": 500,
        "brier_score": 0.18,
        "log_loss": 0.55,
        "evaluation_method": "event_block_bootstrap",
        "bootstrap_draws": 2000,
        "model_selection_candidates": 1,
        "multiple_comparison_method": "predeclared_single_model",
        "comparisons": {
            "equal_family_logit": {
                "sample_size": 1500,
                "brier_score": 0.21,
                "log_loss": 0.62,
                "probability_brier_better": 0.98,
                "probability_log_loss_better": 0.97,
            },
            "pregame_market": {
                "sample_size": 1500,
                "brier_score": 0.22,
                "log_loss": 0.64,
                "probability_brier_better": 0.99,
                "probability_log_loss_better": 0.98,
            },
            "stern_brownian_benchmark": {
                "sample_size": 1500,
                "brier_score": 0.23,
                "log_loss": 0.66,
                "probability_brier_better": 0.99,
                "probability_log_loss_better": 0.99,
            },
        },
        "calibration_slices": {
            "time_remaining": {
                "early": {
                    "sample_size": 800, "brier_score": 0.19,
                    "log_loss": 0.56, "calibration_intercept": 0.01,
                    "calibration_slope": 0.98,
                },
                "late": {
                    "sample_size": 700, "brier_score": 0.17,
                    "log_loss": 0.52, "calibration_intercept": -0.02,
                    "calibration_slope": 1.02,
                },
            },
            "lead_bucket": {
                "close": {
                    "sample_size": 900, "brier_score": 0.2,
                    "log_loss": 0.58, "calibration_intercept": 0.0,
                    "calibration_slope": 0.96,
                },
                "clear": {
                    "sample_size": 600, "brier_score": 0.14,
                    "log_loss": 0.45, "calibration_intercept": 0.02,
                    "calibration_slope": 1.04,
                },
            },
        },
        "missing_feature_behavior": "omit_output",
        "known_limitations": [
            "NBA moneyline only; requires verified possession and pregame spread."
        ],
        "approved_for_display": True,
        "reviewed_by": "research-review",
        "reviewed_at": "2026-04-15T12:00:00Z",
    }
    model.update(overrides)
    return {"artifact_version": "1", "models": [model]}


def write_artifact(tmp_path, **overrides):
    path = tmp_path / "independent-models-v1.json"
    path.write_text(json.dumps(model_payload(**overrides)), encoding="utf-8")
    return path


def test_valid_registry_requires_an_exact_reviewed_segment(tmp_path):
    artifact = IndependentModelArtifact.load(write_artifact(tmp_path))

    policy = artifact.policy_for("basketball", "nba", "moneyline")

    assert artifact.eligible_for_display
    assert policy is not None
    assert policy.model_type == "basketball_game_state_logit_v1"
    assert dict(policy.parameters)["score_differential"] == .22
    assert policy.to_engine_dict()["test_event_count"] == 500
    assert artifact.policy_for("basketball", "wnba", "moneyline") is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"league": "*"}, "exact sport, league, and market"),
        ({"league": "wnba"}, "unsupported independent model"),
        ({"test_sample_size": 999}, "1,000 test observations"),
        ({"test_sample_size": "1500"}, "JSON integer"),
        ({"test_event_count": 199}, "200 test events"),
        ({"bootstrap_draws": 999}, "1,000 event-block draws"),
        ({"approved_for_display": False}, "review approval"),
        ({"approved_for_display": "true"}, "JSON boolean"),
        ({"model_hash": "not-a-hash"}, "model hash"),
        ({"calibration_version": ""}, "calibration version"),
        ({"evaluated_from": "2025-09-01T00:00:00Z"}, "chronological"),
        ({"required_inputs": ["home_score"]}, "required model input"),
        ({"parameters": {"late_game_threshold": 0}}, "parameter schema"),
        ({"missing_feature_behavior": "fill_zero"}, "must omit output"),
    ],
)
def test_registry_rejects_incomplete_or_leaking_evidence(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        IndependentModelArtifact.load(write_artifact(tmp_path, **overrides))


def test_registry_requires_both_proper_scores_and_bootstrap_support(tmp_path):
    payload = model_payload()
    payload["models"][0]["comparisons"]["equal_family_logit"]["brier_score"] = .17
    path = tmp_path / "weak.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="beat every declared baseline"):
        IndependentModelArtifact.load(path)

    payload = model_payload()
    payload["models"][0]["comparisons"]["pregame_market"][
        "probability_log_loss_better"
    ] = .90
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="bootstrap support"):
        IndependentModelArtifact.load(path)


def test_searching_multiple_models_requires_a_multiple_comparison_control(tmp_path):
    with pytest.raises(ValueError, match="multiple-comparison control"):
        IndependentModelArtifact.load(write_artifact(
            tmp_path,
            model_selection_candidates=5,
            multiple_comparison_method="predeclared_single_model",
        ))


def test_registry_requires_same_test_rows_and_slice_evidence(tmp_path):
    payload = model_payload()
    payload["models"][0]["comparisons"]["pregame_market"]["sample_size"] = 1499
    path = tmp_path / "mismatched-comparison.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="same untouched test rows"):
        IndependentModelArtifact.load(path)

    payload = model_payload()
    payload["models"][0]["calibration_slices"]["lead_bucket"].pop("clear")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="lead_bucket calibration slices"):
        IndependentModelArtifact.load(path)


def test_python_reference_kernel_uses_possession_phase_and_overtime(tmp_path):
    policy = IndependentModelArtifact.load(write_artifact(tmp_path)).policies[0]

    home_possession = policy.predict_home(
        home_score=75,
        away_score=65,
        seconds_remaining=720,
        possession_home=1,
        overtime_number=0,
        pregame_spread=-4,
    )
    away_possession = policy.predict_home(
        home_score=75,
        away_score=65,
        seconds_remaining=720,
        possession_home=0,
        overtime_number=0,
        pregame_spread=-4,
    )
    overtime = policy.predict_home(
        home_score=110,
        away_score=110,
        seconds_remaining=150,
        possession_home=1,
        overtime_number=1,
        pregame_spread=-4,
    )

    assert home_possession > away_possession
    assert 0 < overtime < 1


def test_engine_requires_both_operator_opt_in_and_exact_model_evidence(tmp_path):
    artifact = IndependentModelArtifact.load(write_artifact(tmp_path))
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    quotes = [
        Quote("event", "moneyline", outcome, probability, source, now,
              bid=probability - .01, ask=probability + .01)
        for source in ("Pinnacle", "Circa")
        for outcome, probability in (("home", .60), ("away", .40))
    ]
    state = GameState(
        "event", 75, 65, "Q3", "04:00", "Polymarket", now,
        possession="home",
        provider_timestamp=now,
    )
    engine = SignalEngine(
        confidence_threshold=0, edge_threshold=0, enable_independent_model=True
    )
    engine.install_independent_models(artifact)

    with_inputs = engine.evaluate(
        "event", quotes, [state], sport="basketball", league="nba",
        pregame_spread=-4, as_of=now,
    )
    missing_pregame = engine.evaluate(
        "event", quotes, [state], sport="basketball", league="nba", as_of=now,
    )
    missing_possession_state = GameState(
        "event", 75, 65, "Q3", "04:00", "Polymarket", now,
        provider_timestamp=now,
    )
    missing_possession = engine.evaluate(
        "event", quotes, [missing_possession_state], sport="basketball", league="nba",
        pregame_spread=-4, as_of=now,
    )

    assert any(signal.independent_model_probability is not None for signal in with_inputs)
    assert all(signal.action == "WATCH" for signal in with_inputs)
    assert all(
        signal.independent_model_version == "nba-moneyline-2026q1"
        for signal in with_inputs
    )
    assert all(signal.independent_model_sample_size == 1500 for signal in with_inputs)
    assert all(
        signal.independent_calibration_version == "nba-moneyline-cal-2026q1"
        for signal in with_inputs
    )
    home_signal = next(signal for signal in with_inputs if signal.outcome == "home")
    expected = artifact.policies[0].predict_home(
        home_score=75,
        away_score=65,
        seconds_remaining=960,
        possession_home=1,
        overtime_number=0,
        pregame_spread=-4,
    )
    assert home_signal.independent_model_probability == pytest.approx(expected)
    assert all(
        signal.independent_model_probability is None for signal in missing_pregame
    )
    assert all(
        signal.independent_model_probability is None for signal in missing_possession
    )

    disabled = SignalEngine(enable_independent_model=False)
    disabled.install_independent_models(artifact)
    assert disabled._independent_model_policies(quotes, "basketball", "nba") == []


def test_reviewed_model_handles_explicit_overtime_without_regulation_zero(tmp_path):
    artifact = IndependentModelArtifact.load(write_artifact(tmp_path))
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    quotes = [
        Quote("event", "moneyline", outcome, probability, source, now,
              bid=probability - .01, ask=probability + .01)
        for source in ("Pinnacle", "Circa")
        for outcome, probability in (("home", .50), ("away", .50))
    ]
    state = GameState(
        "event", 110, 110, "OT", "02:30", "Polymarket", now,
        possession="home", provider_timestamp=now, overtime_number=1,
        normalized_seconds_remaining=150,
    )
    engine = SignalEngine(enable_independent_model=True)
    engine.install_independent_models(artifact)

    signals = engine.evaluate(
        "event", quotes, [state], sport="basketball", league="nba",
        pregame_spread=-4, as_of=now,
    )

    home = next(signal for signal in signals if signal.outcome == "home")
    expected = artifact.policies[0].predict_home(
        home_score=110,
        away_score=110,
        seconds_remaining=150,
        possession_home=1,
        overtime_number=1,
        pregame_spread=-4,
    )
    assert home.independent_model_probability == pytest.approx(expected)
