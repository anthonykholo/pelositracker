# Independent-model registry

The repository ships no independent-model artifact. This contract exists so a
feature flag cannot turn a research formula into a labelled model probability.
The registry permits reviewed output as a cross-check only; paper actions still
use calibrated consensus, executable cost, uncertainty, and portfolio gates.

## Promotion evidence

Every entry is for one exact sport, league, and canonical market. Wildcards are
rejected. The loader requires immutable model/training-data hashes, exact
feature and game-state schemas, complete required inputs, chronological
train/validation/untouched-test windows, at least 1,000 test observations from
200 events, at least 1,000 event-block draws, proper-score improvement over
equal-family consensus, the pregame market, and the non-promotable Stern
benchmark on the same test rows with at least 95% bootstrap support,
time-remaining and lead-bucket calibration slices, multiplicity control when
multiple candidates were searched, and a review timestamp after the test
interval.

Runtime then independently checks the model type, exact segment, SHA-256 model
hash, sample/event support, required input names, parameter ranges, and
calibration before emitting `independent_model_probability`. Registry v1 is
restricted to a fitted NBA moneyline logistic model with score, time,
possession, overtime, pregame, and late-game interaction features. The older
Stern score/clock formula remains a benchmark and can never be promoted through
this registry. Missing inputs, stale/invalid state, or a mismatched league
produces no model output—not a fallback estimate.

## Artifact shape

```json
{
  "artifact_version": "1",
  "models": [{
    "model_id": "nba-moneyline-game-state-logit",
    "model_version": "reviewed-version",
    "model_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "training_data_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "sport": "basketball",
    "league": "nba",
    "market": "moneyline",
    "model_type": "basketball_game_state_logit_v1",
    "feature_schema_version": "independent-features-v1",
    "state_schema_version": "game-state-v2",
    "required_inputs": [
      "home_score", "away_score", "seconds_remaining", "possession_home",
      "overtime_number", "provider_timestamp", "pregame_spread"
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
      "overtime_period_seconds": 300
    },
    "calibration_method": "identity",
    "calibration_version": "reviewed-calibration-version",
    "calibration_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
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
        "probability_log_loss_better": 0.97
      },
      "pregame_market": {
        "sample_size": 1500,
        "brier_score": 0.22,
        "log_loss": 0.64,
        "probability_brier_better": 0.99,
        "probability_log_loss_better": 0.98
      },
      "stern_brownian_benchmark": {
        "sample_size": 1500,
        "brier_score": 0.23,
        "log_loss": 0.66,
        "probability_brier_better": 0.99,
        "probability_log_loss_better": 0.99
      }
    },
    "calibration_slices": {
      "time_remaining": {
        "early": {"sample_size": 800, "brier_score": 0.19, "log_loss": 0.56, "calibration_intercept": 0.01, "calibration_slope": 0.98},
        "late": {"sample_size": 700, "brier_score": 0.17, "log_loss": 0.52, "calibration_intercept": -0.02, "calibration_slope": 1.02}
      },
      "lead_bucket": {
        "close": {"sample_size": 900, "brier_score": 0.20, "log_loss": 0.58, "calibration_intercept": 0.00, "calibration_slope": 0.96},
        "clear": {"sample_size": 600, "brier_score": 0.14, "log_loss": 0.45, "calibration_intercept": 0.02, "calibration_slope": 1.04}
      }
    },
    "missing_feature_behavior": "omit_output",
    "known_limitations": ["NBA moneyline only; requires verified possession."],
    "approved_for_display": true,
    "reviewed_by": "named-reviewer",
    "reviewed_at": "2026-04-15T12:00:00Z"
  }]
}
```

The values above demonstrate the contract and are not a fitted artifact or a
claim about NBA performance. Promotion requires separate reproducible data,
code, review records, and rollback criteria.
