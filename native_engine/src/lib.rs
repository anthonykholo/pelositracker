use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize)]
struct QuoteInput {
    market: String,
    outcome: String,
    #[serde(default)]
    comparison_market: Option<String>,
    #[serde(default)]
    comparison_outcome: Option<String>,
    #[serde(default)]
    comparison_source: Option<String>,
    probability: f64,
    source: String,
    observed_at: f64,
    #[serde(default)]
    timestamp_trusted: bool,
    #[serde(default = "default_true")]
    identity_valid: bool,
    bid: Option<f64>,
    ask: Option<f64>,
    // Phase 0 additions (all optional so older callers still deserialize):
    // Deprecated transport field; subjective source weights are ignored.
    #[allow(dead_code)]
    source_weight: Option<f64>,
    // exchanges (Polymarket, Betfair) already trade near a de-vigged mid, so
    // we do NOT multiplicatively normalize them.
    is_exchange: Option<bool>,
    #[allow(dead_code)]
    decimal_odds: Option<f64>,
    #[allow(dead_code)]
    liquidity: Option<f64>,
    #[serde(default)]
    ask_size: Option<f64>,
    #[serde(default)]
    depth_complete: bool,
    #[serde(default)]
    fee_metadata_known: bool,
    #[serde(default = "default_true")]
    accepting_orders: bool,
    // Phase 2b: parsed spread/total line and normalized side
    // (home | away | over | under), resolved in Python.
}

impl QuoteInput {
    fn executable_probability(&self) -> f64 {
        self.ask.unwrap_or(self.probability)
    }
    fn exchange(&self) -> bool {
        self.is_exchange.unwrap_or(false)
    }
    fn market_key(&self) -> &str {
        self.comparison_market.as_deref().unwrap_or(&self.market)
    }
    fn outcome_key(&self) -> &str {
        self.comparison_outcome.as_deref().unwrap_or(&self.outcome)
    }
    fn source_key(&self) -> String {
        self.comparison_source.clone().unwrap_or_else(|| {
            self.source
                .chars()
                .filter(|character| character.is_ascii_alphanumeric())
                .flat_map(char::to_lowercase)
                .collect()
        })
    }
}

// Reviewed game-state features can produce an independent live probability.
// The result is a displayed cross-check only and never changes the action basis.
#[derive(Clone, Debug, Deserialize)]
struct StateInput {
    home_score: f64,
    away_score: f64,
    observed_at: f64,
    #[serde(default)]
    seconds_remaining: Option<f64>,
    #[serde(default)]
    overtime_number: Option<i32>,
    #[serde(default)]
    timestamp_trusted: bool,
    #[serde(default)]
    state_valid: bool,
    #[serde(default)]
    possession_home: Option<f64>,
}

#[derive(Clone, Debug, Deserialize)]
struct ModelPolicyInput {
    market: String,
    devig_method: String,
    consensus_method: String,
    calibration_method: String,
    beta_coefficients: Vec<f64>,
    #[serde(default)]
    beta_bootstrap_coefficients: Vec<Vec<f64>>,
    #[serde(default)]
    execution_cost_offsets: Vec<f64>,
    min_probability_positive: f64,
    min_expected_value_dollars: f64,
    sample_size: usize,
    #[serde(default)]
    model_sample_size: Option<usize>,
    #[serde(default)]
    sharp_source_family: Option<String>,
    #[serde(default)]
    consensus_intercept: f64,
    #[serde(default)]
    family_coefficients: BTreeMap<String, f64>,
    #[serde(default)]
    missing_family_coefficients: BTreeMap<String, f64>,
    #[serde(default)]
    uncertainty_draws: Vec<UncertaintyDrawInput>,
}

#[derive(Clone, Debug, Deserialize)]
struct UncertaintyDrawInput {
    #[allow(dead_code)]
    #[serde(default)]
    pipeline: String,
    devig_method: String,
    consensus_method: String,
    beta_coefficients: Vec<f64>,
    execution_cost_offset: f64,
    #[serde(default)]
    sharp_source_family: Option<String>,
    #[serde(default)]
    consensus_intercept: f64,
    #[serde(default)]
    family_coefficients: BTreeMap<String, f64>,
    #[serde(default)]
    missing_family_coefficients: BTreeMap<String, f64>,
}

#[derive(Clone, Debug, Deserialize)]
struct IndependentModelPolicyInput {
    model_id: String,
    model_version: String,
    model_hash: String,
    training_data_hash: String,
    registry_artifact_hash: String,
    sport: String,
    league: String,
    market: String,
    model_type: String,
    feature_schema_version: String,
    state_schema_version: String,
    required_inputs: Vec<String>,
    parameters: BTreeMap<String, f64>,
    calibration_method: String,
    calibration_version: String,
    calibration_hash: String,
    beta_coefficients: Vec<f64>,
    test_sample_size: usize,
    test_event_count: usize,
    missing_feature_behavior: String,
    #[serde(default)]
    evidence_passed: bool,
}

#[derive(Debug, Deserialize)]
struct EvaluateRequest {
    as_of: f64,
    event_id: String,
    confidence_threshold: f64,
    edge_threshold: f64,
    max_age_seconds: f64,
    away_outcome: String,
    quotes: Vec<QuoteInput>,
    #[serde(default)]
    states: Vec<StateInput>,
    #[serde(default)]
    sport: Option<String>,
    #[serde(default)]
    league: Option<String>,
    // Missing pregame information disables the independent model; there is no
    // zero-margin fallback.
    #[serde(default)]
    pregame_spread: Option<f64>,
    #[allow(dead_code)]
    #[serde(default)]
    pregame_total: Option<f64>,
    // Fractional-Kelly lambda applied to the historically estimated lower bound.
    #[serde(default)]
    kelly_fraction: Option<f64>,
    #[serde(default)]
    enable_independent_model: bool,
    #[serde(default)]
    model_policies: Vec<ModelPolicyInput>,
    #[serde(default)]
    independent_model_policies: Vec<IndependentModelPolicyInput>,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Serialize)]
struct GateOutput {
    code: String,
    passed: Option<bool>,
    status: String,
    value: Option<f64>,
    threshold: Option<f64>,
    explanation: String,
}

#[derive(Debug, Serialize)]
struct SignalOutput {
    event_id: String,
    market: String,
    outcome: String,
    model_probability: f64,
    market_probability: f64,
    edge: f64,
    confidence: f64,
    action: String,
    reasons: Vec<String>,
    quote_source: String,
    // Phase 0 auditable fields:
    market_fair_prob: f64,
    devig_method: String,
    overround: f64,
    n_reference_sources: i64,
    // Phase 2a: independent live win-probability (moneyline only, when game
    // state is available). null otherwise. A cross-check, not the edge basis.
    model_live_prob: Option<f64>,
    // Legacy sizing aliases retained at the Python boundary.
    ev_per_stake: f64,   // calibrated consensus / executable - 1
    kelly_fraction: f64, // fractional Kelly on the historical lower bound
    required_edge: f64,  // configured base + market premium
    fair_stderr: f64,    // compatibility approximation from the bootstrap interval
    fillable_size: Option<f64>,
    // Auditable data-quality components (0-100). Edge is deliberately absent:
    // opportunity size must not make the underlying data look more reliable.
    quality_freshness: f64,
    quality_agreement: f64,
    quality_sources: f64,
    quality_execution: f64,
    quality_calibration: f64,
    quality_data_completeness: f64,
    quality_provider_freshness: f64,
    quality_identity: f64,
    quality_model_sample_support: f64,
    quality_calibration_support: f64,
    quality_source_independence: f64,
    consensus_probability: f64,
    calibrated_consensus_probability: Option<f64>,
    independent_model_probability: Option<f64>,
    independent_model_version: Option<String>,
    independent_model_hash: Option<String>,
    independent_calibration_version: Option<String>,
    independent_calibration_hash: Option<String>,
    independent_model_sample_size: usize,
    independent_model_event_count: usize,
    uncertainty_low: Option<f64>,
    uncertainty_high: Option<f64>,
    probability_net_ev_positive: Option<f64>,
    net_expected_value_per_share: Option<f64>,
    net_expected_value_total: Option<f64>,
    consensus_method: String,
    model_sample_size: usize,
    calibration_sample_size: usize,
    gate_results: Vec<GateOutput>,
}

fn clamp(value: f64, low: f64, high: f64) -> f64 {
    value.max(low).min(high)
}

fn mean(values: &[f64]) -> f64 {
    values.iter().sum::<f64>() / values.len() as f64
}

fn population_std_dev(values: &[f64]) -> f64 {
    let average = mean(values);
    (values
        .iter()
        .map(|value| (value - average).powi(2))
        .sum::<f64>()
        / values.len() as f64)
        .sqrt()
}

fn logit(p: f64) -> f64 {
    let p = clamp(p, 1e-6, 1.0 - 1e-6);
    (p / (1.0 - p)).ln()
}

