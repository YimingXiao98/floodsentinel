# Study 2 (v3): held-out evaluation of the verification-triggered hybrid

**Outcome: the prespecified positive criterion was not satisfied.** The
manuscript is not restructured around a positive claim. The audited v2
negative result stands as the paper's contribution, and this document records
what the hybrid did, why the criterion failed, and what would be needed to
settle the question.

Source of record: `eval/results/study_v3_hybrid_rev2/` (post-audit rerun);
`eval/results/study_v3_hybrid/` retains the original run. Protocol:
`eval/HYBRID_STUDY_PROTOCOL.md`, frozen before any held-out policy execution.
Audit outcomes: `eval/V3_DEFECT_REGISTER.md`. The two runs produce identical
criterion inputs, schedule rows, and matched-budget rows.

Artifact SHA-256 (`eval/results/study_v3_hybrid_rev2/`):

- artifact manifest `af3ebe2dcf9790ddf643214266017b4b29e1484a7d394669be500cb63388058b`
- aggregate `27638de2feb42b62cec82983717909f74c4f8ca25c99bd9ee14a3f5117ee7ef9`
- tables `d5d51c12ef60d1500b994d99bb331964a90741a96c964b2825b3aa30646e40a0`
- preflight QC `1b4bad071a4f8eeae9ed4395576d370fe04cef072e504f6801f03e95739ba25d`

Frozen inputs: protocol
`98e6289d6a4d932c2c9256007fd4b9936c04dd7f71dd3b5ded40dc5aa311bba6`, study
manifest `e545a0a396c641142c664f687f842aaecec77db3aa7573b344e98a096890ac8c`.

Reproduction:

```bash
python3 eval/run_study_v3.py --manifest eval/corpus/study_v3/manifest.json \
    --out-dir eval/results/study_v3_hybrid_rev2
```

## What was tested

A bounded verification trigger: an official NWS warning stream, consulted on
the existing 15-minute heartbeat grid, may request one immediate official-data
acquisition and cap the acquisition interval at 2 h for 24 h after a
qualifying flood warning's hazard-begin time. The trigger's authority is
scheduling only; flood verdict, phase, alert, de-escalation, and recovery
continue to run exclusively on official gauge evidence, which regression tests
enforce.

Frozen configuration (selected on the four development county-events, before
any held-out data was touched): qualifying `{FF.W, FA.W, FL.W}`, burst cadence
7200 s, burst window 86400 s, poll interval 900 s.

Held-out set: nine episode-evaluation county-events across nine independent
events (Somerset NJ/Ida, Mecklenburg NC/Helene, Osceola FL/Milton, Chatham
NC/Michael, Horry SC/Florence, East Baton Rouge LA/August-2016, Wake
NC/Matthew, Baldwin AL/Sally, Montgomery TX/Beryl) carrying 10 hydrologic
episodes, plus two coverage-only cases (Horry and Richland under Ian). County
selection was mechanical and outcome-blind; every candidate and exclusion is
recorded in `eval/corpus/study_v3/holdout_selection.json`.

## Criterion result

| Component | Requirement | Result | Pass |
|---|---|---|---|
| P1 completeness | pooled hybrid episodes >= summed per-county best matched-budget offset, and >= adaptive | 9 vs 9 vs 8 | **yes** |
| P2 delay | hybrid below its county's matched-budget offset **minimum** in strictly more county-events than above | 4 win / 4 loss / 1 tie | **no** |
| P3 cost | pooled hybrid acquisitions <= 60% of pooled hourly | 740 vs 2,252 (32.9%) | **yes** |

P2 fails, so the criterion fails. The pooled-duration half of P2 was met
(9.00 h hybrid vs 21.88 h summed offset medians); the paired sign half was not.

## Per-county outcome

Unobserved truth-positive duration, hours (lower is better):

| County (event) | Hybrid | Adaptive | Best offset | Median offset | Hybrid acq. | Adaptive acq. |
|---|---:|---:|---:|---:|---:|---:|
| Somerset (Ida) | 1.75 | 5.00 | 0.00 | 2.38 | 64 | 59 |
| Mecklenburg (Helene) | 0.50 | 2.50 | 0.75 | 2.50 | 40 | 28 |
| Osceola (Milton) | 0.75 | 5.00 | 0.25 | 2.25 | 76 | 52 |
| Chatham (Michael) | 0.00 | 0.50 | 0.25 | 3.88 | 42 | 28 |
| Horry (Florence) | 0.00 | 5.50 | 0.25 | 1.12 | 128 | 110 |
| East Baton Rouge (Aug 2016) | 0.25 | 0.25 | 0.25 | 1.25 | 140 | 136 |
| Wake (Matthew) | 0.00 | 0.50 | 0.25 | 3.25 | 50 | 26 |
| Baldwin (Sally) | 3.25 | 3.75 | 1.50 | 2.62 | 136 | 130 |
| Montgomery (Beryl) | 2.50 | 0.50 | 0.25 | 2.62 | 64 | 38 |
| **Pooled** | **9.00** | **23.50** | 3.75 | 21.88 | 740 | 607 |

