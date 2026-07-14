use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Debug, Deserialize)]
struct QuoteInput {
    market: String,
    outcome: String,
    probability: f64,
    source: String,
    observed_at: f64,
    bid: Option<f64>,
    ask: Option<f64>,
}

impl QuoteInput {
    fn executable_probability(&self) -> f64 {
        self.ask.unwrap_or(self.probability)
    }
}

#[derive(Clone, Debug, Deserialize)]
struct StateInput {
    home_score: f64,
    away_score: f64,
    observed_at: f64,
}

#[derive(Debug, Deserialize)]
struct EvaluateRequest {
    event_id: String,
    confidence_threshold: f64,
    edge_threshold: f64,
    max_age_seconds: f64,
    away_outcome: String,
    quotes: Vec<QuoteInput>,
    states: Vec<StateInput>,
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

fn momentum(
    states: &[StateInput],
    market: &str,
    outcome: &str,
    away_outcome: &str,
) -> (f64, String) {
    if !matches!(
        market.to_lowercase().as_str(),
        "h2h" | "moneyline" | "winner"
    ) {
        return (
            0.0,
            "momentum adjustment not used for this market type".to_string(),
        );
    }
    if states.len() < 2 {
        return (0.0, "insufficient score history".to_string());
    }
    let mut recent = states.to_vec();
    recent.sort_by(|a, b| a.observed_at.total_cmp(&b.observed_at));
    let start = recent.len().saturating_sub(6);
    let recent = &recent[start..];
    let first = &recent[0];
    let last = &recent[recent.len() - 1];
    let mut direction = (last.home_score - first.home_score) - (last.away_score - first.away_score);
    if outcome.eq_ignore_ascii_case("away") || outcome.eq_ignore_ascii_case(away_outcome) {
        direction *= -1.0;
    }
    let adjustment = clamp(direction * 0.008, -0.06, 0.06);
    (
        adjustment,
        format!("recent scoring differential {direction:+.0}"),
    )
}

fn evaluate(request: EvaluateRequest, now_seconds: f64) -> Vec<SignalOutput> {
    if request.quotes.is_empty() {
        return Vec::new();
    }

    let mut freshest: BTreeMap<(String, String, String), QuoteInput> = BTreeMap::new();
    for quote in request.quotes {
        let key = (
            quote.market.clone(),
            quote.outcome.clone(),
            quote.source.clone(),
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
        .map(|quote| (quote.market.clone(), quote.outcome.clone()))
        .collect();
    let mut signals = Vec::new();

    for (market, outcome) in pairs {
        let same_market: Vec<&QuoteInput> = current
            .iter()
            .filter(|quote| quote.market == market)
            .collect();
        let target_quotes: Vec<&QuoteInput> = same_market
            .iter()
            .copied()
            .filter(|quote| quote.outcome == outcome)
            .collect();

        let sources: BTreeSet<&str> = same_market
            .iter()
            .map(|quote| quote.source.as_str())
            .collect();
        let mut source_fairs = Vec::new();
        for source in sources {
            let source_quotes: Vec<&QuoteInput> = same_market
                .iter()
                .copied()
                .filter(|quote| quote.source == source)
                .collect();
            if let Some(target) = source_quotes.iter().find(|quote| quote.outcome == outcome) {
                let total: f64 = source_quotes.iter().map(|quote| quote.probability).sum();
                source_fairs.push(if total > 1.01 {
                    target.probability / total
                } else {
                    target.probability
                });
            }
        }
        if source_fairs.is_empty() || target_quotes.is_empty() {
            continue;
        }
        let fair = mean(&source_fairs);
        let dispersion = if source_fairs.len() > 1 {
            population_std_dev(&source_fairs)
        } else {
            0.08
        };
        let source_count = source_fairs.len();
        let (momentum_adjustment, momentum_reason) =
            momentum(&request.states, &market, &outcome, &request.away_outcome);
        let model_probability = clamp(fair + momentum_adjustment, 0.01, 0.99);
        let best = target_quotes
            .iter()
            .min_by(|a, b| {
                a.executable_probability()
                    .total_cmp(&b.executable_probability())
            })
            .expect("target quotes checked above");
        let executable = best.executable_probability();
        let edge = model_probability - executable;
        let age = (now_seconds - best.observed_at).max(0.0);
        let spread = match (best.ask, best.bid) {
            (Some(ask), Some(bid)) => ask - bid,
            _ => 0.04,
        };

        let freshness_score = (1.0 - age / request.max_age_seconds).max(0.0);
        let agreement_score = (1.0 - dispersion / 0.12).max(0.0);
        let source_score = (source_count as f64 / 3.0).min(1.0);
        let spread_score = (1.0 - spread / 0.12).max(0.0);
        let edge_stability = clamp(
            edge.max(0.0) / (request.edge_threshold * 2.0).max(0.001),
            0.0,
            1.0,
        );
        let confidence = 100.0
            * (0.28 * freshness_score
                + 0.24 * agreement_score
                + 0.18 * source_score
                + 0.15 * spread_score
                + 0.15 * edge_stability);

        let mut blockers = Vec::new();
        if age > request.max_age_seconds {
            blockers.push(format!("quote stale ({age:.0}s)"));
        }
        if source_count < 2 {
            blockers.push("fewer than 2 independent price sources".to_string());
        }
        if spread > 0.08 {
            blockers.push(format!("wide executable spread ({:.1}%)", spread * 100.0));
        }
        if edge < request.edge_threshold {
            blockers.push(format!(
                "edge {:.1}% below {:.1}% threshold",
                edge * 100.0,
                request.edge_threshold * 100.0
            ));
        }
        if confidence < request.confidence_threshold {
            blockers.push(format!(
                "signal quality {confidence:.0} below {:.0}",
                request.confidence_threshold
            ));
        }
        let action = if blockers.is_empty() {
            "PAPER_BET"
        } else {
            "WATCH"
        };
        let mut reasons = vec![
            momentum_reason,
            format!(
                "{source_count} price source(s), dispersion {:.1}%",
                dispersion * 100.0
            ),
            format!(
                "best executable probability {:.1}% via {}",
                executable * 100.0,
                best.source
            ),
        ];
        reasons.extend(blockers);
        signals.push(SignalOutput {
            event_id: request.event_id.clone(),
            market,
            outcome,
            model_probability,
            market_probability: executable,
            edge,
            confidence: (confidence * 10.0).round() / 10.0,
            action: action.to_string(),
            reasons,
            quote_source: best.source.clone(),
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
    let now_seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| PyValueError::new_err(format!("system clock error: {error}")))?
        .as_secs_f64();
    serde_json::to_string(&evaluate(request, now_seconds))
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
            probability,
            source: source.to_string(),
            observed_at: now,
            bid: Some(probability - 0.01),
            ask: Some(probability + 0.01),
        }
    }

    fn request(quotes: Vec<QuoteInput>, states: Vec<StateInput>) -> EvaluateRequest {
        EvaluateRequest {
            event_id: "e".to_string(),
            confidence_threshold: 50.0,
            edge_threshold: 0.02,
            max_age_seconds: 20.0,
            away_outcome: "away".to_string(),
            quotes,
            states,
        }
    }

    #[test]
    fn momentum_and_three_sources_produce_a_paper_signal() {
        let now = 1_000.0;
        let mut quotes = Vec::new();
        for (source, probability) in [("a", 0.50), ("b", 0.505), ("c", 0.495)] {
            quotes.push(quote(source, "home", probability, now));
            quotes.push(quote(source, "away", 1.0 - probability, now));
        }
        let states = vec![
            StateInput {
                home_score: 10.0,
                away_score: 10.0,
                observed_at: 1.0,
            },
            StateInput {
                home_score: 18.0,
                away_score: 10.0,
                observed_at: 2.0,
            },
        ];
        let results = evaluate(request(quotes, states), now);
        let home = results
            .iter()
            .find(|signal| signal.outcome == "home")
            .unwrap();
        assert_eq!(home.action, "PAPER_BET");
        assert!(home.edge > 0.02);
    }

    #[test]
    fn a_single_source_is_blocked() {
        let now = 1_000.0;
        let results = evaluate(
            request(
                vec![
                    quote("one", "home", 0.5, now),
                    quote("one", "away", 0.5, now),
                ],
                Vec::new(),
            ),
            now,
        );
        assert!(results.iter().all(|signal| signal.action == "WATCH"));
        assert!(results[0]
            .reasons
            .iter()
            .any(|reason| reason.contains("fewer than 2")));
    }
}
