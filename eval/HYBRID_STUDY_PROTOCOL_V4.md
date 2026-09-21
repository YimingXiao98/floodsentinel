# Additive verification-triggered scheduling — evaluation contract (v4)

Status: **FROZEN — 28 August 2026, before any v4 held-out policy or
comparator execution.** Written after the v3 held-out study failed its
prespecified criterion (`eval/V3_HELDOUT_REPORT.md`); the development
selection criterion below was fixed before the v4 development grid ran, and
the held-out selection rules were fixed before selection ran. This file's
SHA-256 is recorded in `eval/corpus/study_v4/manifest.json` and in the v4
artifact manifest. After this freeze, no parameter, comparator, criterion, or
selection rule may change in response to any v4 held-out outcome. The v3
protocol (`eval/HYBRID_STUDY_PROTOCOL.md`) remains frozen and its outcome
remains permanently reported; nothing here revises it.

## Frozen v4 configuration (selected on development data)

Selected mechanically by the development criterion below, recorded in
`eval/results/study_v4_dev/dev_grid.json` (`selected_config_id:
warnings_i120m_d24h_additive`) and bound by hash in the study manifest:

- anchor mode: **additive** (design-fixed, not grid-tuned);
- qualifying triggers: `FF.W`, `FA.W`, `FL.W` (warnings only);
- burst cadence: 7200 s; burst window: 86400 s per qualifying event;
- trigger-stream poll interval: 900 s.

On the thirteen development county-events this configuration observed 16/16
episodes at 1{,}276 pooled acquisitions (31% of the pooled nominal hourly
budget), was below the matched-budget offset median in 11 of 13 county-events,
and had a pooled median margin of -6.38 h. Development performance is
diagnostic only and is not evidence for the held-out claim.

## Held-out set as selected (recorded at freeze, before any policy run)

The mechanical selection produced 24 episode-evaluation county-events from 24
distinct frame events (2016--2024; 14 states plus Puerto Rico) carrying 46
hydrologic episodes, plus 4 coverage-only county-events, from 57 candidates
considered; every exclusion reason is recorded in
`eval/corpus/study_v4/holdout_selection.json`. One frame-attribution caveat is
disclosed: the flooding-event rule matches declarations by date window when an
event name carries no state token, so the county-event admitted under the
``Fort Lauderdale Flash Flood (April 2023)`` label (Marshall, MN) belongs
meteorologically to the concurrent April 2023 upper-Midwest flooding; it is a
legitimate, independently declared flood county-event, and no other included
county-event shares that system.

Everything in the v2 grading contract (`eval/STUDY_PROTOCOL.md`) and the v3
safety contract applies unchanged: common 15-minute UTC grid, half-open
windows, finite-age three-valued hydrologic reference, interval censoring,
acquisition-provenance episode observation, scheduling-only trigger authority,
counted trigger-stream consultations, fail-closed hashed inputs.

## Relationship to v3, stated plainly

The v3 hybrid failed P2: 4 wins / 4 losses / 1 tie against each county's
*minimum* over equal-budget clock offsets. Two mechanisms were diagnosed on
that data: (a) benefit requires the qualifying warning to precede hydrologic
onset, and (b) the burst **re-anchored** the base cadence, so verification
acquisitions could shift scheduled acquisitions off a favorable alignment the
base policy already had (Montgomery: 64 acquisitions vs 38, later
observation). Because those nine county-events' outcomes have been examined,
they are **demoted to development data** for v4 and can never again support a
confirmatory claim. The v4 confirmatory set consists of fresh county-events
selected by the rules below.

## Design change (mechanism-motivated, fixed before tuning)

**Additive (non-re-anchoring) scheduling.** Two clocks. The base phase
cadence is credited only by acquisitions that were due under the phase
cadence itself; verification acquisitions are additive and cap the gap since
the last acquisition of any kind at the burst cadence. Given an identical
phase trajectory, the hybrid's base acquisitions land at exactly the adaptive
policy's instants (test-enforced on a no-escalation corpus), so the trigger
can add information but can never move the baseline. Implemented as
`anchor_mode: "additive"` beside the retained v3 `"reanchor"` mode, which
becomes a diagnostic arm. This choice comes from the v3 mechanism diagnosis,
not from the tuning grid; the grid tunes only the qualifying set, burst
cadence, and burst duration, all under additive mode.

## Primary comparator (changed from v3, with disclosure)

v3 compared against the per-county offset **minimum** — a best-phased oracle:
no operator can phase a clock to a future flood onset, and the pooled oracle
value (3.75 h) is unattainable by any deployable fixed schedule. The v4
primary comparator is the per-county matched-budget offset **median**, the
outcome of a deployable fixed clock whose phase is arbitrary with respect to
onset, with the complete offset distribution and the hybrid's rank reported
for every county-event.

Disclosure: this comparator was chosen after the v3 min-comparator failure
was known. The justification is operational (deployment phase is
uncontrollable), not empirical, and the v3 result under the min comparator is
reported alongside every v4 result. The v4 criterion is evaluated only on
fresh county-events on which no policy or comparator has ever been run.

## Development phase (fixed before the v4 tuning grid runs)

Development pool: the four v2 development county-events (Harris/Harvey;
Duval, Hillsborough, Polk/Irma) **plus** the nine demoted v3 county-events
(Somerset/Ida, Mecklenburg/Helene, Osceola/Milton, Chatham/Michael,
Horry/Florence, East Baton Rouge/Aug-2016, Wake/Matthew, Baldwin/Sally,
Montgomery/Beryl) — thirteen county-events, fourteen reference episodes.

