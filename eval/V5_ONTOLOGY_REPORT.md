# v5 ontology studies: credibility measurement and the T2 anticipation gap

Protocol: `eval/ONTOLOGY_STUDY_PROTOCOL.md`, frozen before any corpus
construction or grading (SHA-256
`b21d0ccb14bf847c01032ab356f2e2f2f837a899675b595e78a1b0929f9ca702`).
Both parts are prespecified extensions with disclosed provenance: they run on
the v4 county-events, whose scheduling outcomes had been examined, with every
policy, detector, and threshold carried frozen from phases that predate this
protocol. Artifacts: `eval/results/study_v5_ontology/`.

## Part A — the credibility composite does not discriminate, because it is degenerate where decisions happen

Over the 24 v4 county-events' additive-hybrid replays, 7,353 extracted claims
were graded per-gauge under the rq3 semantics. Verdict on the prespecified
statement: **the composite does not discriminate** (AUC 0.493, 95%
county-event cluster-bootstrap CI [0.476, 0.507], straddling 0.5).

The reason is structural, and it is the more useful finding: on the official
T0 gauge stream, the only stream allowed to drive operational decisions, the
four-term composite collapses to **two distinct values** (0.890 and 0.907).
Its designed variance lives in the source-tier and evidence-path terms that
separate official from low-tier evidence, and the downstream boundary vetoes
exactly those paths from decisions. Aggregate calibration is respectable
(Brier 0.104, ECE 0.027 against an 88.2% base accuracy) because the scores
sit near the base rate, not because they rank claims. The belief layer's
scoring is therefore inert precisely where the system decides, which sharpens
the earlier architectural null from "the gate has no route to decisions" to
"the score also carries no within-stream information."

Artifact: `credibility_measurement.json` (SHA-256
`6a1a166572dfd2e41c1184bc73286b29f094fd42bbf49f4dcdf292795a1f493e`).

## Part B — the T2 verification role meets its prespecified criterion, with a small effect that must be stated plainly

Design: on the six county-events holding all 21 unwarned held-out episodes,
admitted positive news indications (production detector, title-only GDELT
proxy, all parameters frozen) request verification under the identical
scheduling-only authority, merged with the official trigger stream.

**Criterion E1/E2/E3: met.** And the magnitude, stated first: the news
trigger observed **zero additional episodes** (11 of 21 unwarned episodes for
both arms) and reduced unwarned-stratum unobserved duration by **1.75 h of
32.5 h (5.4%)**, with the warned stratum bit-identical (no harm) and pooled
acquisitions well inside the budget guard (597 versus 60% of 1,747 hourly).
E1 passed through its second clause (equal episodes, strictly lower
duration).

Where the effect lives, and what it cost:

| County (event) | News articles | T2 events | Unwarned unobs. (off -> +T2) | Extra acq. | Known-clear burden (off -> +T2) |
|---|---:|---:|---|---:|---|
| Marion (Ian) | 178 | 4 | 4.75 -> 3.00 h | +10 | 0 -> 1 |
| Seminole (Nicole) | 332 | 6 | 2.50 -> 2.50 h | +24 | 3 -> 28 |
| Sarasota (Idalia) | 12 | 0 | 16.00 -> 16.00 h | 0 | 0 -> 0 |
| Marshall (Apr 2023) | 0 | 0 | 6.75 -> 6.75 h | 0 | 3 -> 3 |
| Caguas (Maria) | 767 | 0 | 0.75 -> 0.75 h | 0 | 6 -> 6 |
| Cherokee (Apr 2016) | 39 | 0 | 1.75 -> 1.75 h | 0 | 0 -> 0 |

The entire benefit is Marion, where four flood-worded local indications
arrived before official warnings did. Seminole shows the burden face: six
triggers, twenty-four extra acquisitions, a ninefold increase in known-clear
verification, no timing gain. The two structural absences are as informative
as the gain:

1. **Sarasota, the county that decided the v4 verdict, has almost no
   county-scoped news in GKG** (12 articles over 13 days): Idalia's coverage
   geocoded to the Big Bend landfall region, so no news trigger was possible
   where it was most needed.
2. **The detector's flood-keyword gate rejects disaster coverage that is not
   flood-worded.** Caguas had 767 geocoded articles during Maria and zero
   admitted triggers: every candidate title failed the keyword prefilter
   ("hurricane," "power grid," "devastation," never "flood"). On the
   title-only proxy, the belief layer sees only what headlines name.

## What this gives the ontology narrative, honestly

- The **governance boundary** is the belief architecture's validated
  property: it held under 590 adversarial admissions (v2), under the trigger
  stress tests (v3/v4), and under live news triggering here, with every
  decision unchanged except acquisition timing.
- The **bounded verification role** is real but currently marginal on this
  proxy: prespecified criterion met, one county's 1.75 h, purchased at
  visible burden elsewhere. The claim the paper can carry is that the
  architecture *supports* granting low-tier evidence a safe, bounded,
  measurable role, and that title-only news filled 5% of the anticipation
  gap; not more.
- The **credibility composite needs redesign before it can matter**: two
  values on the decision stream is a measured fact, and any future belief
  layer claim should start from within-stream features that actually vary.

Reproduction:

```bash
python3 eval/run_credibility_study.py
python3 eval/run_t2_gap_study.py
```

Part B artifact: `t2_gap_study.json` (SHA-256
`b08c19befaa599409477dfd9085ec08f595fc2d68af16c701a50b460c5be3585`).
