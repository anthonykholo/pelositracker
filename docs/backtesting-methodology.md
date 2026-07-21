# Backtesting methodology

The current replay is a deterministic audit harness, not evidence of strategy
profitability. It preserves provider/receipt/processing time, original ordering,
ask-size-only changes, terminal cutoffs, exact configuration, and decision
hashes. A historical execution study must fill only against the first eligible
complete snapshot at or after signal time plus declared latency.

Model or calibration promotion requires rolling-origin train/validation/test
splits, all markets from one event in the same fold, training cutoff before the
evaluation interval, and no threshold tuning on the final test period. Report
eligible opportunities and rejection coverage separately from selected paper
signals, plus fill/slippage/fees, Brier and log score, reliability, executable
CLV, turnover, drawdown, concentration, and event-block uncertainty intervals.

The report emits calibrated-consensus Brier/log loss, binned reliability,
Murphy reliability/resolution/uncertainty decomposition, calibration intercept
and slope, submitted/filled/fill-rate/turnover/fees/net paper return, maximum
drawdown, sport/event concentration, and an event-block interval for executable
CLV. Target-executable and reference-consensus CLV are named separately.
Decision-mark coverage includes every evaluated `WATCH` and `PAPER_BET` row plus
failed-gate counts. Target-mid close marks remain explicitly unavailable; they
are never synthesized from settlement.

When a reviewed independent model is present, its Brier/log scores are reported
only on settled rows that contain that exact model output and are paired with
the calibrated-consensus scores on those same rows. This is a cross-check
report, not evidence that the independent model affected paper selection.

Required benchmarks are executable target price, equal-family consensus,
sharp-source consensus when independently defined, uncalibrated consensus, and
no-independent-model policy. Searching many thresholds requires an explicit
multiple-comparison warning. No artifact is promoted merely from in-sample ROI.
The machine-readable report always sets `statistical_claim_supported=false`
until a separately reviewed evaluation establishes otherwise.