fn inv_logit(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

fn beta_calibrate(probability: f64, coefficients: &[f64]) -> Option<f64> {
    if coefficients.len() != 3
        || coefficients.iter().any(|value| !value.is_finite())
        || coefficients[0] < 0.0
        || coefficients[1] < 0.0
        || !probability.is_finite()
        || !(0.0..1.0).contains(&probability)
    {
        return None;
    }
    let p = clamp(probability, 1e-9, 1.0 - 1e-9);
    Some(clamp(
        inv_logit(coefficients[0] * p.ln() - coefficients[1] * (-p).ln_1p() + coefficients[2]),
        1e-9,
        1.0 - 1e-9,
    ))
}

fn quantile(values: &[f64], probability: f64) -> Option<f64> {
    if values.is_empty() || values.iter().any(|value| !value.is_finite()) {
        return None;
    }
    let mut ordered = values.to_vec();
    ordered.sort_by(f64::total_cmp);
    let position = clamp(probability, 0.0, 1.0) * (ordered.len() - 1) as f64;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    let weight = position - lower as f64;
    Some(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)
}

fn consensus_probability(
    reference: &[&Fair],
    method: &str,
    sharp_source_family: Option<&str>,
    intercept: f64,
    family_coefficients: &BTreeMap<String, f64>,
    missing_family_coefficients: &BTreeMap<String, f64>,
) -> Option<f64> {
    if reference.is_empty() {
        return None;
    }
    let equal_family = clamp(
        inv_logit(
            reference.iter().map(|fair| logit(fair.prob)).sum::<f64>() / reference.len() as f64,
        ),
        0.001,
        0.999,
    );
    match method {
        "equal_family_logit" => Some(equal_family),
        "sharp_source" => sharp_source_family.and_then(|source| {
            reference
                .iter()
                .find(|fair| fair.source_key.eq_ignore_ascii_case(source))
                .map(|fair| fair.prob)
        }),
        "stacked_logit" if !family_coefficients.is_empty() => {
            let mut linear = intercept;
            for (family, coefficient) in family_coefficients {
                if let Some(source) = reference
                    .iter()
                    .find(|fair| fair.source_key.eq_ignore_ascii_case(family))
                {
                    linear += coefficient * logit(source.prob);
                } else {
                    let missing = missing_family_coefficients.get(family)?;
                    linear += missing;
                }
            }
            Some(clamp(inv_logit(linear), 0.001, 0.999))
        }
        _ => None,
    }
}

fn gate(
    code: &str,
    passed: Option<bool>,
    value: Option<f64>,
    threshold: Option<f64>,
    explanation: impl Into<String>,
) -> GateOutput {
    GateOutput {
        code: code.to_string(),
        passed,
        status: match passed {
            Some(true) => "pass",
            Some(false) => "fail",
            None => "unknown",
        }
        .to_string(),
        value,
        threshold,
        explanation: explanation.into(),
    }
}

/// Abramowitz & Stegun 7.1.26 approximation of erf (max abs error ~1.5e-7).
#[cfg(test)]
fn erf(x: f64) -> f64 {
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();
    let t = 1.0 / (1.0 + 0.3275911 * x);
    let y = 1.0
        - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
            + 0.254829592)
            * t
            * (-x * x).exp();
    sign * y
}

#[cfg(test)]
fn normal_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / std::f64::consts::SQRT_2))
}

fn is_moneyline(market: &str) -> bool {
    matches!(
        market.to_lowercase().as_str(),
        "moneyline" | "h2h" | "winner" | "match_winner"
    )
}

/// Stern (1994) Brownian-motion live win probability for the HOME side.
/// Final margin ~ Normal(lead + mu * f, sigma^2 * f), f = fraction remaining,
/// mu = pregame expected home margin. P(home win) = Phi(E[margin] / sd).
/// f is floored so the sqrt(f) denominator cannot blow up at the buzzer.
#[cfg(test)]
fn live_winprob(lead: f64, pregame_margin: f64, fraction_remaining: f64, sigma: f64) -> f64 {
    let f = fraction_remaining.clamp(0.0, 1.0);
    if f <= 1e-4 {
        return if lead > 0.0 {
            0.999
        } else if lead < 0.0 {
            0.001
        } else {
            0.5
        };
    }
    let expected_margin = lead + pregame_margin * f;
    normal_cdf(expected_margin / (sigma * f.sqrt())).clamp(0.001, 0.999)
}

fn is_spread(market: &str) -> bool {
    matches!(
        market.to_lowercase().as_str(),
        "spread" | "spreads" | "handicap" | "point_spread"
    )
}

fn is_total(market: &str) -> bool {
    matches!(
        market.to_lowercase().as_str(),
        "total" | "totals" | "over_under" | "ou" | "game_total"
    )
}

fn canonical_model_market(market: &str) -> Option<&'static str> {
    if is_moneyline(market) {
        Some("moneyline")
    } else if is_spread(market) {
        Some("spread")
    } else if is_total(market) {
        Some("total")
    } else {
        None
    }
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .chars()
            .all(|character| character.is_ascii_hexdigit() && !character.is_ascii_uppercase())
}

fn independent_policy_eligible(
    policy: &IndependentModelPolicyInput,
    sport: &str,
    league: &str,
    market: &str,
) -> bool {
    let required: BTreeSet<&str> = policy.required_inputs.iter().map(String::as_str).collect();
    let common_inputs = [
        "home_score",
        "away_score",
        "seconds_remaining",
        "possession_home",
        "overtime_number",
        "provider_timestamp",
        "pregame_spread",
    ];
    let coefficients = [
        "intercept",
        "score_differential",
        "pregame_home_margin",
        "time_remaining_fraction",
        "score_time_interaction",
        "pregame_time_interaction",
        "home_possession",
        "overtime",
        "late_game",
    ];
    let valid_calibration = match policy.calibration_method.as_str() {
        "identity" => policy.beta_coefficients == [1.0, 1.0, 0.0],
        "beta" => beta_calibrate(0.5, &policy.beta_coefficients).is_some(),
        _ => false,
    };
    policy.evidence_passed
        && !policy.model_id.is_empty()
        && !policy.model_version.is_empty()
        && valid_sha256(&policy.model_hash)
        && valid_sha256(&policy.training_data_hash)
        && valid_sha256(&policy.registry_artifact_hash)
        && !policy.calibration_version.is_empty()
        && valid_sha256(&policy.calibration_hash)
        && policy.model_type == "basketball_game_state_logit_v1"
        && policy.feature_schema_version == "independent-features-v1"
        && policy.state_schema_version == "game-state-v2"
        && policy.sport.eq_ignore_ascii_case(sport)
        && policy.league.eq_ignore_ascii_case(league)
        && policy.market.eq_ignore_ascii_case(market)
        && sport.eq_ignore_ascii_case("basketball")
        && league.eq_ignore_ascii_case("nba")
        && market.eq_ignore_ascii_case("moneyline")
        && policy.test_sample_size >= 1000
        && policy.test_event_count >= 200
        && required.len() == common_inputs.len()
        && common_inputs.iter().all(|name| required.contains(name))
        && policy.parameters.len() == coefficients.len() + 3
        && coefficients.iter().all(|name| {
            policy
                .parameters
                .get(*name)
                .is_some_and(|value| value.is_finite() && value.abs() <= 100.0)
        })
        && policy
            .parameters
            .get("regulation_seconds")
            .is_some_and(|value| *value == 48.0 * 60.0)
        && policy
            .parameters
            .get("overtime_period_seconds")
            .is_some_and(|value| *value == 5.0 * 60.0)
        && policy
            .parameters
            .get("late_game_threshold")
            .is_some_and(|value| value.is_finite() && *value > 0.0 && *value < 1.0)
        && policy.missing_feature_behavior == "omit_output"
        && valid_calibration
}

fn independent_home_probability(
    policy: &IndependentModelPolicyInput,
    state: &StateInput,
    pregame_spread: f64,
) -> Option<f64> {
    let seconds_remaining = state.seconds_remaining?;
    let possession_home = state.possession_home?;
    let overtime_number = state.overtime_number?;
    if !pregame_spread.is_finite()
        || !seconds_remaining.is_finite()
        || (possession_home != 0.0 && possession_home != 1.0)
        || overtime_number < 0
    {
        return None;
    }
    let period_seconds = if overtime_number > 0 {
        *policy.parameters.get("overtime_period_seconds")?
    } else {
        *policy.parameters.get("regulation_seconds")?
    };
    if !(0.0..=period_seconds).contains(&seconds_remaining) {
        return None;
    }
    let parameter = |name: &str| policy.parameters.get(name).copied();
    let time_fraction = seconds_remaining / period_seconds;
    let lead = state.home_score - state.away_score;
    let pregame_home_margin = -pregame_spread;
    let late_game = if time_fraction <= parameter("late_game_threshold")? {
        1.0
    } else {
        0.0
    };
    let linear = parameter("intercept")?
        + parameter("score_differential")? * lead
        + parameter("pregame_home_margin")? * pregame_home_margin
        + parameter("time_remaining_fraction")? * time_fraction
        + parameter("score_time_interaction")? * lead * time_fraction
        + parameter("pregame_time_interaction")? * pregame_home_margin * time_fraction
        + parameter("home_possession")? * possession_home
        + parameter("overtime")? * if overtime_number > 0 { 1.0 } else { 0.0 }
        + parameter("late_game")? * late_game;
    if !linear.is_finite() {
        return None;
    }
    beta_calibrate(
        clamp(inv_logit(linear), 1e-9, 1.0 - 1e-9),
        &policy.beta_coefficients,
    )
}