Tuning grid, all under additive anchor mode, frozen v2 replay environment:
qualifying in {warnings; warnings+watches; warnings+watches+advisories} x
burst cadence in {3600 s, 7200 s} x burst duration in {21600 s, 43200 s,
86400 s}. For every configuration and county-event: one hybrid run plus the
complete matched-budget offset distribution at that run's realized
acquisition count (15-minute offsets).

**Development selection criterion** (fixed here, before the grid runs):

1. Budget guard: pooled hybrid acquisitions across the thirteen development
   county-events must not exceed 60% of the pooled nominal hourly budget
   (0.60 x 13 x 312 = 2433.6, i.e. at most 2433).
2. Among configurations passing the guard, maximize pooled episodes observed.
3. Tie-break 1: maximize the number of development county-events whose hybrid
   unobserved positive duration is strictly below that county-event's
   matched-budget offset median.
4. Tie-break 2: minimize the pooled margin
   `sum_c (hybrid unobserved_c - offset median_c)`.
5. Tie-break 3: minimize pooled unobserved hours; then fewer pooled
   acquisitions; then shorter burst duration; then longer burst cadence;
   then smaller qualifying set.

The selected configuration is frozen verbatim for the v4 held-out study.

## Held-out selection (rules fixed before selection runs)

Same mechanical machinery as v3 (`eval/select_holdout.py`), with these
prespecified amendments, each closing a v3 defect-register disclosure or
widening the frame; none is conditioned on any policy outcome:

1. The FIPS-to-USPS map covers the U.S. territories (PR 72, VI 78, GU 66,
   AS 60, MP 69), so territory declarations can produce candidates.
2. Excluded outright: the fourteen development counties (five v2 + nine v3,
   by FIPS) and the eleven development events (Harvey, Irma, plus the nine
   v3 events). v4 held-out county-events come from fresh events only.
3. Up to six candidates are tried per event (was three), and up to three
   counties per event may be admitted across passes 1-3 (one new county per
   event per pass), in the same descending-cost event order.
4. Stop rule: processing ends when the included set reaches 24
   episode-evaluation county-events, or when the frame and passes are
   exhausted. If fewer than 12 episode-evaluation county-events materialize,
   the study is reported descriptively as underpowered and no criterion
   verdict is claimed.
5. Coverage-only retention cap: four.
6. Rules previously implicit in code are now protocol text: candidates
   require at least one in-service registry gauge with a parseable flood
   stage, a non-empty USGS id, and a non-tidal datum (tidal-datum thresholds
   are excluded from ranking and threshold-excluded symmetrically); the
   near-continuous-flood site screen applies to sites with at least 20
   in-window stage observations; limited truth coverage in [0.4, 0.8) is
   acknowledged with a censoring rationale, below 0.4 the candidate is
   excluded.
7. The OpenFEMA responses driving candidate enumeration are cached verbatim
   and hashed in a selection provenance addendum, as retrofitted for v3.

## Policies compared on v4 held-out data

1. v2 phase-adaptive (unchanged);
2. **v4 additive hybrid** (frozen configuration), primary;
3. hybrid with trigger disabled (parity gate: must equal policy 1
   tick-for-tick or generation aborts);
4. v3 re-anchoring hybrid at the same frozen parameters (diagnostic arm);
5. fixed 1/2/3/6/12 h cadences at every 15-minute offset;
6. matched-budget fixed cadence at the v4 hybrid's realized count, every
   15-minute offset (primary comparator source), and at the adaptive count
   (secondary);
7. component ablations: immediate-only and burst-only, additive mode;
8. hybrid poll-grid offsets {0, 300, 600 s} as sensitivity.

## Primary success criterion (frozen before held-out execution)

Evaluated over held-out episode-evaluation county-events, n of them:

- **Q1 (completeness):** pooled hybrid episodes observed >= the sum over
  county-events of each county-event's matched-budget offset **median**
  episodes observed, and >= pooled adaptive episodes observed.
- **Q2 (delay):** with wins = county-events where hybrid unobserved positive
  duration is strictly below that county-event's matched-budget offset
  median, losses = strictly above, ties dropped: the exact one-sided binomial
  sign test on (wins, losses) rejects p = 1/2 at alpha = 0.05; **and** pooled
  hybrid unobserved duration <= the sum of per-county offset medians.
- **Q3 (no hidden cost):** pooled hybrid acquisitions <= 60% of the pooled
  realized fixed-1 h offset-0 count; trigger-stream consultations reported at
  15-minute poll cost with no additional unreported stream.

The criterion passes only if Q1, Q2, and Q3 all hold. Rank/percentile of the
hybrid within each complete offset distribution, the min-comparator outcome
(the v3 test), an anticipation-stratified breakdown (episodes whose burst
window opened at or before onset versus not; the stratification rule is this
sentence, fixed now), the diagnostic re-anchoring arm, component ablations,
and trigger cost/yield/burden are all reported as secondary, never gating.

Sample-size note: with the sign test at alpha = 0.05 and a true per-county
win probability of 0.75-0.8 (the development-era range), n = 20-24 yields
power near 0.6-0.8; below n = 12 the underpower rule above applies.

## Artifacts

Development: `eval/results/study_v4_dev/`. Held-out inputs:
`eval/corpus/study_v4/` with `manifest.json` binding this protocol, the
selection artifact and provenance addendum, the development grid, every gauge
corpus and trigger corpus by SHA-256. Held-out outputs:
`eval/results/study_v4_additive/` from the fail-closed generator, which
hashes the implementation before any replay runs, enforces the parity gate,
and computes Q1/Q2/Q3 mechanically. v2 and v3 artifacts are never modified.
