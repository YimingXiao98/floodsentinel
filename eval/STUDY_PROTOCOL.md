# FloodSentinel evaluation contract

Status: frozen for the post-audit re-evaluation. This document was written
after the original analysis was found to use policy-dependent timestamps and
before generating the replacement result tables. It is an analysis contract,
not a claim of prospective preregistration.

## Scope and claim

The evaluated contribution is the **phase-adaptive observation schedule** for
county-scale river-gauge monitoring. The study estimates its trade-off between
acquisition volume and delay in observing hydrologic flood episodes.
It does not evaluate flood-stage thresholds as a learned detector, claim a
reduction in human workload, or claim that the belief layer improves decisions.

The belief/evidence model is reported as system architecture and as a null
ablation result. Low-tier evidence is not an effectiveness contribution in this
study.

The auxiliary belief/news audit crosses `full` versus `no_belief` admission
with a frozen GDELT GKG feed present versus absent. Its corpus must cover the
entire Harris replay plus the 24-hour query lookback, declare GKG `V2.1DATE` as
the 15-minute publication-time as-of field, enumerate missing source slices,
and match a separate frozen checksum manifest. Replay scores URL-derived titles
only: titles need at least four alphabetic words, the selected county/state is
supplied as a synthetic dateline, at most 120 recent candidates are exposed,
and at most five records above the strict 0.75 page-score threshold continue.
Claim counts describe this proxy, not general news-evidence quality. The six
operational projections are expected to remain equal because a downstream hard
T2 veto makes the experiment an architecture test.

## Frozen data and windows

- Harris County, Texas: Hurricane Harvey, 2017-08-24 through 2017-09-05 in
  local calendar dates, analysed on the half-open UTC interval
  `[2017-08-24T05:00Z, 2017-09-06T05:00Z)`.
- Duval, Hillsborough, Polk, and Miami-Dade Counties, Florida: Hurricane Irma,
  2017-09-08 through 2017-09-20 in local calendar dates, analysed on
  `[2017-09-08T04:00Z, 2017-09-21T04:00Z)`.
- Gauge observations come from frozen USGS instantaneous-value corpora. The
  corpus builder, artifact SHA-256 checksums, site-selection metadata, and the
  number of observations eligible in each analysis window are reported.
- Every external input used by generation is fail-closed and checksum-bound in
  the study manifest: gauge corpora, the Harris NWS timing reference, and the
  Duval datum-calibration artifact. A path without a checksum, a checksum
  without a path, or a mismatch aborts generation.
- NWS VTEC records are an external timing reference only. They are not supplied
  to the gauge-only primary replay. Alert-inclusive runs, if shown, are a
  separately labelled supplementary analysis.

Miami-Dade has no gauge with a published flood threshold in the frozen source
registry. It is retained only as a coverage/abstention case and contributes no
hydrologic episode or latency estimate. The manifest labels every county as
`episode_evaluation` or `coverage_only`; aggregate artifacts report reference
coverage both with and without coverage-only counties so the denominator is
machine-readable rather than implicit.

## Time base and hydrologic reference

- Every policy is evaluated on the same 15-minute UTC grid over the half-open
  analysis interval, independent of when that policy acquires data. The end
  instant may be retained as a zero-duration boundary state but is not an
  acquisition or an outcome interval.
- Between acquisitions, the policy's most recent output is carried forward.
- Hydrologic reference is three-valued over reporting, datum-compatible sites.
  It is positive when at least one thresholded gauge has a stage observation no
  more than one hour old at or above flood stage; it is negative only when
  every reporting thresholded reference gauge is fresh and none is positive.
  Otherwise it is unknown. A final reading is not forward-filled indefinitely.
- Reference-unknown intervals are reported and excluded from positive-duration
  denominators. Reference-age sensitivity uses 0.5, 2, and 6 hours.
- A flood episode begins on the first common-grid instant with a fresh gauge at
  or above its published flood stage. Positive segments separated only by
  unknown observations remain one interval-censored episode; only a known-clear
  interval splits episodes. Unknown time is excluded from positive and missed
  durations. Episodes ending outside the frozen window or in an unknown
  interval are labelled right-censored.

This hydrologic series is an oracle for evaluating *when a schedule observes a
threshold crossing*. It is not independent evidence that the shared threshold
classifier is accurate.

## Policies and baselines

All primary policies use the same extraction, confidence, phase-transition,
and verdict code. Only the acquisition schedule changes.

1. Phase-adaptive: 6 h in early/elevated watch, 2 h in imminent/active, and
   12 h in recovery.
2. Fixed cadence: 1 h, 2 h, 3 h, 6 h, and 12 h while retaining phase reasoning.
3. Equal-budget fixed cadence: the fixed interval derived from the adaptive
   acquisition count for that county-window.

