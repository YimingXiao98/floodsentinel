# Hybrid verification-triggered scheduling — evaluation contract (v3)

Status: **FROZEN — 20 August 2026, before any held-out policy or comparator
execution.** The development sections below were fixed before the development
tuning grid was executed; the held-out selection rules were fixed before
selection ran (with the one disclosed pre-freeze amendment noted in the
selection section); and this file's SHA-256 is recorded in
`eval/corpus/study_v3/manifest.json` and in the held-out artifact manifest.
After this freeze, no parameter, comparator, criterion, or selection rule may
change in response to any held-out outcome; a genuine implementation defect
found later must be documented with its fix, affected artifacts, and rerun
rationale inside `eval/results/study_v3_hybrid/`.

## Frozen hybrid configuration (selected on development data)

Selected mechanically by the development criterion below, recorded in
`eval/results/study_v3_dev/dev_grid.json` (`selected_config_id:
warnings_i120m_d24h`) and bound by hash in the study manifest:

- qualifying triggers: `FF.W`, `FA.W`, `FL.W` (flood warnings only; watches
  and advisories were dominated on development data);
- burst cadence: 7200 s; burst duration: 86400 s per qualifying event window;
- trigger-stream poll interval: 900 s (the production heartbeat grid);
- immediate fire on the inactive-to-active burst transition only.

On development data this configuration observed 6/6 episodes at 515 pooled
acquisitions (41% of the pooled hourly budget) with a pooled matched-budget
margin of -2.25 h. Development performance is diagnostic only and is not
evidence for the held-out claim.

This contract extends, and never weakens, the frozen v2 contract in
`eval/STUDY_PROTOCOL.md`. Every v2 rule about the common 15-minute UTC grid,
half-open windows, finite-age three-valued hydrologic reference, interval
censoring, acquisition-provenance episode observation, unknown-time exclusion,
source missing/stale accounting, and non-interpretation rules applies
unchanged. The v2 artifacts under `eval/results/study_v2/` remain immutable.

## Scope and claim under test

The v2 study found that the phase-adaptive schedule reduces acquisition volume
but is matched or beaten by equal-budget fixed-clock offsets in three of four
development counties. The diagnosed mechanism is self-triggering feedback
latency: in a 6 h pre-event cadence the policy cannot accelerate before an
acquisition reveals the change that would tell it to accelerate.

The v3 policy under test adds a **verification trigger**: an independently
available official warning stream, consulted cheaply on the existing 15-minute
heartbeat grid, may request an immediate official-data verification
acquisition and a bounded temporarily faster cadence ("burst"). The claim
form, testable only on held-out data, is:

> A bounded external verification trigger concentrates official acquisitions
> near flood onset and reduces episode-observation delay at a matched
> acquisition budget.

If the prespecified held-out criterion is not satisfied, the result is
reported as negative and the v2 negative paper stands.

## Safety contract (architectural, test-enforced)

1. The trigger's entire authority is acquisition scheduling. It may only
   (a) request one immediate verification acquisition on the inactive-to-active
   burst transition and (b) cap the acquisition interval at the burst cadence
   while a burst window is active.
2. The trigger never declares flooding, never changes the public alert level,
   never establishes an all-clear, never folds or advances recovery, and never
   substitutes for official hydrologic evidence. Trigger-stream records do not
   enter claim extraction in the primary configuration; flood verdict, phase,
   alert, de-escalation, and recovery decisions run exclusively on the
   unchanged official claim path.
3. Every verification acquisition is an ordinary full acquisition and is
   counted in the acquisition budget. Trigger-stream consultations are counted
   separately (one per 15-minute poll while the mission is active) and
   reported; the trigger stream is not free information.
4. The shared decision function (`watcher/verification_trigger.py`) is pure
   and is the only trigger logic in both production and replay. Production is
   feature-gated **off by default** (`SENTINEL_VERIFICATION_TRIGGER`).

## Trigger stream and as-of semantics

- Source: Iowa Environmental Mesonet VTEC archive, per county, merging the
  county UGC and the forecast zones named for the county
  (`eval/vtec_trigger.py`; schema `sentinel-vtec-trigger-corpus/1`).
- **Availability** of an event is the product transmission time parsed from
  the archive `product_id` prefix, never the hazard begin or end time.
