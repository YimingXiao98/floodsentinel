# Robustness checks on the frozen Study 3 artifacts (post hoc)

Two post-hoc robustness checks on the frozen Study 3 (v4) county-events.

Neither was prespecified; both were requested by the simulated review of the manuscript (1 September 2026).

1. Mission-start offsets. The frozen study ran phase adaptation and the additive hybrid once per county-event, anchored at the window start, while every fixed clock was averaged over all of its 15-minute start offsets. Here both policies are run at every start offset in 15-minute steps over one base interval (6 h, 24 offsets), so their own alignment luck is averaged the same way. For each offset draw the frozen Q2 comparison is repeated against the frozen budget-matched offset median, and against the uniform 3 h clock's offset median.

2. Phase-free control. The same verification trigger (frozen v4 policy, additive anchoring) placed on a fixed 6 h base clock instead of the phase-adaptive cadence: 2 h while a qualifying warning holds a burst window open, 6 h otherwise, with the phase model still computing verdicts but never setting timing. Run at the same 24 start offsets. This isolates what the phase model contributes to timing once the trigger exists.

Reads the frozen v4 manifest and corpora; writes only under eval/results/robustness_checks/. Reuses the v3/v4 generators' run and summary helpers unchanged so every row is computed exactly as the study's schedule rows were.

Run 24 county-events x 24 start offsets (15-minute steps over the 6 h base interval); offset 0 reproduces the frozen study for every county-event: True. Source: `eval/robustness_checks.py`; results `eval/results/robustness_checks/summary.json`.

## Check 1: mission-start offsets

| policy | pooled unobserved h across offsets (min / median / max) | pooled episodes observed | pooled acquisitions |
|---|---|---|---|
| hybrid | 46.00 / 52.62 / 64.25 | 33 / 35.0 / 38 | 2114 / 2126 / 2135 |
| phase_free | 47.50 / 56.00 / 64.00 | 32 / 33.0 / 36 | 1674 / 1698 / 1706 |
| adaptive | 78.50 / 103.88 / 120.75 | 31 / 33.5 / 36 | 1601 / 1676 / 1701 |

Frozen references (pooled): budget-matched offset median 82.62 h; uniform 3 h clock offset median 60.62 h.

- Hybrid below the frozen budget-matched median: 18 to 21 of 24 county-events across offsets (median 20.0); one-sided sign p from 3.3e-05 to 0.0053; 15 of 24 offsets reach the frozen 20 of 24.
- Hybrid below the uniform 3 h clock's offset median: 13 to 20 of 24 (median 16.5); pooled hours below the clock's 60.62 h at 23 of 24 offsets.

## Check 2: phase-free control (fixed 6 h base under the same trigger)

- Paired by county-event and offset (576 draws): hybrid lower in 45, higher in 8, tied in 523. County-events where the two ever differ: Caguas, Marion, Penobscot, Sarasota.
- Pooled unobserved, hybrid minus phase-free per offset: -7.75 to 0.25 h (median -2.25 h), for a median of 430 more acquisitions.
- Phase-free control against phase adaptation (near-equal budgets): lower in 399 draws, higher in 26, tied in 151; per offset it wins 10 to 21 of 24 county-events and never loses more than 2.
