# v3 hybrid study — post-implementation audit and defect register

An independent read-only audit was run over the v3 implementation after the
held-out study completed. It raised twenty candidate defects across six
dimensions (lookahead, safety, live/replay parity, accounting, criterion
fidelity, determinism). Its automated verification stage did not complete, so
every candidate was checked by hand against the code and the generated
artifacts; the outcome of each check is recorded below.

**None of the corrections changed the held-out criterion verdict.** The
prespecified criterion in `eval/HYBRID_STUDY_PROTOCOL.md` failed on P2 in both
the pre-fix run (`eval/results/study_v3_hybrid/`, retained unmodified) and the
post-fix rerun (`eval/results/study_v3_hybrid_rev2/`, source of record).

Verified after the rerun: `criterion_inputs` are byte-identical between the
two runs, and in every county both `schedule_rows` and `matched_budget_rows`
are identical. The differences are confined to the reporting fields the
corrections targeted — `immediate_fires` and the new
`immediate_fires_before_component_ablation` (D5), `run_terminated_at` and
`reference_episodes_after_termination` (D6), and the persisted trigger-state
shape (D4). This is the intended evidence that the corrections were
result-inert.

## Corrected defects

### D1 — Non-finite trigger interval could abort the live heartbeat
*Live path only. Severity: real, not result-affecting.*

`trigger_policy_from_env()` parsed `SENTINEL_TRIGGER_BURST_*_SECONDS` with
`int(float(raw))`, catching only `ValueError`. `inf`/`1e400` raise
`OverflowError`, which would have escaped `_consult_verification_trigger` into
the `cmd_run_heartbeat` mission loop and denied *every* scheduled acquisition
for as long as the variable stayed set — the exact opposite of the
shorten-only authority the trigger is allowed to have. The parse also ran
before the enabled check, so a disabled deployment was exposed too.

Correction: non-finite values are rejected and fall back to the frozen
defaults; `OverflowError` is caught; the heartbeat wraps the whole
consultation in a guard so no trigger-stream defect can deny the cadence.
Regression test: `test_non_finite_values_cannot_raise`.

### D2 — A fired verification could be consumed by a failed cycle
*Live path only.*

Trigger state was persisted inside the consultation, so if the coordinator
cycle then errored, the immediate fire had already been recorded as consumed
and would not be retried on the next poll.

Correction: the consultation is now pure, and the heartbeat persists state
only after the acquisition it authorized has actually run (or immediately when
nothing was authorized).

### D3 — Live burst anchoring ignored the published hazard onset
*Live path only; live/replay fidelity.*

Burst windows anchor at `max(available_at, begins_at)`. The replay corpus
takes `begins_at` from the VTEC hazard-begin time, but the live mapping set it
to the CAP `effective` timestamp because `FloodAlert` carried no onset field.
An advance-issued river flood warning would therefore have armed a different
window live than the evaluated policy. The previous docstring claim that the
only live/replay divergence "can only shorten live acquisition intervals" was
wrong for this case and has been corrected.

Correction: `FloodAlert` now carries CAP `onset`, and the live mapping uses it
when the product publishes one, falling back to `effective`. The residual
divergence (products without an onset) is documented in the function
docstring as a pre-deployment review item. Regression test:
`test_published_onset_anchors_the_live_burst_window`.

### D4 — Seen-key pruning assumed VTEC-shaped identifiers
*Live path only.*

Pruning parsed a year out of the key. Replay keys (`2017-HGX-FF-W-0043`)
pruned correctly; live CAP message ids (`urn:oid:…`) never would, so persisted
state on a long-lived mission grew without bound.

Correction: state records a first-sight timestamp per key and prunes on that,
so any identifier scheme behaves alike. Legacy key-only state migrates through
a fixed sentinel rather than a wall-clock stamp, keeping replay deterministic.
Regression tests: `test_seen_keys_prune_by_recorded_age_not_key_format`,
`test_legacy_keyed_state_migrates_without_wall_clock`.

### D5 — Component-ablation arms were credited with suppressed fires
*Reporting only; ablation arms only.*

`immediate_fires` was read from the decision function's own counter, which is
computed before the component-ablation mask is applied. The `burst_only` arm
therefore reported `immediate_fires: 1` while its acquisition ledger correctly
showed zero `verification_immediate` acquisitions.

Correction: the arm reports the fires it acted on, retaining the unmasked
count as `immediate_fires_before_component_ablation`. No schedule, acquisition
count, or criterion input changed. Regression test:
`test_component_ablation_reports_only_fires_it_acted_on`.

