# Reproducibility notes

Written 21 September 2026 when this repository was exported from the development repository. Every check below was run inside this tree, not the development tree, with Python 3.14 (miniconda) and the pinned requirements. Where a check depends on data not shipped here, it says so.

## What the study manifests bind

Each study's `artifact_manifest.json` records SHA-256 hashes for its corpora and trigger corpora (through each county-event's `corpus_manifest_entry`), its selection artifact, its development grid, its frozen protocol, its corpus manifest, and the implementation set at the moment the study was generated. The implementation rule is: sorted `eval/*.py`, `watcher/**/*.py`, `watcher/ontology/stub_*.json`, and `skills/county-flood-data/county_flood/**/*.py`, excluding caches, tests, and results. The study generators recompute these hashes and refuse to run when an input does not match.

Result of recomputing every non-implementation hash in this tree on 21 September 2026: all match. That covers 78 corpus and trigger-corpus files for Studies 2 and 3, the 5 Study 1 corpus artifacts and their manifest, both selection artifacts, both development grids, the three frozen protocols (`STUDY_PROTOCOL.md` is bound through the Study 1 source manifest; `HYBRID_STUDY_PROTOCOL.md` and `HYBRID_STUDY_PROTOCOL_V4.md` directly; `ONTOLOGY_STUDY_PROTOCOL.md` through both v5 result files), and the news manifest bound by `belief_news_null.json`.

## Implementation drift since each freeze

This repository ships the current implementation, which continued to change after each study was frozen because later studies added the verification trigger and its variants. The tables list every hashed file whose current SHA-256 differs from the value the manifest recorded. All other hashed files are byte-identical to their frozen state. The manuscript's three-case mechanism figure was produced by replaying three county-events through this current code and checking the results against each study's stored policy summaries (acquisition counts, termination, fold instant, acquisitions by reason, burst spans, immediate fires, and consultation counts); all checks pass, which is the evidence that the drift below does not change the evaluated behavior for those cases. The manifest's own reproduction command for Study 3 (see the README) is the full check.

### Study 1: 8 of 73 hashed files differ

| File | Frozen SHA-256 (prefix) | Current SHA-256 (prefix) | Change |
|---|---|---|---|
| `eval/run_replay.py` | `93197316d276` | `9b277af500c7` | opt-in fixed base schedule under the verification trigger (phase-free control, 15 Sep 2026) and, for the earlier studies, the trigger integration itself |
| `skills/county-flood-data/county_flood/models.py` | `9951789df450` | `5021dec1e548` | trigger-era model fields (Aug 2026) |
| `skills/county-flood-data/county_flood/sources/nws_alerts.py` | `51dca7e3af28` | `e64d3ae3a293` | trigger-era alert handling (Aug 2026) |
| `watcher/cli.py` | `241318aa231c` | `e12bbc81a8b2` | verification-trigger integration in the live heartbeat and CLI (Aug 2026) |
| `watcher/config.py` | `98a83b99bb1f` | `ba68c18dca09` | trigger environment variables added (Aug 2026) |
| `watcher/county_flood_bridge.py` | `6e9d2ddd95b3` | `ff70be797c8a` | trigger-era bridge changes (Aug 2026) |
| `watcher/neo4j_client.py` | `8057fdbe46e6` | `7aeb1aae329e` | persistence of the trigger state on the mission record (Aug 2026) |
| `watcher/timezone_util.py` | `0070240c8485` | `f26a4f35ab7e` | timezone helpers added for the trigger streams (Aug 2026) |

### Study 2: 4 of 81 hashed files differ

| File | Frozen SHA-256 (prefix) | Current SHA-256 (prefix) | Change |
|---|---|---|---|
| `eval/run_replay.py` | `0473ac1c810e` | `9b277af500c7` | opt-in fixed base schedule under the verification trigger (phase-free control, 15 Sep 2026) and, for the earlier studies, the trigger integration itself |
| `watcher/cli.py` | `929526b480bd` | `e12bbc81a8b2` | verification-trigger integration in the live heartbeat and CLI (Aug 2026) |
| `watcher/timezone_util.py` | `0070240c8485` | `f26a4f35ab7e` | timezone helpers added for the trigger streams (Aug 2026) |
| `watcher/verification_trigger.py` | `a984c8e013c4` | `bd9a778460ce` | additive (non-re-anchoring) anchor mode added for Study 3 (Aug 2026) |

### Study 3: 1 of 85 hashed files differ

| File | Frozen SHA-256 (prefix) | Current SHA-256 (prefix) | Change |
|---|---|---|---|
| `eval/run_replay.py` | `59538f02ef0e` | `9b277af500c7` | opt-in fixed base schedule under the verification trigger (phase-free control, 15 Sep 2026) and, for the earlier studies, the trigger integration itself |

## Checks run at export

