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
    // Phase 0 additions (all optional so older callers still deserialize):
    // relative trust of the source when forming the consensus fair value.
    source_weight: Option<f64>,
    // exchanges (Polymarket, Betfair) already trade near a de-vigged mid, so
    // we do NOT multiplicatively normalize them.
    is_exchange: Option<bool>,
    #[allow(dead_code)]
    decimal_odds: Option<f64>,
    #[allow(dead_code)]
    liquidity: Option<f64>,
}

impl QuoteInput {
    fn executable_probability(&self) -> f64 {
        self.ask.unwrap_or(self.probability)
    }
    fn weight(&self) -> f64 {
        self.source_weight.unwrap_or(0.35).max(0.0)
    }
    fn exchange(&self) -> bool {
        self.is_exchange.unwrap_or(false)
    }
}

// Phase 2a: game state carries fraction_remaining so an INDEPENDENT live
// win-probability model can be computed. It is used only as a cross-check
// (and, later, a stale-quote fallback) — never added on top of an already-live
// market line, which would double-count information the market has priced.
#[derive(Clone, Debug, Deserialize)]
struct StateInput {
    home_score: f64,
    away_score: f64,
    observed_at: f64,
    #[serde(default)]
    fraction_remaining: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct EvaluateRequest {
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
    // Pregame expected home margin prior = -(pregame home spread). None until
    // Phase 2b captures pregame lines; the model then falls back to pure
    // current-lead extrapolation (mu = 0).
    #[serde(default)]
    pregame_spread: Option<f64>,
    #[allow(dead_code)]
    #[serde(default)]
    pregame_total: Option<f64>,
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

/// Abramowitz & Stegun 7.1.26 approximation of erf (max abs error ~1.5e-7).
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

fn normal_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / std::f64::consts::SQRT_2))
}

fn is_moneyline(market: &str) -> bool {
    matches!(market.to_lowercase().as_str(), "moneyline" | "h2h" | "winner")
}

/// Standard deviation of the FINAL score margin per sport (points/goals).
fn sport_margin_sigma(sport: &str) -> f64 {
    match sport.trim().to_lowercase().as_str() {
        "basketball" | "nba" => 11.5,
        "wnba" => 10.5,
        "ncaab" => 10.5,
        "football" | "nfl" => 13.5,
        "ncaaf" => 16.0,
        "hockey" | "nhl" => 2.2,
        "baseball" | "mlb" => 4.0,
        _ => 11.5,
    }
}

/// Stern (1994) Brownian-motion live win probability for the HOME side.
/// Final margin ~ Normal(lead + mu * f, sigma^2 * f), f = fraction remaining,
/// mu = pregame expected home margin. P(home win) = Phi(E[margin] / sd).
/// f is floored so the sqrt(f) denominator cannot blow up at the buzzer.
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
            .map(|&q| {
                ((z * z + 4.0 * (1.0 - z) * q * q / booksum).sqrt() - z) / (2.0 * (1.0 - z))
            })
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
    source: String,
    prob: f64,
    booksum: f64,
    method: &'static str,
    weight_base: f64,
    observed_at: f64,
}

/// Compute one source's fair value for `outcome` given all of its quotes in the
/// market. Exchanges are read at their mid (already ~de-vigged); traditional
/// books are de-vigged with Shin, falling back to proportional.
fn source_fair(outcome: &str, source_quotes: &[&QuoteInput]) -> Option<Fair> {
    let target = source_quotes.iter().find(|q| q.outcome == outcome)?;
    let implied: Vec<f64> = source_quotes.iter().map(|q| q.probability).collect();
    let booksum: f64 = implied.iter().sum();
    let is_exchange = source_quotes.iter().any(|q| q.exchange());

    let (fair, method) = if is_exchange || booksum <= 1.01 || implied.len() < 2 {
        // Exchange mid / already-fair single-sided quote: no multiplicative devig.
        (target.probability.min(0.999).max(0.001), "exchange-mid")
    } else {
        match devig_shin(&implied) {
            Some(fairs) => {
                let idx = source_quotes
                    .iter()
                    .position(|q| q.outcome == outcome)
                    .unwrap();
                (fairs[idx], "shin")
            }
            None => {
                let fairs = devig_proportional(&implied);
                let idx = source_quotes
                    .iter()
                    .position(|q| q.outcome == outcome)
                    .unwrap();
                (fairs[idx], "proportional")
            }
        }
    };

    Some(Fair {
        source: target.source.clone(),
        prob: clamp(fair, 0.001, 0.999),
        booksum: booksum.max(1.0),
        method,
        weight_base: target.weight(),
        observed_at: target.observed_at,
    })
}