/// Extra required edge by market efficiency/limits. Player props (anything not
/// a mainline market) have high vig and low limits, so demand more; totals a
/// little; moneyline/spread none.
fn market_premium(market: &str) -> f64 {
    if is_moneyline(market) || is_spread(market) {
        0.0
    } else if is_total(market) {
        0.01
    } else {
        0.02 // player props and other thin markets
    }
}

/// Probability the given spread side covers. `point` is the side's line
/// (e.g. home -6.5 -> point=-6.5); it covers if side_margin + point > 0.
/// Gaussian approximation — NFL key-number masses at 3/7 are a known
/// limitation and are not modeled here (this is a cross-check, not the price).
#[cfg(test)]
fn spread_cover_prob(
    lead: f64,
    pregame_margin: f64,
    fraction_remaining: f64,
    sigma: f64,
    point: f64,
    side: &str,
) -> Option<f64> {
    let f = fraction_remaining.clamp(0.0, 1.0);
    let side_lead = match side {
        "home" => lead,
        "away" => -lead,
        _ => return None,
    };
    if f <= 1e-4 {
        let margin = side_lead + point;
        return Some(if margin > 0.0 {
            0.999
        } else if margin < 0.0 {
            0.001
        } else {
            0.5
        });
    }
    let side_margin = match side {
        "home" => lead + pregame_margin * f,
        "away" => -(lead + pregame_margin * f),
        _ => return None,
    };
    Some(normal_cdf((side_margin + point) / (sigma * f.sqrt())).clamp(0.001, 0.999))
}

/// Probability the total goes over/under `line`. E[final] blends a pregame
/// total prior (weighted by fraction remaining) with the observed pace.
#[cfg(test)]
fn total_prob(
    current_total: f64,
    pregame_total: Option<f64>,
    fraction_remaining: f64,
    sigma_total: f64,
    line: f64,
    side: &str,
) -> f64 {
    let f = fraction_remaining.clamp(0.0, 1.0);
    let over = if f <= 1e-4 {
        if current_total > line {
            0.999
        } else if current_total < line {
            0.001
        } else {
            0.5
        }
    } else {
        let pace_final = if (1.0 - f) > 1e-3 {
            current_total / (1.0 - f)
        } else {
            current_total
        };
        let expected_final = match pregame_total {
            // w = f: trust the prior early, the observed pace late.
            Some(prior) => f * (current_total + prior * f) + (1.0 - f) * pace_final,
            None => pace_final,
        };
        (1.0 - normal_cdf((line - expected_final) / (sigma_total * f.sqrt()))).clamp(0.001, 0.999)
    };
    if side == "under" {
        1.0 - over
    } else {
        over
    }
}