| Check | Result |
|---|---|
| `pytest watcher/tests eval/tests skills/county-flood-data/county_flood/tests skills/flood-article-detector/tests -q` | 400 passed, 5 subtests passed |
| Non-implementation manifest hashes recomputed | all match (see above) |
| `figures/make_results_figure.py` (Study 1 figure) | PDF and PNG byte-identical to `figures/fig_acquisition_delay_tradeoff.*` |
| `eval/figures_v3.py` and `eval/figures_v3.py --v4` (Study 2 and 3 figures) | PNGs byte-identical to the shipped files; PDFs identical after removing the embedded `/CreationDate`, which matplotlib sets from the clock (set `SOURCE_DATE_EPOCH` for a fixed date) |
| `figures/make_county_map.py` | PNG byte-identical to `figures/fig_county_events_map.png`; 5, 11, and 28 county-events verified against the manifests |
| `figures/make_mechanism_figure.py --report` | all fail-closed checks pass; PDF SHA-256 `399667e917c5cacb...` equals the shipped file |
| `eval/post_hoc_comparators.py` | `summary.json` and `summary.md` byte-identical to the shipped files |
| `eval/posthoc_bootstrap_intervals.py` | `summary.json` and `summary.md` byte-identical to the shipped files (fixed seed 20260918) |
| `eval/posthoc_data_statistics.py` | fails closed on the absent GDELT corpora (`eval/corpus/study_v5/*_gdelt.json`); regenerate them first (README, News corpora). The shipped `data_statistics.md` was produced with the corpora present. |
| `eval/robustness_checks.py`, `eval/run_study_v4.py` | not rerun at export (long); shipped results are the frozen originals |
| `eval/deployment_cost.py` | not rerun; requires the raw log, which is not shipped |

## Update of 15 October 2026

Brought in line with the manuscript as submitted to Computers, Environment and Urban Systems. Changes: the Study 1 figure script no longer draws a title, subtitle, or source note; `eval/figures_v3.py` labels Marshall County by its flood region and now writes into `figures/` (it previously wrote to a `paper/figures/` directory that does not exist in this repository); the Storm Events comparison (`eval/posthoc_storm_events.py`) and the phase-occupancy summary (`eval/posthoc_phase_occupancy.py`) and their results were added; the hand-drawn architecture figure (`figures/overview.svg` and `.png`) replaced an earlier version. None of these files is in any study manifest's implementation set, so every recorded hash above is unaffected.

| Check | Result |
|---|---|
| `pytest watcher/tests eval/tests skills/county-flood-data/county_flood/tests skills/flood-article-detector/tests -q` | 400 passed, 5 subtests passed |
| `figures/make_results_figure.py` | PNG byte-identical to the manuscript's figure; PDF identical after removing `/CreationDate` |
| `eval/figures_v3.py` and `--v4` | PNGs byte-identical to the manuscript's figures; PDFs identical after removing `/CreationDate` |
| `eval/posthoc_phase_occupancy.py` | `summary.json` byte-identical to the shipped file |
| `eval/posthoc_storm_events.py --storm-dir <dir>` | run with the nine bulk files recorded in the shipped `summary.json` (SHA-256 verified); every replay matched its stored summary, and the output differed from the shipped files only in the generation date and the path of the imported figure script. The shipped files are the source of record produced on 23 September 2026. |

Every decimal or thousands-separated number in the manuscript's abstract, body, and tables was searched for in `eval/results/` on 15 October 2026; all 176 are present there, apart from the 2,089.25 reference-positive hours that divide the robustness table's percentages, which is the sum of `observed_flood_duration_hours` over the hybrid's 24 episode-evaluation rows in `eval/results/study_v4_additive/counties/*/result.json`.

## Known contents that reveal the development environment

- `eval/corpus/study/belief_news_manifest.json` records the exact regeneration command for the Harris GDELT corpus, including the interpreter path on the development machine. The file is hash-bound by `belief_news_null.json`, so it is shipped unchanged.
- `eval/corpus/study_v5/*_t2_triggers.json` record the path of the GDELT corpus each trigger stream was built from, on the development machine. They are hash-bound and shipped unchanged.
- `watcher/slack_delivery.py` carries the production Slack channel identifier as its default; it is in the hashed implementation set and is overridable through `SENTINEL_SLACK_CHANNEL`. A channel identifier is not a credential.
- `watcher/config.py`, `watcher/heartbeat_cron.py`, and `watcher/slack_logger.py` carry default paths from the production host (`/mnt/secondary/...`). They are in the hashed implementation set and are shipped unchanged; every default is overridable through the environment variables documented in `watcher/config.py`, and `bin/sentinel-cli` reads `NEO4J_PASSWORD` or `NEO4J_PASSWORD_FILE` before falling back to that default.
- Two test files, `watcher/tests/test_format_analyst.py` and `watcher/tests/test_format_slack.py`, compare rendered output against strings that contain the development name; they are unchanged so the tests pass.
- `skills/flood-article-detector/article_verdict.py` names a language model for an optional article-verdict step that is disabled by default and was not used in any reported result; `watcher/format_analyst.py` does the same for the optional analyst summary.