- A qualifying event arms one burst window
  `[max(available_at, begins_at), max(available_at, begins_at) + D)` where
  `begins_at` is the hazard begin time published inside the transmitted
  product (knowable at transmission) and `D` is the frozen burst duration.
  Published/final expiry times are never used: the archive expiry embeds
  post-hoc cancellations a live consumer could not have known.
- Events are deduplicated on VTEC identity (year, WFO, phenomena,
  significance, ETN). A warning first seen after its window lapsed activates
  nothing.
- NWPS forecast streams are excluded as a trigger source: no archive with
  defensible as-of forecast timestamps for the study periods was identified,
  and forecast-driven river warnings already surface through VTEC FL.W
  issuance. T2/news triggers are exploratory only (below) and are excluded
  from the primary hybrid configuration.

The NWS warning stream is a **system input** in v3. It is therefore not used
as a timing/grading reference anywhere in v3: grading is exclusively the v2
hydrologic gauge reference (`truth_path` is not supplied to v3 runs).

## Development phase (fixed before the tuning grid ran)

Development cases are the four v2 episode-evaluation county-events (Harris/
Harvey; Duval, Hillsborough, Polk/Irma) plus Miami-Dade as coverage-only.
Nothing measured on these cases is confirmatory; they diagnose and tune.

Tuning grid (all combinations; frozen v2 replay environment):

- qualifying significance sets: warnings `{FF.W, FA.W, FL.W}`;
  warnings+watches `{…, FF.A, FA.A, FL.A}`;
  warnings+watches+advisories `{…, FA.Y, FL.Y, FF.Y}`;
- burst cadence: 3600 s, 7200 s;
- burst duration: 21600 s, 43200 s, 86400 s.

For every configuration and county: one hybrid run, plus the complete
matched-budget fixed-offset distribution at that hybrid run's realized
acquisition count (15-minute offsets, outcome-blind selection rule identical
to v2). Fixed 1/2/3/6/12 h offset distributions and the adaptive baseline are
shared across configurations.

Disclosure: one development smoke run of the default configuration
(`{FF.W, FA.W, FL.W}`, 3600 s cadence, 21600 s duration) was executed before
this criterion was written, as an implementation check. Development tuning is
permitted to observe development outcomes, so this affects no validity claim,
but the criterion below was not written blind to that single configuration.

**Development selection criterion** (fixed here, before grid execution):

1. Budget guard: pooled hybrid acquisitions across the four development
   counties must not exceed 60% of the pooled hourly-clock budget (0.60 x
   1,248 = 748.8, i.e. at most 748), preserving the efficiency claim.
2. Among configurations passing the guard, maximize pooled episodes observed.
3. Tie-break 1: minimize the pooled matched-budget margin
   `sum_c (hybrid unobserved positive hours_c - median over that county's
   matched-budget offsets)`.
4. Tie-break 2: minimize pooled unobserved positive hours.
5. Tie-break 3: fewer pooled acquisitions; then shorter burst duration; then
   longer burst cadence (cheaper); then smaller qualifying set.

The selected configuration is frozen verbatim for the held-out study. No
parameter may change after any held-out outcome is examined; if a genuine
implementation defect is found later, the defect, fix, affected artifacts,
and rerun rationale must be documented in the held-out artifact directory.

## Held-out evaluation set — selection rules (fixed before selection ran)

Goal: 10–20 hydrologic episodes across multiple independent U.S. flood events,
selected without reference to any policy's performance.

**Event frame.** U.S. flood and tropical-cyclone events, 2016–2024, drawn from
the NOAA/NCEI Billion-Dollar Weather and Climate Disasters events CSV
(flooding and tropical-cyclone categories), the authoritative event register
least connected to this system; the downloaded CSV is frozen and hashed at
`eval/corpus/study_v3/ncei_events_us.csv`. Exclusions, all mechanical:
Hurricane Harvey and Hurricane Irma (development events); events whose NCEI
impact period exceeds 10 days, because the frozen 13-calendar-day window
convention (impact begin minus 2 days through begin plus 10 days) could not
contain the impact period without a discretionary peak-picking choice; events
before 2016 and after 2024. Events are processed in descending CPI-adjusted
cost order: the largest events have the largest hydrologic footprint, so this
order reaches the target episode count with the fewest built corpora, and it
is fixed before any held-out gauge data is examined.