### D6 — Fold truncation was invisible in trigger yield denominators
*Reporting only.*

A run that folds stops acquiring and stops consulting the stream, so burst
windows and reference episodes after the fold instant lie outside what that
run could observe. The yield and coverage denominators silently reflected the
truncated span. Folds do occur in the held-out set (Chatham/Michael).

Correction: `run_terminated_at` and `reference_episodes_after_termination` are
emitted alongside the yield metrics, and the denominator semantics string says
so. Regression test: `test_trigger_metrics_expose_fold_truncated_span`.

### D7 — Generator environment restore and hash timing
*Provenance hygiene.*

`os.environ.update(FROZEN_REPLAY_ENVIRONMENT)` ran alongside the restoring
context manager, defeating the restore for the calling shell; and the
implementation manifest was hashed after generation, so it attested whatever
the tree held when a long run finished rather than the code that produced the
results.

Correction: only the context manager sets the frozen values, and the
implementation is hashed before any replay runs
(`implementation_hashed_before_generation: true`).

## Side effect introduced by D4, verified inert

Age-based pruning (7-day retention) is shorter than a 312-hour analysis
window, and the replay driver presents every already-available event at each
poll. An event can therefore age out of the deduplication set and be recorded
as newly seen a second time, inflating `burst_windows_total` and
`events_first_seen` in rev2 relative to rev1 (295 re-presented rows across the
held-out set).

Verified inert: a re-presented event was first seen more than 7 days earlier,
so with the frozen 24-hour burst duration its window has necessarily lapsed
and it is stale on re-presentation. All 295 re-presented rows are stale, none
can arm a burst, and every re-presentation repeats the same window bounds.
Accordingly `burst_windows_active_at_first_sight`,
`burst_window_episode_yield`, `episodes_with_trigger_coverage`,
`episodes_with_onset_anticipating_trigger`, every schedule row, every
matched-budget row, and every criterion input are identical between rev1 and
rev2. Live operation cannot exhibit this at all, because the alert feed
returns only active alerts. A future revision should either raise retention
above the longest analysis window or have the replay driver present only
newly-available events, matching live feed semantics.

## Verified-inert findings (no correction applied)

- **Final-expiry field in the corpus builder.** `normalize_trigger_events`
  computes an `available_after_final_expire` sanity counter using the
  archive's final expiry, a field the protocol forbids as a trigger input.
  The counter is **0 in every frozen corpus** (dev and held-out), so it
  removed no event and no retained event's timing depends on it. The frozen
  corpora are left untouched rather than rebuilt; the check should be dropped
  from any future rebuild.
- **Transient-network exclusions.** The selector records build/QC failures as
  exclusions. `build_or_qc_error` count in the frozen selection is **0**, so
  no candidate was excluded by a transient failure.
- **Pass-2 selection semantics.** The frozen stop rule was met inside pass 1,
  so pass-2 behavior never executed and cannot have influenced the set.

## Disclosed deviations between frozen protocol text and selector code

These are recorded rather than corrected, because changing selection after
seeing held-out outcomes would be exactly the tuning the protocol forbids.

1. **Territories are unreachable.** The FIPS-to-USPS map covers the 50 states
   and DC, so Puerto Rico and other territories can produce no candidate.
   Hurricane Maria (2017) is in the event frame and was therefore passed over
   with zero candidates. The protocol text does not state this restriction.
2. **Ranking requires a USGS id.** Rule 2 counts registry gauges with a
   parseable flood stage; the code additionally requires a non-empty USGS id
   (the corpus builder can only fetch such sites), and excludes tidal-datum
   gauges from that count while the tie-break total still includes them.
3. **Datum screen minimum sample.** The 98%-at-or-above-flood-stage screen is
   applied only to sites with at least 20 in-window stage observations.
4. **Development grid scope.** The tuning grid ran on the four development
   episode-evaluation counties; Miami-Dade (coverage-only, no episodes and no
   threshold-compatible reference site) was not included, and could not have
   affected any term of the development selection criterion.

## Live/replay timing caveat (documented, not a defect of this work)

Replay advances its clock from the acquisition instant; production stamps
`last_heartbeat_at` when the coordinator cycle finishes persisting. Live
inter-acquisition intervals are therefore up to one 15-minute poll longer than
the nominal interval, for the phase-adaptive baseline exactly as for the
hybrid. This property predates v3 and applies to both compared policies; it
means a live burst cadence realizes as the configured cadence plus up to one
poll step. Any deployment claim about absolute live latency must account for
it.
