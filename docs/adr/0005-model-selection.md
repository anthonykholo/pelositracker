# ADR 0005: Model selection

Status: accepted.

The default is equal-weight logit pooling by independent source family. It is a
market consensus, not an independent sport model. Learned stacking,
calibration, and sport models stay ineligible until versioned chronological
out-of-sample artifacts beat simple baselines and declare exact supported
segments. Missing/invalid artifacts produce display-only `WATCH`; feature flags
alone cannot promote a model.

Version 2 uses event-grouped nested chronological selection, calibration,
validation, and untouched-test folds. The accepted calibrator is monotone beta
or identity; learned stacking must beat the equal-family baseline on validation
proper scores. Uncertainty is derived from aligned event-block coefficient and
execution-cost draws. Artifact construction and live installation are separate
operator actions, and v1 artifacts remain display-only.

Independent sport models use a distinct v1 registry. An entry must name one
exact sport/league/market segment, immutable model/data hashes, exact feature
and state schemas, required live inputs, chronological train/validation/test
windows, proper-score improvements against consensus and pregame baselines,
event-block comparison support, search multiplicity control, and a later human
review. Both the artifact and operator flag are required. Passing that contract
permits display as a versioned cross-check only; it does not alter the
calibrated-consensus paper-action rule.