/// Shin (1992/1993) de-vig: recovers "fair" probabilities from a booksum-laden
/// set of implied probabilities by estimating the insider-trading proportion z.
/// Relative to proportional (multiplicative) normalization, Shin shifts weight
/// toward favorites and away from longshots, partially correcting the
/// favorite-longshot bias. Solved for z by bisection so that sum(p_i) == 1.
fn devig_shin(implied: &[f64]) -> Option<Vec<f64>> {
    let booksum: f64 = implied.iter().sum();
    if booksum <= 1.0 || implied.len() < 2 {
        return None;
    }
    let sum_at = |z: f64| -> f64 {
        implied
            .iter()
            .map(|&q| ((z * z + 4.0 * (1.0 - z) * q * q / booksum).sqrt() - z) / (2.0 * (1.0 - z)))
            .sum::<f64>()
    };
    // f(z) = sum_at(z) - 1. f(0) = sqrt(booksum) - 1 > 0; sum decreases as z grows.
    let f = |z: f64| sum_at(z) - 1.0;
    if f(0.0) <= 0.0 {
        return None;
    }
    let (mut lo, mut hi) = (0.0_f64, 0.2_f64);
    let mut guard = 0;
    while f(hi) > 0.0 && hi < 0.95 {
        hi += 0.1;
        guard += 1;
        if guard > 12 {
            break;
        }
    }
    if f(hi) > 0.0 {
        return None;
    }
    for _ in 0..64 {
        let mid = 0.5 * (lo + hi);
        if f(mid) > 0.0 {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let z = 0.5 * (lo + hi);
    let raw: Vec<f64> = implied
        .iter()
        .map(|&q| ((z * z + 4.0 * (1.0 - z) * q * q / booksum).sqrt() - z) / (2.0 * (1.0 - z)))
        .collect();
    let total: f64 = raw.iter().sum();
    if !total.is_finite() || total <= 0.0 {
        return None;
    }
    Some(raw.iter().map(|p| p / total).collect())
}

fn devig_proportional(implied: &[f64]) -> Vec<f64> {
    let booksum: f64 = implied.iter().sum();
    if booksum <= 0.0 {
        return implied.to_vec();
    }
    implied.iter().map(|p| p / booksum).collect()
}

/// The de-vigged fair probability contributed by one source for one outcome,
/// with the method used and the source's booksum (overround proxy).
struct Fair {
    source_key: String,
    prob: f64,
    booksum: f64,
    method: &'static str,
    observed_at: f64,
    timestamp_trusted: bool,
}

/// Compute one source's fair value for `outcome` given all of its quotes in the
/// market. Exchanges are read at their mid (already ~de-vigged); traditional
/// books are de-vigged with Shin, falling back to proportional.
fn source_fair(
    outcome_key: &str,
    source_quotes: &[&QuoteInput],
    selected_method: Option<&str>,
) -> Option<Fair> {
    let target = source_quotes
        .iter()
        .find(|q| q.outcome_key() == outcome_key)?;
    let implied: Vec<f64> = source_quotes.iter().map(|q| q.probability).collect();
    let booksum: f64 = implied.iter().sum();
    let is_exchange = source_quotes.iter().any(|q| q.exchange());

    let (fair, method) = if is_exchange {
        // Exchange mid is already ~de-vigged; no multiplicative normalization.
        (clamp(target.probability, 0.001, 0.999), "exchange-mid")
    } else if implied.len() < 2 {
        // A single side from a traditional book cannot be de-vigged, so its
        // vig-laden price must not enter the consensus. Exclude the source.
        return None;
    } else {
        let idx = source_quotes
            .iter()
            .position(|q| q.outcome_key() == outcome_key)
            .unwrap();
        // Shin handles the vig; for a ~zero-hold 2-way book (booksum <= 1) it
        // returns None and we fall back to proportional (which is ~identity).
        match selected_method {
            Some("proportional") => (devig_proportional(&implied)[idx], "proportional"),
            Some("shin") if booksum <= 1.0 + 1e-12 => {
                (devig_proportional(&implied)[idx], "proportional-zero-hold")
            }
            Some("shin") => (devig_shin(&implied)?[idx], "shin"),
            Some(_) => return None,
            None => match devig_shin(&implied) {
                Some(fairs) => (fairs[idx], "shin"),
                None => (devig_proportional(&implied)[idx], "proportional"),
            },
        }
    };

    Some(Fair {
        source_key: target.source_key(),
        prob: clamp(fair, 0.001, 0.999),
        booksum: booksum.max(1.0),
        method,
        // A fair price depends on every leg used in the de-vig.  Timestamp it
        // at the oldest component so a fresh favorite cannot revive a stale
        // opposing side.
        observed_at: source_quotes
            .iter()
            .map(|quote| quote.observed_at)
            .fold(f64::INFINITY, f64::min),
        timestamp_trusted: source_quotes.iter().all(|quote| quote.timestamp_trusted),
    })
}

fn evaluate(request: EvaluateRequest, now_seconds: f64) -> Vec<SignalOutput> {
    if request.quotes.is_empty() {
        return Vec::new();
    }
    let max_age = request.max_age_seconds.max(1.0);

    // Independent live-model inputs are considered only when a reviewed exact-
    // segment policy is present. The operator flag alone cannot expose a model.
    let sport = request.sport.clone().unwrap_or_default();
    let league = request.league.clone().unwrap_or_default();
    let latest_state = request
        .states
        .iter()
        .filter(|s| {
            request.enable_independent_model
                && !request.independent_model_policies.is_empty()
                && s.timestamp_trusted
                && s.state_valid
                && s.home_score.is_finite()
                && s.away_score.is_finite()
                && s.home_score >= 0.0
                && s.away_score >= 0.0
                && s.seconds_remaining
                    .is_some_and(|seconds| seconds.is_finite() && seconds >= 0.0)
                && s.overtime_number.is_some_and(|number| number >= 0)
                && s.possession_home
                    .is_some_and(|value| value == 0.0 || value == 1.0)
                && now_seconds >= s.observed_at
                && now_seconds - s.observed_at <= max_age
        })
        .max_by(|a, b| a.observed_at.total_cmp(&b.observed_at));

    // Keep the freshest quote per (market, outcome, source).
    let mut freshest: BTreeMap<(String, String, String), QuoteInput> = BTreeMap::new();
    for quote in request.quotes {
        let valid_probability = |value: f64| value.is_finite() && value > 0.0 && value < 1.0;
        let valid_size = |value: f64| value.is_finite() && value >= 0.0;
        if !valid_probability(quote.probability)
            || quote.ask.is_some_and(|value| !valid_probability(value))
            || quote
                .bid
                .is_some_and(|value| !value.is_finite() || !(0.0..1.0).contains(&value))
            || quote.ask_size.is_some_and(|value| !valid_size(value))
            || quote.liquidity.is_some_and(|value| !valid_size(value))
        {
            continue;
        }
        let key = (
            quote.market_key().to_string(),
            quote.outcome_key().to_string(),
            quote.source_key().to_string(),
        );
        let replace = freshest
            .get(&key)
            .map(|current| quote.observed_at > current.observed_at)
            .unwrap_or(true);
        if replace {
            freshest.insert(key, quote);
        }
    }
    let current: Vec<QuoteInput> = freshest.into_values().collect();
    let pairs: BTreeSet<(String, String)> = current
        .iter()
        .map(|quote| {
            (
                quote.market_key().to_string(),
                quote.outcome_key().to_string(),
            )
        })
        .collect();
    let mut signals = Vec::new();

    for (market_key, outcome_key) in pairs {
        let same_market: Vec<&QuoteInput> = current
            .iter()
            .filter(|quote| quote.market_key() == market_key)
            .collect();
        let target_quotes: Vec<&QuoteInput> = same_market
            .iter()
            .copied()
            .filter(|quote| quote.outcome_key() == outcome_key)
            .collect();
        if target_quotes.is_empty() {
            continue;
        }
        let market_type = target_quotes[0].market.clone();
        let policy = request
            .model_policies
            .iter()
            .find(|candidate| candidate.market.eq_ignore_ascii_case(&market_type));
        let expected_outcomes: BTreeSet<&str> = same_market
            .iter()
            .map(|quote| quote.outcome_key())
            .collect();

        let model_market = canonical_model_market(&market_type);
        let independent_policy = model_market.and_then(|canonical_market| {
            request.independent_model_policies.iter().find(|candidate| {
                independent_policy_eligible(candidate, &sport, &league, canonical_market)
            })
        });

        // Reviewed independent live model for this outcome. It is a displayed
        // cross-check and never replaces the calibrated-consensus action basis.
        let model_live: Option<f64> = independent_policy.and_then(|model| {
            latest_state.and_then(|st| {
                let p_home = independent_home_probability(model, st, request.pregame_spread?)?;
                let is_away = outcome_key.eq_ignore_ascii_case("away")
                    || target_quotes[0]
                        .outcome
                        .eq_ignore_ascii_case(&request.away_outcome);
                Some(if is_away { 1.0 - p_home } else { p_home })
            })
        });
        let independent_model_version =
            model_live.and_then(|_| independent_policy.map(|model| model.model_version.clone()));
        let independent_model_hash =
            model_live.and_then(|_| independent_policy.map(|model| model.model_hash.clone()));
        let independent_calibration_version = model_live
            .and_then(|_| independent_policy.map(|model| model.calibration_version.clone()));
        let independent_calibration_hash =
            model_live.and_then(|_| independent_policy.map(|model| model.calibration_hash.clone()));
        let independent_model_sample_size = if model_live.is_some() {
            independent_policy
                .map(|model| model.test_sample_size)
                .unwrap_or(0)
        } else {
            0
        };
        let independent_model_event_count = if model_live.is_some() {
            independent_policy
                .map(|model| model.test_event_count)
                .unwrap_or(0)
        } else {
            0
        };

        // Per-source de-vigged fair for this outcome.
        let sources: BTreeSet<String> =
            same_market.iter().map(|quote| quote.source_key()).collect();
        let mut fairs: Vec<Fair> = Vec::new();
        for source in &sources {
            let source_quotes: Vec<&QuoteInput> = same_market
                .iter()
                .copied()
                .filter(|quote| quote.source_key() == *source)
                .collect();
            let source_outcomes: BTreeSet<&str> = source_quotes
                .iter()
                .map(|quote| quote.outcome_key())
                .collect();
            // Traditional books must provide the complete 2-way/3-way market
            // before de-vigging.  Treating a soccer home/away pair as binary
            // while omitting Draw materially inflates both fair prices.
            if !source_quotes.iter().any(|quote| quote.exchange())
                && source_outcomes != expected_outcomes
            {
                continue;
            }
            if let Some(fair) = source_fair(
                &outcome_key,
                &source_quotes,
                policy.map(|value| value.devig_method.as_str()),
            ) {
                fairs.push(fair);
            }
        }
        if fairs.is_empty() {
            continue;
        }

        // This app displays Polymarket execution. If a Polymarket quote exists,
        // evaluate that exact ask rather than silently borrowing a cheaper
        // sportsbook ask and showing its edge beside the Polymarket card.
        let has_polymarket = target_quotes
            .iter()
            .any(|quote| quote.source_key() == "polymarket");
        let preferred: Vec<&&QuoteInput> = target_quotes
            .iter()
            .filter(|quote| !has_polymarket || quote.source_key() == "polymarket")
            .collect();
        let fresh_preferred: Vec<&&QuoteInput> = preferred
            .iter()
            .copied()
            .filter(|quote| (now_seconds - quote.observed_at) <= max_age)
            .collect();
        let candidates = if fresh_preferred.is_empty() {
            &preferred
        } else {
            &fresh_preferred
        };
        let best = candidates
            .iter()
            .copied()
            .min_by(|a, b| {
                a.executable_probability()
                    .total_cmp(&b.executable_probability())
            })
            .expect("target quotes checked above");
        let executable = best.executable_probability();
        let target_source = best.source.clone();
        let target_source_key = best.source_key().to_string();
        let display_market = best.market.clone();
        let display_outcome = best.outcome.clone();
        let market_is_calibrated = policy.is_some();
        let target_booksum = fairs
            .iter()
            .find(|f| f.source_key == target_source_key)
            .map(|f| f.booksum)
            .unwrap_or(1.0);

        // LEAVE-ONE-OUT: the consensus fair excludes the book we would bet
        // (genuinely independent), and drops references older than max_age so a
        // dead source cannot linger in the consensus.
        let reference: Vec<&Fair> = fairs
            .iter()
            .filter(|f| {
                f.source_key != target_source_key
                    && f.timestamp_trusted
                    && now_seconds >= f.observed_at
                    && (now_seconds - f.observed_at) <= max_age
            })
            .collect();

        let age = (now_seconds - best.observed_at).max(0.0);
        // Spread is only defined when we have both sides of the book. For
        // fixed-odds quotes without bid/ask it is unknown, not zero.
        let spread: Option<f64> = match (best.ask, best.bid) {
            (Some(ask), Some(bid)) => Some((ask - bid).max(0.0)),
            _ => None,
        };
        let freshness_score = (1.0 - age / max_age).max(0.0);
        let spread_score = match spread {
            Some(s) => (1.0 - s / 0.12).max(0.0),
            None => 0.5, // unknown: neutral, neither rewarded nor blocked
        };

        let mut reasons = Vec::new();

        // Single-source case: no independent reference, so an edge is NOT
        // estimable. Report the market price honestly and block.
        if reference.is_empty() {
            let own = fairs
                .iter()
                .find(|f| f.source_key == target_source_key)
                .map(|f| f.prob)
                .unwrap_or(executable);
            let own_method = fairs
                .iter()
                .find(|f| f.source_key == target_source_key)
                .map(|f| f.method)
                .unwrap_or("n/a");
            reasons.push(format!(
                "only 1 price source ({target_source}); no independent fair, edge not estimable"
            ));
            reasons.push(format!(
                "market price {:.1}% ({} devig, overround {:.1}%)",
                executable * 100.0,
                own_method,
                (target_booksum - 1.0) * 100.0
            ));
            if let (Some(p), Some(st)) = (model_live, latest_state) {
                reasons.push(format!(
                    "reviewed independent model {:.1}% ({}, {:.0}s in modeled period left)",
                    p * 100.0,
                    independent_policy
                        .map(|model| model.model_version.as_str())
                        .unwrap_or("unavailable"),
                    st.seconds_remaining.unwrap_or(0.0)
                ));
            }
            signals.push(SignalOutput {
                event_id: request.event_id.clone(),
                market: display_market,
                outcome: display_outcome,
                model_probability: own,
                market_probability: executable,
                edge: 0.0,
                confidence: 0.0,
                action: "WATCH".to_string(),
                reasons,
                quote_source: target_source.clone(),
                market_fair_prob: own,
                devig_method: own_method.to_string(),
                overround: target_booksum,
                n_reference_sources: 0,
                model_live_prob: model_live,
                ev_per_stake: 0.0,
                kelly_fraction: 0.0,
                required_edge: 0.0,
                fair_stderr: 0.0,
                fillable_size: if best.exchange() {
                    best.ask_size
                } else {
                    best.liquidity
                },
                quality_freshness: (freshness_score * 1000.0).round() / 10.0,
                quality_agreement: 0.0,
                quality_sources: 0.0,
                quality_execution: (spread_score * 1000.0).round() / 10.0,
                quality_calibration: if market_is_calibrated { 100.0 } else { 0.0 },
                quality_data_completeness: 0.0,
                quality_provider_freshness: (freshness_score * 1000.0).round() / 10.0,
                quality_identity: if best.identity_valid { 100.0 } else { 0.0 },
                quality_model_sample_support: 0.0,
                quality_calibration_support: 0.0,
                quality_source_independence: 0.0,
                consensus_probability: own,
                calibrated_consensus_probability: None,
                independent_model_probability: model_live,
                independent_model_version,
                independent_model_hash,
                independent_calibration_version,
                independent_calibration_hash,
                independent_model_sample_size,
                independent_model_event_count,
                uncertainty_low: None,
                uncertainty_high: None,
                probability_net_ev_positive: None,
                net_expected_value_per_share: None,
                net_expected_value_total: None,
                consensus_method: policy
                    .map(|value| value.consensus_method.clone())
                    .unwrap_or_else(|| "display_only".to_string()),
                model_sample_size: policy
                    .map(|value| value.model_sample_size.unwrap_or(value.sample_size))
                    .unwrap_or(0),
                calibration_sample_size: policy.map(|value| value.sample_size).unwrap_or(0),
                gate_results: vec![gate(
                    "reference_source_support",
                    Some(false),
                    Some(0.0),
                    Some(2.0),
                    "no independent reference source family is available",
                )],
            });
            continue;
        }

        // One observation per canonical source family. A versioned artifact
        // may select a sharp-family baseline or regularized stacked logit;
        // otherwise equal-family logit remains display-only.
        let ref_probs: Vec<f64> = reference.iter().map(|f| f.prob).collect();
        let equal_family_fair = clamp(
            inv_logit(
                reference.iter().map(|f| logit(f.prob)).sum::<f64>() / reference.len() as f64,
            ),
            0.001,
            0.999,
        );
        let consensus_method = policy
            .map(|value| value.consensus_method.as_str())
            .unwrap_or("equal_family_logit");
        let selected_consensus = policy.and_then(|model| {
            consensus_probability(
                &reference,
                &model.consensus_method,
                model.sharp_source_family.as_deref(),
                model.consensus_intercept,
                &model.family_coefficients,
                &model.missing_family_coefficients,
            )
        });
        let consensus_supported = policy.is_none() || selected_consensus.is_some();
        let fair = selected_consensus.unwrap_or(equal_family_fair);

        let dispersion = if ref_probs.len() > 1 {
            population_std_dev(&ref_probs)
        } else {
            0.08
        };
        let source_count = reference.len();
        let fillable_size = if best.exchange() {
            best.ask_size
        } else {
            best.liquidity
        };

        let calibrated = policy.and_then(|model| match model.calibration_method.as_str() {
            "identity" => Some(fair),
            "beta" => beta_calibrate(fair, &model.beta_coefficients),
            _ => None,
        });
        let mut probability_samples = Vec::new();
        let mut net_samples = Vec::new();
        let mut execution_cost_offsets = Vec::new();
        if let Some(model) = policy {
            if !model.uncertainty_draws.is_empty() {
                for draw in &model.uncertainty_draws {
                    let mut draw_fairs: Vec<Fair> = Vec::new();
                    for source in &sources {
                        let source_quotes: Vec<&QuoteInput> = same_market
                            .iter()
                            .copied()
                            .filter(|quote| quote.source_key() == *source)
                            .collect();
                        let source_outcomes: BTreeSet<&str> = source_quotes
                            .iter()
                            .map(|quote| quote.outcome_key())
                            .collect();
                        if !source_quotes.iter().any(|quote| quote.exchange())
                            && source_outcomes != expected_outcomes
                        {
                            continue;
                        }
                        if let Some(draw_fair) =
                            source_fair(&outcome_key, &source_quotes, Some(&draw.devig_method))
                        {
                            draw_fairs.push(draw_fair);
                        }
                    }
                    let draw_reference: Vec<&Fair> = draw_fairs
                        .iter()
                        .filter(|draw_fair| {
                            draw_fair.source_key != target_source_key
                                && draw_fair.timestamp_trusted
                                && now_seconds >= draw_fair.observed_at
                                && (now_seconds - draw_fair.observed_at) <= max_age
                        })
                        .collect();
                    let draw_consensus = consensus_probability(
                        &draw_reference,
                        &draw.consensus_method,
                        draw.sharp_source_family.as_deref(),
                        draw.consensus_intercept,
                        &draw.family_coefficients,
                        &draw.missing_family_coefficients,
                    );
                    if let Some(sample) = draw_consensus.and_then(|probability| {
                        beta_calibrate(probability, &draw.beta_coefficients)
                    }) {
                        if draw.execution_cost_offset.is_finite() {
                            probability_samples.push(sample);
                            net_samples.push(sample - executable - draw.execution_cost_offset);
                            execution_cost_offsets.push(draw.execution_cost_offset);
                        }
                    }
                }
            } else if model.beta_bootstrap_coefficients.len() == model.execution_cost_offsets.len()
            {
                for (coefficients, cost_offset) in model
                    .beta_bootstrap_coefficients
                    .iter()
                    .zip(model.execution_cost_offsets.iter())
                {
                    let sample = beta_calibrate(fair, coefficients);
                    if let Some(probability) = sample {
                        if cost_offset.is_finite() {
                            probability_samples.push(probability);
                            net_samples.push(probability - executable - cost_offset);
                            execution_cost_offsets.push(*cost_offset);
                        }
                    }
                }
            }
        }
        let uncertainty_low = quantile(&probability_samples, 0.025);
        let uncertainty_high = quantile(&probability_samples, 0.975);
        let net_uncertainty_low = quantile(&net_samples, 0.025);
        let expected_execution_cost_offset = if execution_cost_offsets.is_empty() {
            None
        } else {
            Some(mean(&execution_cost_offsets))
        };
        let probability_net_ev_positive = if net_samples.is_empty() {
            None
        } else {
            Some(
                net_samples.iter().filter(|value| **value > 0.0).count() as f64
                    / net_samples.len() as f64,
            )
        };
        let decision_probability = calibrated.unwrap_or(fair);
        let gross_probability_gap = decision_probability - executable;
        let net_expected_value_per_share = calibrated
            .zip(expected_execution_cost_offset)
            .map(|(value, cost_offset)| value - executable - cost_offset);
        let net_expected_value_total = net_expected_value_per_share
            .zip(fillable_size)
            .map(|(value, shares)| value * shares);
        let edge = net_expected_value_per_share.unwrap_or(gross_probability_gap);

        // Fractional Kelly uses the lower historically bootstrapped probability
        // bound. Cross-book dispersion is a quality dimension, not a standard error.
        let lambda = request.kelly_fraction.unwrap_or(0.25);
        let fair_stderr = match (uncertainty_low, uncertainty_high) {
            (Some(low), Some(high)) => (high - low) / (2.0 * 1.96),
            _ => 0.0,
        };
        let required_edge = request.edge_threshold + market_premium(&display_market);
        let expected_executable_cost = clamp(
            executable + expected_execution_cost_offset.unwrap_or(0.0),
            0.001,
            0.999,
        );
        let ev_per_stake = if expected_executable_cost > 1e-6 {
            decision_probability / expected_executable_cost - 1.0
        } else {
            0.0
        };
        let edge_shrunk = net_uncertainty_low.unwrap_or(0.0).max(0.0);
        let kelly_fraction = if expected_executable_cost < 0.999 {
            (lambda * edge_shrunk / (1.0 - expected_executable_cost)).max(0.0)
        } else {
            0.0
        };

        let agreement_score = (1.0 - dispersion / 0.12).max(0.0);
        let source_score = (source_count as f64 / 3.0).min(1.0);
        let reference_freshness = reference
            .iter()
            .map(|f| (-(now_seconds - f.observed_at).max(0.0) / max_age).exp())
            .sum::<f64>()
            / source_count as f64;
        let quality_freshness = 0.5 * freshness_score + 0.5 * reference_freshness;
        let confidence = 100.0
            * (0.30 * quality_freshness
                + 0.30 * agreement_score
                + 0.25 * source_score
                + 0.15 * spread_score);

        let target_method = fairs
            .iter()
            .find(|f| f.source_key == target_source_key)
            .map(|f| f.method)
            .unwrap_or("n/a");

        reasons.push(format!(
            "{} consensus {:.1}% from {} independent source families; dispersion {:.1}% is quality-only",
            consensus_method,
            fair * 100.0,
            source_count,
            dispersion * 100.0
        ));
        reasons.push(format!(
            "best executable {:.1}% via {} ({} devig, overround {:.1}%)",
            executable * 100.0,
            target_source,
            target_method,
            (target_booksum - 1.0) * 100.0
        ));
        if let (Some(p), Some(st)) = (model_live, latest_state) {
            reasons.push(format!(
                "reviewed independent model {:.1}% ({}, {:+.1}pp vs consensus, {:.0}s in modeled period left)",
                p * 100.0,
                independent_policy
                    .map(|model| model.model_version.as_str())
                    .unwrap_or("unavailable"),
                (p - fair) * 100.0,
                st.seconds_remaining.unwrap_or(0.0)
            ));
        }
        if let Some(probability) = calibrated {
            reasons.push(format!(
                "calibrated consensus {:.1}% with P(net EV > 0) {} and net EV {}",
                probability * 100.0,
                probability_net_ev_positive
                    .map(|value| format!("{:.1}%", value * 100.0))
                    .unwrap_or_else(|| "unavailable".to_string()),
                net_expected_value_total
                    .map(|value| format!("${value:+.2}"))
                    .unwrap_or_else(|| "unavailable".to_string()),
            ));
        }
        reasons.push(format!(
            "EV {:+.1}%/stake; Kelly {:.1}% bankroll; required edge {:.1}%{}",
            ev_per_stake * 100.0,
            kelly_fraction * 100.0,
            required_edge * 100.0,
            fillable_size
                .map(|size| format!("; fillable {size:.0} shares"))
                .unwrap_or_default(),
        ));

        let mut blockers = Vec::new();
        let mut gate_results = Vec::new();
        if age > max_age {
            blockers.push(format!("quote stale ({age:.0}s)"));
        }
        if !best.timestamp_trusted {
            blockers.push("provider timestamp unavailable or untrusted".to_string());
        }
        if !best.identity_valid {
            blockers.push("market identity is ambiguous or quarantined".to_string());
        }
        if best.observed_at > now_seconds + 5.0 {
            blockers.push("provider timestamp is in the future".to_string());
        }
        if source_count < 2 {
            blockers.push("fewer than 2 independent reference sources".to_string());
        }
        if let Some(s) = spread {
            if s > 0.08 {
                blockers.push(format!("wide executable spread ({:.1}%)", s * 100.0));
            }
        }
        if best.exchange() && best.ask.is_none() {
            blockers.push("no executable ask for exchange quote".to_string());
        }
        if best.exchange() && !best.depth_complete {
            blockers.push("complete executable order-book depth unavailable".to_string());
        }
        if best.exchange() && !best.fee_metadata_known {
            blockers.push("fee metadata unavailable".to_string());
        }
        let execution_ready = best.ask.is_some()
            && best.depth_complete
            && best.fee_metadata_known
            && fillable_size.is_some_and(|size| size > 0.0)
            && best.accepting_orders;
        if !execution_ready && !best.exchange() {
            blockers.push(
                "sportsbook quote is reference-only; complete executable depth unavailable"
                    .to_string(),
            );
        }
        if !best.accepting_orders {
            blockers.push("market is not accepting orders".to_string());
        }
        if !consensus_supported {
            blockers.push("selected consensus model lacks required source families".to_string());
        }
        if !market_is_calibrated || calibrated.is_none() {
            blockers.push("validated calibration artifact unavailable for market".to_string());
        }
        if let Some(model) = policy {
            if model.model_sample_size.unwrap_or(model.sample_size) < 1000 {
                blockers.push("chronological model-selection sample is below 1,000".to_string());
            }
            if model.sample_size < 1000 {
                blockers.push("chronological calibration sample is below 1,000".to_string());
            }
        }
        if probability_samples.len() < 200 || probability_net_ev_positive.is_none() {
            blockers.push("historically estimated event-block uncertainty unavailable".to_string());
        }
        if edge < required_edge {
            blockers.push(format!(
                "net edge {:.1}% below required {:.1}% (base {:.1}% + market premium {:.1}%)",
                edge * 100.0,
                required_edge * 100.0,
                request.edge_threshold * 100.0,
                (required_edge - request.edge_threshold) * 100.0
            ));
        }
        if let (Some(model), Some(probability)) = (policy, probability_net_ev_positive) {
            if probability < model.min_probability_positive {
                blockers.push(format!(
                    "P(net EV > 0) {:.1}% below required {:.1}%",
                    probability * 100.0,
                    model.min_probability_positive * 100.0,
                ));
            }
        }
        if let Some(model) = policy {
            match net_expected_value_total {
                Some(value) if value >= model.min_expected_value_dollars => {}
                Some(value) => blockers.push(format!(
                    "net expected value ${value:.2} below required ${:.2}",
                    model.min_expected_value_dollars,
                )),
                None => blockers.push("net expected dollar value unavailable".to_string()),
            }
        }
        if confidence < request.confidence_threshold {
            blockers.push(format!(
                "signal quality {confidence:.0} below {:.0}",
                request.confidence_threshold
            ));
        }
        gate_results.push(gate(
            "provider_freshness",
            Some(age <= max_age && best.timestamp_trusted && best.observed_at <= now_seconds + 5.0),
            Some(age),
            Some(max_age),
            "provider time is trusted, non-future, and within the configured age limit",
        ));
        gate_results.push(gate(
            "reference_source_support",
            Some(source_count >= 2),
            Some(source_count as f64),
            Some(2.0),
            "at least two leave-one-out source families are required",
        ));
        gate_results.push(gate(
            "market_identity",
            Some(best.identity_valid),
            None,
            None,
            "canonical event, market, line, scope, and outcome identity must be unambiguous",
        ));
        gate_results.push(gate(
            "market_status",
            Some(best.accepting_orders),
            None,
            None,
            "target market must be active, unresolved, unrestricted, and accepting orders",
        ));
        gate_results.push(gate(
            "executable_fill",
            Some(execution_ready),
            fillable_size,
            Some(0.0),
            "target requires an ask, complete depth, fee metadata, and a positive simulated fill",
        ));
        gate_results.push(gate(
            "consensus_policy",
            policy.map(|_| consensus_supported),
            None,
            None,
            format!("selected consensus method: {consensus_method}"),
        ));
        gate_results.push(gate(
            "model_sample_support",
            policy.map(|model| model.model_sample_size.unwrap_or(model.sample_size) >= 1000),
            policy.map(|model| model.model_sample_size.unwrap_or(model.sample_size) as f64),
            Some(1000.0),
            "chronological model-selection sample must meet the policy minimum",
        ));
        gate_results.push(gate(
            "calibration_support",
            policy.map(|_| calibrated.is_some()),
            policy.map(|model| model.sample_size as f64),
            Some(1000.0),
            "versioned chronological calibration policy is required",
        ));
        gate_results.push(gate(
            "uncertainty_support",
            policy.map(|_| probability_samples.len() >= 200),
            Some(probability_samples.len() as f64),
            Some(200.0),
            "event-block bootstrap draws must cover calibration and execution cost",
        ));
        gate_results.push(gate(
            "probability_net_ev_positive",
            policy.and_then(|model| {
                probability_net_ev_positive.map(|value| value >= model.min_probability_positive)
            }),
            probability_net_ev_positive,
            policy.map(|model| model.min_probability_positive),
            "historical bootstrap probability that net EV is positive",
        ));
        gate_results.push(gate(
            "minimum_expected_value",
            policy.and_then(|model| {
                net_expected_value_total.map(|value| value >= model.min_expected_value_dollars)
            }),
            net_expected_value_total,
            policy.map(|model| model.min_expected_value_dollars),
            "minimum expected paper dollars after executable cost",
        ));
        gate_results.push(gate(
            "net_edge",
            Some(edge >= required_edge),
            Some(edge),
            Some(required_edge),
            "calibrated probability minus executable cost exceeds the policy floor",
        ));
        gate_results.push(gate(
            "signal_quality",
            Some(confidence >= request.confidence_threshold),
            Some(confidence),
            Some(request.confidence_threshold),
            "policy summary of data reliability; not a win probability",
        ));
        let action = if blockers.is_empty() {
            "PAPER_BET"
        } else {
            "WATCH"
        };
        reasons.extend(blockers);

        signals.push(SignalOutput {
            event_id: request.event_id.clone(),
            market: display_market,
            outcome: display_outcome,
            // Legacy transport alias; canonical fields below keep consensus
            // and independent-model output separate.
            model_probability: fair,
            market_probability: executable,
            edge,
            confidence: (confidence * 10.0).round() / 10.0,
            action: action.to_string(),
            reasons,
            quote_source: target_source,
            market_fair_prob: fair,
            devig_method: target_method.to_string(),
            overround: target_booksum,
            n_reference_sources: source_count as i64,
            model_live_prob: model_live,
            ev_per_stake,
            kelly_fraction,
            required_edge,
            fair_stderr,
            fillable_size,
            quality_freshness: (quality_freshness * 1000.0).round() / 10.0,
            quality_agreement: (agreement_score * 1000.0).round() / 10.0,
            quality_sources: (source_score * 1000.0).round() / 10.0,
            quality_execution: (spread_score * 1000.0).round() / 10.0,
            quality_calibration: policy
                .map(|model| (model.sample_size as f64 / 1000.0).min(1.0) * 100.0)
                .unwrap_or(0.0),
            quality_data_completeness: if best.depth_complete
                && best.fee_metadata_known
                && fillable_size.is_some()
            {
                100.0
            } else {
                0.0
            },
            quality_provider_freshness: (quality_freshness * 1000.0).round() / 10.0,
            quality_identity: if best.identity_valid { 100.0 } else { 0.0 },
            quality_model_sample_support: policy
                .map(|model| {
                    (model.model_sample_size.unwrap_or(model.sample_size) as f64 / 1000.0).min(1.0)
                        * 100.0
                })
                .unwrap_or(0.0),
            quality_calibration_support: policy
                .map(|model| (model.sample_size as f64 / 1000.0).min(1.0) * 100.0)
                .unwrap_or(0.0),
            quality_source_independence: (source_score * 1000.0).round() / 10.0,
            consensus_probability: fair,
            calibrated_consensus_probability: calibrated,
            independent_model_probability: model_live,
            independent_model_version,
            independent_model_hash,
            independent_calibration_version,
            independent_calibration_hash,
            independent_model_sample_size,
            independent_model_event_count,
            uncertainty_low,
            uncertainty_high,
            probability_net_ev_positive,
            net_expected_value_per_share,
            net_expected_value_total,
            consensus_method: consensus_method.to_string(),
            model_sample_size: policy
                .map(|model| model.model_sample_size.unwrap_or(model.sample_size))
                .unwrap_or(0),
            calibration_sample_size: policy.map(|model| model.sample_size).unwrap_or(0),
            gate_results,
        });
    }

    signals.sort_by(|a, b| {
        let a_paper = a.action == "PAPER_BET";
        let b_paper = b.action == "PAPER_BET";
        b_paper
            .cmp(&a_paper)
            .then_with(|| b.edge.total_cmp(&a.edge))
    });
    signals
}

#[pyfunction]
fn evaluate_json(request_json: &str) -> PyResult<String> {
    let request: EvaluateRequest = serde_json::from_str(request_json)
        .map_err(|error| PyValueError::new_err(format!("invalid engine request: {error}")))?;
    if !request.as_of.is_finite() {
        return Err(PyValueError::new_err(
            "as_of must be a finite Unix timestamp",
        ));
    }
    let as_of = request.as_of;
    serde_json::to_string(&evaluate(request, as_of))
        .map_err(|error| PyValueError::new_err(format!("could not encode signals: {error}")))
}

#[pymodule]
fn _native_engine(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(evaluate_json, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quote(source: &str, outcome: &str, probability: f64, now: f64) -> QuoteInput {
        QuoteInput {
            market: "moneyline".to_string(),
            outcome: outcome.to_string(),
            comparison_market: None,
            comparison_outcome: None,
            comparison_source: None,
            probability,
            source: source.to_string(),
            observed_at: now,
            timestamp_trusted: true,
            identity_valid: true,
            bid: Some(probability - 0.01),
            ask: Some(probability + 0.01),
            source_weight: Some(1.0),
            is_exchange: Some(true), // exchange-mid: no multiplicative devig in tests
            decimal_odds: None,
            liquidity: None,
            ask_size: Some(100.0),
            depth_complete: true,
            fee_metadata_known: true,
            accepting_orders: true,
        }
    }

    fn request(quotes: Vec<QuoteInput>) -> EvaluateRequest {
        request_with(quotes, Vec::new(), None)
    }

    fn request_with(
        quotes: Vec<QuoteInput>,
        states: Vec<StateInput>,
        sport: Option<String>,
    ) -> EvaluateRequest {
        let policy = ModelPolicyInput {
            market: "moneyline".to_string(),
            devig_method: "shin".to_string(),
            consensus_method: "equal_family_logit".to_string(),
            calibration_method: "identity".to_string(),
            beta_coefficients: vec![1.0, 1.0, 0.0],
            beta_bootstrap_coefficients: vec![vec![1.0, 1.0, 0.0]; 200],
            execution_cost_offsets: vec![0.0; 200],
            min_probability_positive: 0.95,
            min_expected_value_dollars: 0.0,
            sample_size: 1000,
            model_sample_size: Some(1000),
            sharp_source_family: None,
            consensus_intercept: 0.0,
            family_coefficients: BTreeMap::new(),
            missing_family_coefficients: BTreeMap::new(),
            uncertainty_draws: Vec::new(),
        };
        let independent_policy = IndependentModelPolicyInput {
            model_id: "nba-moneyline-game-state-logit".to_string(),
            model_version: "nba-moneyline-test".to_string(),
            model_hash: "a".repeat(64),
            training_data_hash: "b".repeat(64),
            registry_artifact_hash: "c".repeat(64),
            sport: "basketball".to_string(),
            league: "nba".to_string(),
            market: "moneyline".to_string(),
            model_type: "basketball_game_state_logit_v1".to_string(),
            feature_schema_version: "independent-features-v1".to_string(),
            state_schema_version: "game-state-v2".to_string(),
            required_inputs: vec![
                "home_score".to_string(),
                "away_score".to_string(),
                "seconds_remaining".to_string(),
                "possession_home".to_string(),
                "overtime_number".to_string(),
                "provider_timestamp".to_string(),
                "pregame_spread".to_string(),
            ],
            parameters: BTreeMap::from([
                ("intercept".to_string(), 0.0),
                ("score_differential".to_string(), 0.22),
                ("pregame_home_margin".to_string(), 0.08),
                ("time_remaining_fraction".to_string(), -0.1),
                ("score_time_interaction".to_string(), -0.05),
                ("pregame_time_interaction".to_string(), 0.02),
                ("home_possession".to_string(), 0.08),
                ("overtime".to_string(), 0.0),
                ("late_game".to_string(), 0.1),
                ("late_game_threshold".to_string(), 0.25),
                ("regulation_seconds".to_string(), 2880.0),
                ("overtime_period_seconds".to_string(), 300.0),
            ]),
            calibration_method: "identity".to_string(),
            calibration_version: "nba-moneyline-cal-test".to_string(),
            calibration_hash: "d".repeat(64),
            beta_coefficients: vec![1.0, 1.0, 0.0],
            test_sample_size: 1000,
            test_event_count: 200,
            missing_feature_behavior: "omit_output".to_string(),
            evidence_passed: true,
        };
        EvaluateRequest {
            as_of: 1_000.0,
            event_id: "e".to_string(),
            confidence_threshold: 50.0,
            edge_threshold: 0.02,
            max_age_seconds: 20.0,
            away_outcome: "away".to_string(),
            quotes,
            states,
            sport,
            league: Some("nba".to_string()),
            pregame_spread: Some(0.0),
            pregame_total: None,
            kelly_fraction: None,
            enable_independent_model: true,
            model_policies: vec![policy],
            independent_model_policies: vec![independent_policy],
        }
    }

    fn state(home: f64, away: f64, frac: f64, at: f64) -> StateInput {
        StateInput {
            home_score: home,
            away_score: away,
            observed_at: at,
            seconds_remaining: Some(frac * 2880.0),
            overtime_number: Some(0),
            timestamp_trusted: true,
            state_valid: true,
            possession_home: Some(1.0),
        }
    }

    #[test]
    fn a_soft_book_lagging_the_consensus_produces_a_paper_bet() {
        let now = 1_000.0;
        let mut quotes = Vec::new();
        // Two sharp/exchange books agree home is ~60%.
        for source in ["A", "B"] {
            quotes.push(quote(source, "home", 0.60, now));
            quotes.push(quote(source, "away", 0.40, now));
        }
        // Soft book C is stale/underpriced on home: we can buy home at ask 0.55.
        let mut c_home = quote("C", "home", 0.545, now);
        c_home.bid = Some(0.54);
        c_home.ask = Some(0.55);
        let mut c_away = quote("C", "away", 0.455, now);
        c_away.bid = Some(0.45);
        c_away.ask = Some(0.46);
        quotes.push(c_home);
        quotes.push(c_away);

        let results = evaluate(request(quotes), now);
        let home = results
            .iter()
            .find(|signal| signal.outcome == "home")
            .unwrap();
        // Executable is C's 0.55 ask; reference (A,B) fair ~0.60 -> edge ~+0.05.
        assert_eq!(home.quote_source, "C");
        assert!(home.edge > 0.02, "edge was {}", home.edge);
        assert_eq!(home.action, "PAPER_BET");
        assert_eq!(home.n_reference_sources, 2);
    }

    #[test]
    fn polymarket_card_uses_the_polymarket_ask_even_when_a_book_is_cheaper() {
        let now = 1_000.0;
        let mut quotes = Vec::new();
        for source in ["A", "B"] {
            quotes.push(quote(source, "home", 0.64, now));
            quotes.push(quote(source, "away", 0.36, now));
        }
        let mut cheaper_book = quote("C", "home", 0.54, now);
        cheaper_book.bid = Some(0.53);
        cheaper_book.ask = Some(0.55);
        quotes.push(cheaper_book);
        quotes.push(quote("C", "away", 0.46, now));
        let mut poly_home = quote("Polymarket", "home", 0.59, now);
        poly_home.bid = Some(0.58);
        poly_home.ask = Some(0.60);
        quotes.push(poly_home);
        quotes.push(quote("Polymarket", "away", 0.41, now));

        let home = evaluate(request(quotes), now)
            .into_iter()
            .find(|signal| signal.outcome == "home")
            .unwrap();
        assert_eq!(home.quote_source, "Polymarket");
        assert!((home.market_probability - 0.60).abs() < 1e-9);
        assert!((home.edge - (home.market_fair_prob - 0.60)).abs() < 1e-9);
    }

    #[test]
    fn data_quality_does_not_increase_just_because_edge_is_larger() {
        let now = 1_000.0;
        let result_for_ask = |ask: f64| {
            let mut quotes = Vec::new();
            for source in ["A", "B"] {
                quotes.push(quote(source, "home", 0.60, now));
                quotes.push(quote(source, "away", 0.40, now));
            }
            let mut poly_home = quote("Polymarket", "home", ask - 0.01, now);
            poly_home.bid = Some(ask - 0.02);
            poly_home.ask = Some(ask);
            quotes.push(poly_home);
            quotes.push(quote("Polymarket", "away", 1.0 - ask + 0.01, now));
            evaluate(request(quotes), now)
                .into_iter()
                .find(|signal| signal.outcome == "home")
                .unwrap()
        };
        let expensive = result_for_ask(0.58);
        let cheap = result_for_ask(0.54);
        assert!(cheap.edge > expensive.edge);
        assert_eq!(cheap.confidence, expensive.confidence);
        assert_eq!(cheap.quality_freshness, expensive.quality_freshness);
        assert_eq!(cheap.quality_agreement, expensive.quality_agreement);
        assert_eq!(cheap.quality_sources, expensive.quality_sources);
        assert_eq!(cheap.quality_execution, expensive.quality_execution);
    }

    #[test]
    fn a_single_source_has_no_independent_reference() {
        let now = 1_000.0;
        let results = evaluate(
            request(vec![
                quote("one", "home", 0.5, now),
                quote("one", "away", 0.5, now),
            ]),
            now,
        );
        assert!(results.iter().all(|signal| signal.action == "WATCH"));
        assert!(results.iter().all(|signal| signal.n_reference_sources == 0));
        assert!(results[0]
            .reasons
            .iter()
            .any(|reason| reason.contains("no independent fair")));
    }

    #[test]
    fn normal_cdf_is_sane() {
        assert!((normal_cdf(0.0) - 0.5).abs() < 1e-6);
        assert!((normal_cdf(1.96) - 0.975).abs() < 1e-3);
        assert!((normal_cdf(-1.96) - 0.025).abs() < 1e-3);
    }

    #[test]
    fn live_winprob_behaves() {
        let sigma = 11.5;
        // Tied at tip-off is a coin flip.
        assert!((live_winprob(0.0, 0.0, 1.0, sigma) - 0.5).abs() < 1e-9);
        // A 10-point lead with a quarter left is very safe.
        assert!(live_winprob(10.0, 0.0, 0.25, sigma) > 0.9);
        // Trailing by 10 late is very unlikely to win.
        assert!(live_winprob(-10.0, 0.0, 0.25, sigma) < 0.1);
        // Monotonic increasing in the lead.
        assert!(live_winprob(5.0, 0.0, 0.5, sigma) > live_winprob(1.0, 0.0, 0.5, sigma));
        // At the buzzer, any lead is decisive.
        assert!(live_winprob(1.0, 0.0, 0.0, sigma) > 0.99);
        // Pregame favorite prior lifts an early tie above 0.5.
        assert!(live_winprob(0.0, 6.0, 0.9, sigma) > 0.5);
    }

    #[test]
    fn spread_cover_prob_behaves() {
        let sigma = 11.5;
        // Home -6.5 (point=-6.5), home up 10 with a quarter left -> likely covers.
        let p = spread_cover_prob(10.0, 0.0, 0.25, sigma, -6.5, "home").unwrap();
        assert!(p > 0.7, "cover prob was {p}");
        // The away +6.5 side is the complement of home -6.5 (continuous, no push).
        let q = spread_cover_prob(10.0, 0.0, 0.25, sigma, 6.5, "away").unwrap();
        assert!((p + q - 1.0).abs() < 1e-6);
        // At the buzzer a covered number is decisive.
        assert!(spread_cover_prob(10.0, 0.0, 0.0, sigma, -6.5, "home").unwrap() > 0.99);
        // Unknown side -> no model.
        assert!(spread_cover_prob(0.0, 0.0, 0.5, sigma, -3.0, "draw").is_none());
    }

    #[test]
    fn sizing_fields_and_risk_normalized_gate() {
        let now = 1_000.0;
        // Three exchange books agree home ~0.60; a cheap book offers home at 0.55.
        let mut quotes = Vec::new();
        for source in ["A", "B"] {
            quotes.push(quote(source, "home", 0.60, now));
            quotes.push(quote(source, "away", 0.40, now));
        }
        let mut cheap = quote("C", "home", 0.545, now);
        cheap.ask = Some(0.55);
        cheap.bid = Some(0.54);
        cheap.liquidity = Some(1234.0);
        cheap.ask_size = Some(1234.0);
        quotes.push(cheap);
        quotes.push(quote("C", "away", 0.455, now));

        let home = evaluate(request(quotes), now)
            .into_iter()
            .find(|s| s.outcome == "home")
            .unwrap();
        // EV per stake = fair/executable - 1 = 0.60/0.55 - 1 ~ 0.0909.
        assert!((home.ev_per_stake - (0.60 / 0.55 - 1.0)).abs() < 0.02);
        // Historically estimated uncertainty is gated separately from the
        // declared base edge; cross-book dispersion is not added as a fake SE.
        assert!((home.required_edge - 0.02).abs() < 1e-6);
        assert!(home.kelly_fraction > 0.0 && home.kelly_fraction < 0.25);
        assert_eq!(home.fillable_size, Some(1234.0));
        assert_eq!(home.action, "PAPER_BET");
    }

    #[test]
    fn total_prob_behaves() {
        let sigma = 16.0;
        // Way over the line already with little time left -> over is near-certain.
        assert!(total_prob(230.0, Some(220.0), 0.05, sigma, 210.5, "over") > 0.9);
        // Over and under are complementary.
        let over = total_prob(100.0, Some(220.0), 0.5, sigma, 220.5, "over");
        let under = total_prob(100.0, Some(220.0), 0.5, sigma, 220.5, "under");
        assert!((over + under - 1.0).abs() < 1e-6);
        // Blistering first-half pace pushes the projection over a pregame-average line.
        assert!(total_prob(130.0, Some(220.0), 0.5, sigma, 220.5, "over") > 0.5);
    }

    #[test]
    fn live_model_is_reported_as_a_cross_check() {
        let now = 1_000.0;
        let mut quotes = Vec::new();
        for source in ["A", "B"] {
            quotes.push(quote(source, "home", 0.60, now));
            quotes.push(quote(source, "away", 0.40, now));
        }
        // Home up 12 with a quarter to go -> live model should be well above 60%.
        let states = vec![state(70.0, 58.0, 0.25, now)];
        let results = evaluate(
            request_with(quotes, states, Some("basketball".to_string())),
            now,
        );
        let home = results.iter().find(|s| s.outcome == "home").unwrap();
        let away = results.iter().find(|s| s.outcome == "away").unwrap();
        let hp = home.model_live_prob.expect("home model prob");
        let ap = away.model_live_prob.expect("away model prob");
        assert!(hp > 0.85, "home live prob was {hp}");
        assert!((hp + ap - 1.0).abs() < 1e-6, "home/away should complement");
        assert!(home
            .reasons
            .iter()
            .any(|r| r.contains("reviewed independent model")));
        assert_eq!(
            home.independent_model_version.as_deref(),
            Some("nba-moneyline-test")
        );
        assert_eq!(
            home.independent_calibration_version.as_deref(),
            Some("nba-moneyline-cal-test")
        );
        assert_eq!(home.independent_model_sample_size, 1000);
    }

    #[test]
    fn operator_flag_without_model_evidence_cannot_expose_independent_output() {
        let now = 1_000.0;
        let quotes = vec![
            quote("A", "home", 0.60, now),
            quote("A", "away", 0.40, now),
            quote("B", "home", 0.60, now),
            quote("B", "away", 0.40, now),
        ];
        let mut input = request_with(
            quotes,
            vec![state(70.0, 58.0, 0.25, now)],
            Some("basketball".to_string()),
        );
        input.independent_model_policies.clear();

        let results = evaluate(input, now);

        assert!(results
            .iter()
            .all(|signal| signal.independent_model_probability.is_none()));
    }

    #[test]
    fn malformed_registry_evidence_is_rejected_again_at_the_rust_boundary() {
        let now = 1_000.0;
        let quotes = vec![
            quote("A", "home", 0.60, now),
            quote("A", "away", 0.40, now),
            quote("B", "home", 0.60, now),
            quote("B", "away", 0.40, now),
        ];
        let mut input = request_with(
            quotes,
            vec![state(70.0, 58.0, 0.25, now)],
            Some("basketball".to_string()),
        );
        input.independent_model_policies[0].calibration_hash = "not-a-hash".to_string();

        let results = evaluate(input, now);

        assert!(results
            .iter()
            .all(|signal| signal.independent_model_probability.is_none()));
    }
}
