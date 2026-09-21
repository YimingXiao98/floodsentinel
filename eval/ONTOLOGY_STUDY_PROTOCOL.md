# Ontology evaluation contract (v5): credibility measurement and the T2 anticipation-gap study

Status: **FROZEN — 30 August 2026, before any corpus construction, claim
grading, or policy execution under this protocol.** This file's SHA-256 is
recorded in `eval/results/study_v5_ontology/` at generation. The v2/v3/v4
protocols and artifacts remain immutable; nothing here revises any earlier
verdict.

Provenance disclosure, stated first because it bounds every claim below: the
county-events used here come from the v4 held-out set, whose scheduling
outcomes have been examined. Part B's stratum sits exactly where the official
trigger was observed to fail. Both parts are therefore **prespecified
extension studies with disclosed provenance**, never virgin confirmation. No
parameter of any policy or detector is tuned under this protocol; every
policy, detector, and threshold is carried frozen from earlier phases that
predate this protocol.

## What is being evaluated

The role-and-belief ontology's two remaining unmeasured claims:

1. **Part A — does the belief layer's credibility composite discriminate?**
   The four-term claim credibility (source tier, location resolution,
   classification method, data quality) has run in every study but was never
   evaluated; the v2 protocol barred reporting calibration *as evidence for
   the belief layer's operational value* because the ablated-uniform
   comparison was ill-posed. Part A instead measures the score itself, as a
   property of the credibility model, under its own contract.
2. **Part B — can the belief layer's bounded verification role close the
   anticipation gap?** v4 located the trigger design's failure boundary:
   episodes with no onset-anticipating official warning. The ontology's
   stated purpose for low-tier evidence is to request verification where
   official coverage is absent, under the same scheduling-only authority.
   Part B tests that role on the mechanically defined unwarned stratum.

## Part A — credibility-model measurement

Data: the additive-hybrid replays of the 24 v4 episode-evaluation
county-events, regenerated deterministically under the frozen v4 environment
and configuration; every extracted claim (admitted and rejected) is graded by
the existing rq3 semantics: a gauge claim against its own gauge's
flood-stage state at that instant, a county-level claim against the county
reference; unscored/abstaining claims excluded; truth-unknown instants
excluded.

Reported, with no pass/fail gate: Brier score, expected calibration error,
distinct confidence values, accuracy in the lower and upper confidence
halves, and discrimination as AUC (probability a correct claim outranks an
incorrect one). The only prespecified inferential statement: the composite
*discriminates* if the AUC's 95% cluster-bootstrap interval (resampling
county-events, 10,000 resamples, seed 20260830) excludes 0.5. Per-tier and
per-claim-type breakdowns are descriptive. This measures the belief model's
scores; it makes no claim about operational decisions, which the earlier
studies showed the composite does not control.

## Part B — T2 verification on the anticipation gap

**Stratum (mechanical, policy-independent).** An episode is *unwarned* when
no qualifying official warning (v4 frozen set `FF.W, FA.W, FL.W`) has a
verification window `[max(available_at, begins_at), +86400 s)` that starts at
or before the episode's onset and covers it. Computed from the frozen v4
trigger corpora and the hydrologic reference alone: 21 unwarned episodes of
46, in six county-events, fixed here before any Part B corpus exists:
Marion, FL (Ian; 7/7 unwarned); Sarasota, FL (Idalia; 7/7); Seminole, FL
(Nicole; 3/3); Marshall, MN (April 2023; 2/3); Caguas, PR (Maria; 1/4);
Cherokee, TX (April 2016; 1/1). The remaining 25 episodes form the warned
stratum.

**News corpora.** GDELT GKG 15-minute archive files, exactly the frozen v2
belief-study method (`eval/news_gdelt.py`; `V2.1DATE` as the as-of field;
URL-derived titles; missing slices enumerated), swept from one day before
each county's analysis window start through its end. Place terms are
mechanical: `"<County> County, <State full name>"` plus
`"<county seat>, <State full name>"`; for the Puerto Rico municipio,
`"Caguas, Puerto Rico"`. The state qualifier is a noise-reduction refinement
of the frozen Harris method (which used unqualified `"Harris County"`),
adopted here before any corpus is built because several stratum county names
are common across states.

**T2 trigger derivation, carried frozen.** Articles pass through the
production detector entry point exactly as in the v2/v3 news studies
(page score strictly greater than 0.75, top five per poll, title-usability
filter, county/state dateline); an article becomes one `T2.W` trigger event
at the first 15-minute poll where the detector admits it with the positive
indication, deduplicated by URL (`eval/build_t2_trigger_corpus.py`). Neutral
and denial indications never become events. No threshold or parameter is
altered.

**Arms, per stratum county-event, all under the v4 frozen additive
configuration (2 h burst cadence, 24 h window, additive anchor, 900 s polls,
frozen replay environment):**

1. `official` — the v4 primary hybrid, regenerated (baseline);
2. `official_plus_t2` — qualifying set `FF.W, FA.W, FL.W, T2.W` over the
   merged trigger corpus (primary Part B arm);
3. `t2_only` — qualifying `T2.W` (diagnostic).

The safety contract is unchanged and test-enforced: T2 events can request
acquisitions and cap the interval; they cannot touch verdict, phase, alert,
all-clear, or recovery.

**Prespecified outcome statement.** The belief layer's verification role is
declared to *close part of the anticipation gap* only if, pooled over the 21
unwarned episodes:

- E1: `official_plus_t2` observes strictly more unwarned episodes than
  `official`, or observes the same number with strictly lower summed
  unobserved positive duration on the unwarned stratum; and
- E2 (no harm): on the warned stratum, `official_plus_t2` observes no fewer
  episodes than `official`, and its pooled all-episode unobserved duration
  across the six county-events is no greater than `official`'s; and
- E3 (bounded cost): pooled `official_plus_t2` acquisitions stay within 60%
  of the pooled fixed-1 h offset-0 budget for these county-events, with
  T2-attributable acquisitions, news-stream consultations, and known-clear
  verification burden reported.

With 21 episodes in six county-events no significance test is prespecified;
results are paired descriptive counts and durations with every per-episode
row retained. The title-only GDELT proxy limitation applies in full.

## Artifacts

Inputs under `eval/corpus/study_v5/` (news corpora, T2 trigger corpora,
merged corpora), each SHA-256-bound in a study manifest that also binds this
protocol, the v4 manifest, and the frozen stratum listing. Outputs under
`eval/results/study_v5_ontology/` from a fail-closed generator that hashes
the implementation before any replay. v2/v3/v4 results are never modified.
