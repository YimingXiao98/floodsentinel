# Belief/news architectural-null artifact

Regenerate the compact publication artifact from the frozen Harris/Harvey
hydrologic and GDELT corpora:

```bash
python3 eval/run_belief_null.py --out eval/results/belief_news_null.json
```

The command verifies the Harris hydrologic checksum in
`eval/corpus/study/manifest.json` and the news checksum/coverage contract in
`eval/corpus/study/belief_news_manifest.json`, records a conservative
implementation manifest, and executes a two-by-two replay:

- belief gate on, news present;
- belief gate off, news present;
- belief gate on, news absent; and
- belief gate off, news absent.

The large `eval/corpus/harris_harvey_gdelt.json` input is intentionally ignored
by Git; the generated artifact records the exact local input hash. It can be
rebuilt from the immutable GDELT archive with the exact command pinned in the
belief-news manifest. The frozen corpus covers the replay's 24-hour warm-up and
full tail, and it records every unavailable 15-minute source slice.

This is a title-only proxy. GKG `V2.1DATE` is the as-of field; URL-derived
titles need at least four alphabetic words, the corpus county/state is supplied
as a synthetic dateline, only the 120 most recent records in 24 hours are
considered, and at most five records above the strict 0.75 page-score boundary
continue. Claim counts therefore describe this construct, not general news
quality.

The reported equality is an expected architectural null: the phase engine
independently vetoes T2/news/crowd claims for phase, alert, de-escalation, and
recovery decisions. It is not evidence that the belief layer improves
calibration, decision quality, or operational efficacy.

The scope is deliberately narrower than byte-equality of whole reports.
Accepted/rejected claim inventories, the report's raw news section, and the
report-delta materiality counterfactual can differ when news is admitted; the
artifact does not claim otherwise.