fn evaluate(request: EvaluateRequest, now_seconds: f64) -> Vec<SignalOutput> {
    if request.quotes.is_empty() {
        return Vec::new();
    }
    let max_age = request.max_age_seconds.max(1.0);

    // Independent live-model inputs (Phase 2a): freshest state with a known
    // fraction remaining, plus the pregame expected home margin prior.
    let sport = request.sport.clone().unwrap_or_default();
    let pregame_margin = request.pregame_spread.map(|s| -s).unwrap_or(0.0);
    let latest_state = request
        .states
        .iter()
        .filter(|s| s.fraction_remaining.is_some())
        .max_by(|a, b| a.observed_at.total_cmp(&b.observed_at));

    // Keep the freshest quote per (market, outcome, source).
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
        if target_quotes.is_empty() {
            continue;
        }

        // Independent live win-probability for this outcome (moneyline only).
        let model_live: Option<f64> = if is_moneyline(&market) {
            latest_state.and_then(|st| {
                st.fraction_remaining.map(|f| {
                    let lead = st.home_score - st.away_score;
                    let p_home = live_winprob(lead, pregame_margin, f, sport_margin_sigma(&sport));
                    let is_away = outcome.eq_ignore_ascii_case("away")
                        || outcome.eq_ignore_ascii_case(&request.away_outcome);
                    if is_away {
                        1.0 - p_home
                    } else {
                        p_home
                    }
                })
            })
        } else {
            None
        };

        // Per-source de-vigged fair for this outcome.
        let sources: BTreeSet<&str> = same_market
            .iter()
            .map(|quote| quote.source.as_str())
            .collect();
        let mut fairs: Vec<Fair> = Vec::new();
        for source in sources {
            let source_quotes: Vec<&QuoteInput> = same_market
                .iter()
                .copied()
                .filter(|quote| quote.source == source)
                .collect();
            if let Some(fair) = source_fair(&outcome, &source_quotes) {
                fairs.push(fair);
            }
        }
        if fairs.is_empty() {
            continue;
        }

        // The quote we would actually take: the best (lowest) executable ask.
        let best = target_quotes
            .iter()
            .min_by(|a, b| {
                a.executable_probability()
                    .total_cmp(&b.executable_probability())
            })
            .expect("target quotes checked above");
        let executable = best.executable_probability();
        let target_source = best.source.clone();
        let target_booksum = fairs
            .iter()
            .find(|f| f.source == target_source)
            .map(|f| f.booksum)
            .unwrap_or(1.0);

        // LEAVE-ONE-OUT: the consensus fair excludes the book we would bet, so
        // it is a genuinely independent reference rather than a self-comparison.
        let reference: Vec<&Fair> = fairs
            .iter()
            .filter(|f| f.source != target_source)
            .collect();

        let age = (now_seconds - best.observed_at).max(0.0);
        let spread = match (best.ask, best.bid) {
            (Some(ask), Some(bid)) => (ask - bid).max(0.0),
            _ => 0.04,
        };
        let freshness_score = (1.0 - age / max_age).max(0.0);
        let spread_score = (1.0 - spread / 0.12).max(0.0);

        let mut reasons = Vec::new();

        // Single-source case: no independent reference, so an edge is NOT
        // estimable. Report the market price honestly and block.
        if reference.is_empty() {
            let own = fairs
                .iter()
                .find(|f| f.source == target_source)
                .map(|f| f.prob)
                .unwrap_or(executable);
            let own_method = fairs
                .iter()
                .find(|f| f.source == target_source)
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
                    "live model {:.1}% (home lead {:+.0}, {:.0}% game left)",
                    p * 100.0,
                    st.home_score - st.away_score,
                    st.fraction_remaining.unwrap_or(0.0) * 100.0
                ));
            }
            signals.push(SignalOutput {
                event_id: request.event_id.clone(),
                market,
                outcome,
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
            });
            continue;
        }

        // Weighted consensus in log-odds space.
        // weight = source trust x recency x 1/overround.
        let mut wsum = 0.0;
        let mut logit_acc = 0.0;
        let ref_probs: Vec<f64> = reference.iter().map(|f| f.prob).collect();
        for f in &reference {
            let ref_age = (now_seconds - f.observed_at).max(0.0);
            let recency = (-ref_age / max_age).exp().max(0.05);
            let w = f.weight_base.max(0.0) * recency / f.booksum.max(1.0);
            if w > 0.0 {
                wsum += w;
                logit_acc += w * logit(f.prob);
            }
        }
        let fair = if wsum > 0.0 {
            clamp(inv_logit(logit_acc / wsum), 0.001, 0.999)
        } else {
            clamp(mean(&ref_probs), 0.001, 0.999)
        };

        let edge = fair - executable;
        let dispersion = if ref_probs.len() > 1 {
            population_std_dev(&ref_probs)
        } else {
            0.08
        };
        let source_count = reference.len();

        let agreement_score = (1.0 - dispersion / 0.12).max(0.0);
        let source_score = (source_count as f64 / 3.0).min(1.0);
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

        let target_method = fairs
            .iter()
            .find(|f| f.source == target_source)
            .map(|f| f.method)
            .unwrap_or("n/a");

        reasons.push(format!(
            "reference fair {:.1}% from {} independent book(s), dispersion {:.1}% (leave-one-out)",
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
                "live model {:.1}% ({:+.1}pp vs market fair), home lead {:+.0}, {:.0}% game left",
                p * 100.0,
                (p - fair) * 100.0,
                st.home_score - st.away_score,
                st.fraction_remaining.unwrap_or(0.0) * 100.0
            ));
        }

        let mut blockers = Vec::new();
        if age > max_age {
            blockers.push(format!("quote stale ({age:.0}s)"));
        }
        if source_count < 2 {
            blockers.push("fewer than 2 independent reference sources".to_string());
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
        reasons.extend(blockers);

        signals.push(SignalOutput {
            event_id: request.event_id.clone(),
            market,
            outcome,
            // Phase 0 has no independent model, so the model probability IS the
            // sharp leave-one-out consensus. These diverge once a projection
            // model is added (Phase 3).
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
            source_weight: Some(1.0),
            is_exchange: Some(true), // exchange-mid: no multiplicative devig in tests
            decimal_odds: None,
            liquidity: None,
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
        EvaluateRequest {
            event_id: "e".to_string(),
            confidence_threshold: 50.0,
            edge_threshold: 0.02,
            max_age_seconds: 20.0,
            away_outcome: "away".to_string(),
            quotes,
            states,
            sport,
            pregame_spread: None,
            pregame_total: None,
        }
    }

    fn state(home: f64, away: f64, frac: f64, at: f64) -> StateInput {
        StateInput {
            home_score: home,
            away_score: away,
            observed_at: at,
            fraction_remaining: Some(frac),
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
        assert!(home.reasons.iter().any(|r| r.contains("live model")));
    }
}