Episode observation: hybrid 9/10, adaptive 8/10, best matched-budget offset
9/10. The episode adaptation missed (Mecklenburg) is recovered by the hybrid.

Secondary comparisons, descriptive only and **not** the frozen test: against
the offset **median** the hybrid wins 8 of 9 county-events; against the
adaptive predecessor it wins 7, ties 1, loses 1.

## Why the criterion failed

**The frozen comparator is an oracle.** P2 compares the hybrid against the
*minimum* over every 15-minute clock phase at the same budget. Choosing that
phase requires knowing when the flood will start. No operator can select it in
advance, and its pooled value (3.75 h) is unattainable by any deployable fixed
schedule. Against that comparator the hybrid is a coin flip: 4-4-1. I froze
this comparator deliberately as the hardest defensible test, and it is the one
that counts. Reporting the median-based comparison as the headline now would
be moving the goalposts after seeing the data.

**Benefit requires the warning to precede the water.** Every county where the
hybrid beat the best offset had a qualifying warning whose burst window opened
at or before hydrologic onset. In the two counties with the weakest
anticipation the hybrid lost decisively:

| County | Burst windows overlapping an episode | Onset anticipated | Verdict |
|---|---:|---|---|
| Chatham, Mecklenburg, Wake, Horry | 0.57-1.00 | yes | win |
| Somerset, Baldwin | 1.00 | yes | loss |
| East Baton Rouge | 0.93 | no | tie |
| Osceola, Montgomery | 0.13, 0.14 | no | loss |

Anticipation is necessary but not sufficient: Somerset and Baldwin had
anticipating warnings and still lost to a perfectly phased clock.

**The burst re-anchors the base cadence — the Montgomery regression.** This is
the one county where the hybrid is worse than the policy it was designed to
repair (2.50 h vs 0.50 h) despite spending 64 acquisitions against 38. Because
every acquisition resets the cadence anchor, verification acquisitions earlier
in the window shifted all subsequent scheduled acquisitions off the phase they
would otherwise have held. Adaptation happened to land an acquisition at
06:00Z, 30 minutes after the 05:30Z onset; the hybrid's re-phased schedule
placed its last pre-onset acquisition at 02:45Z and did not acquire again
until a warning fired at 08:00Z, after onset. More acquisitions produced a
later observation. This is the same alignment luck that made Polk the v2
exception, now running against the hybrid.

## What would be needed next

1. **A comparator that matches the operational claim, prespecified.** If the
   claim is "better than a fixed clock an operator could actually deploy," the
   comparator should be the offset distribution (median, or a randomly phased
   clock), not its minimum. That choice must be frozen in a new protocol
   before data is examined; it cannot be substituted into this study.
2. **More event units.** A 4-4-1 sign test over nine county-events has almost
   no power. Distinguishing "no effect" from "a real effect the oracle
   comparator hides" needs on the order of 25-40 county-events, which the
   selection machinery in `eval/select_holdout.py` can produce by relaxing the
   stop rule and extending the frame (including the territories the current
   FIPS map silently excludes — see the defect register).
3. **A prespecified anticipation stratum.** The mechanism predicts benefit
   only where official warning precedes hydrologic onset, which held for 6 of
   10 episodes here. That is a testable hypothesis, not a post-hoc subset: a
   new study can prespecify the stratification and report both strata.
4. **A non-re-anchoring burst design.** Keeping the base cadence on its own
   clock and treating verification acquisitions as additions, rather than
   letting them reset the anchor, removes the mechanism behind the Montgomery
   regression. It must be selected on development data and confirmed on a
   fresh held-out set; evaluating it against the counties above would be
   tuning on the test set.

## Costs actually incurred

The hybrid spent 740 acquisitions against 607 for adaptation and 2,252 for an
hourly clock. Trigger-stream consultations totalled 9,024 across the nine
counties (one bounded warning-stream poll per 15-minute heartbeat while a
mission is active); they are counted separately and are not free. Verification
acquisitions taken while the hydrologic reference was known clear — the
unnecessary-burden measure — totalled 114.

## Exploratory T2/news trigger (development only)

On Harris/Harvey, admitted positive article indications were allowed to
request verification under the identical scheduling-only boundary. T2-only
reached 0.50 h unobserved at 156 acquisitions with 28 clear-time verification
acquisitions; the official trigger reached 0.25 h at 139 acquisitions with 2.
Adding T2 to the official stream changed nothing (0.50 h, 156). News evidence
therefore bought no timing benefit at materially higher verification burden,
on a title-only GDELT proxy, on a development case. It remains exploratory and
did not enter the held-out study. Artifact:
`eval/results/study_v3_dev/t2_exploratory.json`.