**County selection within an event** (mechanical; policy-outcome-blind):

1. Candidate counties are the county-level areas (FEMA `fipsCountyCode` not
   `000`) designated under major-disaster (DR) declarations in the OpenFEMA
   DisasterDeclarationsSummaries dataset whose `incidentBeginDate` falls
   within the NCEI impact period padded by 5 days on each side and whose
   `incidentType` is storm/flood-related. For tropical-cyclone events the
   declaration title must contain the storm name token from the NCEI event
   name; for flooding events whose NCEI name names states, candidates are
   limited to those states. Raw API responses are cached and hashed.
2. For each candidate, count in-service gauges in the frozen national NWPS
   registry CSV (`skills/county-flood-data/nwps_all_gauges_report.csv`) with
   a parseable flood stage. Rank candidates by that count (ties: more total
   in-service gauges, then state-county name order). The five development
   counties are excluded outright.
3. For each event, candidates are tried strictly in rank order, at most three
   per event, until one is included; one included county per event in the
   first pass. If the completed first pass yields fewer than 10 episodes, a
   second pass in the same event order admits the next passing candidate per
   event until the stop rule is met.
4. Build the 13-calendar-day gauge corpus with the v2 builder
   (`eval/build_corpus.py`), window = impact begin minus 2 days through begin
   plus 10 days; analysis windows are half-open UTC, 312 elapsed hours,
   converted with the county's local standard offset exactly as in v2.
5. Site-level datum screen, applied before preflight: any site whose in-window
   stage observations sit at or above its published flood stage in at least
   98% of observations is treated as datum-incompatible and excluded
   symmetrically (from policy input and reference) via a manifest
   `threshold_exclusions` entry, mirroring the v2 near-continuous-flood
   tripwire; no new datum calibrations are introduced in v3.
   In addition, reference sites whose registry vertical datum is tidal
   (`MHHW*`, `MLLW*`, `MHW`, `MSL`, `HAT` in the frozen registry's
   `nrldb vertical datum name` column) are threshold-excluded symmetrically,
   and tidal-datum gauges are omitted from the rule-2 ranking counts: their
   published thresholds mark coastal/tidal inundation relative to a tide
   datum, which every post-storm high tide can cross, not riverine flood
   stage, and the v2 contract this study extends is river-gauge monitoring.
   Disclosure: this tidal exclusion was added before the protocol freeze but
   after the first mechanical selection pass surfaced a county (Taylor, FL)
   whose reference episodes were semidiurnal tide crossings of MHHW
   thresholds; the pre-amendment pass is archived unmodified at
   `eval/corpus/study_v3/holdout_selection_prescreen_v1.json`. No policy or
   comparator had been run on any held-out window at that time, and the rule
   removes a phenomenon class symmetrically rather than steering any
   comparison.
6. Run the v2 preflight QC (`eval/run_study.py` machinery) unchanged. If the
   primary 1 h truth coverage is below 0.8 but at least 0.4, the limited-
   coverage acknowledgement is granted with an explicit censoring rationale
   (the v2 Duval precedent); below 0.4 the candidate is excluded.
7. A candidate is included as `episode_evaluation` only if preflight passes
   under those rules and the 1 h reference contains at least one hydrologic
   episode. Up to two candidates that pass QC with zero episodes are retained
   as `coverage_only` trigger-burden cases (no episode or latency
   denominators).
8. Stop rule: processing ends when the included set reaches at least 10
   episodes across at least 5 distinct events, or at 12 included
   episode-evaluation county-events, whichever comes first; the frame is
   never reordered based on what any county's data shows.
9. Every candidate considered and every exclusion reason is recorded in
   `eval/corpus/study_v3/holdout_selection.json`.

Inspecting gauge coverage and the hydrologic reference before freeze is
allowed; running the hybrid or any comparator on held-out windows before
freeze is not.

## Policies compared on held-out data

1. v2 phase-adaptive (unchanged);
2. hybrid verification-triggered (frozen configuration), primary;
3. hybrid with the trigger disabled (must equal policy 1; parity check);
4. fixed 1, 2, 3, 6, 12 h cadences at every 15-minute clock offset;
5. matched-budget fixed cadence at the hybrid's realized acquisition count,
   every 15-minute offset (primary comparator), and at the adaptive count
   (secondary);
