# Study 3 (v4): held-out evaluation of the additive verification trigger

**Outcome: the prespecified criterion was not satisfied.** Q2 (delay) passed
decisively and Q3 (budget) passed, but Q1 (completeness) failed by one pooled
episode, so the frozen conjunction fails and no positive headline claim is
made. The verdict, its exact location, and what it teaches are below.

Source of record: `eval/results/study_v4_additive/`. Protocol:
`eval/HYBRID_STUDY_PROTOCOL_V4.md` (SHA-256
`fb26dac4549e7e49ed38db420810862addeff7c763b7927404cb02b07fd83a29`), frozen
with the input manifest (`eval/corpus/study_v4/manifest.json`, SHA-256
`47917d84775fe990b6aa9bb6acf452d10f67b4235d0b7da417832dd6743b78fd`) before any
v4 held-out policy or comparator executed. An independent recomputation of
Q1/Q2/Q3 from the raw per-county schedule rows matches the generator's
aggregate exactly; every generated artifact matches its manifest hash; the
frozen v2 and v3 artifacts are byte-identical to their published hashes.

Artifact SHA-256 (`eval/results/study_v4_additive/`):

- artifact manifest `c1f8ecf4821115f8250beda3b1cbce69037f07ec93d80d0bf6390c1be5e45c39`
- aggregate `3605f7cad873e00f225ef630424ed69f29da73ecf6f51e67155b9ffd07f81203`
- tables `92856a5f9ef3e1e066bae3a4a1c48bbb38f44484421090b64f45e830d2239d7f`

Reproduction:

```bash
python3 eval/run_study_v4.py --manifest eval/corpus/study_v4/manifest.json \
    --out-dir eval/results/study_v4_additive
```

## What changed from v3, and why

Both changes were fixed before any v4 data was touched, with the v3 held-out
county-events demoted to development data:

1. **Additive (non-re-anchoring) scheduling.** The base phase cadence keeps
   its own clock, credited only by base-due acquisitions; verification
   acquisitions are additive and cap the gap since the last acquisition of
   any kind. This removes the v3 Montgomery pathology by construction.
2. **A deployable-policy comparator.** Q2 compares against each county-event's
   matched-budget offset *median* (an arbitrary-phase clock an operator could
   actually deploy) with an exact one-sided binomial sign test at alpha 0.05,
   instead of v3's best-phased oracle minimum. The change is justified
   operationally and was frozen with full disclosure that it followed the v3
   failure; it was tested only on fresh county-events.

Frozen configuration (selected mechanically on the 13 development
county-events): warnings `{FF.W, FA.W, FL.W}`, 7200 s burst cadence, 86400 s
window per warning, 900 s polls, additive anchor.

Held-out set: 24 episode-evaluation county-events from 24 distinct frame
events (2016--2024, 14 states plus Puerto Rico), 46 hydrologic episodes, plus
4 coverage-only county-events; selected mechanically with every candidate and
exclusion recorded (`eval/corpus/study_v4/holdout_selection.json`). The
Marshall, MN frame-label caveat is disclosed in the manifest.

## Criterion result

| Component | Requirement | Result | Pass |
|---|---|---|---|
| Q1 completeness | pooled hybrid episodes >= summed per-county offset-median episodes, and >= adaptive | 33 vs 34.0 vs 32 | **no** |
| Q2 delay | sign test vs offset median at alpha 0.05, and pooled <= pooled medians | 20 wins / 4 losses, p = 0.00077; 60.25 h vs 82.63 h | **yes** |
| Q3 cost | pooled hybrid acquisitions <= 60% of pooled hourly | 2,127 vs 6,789 (31.3%) | **yes** |

The conjunction fails. The verdict is **fail**, reported as such.

## Where the one episode went

The Q1 gap is a single county-event. Sarasota, FL under Hurricane Idalia
carries 7 short recurring reference episodes; the equal-budget clock's median
offset observes all 7, the hybrid observes 4. Five of those episodes occurred
*before* Idalia's flood warnings existed (pre-landfall crossings on 27--28
August 2023), and none of the seven had an onset-anticipating warning, so the
trigger had nothing to act on while the hybrid's larger early share of the
budget bought nothing. Everywhere else the hybrid's episode count is at or
above the offset median, and it never loses an episode to its predecessor
(33 vs 32 pooled).

This is the anticipation boundary the v3 report predicted, now measured at
scale: of the 46 held-out episodes, 30 fell inside some warning's burst
window and 24 had an onset-anticipating warning. Where the warning precedes
the water, event-concentration wins on delay; where episodes recur unwarned,
a uniform clock's evenly spread budget wins on completeness.

## What did replicate

- **The additive design never regresses its predecessor:** 20 wins, 0
  losses, 4 ties against phase adaptation on unobserved duration; pooled
  109.5 h down to 60.25 h (45% reduction) with one more episode observed, at
  2,127 versus 1,610 acquisitions.
- **The delay improvement over a deployable clock is real and prespecified:**
  20/4 with p = 0.00077 is the confirmatory version of the pattern v3 could
  only report as secondary.
- The hybrid was at or tied with the *best* offset in 5 of 24 county-events;
  against the oracle minimum (the v3 test) it remains behind (4 below, 19
  above, minimum sum 29.0 h), consistent with v3: no realizable policy
  evaluated here approaches the best-phased oracle.

## Diagnostic observation, explicitly not a claim

The v3-style re-anchoring arm, run at the same frozen parameters as a
diagnostic, happened to outperform the additive primary descriptively on this
set (53.5 h unobserved, 34 episodes observed). Its matched-budget comparators
were not generated for criterion evaluation and no selection between anchor
modes on this data is legitimate; the observation is recorded because
re-anchoring's occasional lucky re-alignments evidently outweighed its
Montgomery-style losses on these particular 24 county-events. Any future
choice between anchor modes must be made on development data and confirmed on
events neither mode has touched.

## Costs

Trigger-stream consultations: 27,195 (one bounded warning-stream poll per
15-minute heartbeat while a mission is active), counted separately from
acquisitions. Verification acquisitions during known-clear time: 141 across
the 24 episode-evaluation county-events.

## Standing of the research program after three frozen studies

1. Study 1 (v2): phase adaptation saves volume but loses to equal-budget
   clocks; mechanism, feedback latency.
2. Study 2 (v3): a bounded warning trigger repairs the latency failure but
   ties a best-phased oracle 4-4-1; mechanisms, anticipation and
   re-anchoring.
3. Study 3 (v4): the non-re-anchoring revision beats a deployable equal-budget
   clock on delay with a prespecified test (p < 0.001) at 31% of the hourly
   budget and never regresses its predecessor, but drops one pooled episode
   of completeness to episode recurrence outside warning coverage, failing
   the frozen conjunctive criterion.

The honest publishable claim set is exactly that sequence. A criterion built
as delay-plus-budget with a completeness *non-inferiority margin* (rather
than strict superiority against the offset median) would likely have passed
here, but that criterion was not frozen, and adopting it now would be
outcome-driven. If a fourth round is ever run, the completeness component
should be specified with an explicit prespecified margin and the anchor-mode
choice should be made on development data.