The legacy `no_role` ablation is descriptive only because it removes phase
reasoning as well as adaptive cadence. It is not the primary schedule baseline.
Fixed-policy phase offsets are enumerated in 15-minute increments over one
fixed interval and summarized as ranges, not selected post hoc. Equal-budget
offsets are enumerated the same way. If one equal-budget run is shown, it is the
offset with the nearest realised acquisition count (earliest offset breaks a
tie); outcome performance never enters that selection.

The current runtime generates one logical coordinator report on every due
acquisition, but Slack rendering can split one report into multiple transport
messages. Replay therefore reports logical report-delivery opportunities,
which equal acquisitions, and separately reports material acquisitions and
`hypothetical_logical_report_delivery_opportunities_if_materiality_gated` as a
counterfactual engineering estimate. It measures neither attempted nor
successful delivery and is not evidence of reduced human workload.

## Outcomes

Primary outcomes, reported by county and pooled descriptively across county
episodes, are:

- number of acquisitions;
- current-runtime logical report-delivery opportunities, equal to acquisitions;
- material acquisitions and hypothetical logical report-delivery opportunities
  under a counterfactual materiality gate;
- episode observation count and unobserved episode count, where observation
  requires an acquisition at or after that episode's onset containing a gauge
  at flood/minor stage or higher; a carried positive acquisition from a prior
  episode cannot observe a later episode;
- onset-to-first-flood-stage-observation delay for each observed episode;
- positive reference duration elapsed before first observation;
- total positive reference time while the carried acquisition output contains
  no flood-stage-or-higher gauge acquired during the current episode. Time
  remains unobserved until a qualifying acquisition; after that, a later
  acquisition without a flood-stage gauge again contributes unobserved time;
- time spent in each phase and number of phase transitions.

No tick-level p-value treats repeated 15-minute observations as independent.
With few county-events, uncertainty is shown as event-level values and ranges
rather than overstated inferential precision.

## Safety and recovery

Replay applies the same recovery/fold helper as production. Recovery accepts
renewed official flood evidence and returns to active incident monitoring.
Autonomous folding requires consecutive due all-clear evaluations whose first
and latest timestamps span at least 48 h, with no intervening evaluated
relapse; the observation count is kept only as audit metadata. Elapsed time
prevents a cycle count from implying different nominal horizons at different
cadences, but cadence still determines timer start, event observability between
acquisitions, and fold overshoot. This is a post-audit engineering correction
motivated by the Hillsborough recurrence, not an optimized or validated value.
Robustness checks use 24 and 72 h, and this choice is never hidden inside the
headline result.

## Robustness and exclusions

- Generation runs under an explicit, recorded environment contract for every
  replay-relevant confidence, freshness, watershed, gauge-delta, gauge-count,
  and materiality setting. Caller overrides are restored afterward. A
  sensitivity arm changes only its declared knob; the 3/6/12-hour policy-age
  arms synchronize payload and claim-currentness horizons.
- Repeat primary outcomes for each fixed cadence and phase offset.
- Repeat reference construction at 0.5, 1, 2, and 6 hour maximum ages.
- Separately vary the policy's own reading-staleness horizon over 3, 6, and 12
  hours, with 6 hours frozen as primary. This changes what replay can act on;
  it is distinct from the oracle truth-age sensitivity above.
- Vary the minimum scorable-gauge requirement used for county verdicts and
  recovery all-clear over 1, 3, 5, and 8 gauges, with 5 frozen as primary.
  Report adaptive and offset-robust matched-budget outcomes for both parameter
  sweeps. These are engineering/safety sensitivities, not optimized settings.
- Report quiet time separately from positive-event time so longer quiet tails
  cannot mechanically manufacture an apparent safety benefit.
- Report missing and stale stage-source states separately at each actual
  acquisition. These are acquisition--source opportunities rather than
  continuous elapsed-time intervals, so publish both counts and within-policy
  opportunity rates.
- Treat primary one-hour truth coverage below 80% as a blocking corpus-QC
  warning by default. A frozen manifest may convert it to a nonblocking,
  still-reported limitation only with `limited_truth_coverage_reviewed: true`
  and a non-empty `limited_truth_coverage_rationale`; this acknowledges
  conservative censoring rather than changing the truth rule or silently using
  `--allow-qc-warnings`.
- Apply manifest and per-site datum/threshold exclusions identically to replay
  inputs, hydrologic reference construction, coverage, and sensitivity; retain
  excluded observations as unscored evidence and report compatible reference-
  site counts and reasons.
- Deduplicate NWS alerts by stable event identifier in any supplementary run.
- Do not report confidence calibration or ordering as evidence for the belief
  layer, and do not report same-series threshold recall as detector validation.