6. burst-component ablation: immediate-fire-only (burst cadence equal to the
   phase cadence) and burst-only (no immediate fire) arms, to attribute any
   improvement.

The hybrid's start alignment is pinned to the analysis window start on the
same 15-minute poll grid production uses; because acquisition times are set
by warning arrivals and phase state rather than a free-running clock, offset
enumeration applies to the fixed comparators, and the hybrid is additionally
reported under poll-grid offsets {0, 300, 600 s} as a sensitivity.

Policy 3 is also a generation gate: if the trigger-disabled hybrid does not
reproduce the adaptive schedule tick-for-tick, generation aborts. The
verification trigger requires role-layer phase reasoning by construction
(replay refuses `no_role` with a trigger, test-enforced); `no_role` remains a
descriptive role ablation exactly as in v2 and is not a hybrid comparator.

## Outcomes and primary criterion (frozen before held-out execution)

Reported per county-event and pooled over held-out episode-evaluation cases,
all on the v2 grading contract:

- total acquisitions; trigger-attributable acquisitions (immediate + burst);
- trigger-stream consultations (poll count);
- episodes observed/missed; onset-to-first-qualifying-acquisition delay;
- truth-positive duration without current-episode flood-stage observation;
- known-clear / flood-positive / unknown durations;
- verification acquisitions during known-clear time (unnecessary burden);
- trigger yield: fraction of burst windows overlapping a hydrologic episode
  within their span (denominator = burst windows, defensible because windows
  are defined by NWS issuance, not by policy outcomes); episodes with no
  preceding qualifying trigger (missed-trigger rate);
- source missing/stale acquisition-opportunity rates; phase residence and
  transitions; recovery/fold behavior; logical report-delivery opportunities
  (equal to acquisitions; never Slack messages or workload).

**Primary success criterion.** The hybrid is declared superior only if, on
held-out episode-evaluation county-events:

- P1 (completeness): pooled hybrid episodes observed >= the sum over
  county-events of each county-event's *best* matched-budget offset
  comparator (equivalently, the hybrid pooled count is at least that of every
  combination of one matched-budget offset per county-event), and pooled
  hybrid episodes observed >= pooled adaptive episodes observed; and
- P2 (delay): the hybrid's truth-positive unobserved duration is strictly
  below its county-event's matched-budget offset *minimum* in more
  county-events than it is strictly above it (paired sign comparison; ties
  count for neither side), and pooled across county-events the hybrid's
  unobserved duration is at or below the sum of per-county matched-budget
  offset medians; and
- P3 (no hidden cost): pooled hybrid acquisitions <= 60% of the pooled
  realized fixed-1 h offset-0 acquisition count, and trigger-stream
  consultations are reported at 15-minute poll cost with no additional
  unreported stream.

Secondary (reported, not gating): position of the hybrid within each county's
complete fixed-offset distributions at 1–12 h; comparison against adaptive;
component ablations; trigger yield and burden. With the expected number of
county-events (~6–10) no tick-level p-values are computed; uncertainty is
shown as county-event-level paired differences and complete offset ranges,
consistent with v2.

## Exploratory T2/news trigger (development only)

T2/news evidence retains its hard exclusion from all operational decisions.
As an exploratory development-only arm, admitted positive T2 article claims
(the existing detector path, title-only GDELT proxy) may *request*
verification exactly like an official trigger, on the Harris development case
only. This arm is labelled exploratory, never enters the held-out primary,
and no admission threshold is retuned for it. If no defensible T2 admission
rule emerges, the exploratory result is reported descriptively and the
official-trigger hybrid remains the sole primary.

## Artifacts

Development outputs: `eval/results/study_v3_dev/` (tuning grid, selection).
Held-out outputs: `eval/results/study_v3_hybrid/` (fail-closed generator,
checksum-bound input manifest `eval/corpus/study_v3/manifest.json`, per-county
results, aggregate, tables, artifact manifest with implementation hashes,
Python version, frozen environment, and this protocol's SHA-256 at freeze).
All hybrid inputs (gauge corpora, trigger corpora) are hashed in the manifest;
generation aborts on any mismatch. v2 artifacts are never regenerated.
