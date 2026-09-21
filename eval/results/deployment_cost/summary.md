# Operational cost of the production deployment (read from the heartbeat cron log)

Source: `logs/sentinel-heartbeat-cron.log` (86,176,521 bytes, sha256 `403fe1c3d70cec8246b5157444684ab6ad79eea512ddcb8270b4066e54756150`), parsed once with `eval.analyze_deployment.iter_concatenated_json`. The log holds 2,273 JSON objects: 2,272 heartbeat polls (objects with `missions_total`) from `run_at` 2026-07-23T02:15:02.248033Z to 2026-08-17T17:00:01.798467Z, and 1 non-poll object. The unit of analysis below is the due acquisition cycle, one entry of a poll's `results` list; there are 689 of them, and `errors` is empty throughout, so due equals ran. 1,818 polls ran no cycle, 219 ran one and 235 ran two. Consecutive poll `run_at` gaps: median 899.7 s, interquartile range 899.7 to 900.6 s, maximum 170,100.1 s. Gaps longer than 1,800 s: 2026-07-24T02:15:02.343996Z to 2026-07-26T01:30:02.419258Z (47.25 h), which lies inside the active window of no mission, so it inflates no mission's duration.

Medians and interquartile ranges use `statistics.median` and `statistics.quantiles(n=4, method="exclusive")`. Every number names the field it came from. Nothing here is converted to money.

## 1. Per-cycle timing

The log records no elapsed, duration or latency field at any level: not on the poll, not on the result, not on any source, gauge, snapshot or Slack part. The key-path probe for timing-like names (`elapsed`, `duration`, `latency`, `_ms`, `took`, `timing`, `started_at`, `finished_at`, `ended_at`) matches only `results[].neo4j_deactivate.ended_at`, none of which is a measured duration (`ended_at` is the mission deactivation time written once when the Escambia mission folded). Per-source fetch latencies do not exist in the log.

What the log does carry are timestamps stamped at fixed points of a cycle that runs strictly sequentially inside one cron process (`cmd_run_heartbeat` iterates missions in a plain loop; `_run_coordinator_cycle` fetches the official sources, then the news detector, then persists and diffs, and only then is Slack called). Wall-time spans between those stamps can therefore be reconstructed. They are reconstructed spans, not recorded durations, and they inherit two caveats: the start of a cycle that is not the first in its poll is taken as the previous cycle's end, which absorbs the between-mission bookkeeping (verification-trigger consultation, off in this deployment, and the Neo4j `get_mission_context` read); and the Slack stamp is the server-assigned `ts` on the accepted post, so the delivery span includes the `openclaw message send` subprocess and the 0.3 s inter-part sleep (`MULTI_MESSAGE_SEND_DELAY_SEC`) as well as network time.

Stamps used (all UTC): **cycle_start**: poll run_at for the first result of a poll; otherwise the previous result's end (its last Slack message_id ts, or its run_at when nothing was delivered); **fetched_at**: results[].fetched_at (county_flood_payload.generated_at, stamped when the official payload is assembled after all three official sources return); **article_at**: results[].article_evidence.generated_at (stamped when the news detector returns); **phase_at**: results[].phase_decision.evaluated_at_utc; **run_at**: results[].run_at (stamped at the end of _run_coordinator_cycle, after Neo4j persistence and the delta report, before Slack delivery); **slack_ts**: results[].slack_delivery.results[].message_id and results[].fold_slack_delivery.message_id (Slack-assigned ts, epoch seconds).

| Span | Definition | n | Median (s) | IQR (s) | Max (s) | Sum (s) |
|---|---|---|---|---|---|---|
| official_fetch_stage | `fetched_at minus cycle_start` | 689 | 3.9 | 2.7 to 5.5 | 66.2 | 3,247.8 |
| news_stage | `article_evidence.generated_at minus fetched_at` | 689 | 5.5 | 5.2 to 5.9 | 17.1 | 3,885.5 |
| post_evidence_stage | `run_at minus article_evidence.generated_at` | 689 | 0.2 | 0.1 to 0.3 | 1.4 | 178.6 |
| compute | `run_at minus cycle_start` | 689 | 9.9 | 8.2 to 11.7 | 72.4 | 7,312.0 |
| slack_delivery | `last Slack message_id ts minus run_at (delivered cycles)` | 602 | 29.0 | 14.9 to 30.8 | 90.5 | 15,989.7 |
| end_to_end | `last Slack ts (or run_at when nothing was delivered) minus cycle_start` | 689 | 37.1 | 22.6 to 41.2 | 101.2 | 23,301.7 |

Restricted to the first result of each poll, where `cycle_start` is exactly the poll `run_at`; the `official_fetch_stage` span then still includes the Neo4j reads that precede the fetch (`get_mission_context`, `_phase_aware_context`, `get_last_coordinator_report`, prior gauge readings) (n = 454):

| Span | Definition | n | Median (s) | IQR (s) | Max (s) | Sum (s) |
|---|---|---|---|---|---|---|
| official_fetch_stage | `fetched_at minus cycle_start` | 454 | 4.3 | 3.1 to 5.8 | 66.2 | 2,341.0 |
| news_stage | `article_evidence.generated_at minus fetched_at` | 454 | 5.6 | 5.3 to 6.0 | 17.1 | 2,621.0 |
| post_evidence_stage | `run_at minus article_evidence.generated_at` | 454 | 0.2 | 0.1 to 0.3 | 1.4 | 121.8 |
| compute | `run_at minus cycle_start` | 454 | 10.5 | 8.8 to 12.1 | 72.4 | 5,083.8 |
| slack_delivery | `last Slack message_id ts minus run_at (delivered cycles)` | 367 | 29.6 | 15.5 to 31.4 | 90.5 | 10,058.5 |
| end_to_end | `last Slack ts (or run_at when nothing was delivered) minus cycle_start` | 454 | 38.3 | 22.1 to 42.2 | 101.2 | 15,142.3 |

Whole-poll wall time for polls that ran at least one cycle (last Slack ts or result `run_at` in the poll minus poll `run_at`; n = 454): median 58.4 s, interquartile range 36.6 to 63.2 s, maximum 136.1 s. Of 1,054 Slack `message_id` values on delivered parts, 0 failed to parse as a Slack `ts`. Negative spans (a later stamp preceding an earlier one) were excluded and counted: none.

## 2. Source requests per due cycle

`sources_attempted` is 3 in 689 cycles; `sources_succeeded` is 0 in 33 cycles, 3 in 656 cycles. These count the three official source records the coordinator asks the county-flood-data skill for (`source_records[]` with `source_id` `src_county_flood_nwps_*`, `src_county_flood_usgs_*`, `src_county_flood_nws_alerts_*`), not HTTP requests; `county_flood_source_mode` is `both` in every cycle. The skill returned a payload in 656 cycles and none in 33 (`county_flood_payload` null, `sources_succeeded` 0, one `failed_fetches[]` entry). The news detector is not counted in `sources_attempted`; it appears as `article_evidence` in every cycle.

Per-cycle quantities the payload records (cycles with a payload):

| Quantity | Field | n | Median | IQR | Max | Total |
|---|---|---|---|---|---|---|
| NWPS gauges selected (one `GET /nwps/v1/gauges/{shef_id}` each in `nwps._fetch_one`) | `county_flood_payload.sources.nwps.csv_match_count` | 656 | 11 | 6 to 30 | 32 | 12,014 |
| USGS sites selected (batched into one NWIS IV `GET` per 50 sites in `fetch_usgs_iv`) | `county_flood_payload.sources.usgs.sites_selected` | 656 | 10 | 4 to 10 | 69 | 11,637 |
| USGS sites found by discovery | `county_flood_payload.sources.usgs.discovered_sites` | 656 | 28 | 3 to 62 | 75 | 23,350 |
| NWS alerts returned (one `GET` to `api.weather.gov/alerts/active` per cycle) | `county_flood_payload.sources.nws_alerts.alert_count` | 656 | 0 | 0 to 0 | 1 | 5 |
| Gauge rows in the payload | `len(county_flood_payload.gauges)` | 656 | 9 | 7 to 10 | 60 | 11,218 |
| Gauges queried (summary) | `county_flood_payload.summary.gauges_queried` | 656 | 20 | 10 to 20 | 99 | 19,397 |
| Gauges with data | `county_flood_payload.summary.gauges_with_data` | 656 | 9 | 7 to 10 | 60 | 11,218 |
| Gauges scored against a flood stage | `county_flood_payload.summary.gauges_scored` | 656 | 3 | 2 to 10 | 29 | 5,641 |
| Gauges unscored | `county_flood_payload.summary.gauges_unscored` | 656 | 5 | 3 to 7 | 31 | 5,440 |

The log does not record HTTP requests. Reading the skill source, one payload implies at least `csv_match_count` NWPS requests plus ceil(`sites_selected`/50) NWIS IV requests plus one NWS alerts request; USGS site discovery, county-to-API mapping (`county_api_mapping.cache_hit` by value: {"False": 43, "True": 613, "no_mapping": 33}) and NWPS threshold enrichment add requests the log does not count. That code-implied lower bound is median 13 official GETs per cycle (interquartile range 8 to 33, maximum 34, sum 13,439 over 656 cycles). It is an inference from code, not a measurement.

News (`article_evidence.search_summary`): the detector issues one Google News RSS `GET` per cycle (`search_google_news_rss`) and then one `curl` page fetch per candidate the secondary web search surfaces. `queries_run` is `max(1, len(build_article_queries(...)))` in `discover_and_fetch`, a constant 5 that does not count requests issued.

| Quantity | Field | n | Median | IQR | Max | Total |
|---|---|---|---|---|---|---|
| Queries declared (constant by construction) | `article_evidence.search_summary.queries_run` | 689 | 5 | 5 to 5 | 5 | 3,445 |
| Candidates found | `article_evidence.search_summary.candidates_found` | 689 | 1 | 0 to 1 | 6 | 716 |
| Candidates fetched (RSS items kept plus pages fetched) | `article_evidence.search_summary.candidates_fetched` | 689 | 1 | 0 to 1 | 6 | 694 |
| Candidates scored | `article_evidence.search_summary.candidates_scored` | 689 | 1 | 0 to 1 | 6 | 672 |
| Articles above the 0.75 threshold | `article_evidence.articles_above_threshold` | 689 | 0 | 0 to 1 | 4 | 287 |

### Fetch errors

Total source fetch errors: 1,811 = 1,778 entries in `county_flood_payload.sources.<provider>.fetch_errors[]` plus 33 coordinator-level `failed_fetches[]` entries (whole-skill failures in cycles with no payload). They fall in 431 of the 689 due cycles; in 307 of those the only error class is `no_iv_data`.

| Provider and class | Field | Count | Meaning |
|---|---|---|---|
| `nwps:timeout` | `county_flood_payload.sources.nwps.fetch_errors[].error` | 2 | one per-gauge NWPS request timed out (`{shef_id, error}`) |
| `nws_alerts:http_503` | `county_flood_payload.sources.nws_alerts.fetch_errors[].error` | 3 | the NWS alerts request failed with HTTP 503 |
| `usgs:http_400` | `county_flood_payload.sources.usgs.fetch_errors[].error` | 76 | the bulk NWIS IV request itself failed with HTTP 400 (`{source: 'usgs_iv', error}`); all selected sites unread |
| `usgs:http_503` | `county_flood_payload.sources.usgs.fetch_errors[].error` | 12 | the bulk NWIS IV request itself failed with HTTP 503 (`{source: 'usgs_iv', error}`); all selected sites unread |
| `usgs:no_iv_data` | `county_flood_payload.sources.usgs.fetch_errors[].error` | 1,685 | a selected site absent from a successful bulk NWIS IV response (`fetch_usgs_county_gauges` appends `{site_no, error: 'no_iv_data'}` per missing site); not a failed request |
| `coordinator:http_400` | `failed_fetches[].failure_reason` | 11 | the county-flood-data skill raised before returning a payload (`county_flood_bridge` records one entry with `sources_succeeded` 0) |
| `coordinator:http_503` | `failed_fetches[].failure_reason` | 22 | the county-flood-data skill raised before returning a payload (`county_flood_bridge` records one entry with `sources_succeeded` 0) |

Extraction failures: 288 entries in `extraction_failures[]`, all `failure_reason` `no_gauges` ({"src_county_flood_usgs_04005": 76, "src_county_flood_usgs_12033": 6, "src_county_flood_usgs_48201": 137, "src_county_flood_usgs_53033": 69} by `source_id`), in 288 cycles. Every `county_flood_payload.gauges[].error` is null (0 non-null of 11,218 rows; rows by `source`: {"nwps": 7689, "usgs": 3529}).

## 3. Slack delivery per due cycle

Logical reports delivered: 602 cycles with `slack_delivery.ok` true. Transport parts: 1,054 (`slack_delivery.messages_sent`), of which 1,054 `slack_delivery.results[].ok` true and 0 otherwise. Parts per delivered report (`messages_sent`): median 2, interquartile range 1 to 2, maximum 5. Skipped deliveries by `slack_delivery.reason`: {"no_material_change": 87}. Failed: 0. Not recorded: 0. Fold notices (`fold_slack_delivery.ok`): 1. Parts prepared (`len(slack_messages)`): 1,147 across all due cycles, 1,054 in delivered cycles, totalling 2,401,399 characters of message text (`sum(len(slack_messages[i]))`, delivered cycles). `slack_delivery.message_id` differed from the last `results[].message_id` in 0 deliveries.

## 4. Acquisitions per county per day, observed and at fixed clocks

Mission-active windows are the first and last poll `run_at` at which a mission appears in `results`, `skipped` or `errors`; the window therefore starts at the first observed poll, not at mission creation, and for missions still active at the end of the log it ends at the last poll. Durations of a county's missions are summed when a county had more than one mission. A fixed clock over the same window needs floor(duration/interval) + 1 acquisitions (one at the window start and one per interval thereafter).

| County | FIPS | Missions | Active days | Due acquisitions (`results[]`) | Due per day | 15 min clock | 1 h clock | 3 h clock | 6 h clock |
|---|---|---|---|---|---|---|---|---|---|
| Coconino, AZ | 04005 | 1 | 19.80 | 76 | 3.84 | 1,901 | 476 | 159 | 80 |
| Escambia, FL | 12033 | 2 | 4.67 | 290 | 62.14 | 450 | 114 | 39 | 20 |
| Harris, TX | 48201 | 2 | 4.23 | 251 | 59.34 | 407 | 102 | 35 | 18 |
| King, WA | 53033 | 1 | 18.80 | 72 | 3.83 | 1,805 | 452 | 151 | 76 |
| All | | 6 | 47.50 | 689 | 14.51 | 4,563 | 1,144 | 384 | 194 |

Observed acquisitions as a fraction of each clock's requirement (`due_acquisitions` / clock):

| County | 15 min | 1 h | 3 h | 6 h |
|---|---|---|---|---|
| Coconino, AZ | 0.040 | 0.160 | 0.478 | 0.950 |
| Escambia, FL | 0.644 | 2.544 | 7.436 | 14.500 |
| Harris, TX | 0.617 | 2.461 | 7.171 | 13.944 |
| King, WA | 0.040 | 0.159 | 0.477 | 0.947 |
| All | 0.151 | 0.602 | 1.794 | 3.551 |

Per mission:

| Mission | First poll `run_at` | Last poll `run_at` | Active days | Opportunities | Due | Skips | `mission_spec.heartbeat_interval_seconds` seen on due results and `skipped[].heartbeat_interval_seconds` |
|---|---|---|---|---|---|---|---|
| `evt_coconino_flood_20260728T213432Z` | 2026-07-28T21:45:01.921572Z | 2026-08-17T17:00:01.798467Z | 19.80 | 1,902 | 76 | 1,826 | {"21600": 1902} |
| `evt_escambia_flood_20260723T003944Z` | 2026-07-23T02:15:02.248033Z | 2026-07-24T02:15:02.343996Z | 1.00 | 97 | 96 | 1 | {"800": 97} |
| `evt_escambia_flood_20260726T051204Z` | 2026-07-26T05:15:02.183979Z | 2026-07-29T21:15:02.228134Z | 3.67 | 353 | 194 | 159 | {"800": 256, "43200": 97} |
| `evt_harris_flood_20260723T153125Z` | 2026-07-23T15:45:01.946227Z | 2026-07-24T02:15:02.343996Z | 0.44 | 43 | 43 | 0 | {"800": 43} |
| `evt_harris_flood_20260726T012445Z` | 2026-07-26T01:30:02.419258Z | 2026-07-29T20:30:02.033149Z | 3.79 | 365 | 208 | 157 | {"800": 271, "43200": 94} |
| `evt_king_flood_20260729T213339Z` | 2026-07-29T21:45:01.938587Z | 2026-08-17T17:00:01.798467Z | 18.80 | 1,806 | 72 | 1,734 | {"21600": 1806} |

Realized inter-acquisition gap by configured interval, parsed from `cycle_due_reason` (`"due (<elapsed>s elapsed, interval <interval>s)"`; the one `first_run` cycle has no gap):

| Configured interval (s) | n | Median elapsed (s) | IQR (s) | Max (s) | Median / configured |
|---|---|---|---|---|---|
| 800 | 533 | 890 | 858 to 924 | 1,799 | 1.113 |
| 21,600 | 152 | 22,488 | 22,486 to 22,489 | 35,165 | 1.041 |
| 43,200 | 3 | 44,089 | 44,088 to 44,095 | 44,095 | 1.021 |

Unparsed `cycle_due_reason` values: {"first_run": 1}. Interval values on due results (`mission_spec.heartbeat_interval_seconds`): {"800": 538, "21600": 148, "43200": 3}; by `mission_spec.current_event_phase`: {"active_incident:800": 4, "early_watch:21600": 148, "early_watch:800": 344, "elevated_watch:800": 189, "folded:43200": 1, "imminent:800": 1, "recovery:43200": 2}. The 800 s interval is the one the Escambia and Harris missions carried across early_watch, elevated_watch, imminent and active_incident; the Coconino and King missions carried 21,600 s in early_watch, and 43,200 s appears in recovery. Because the cron polls every 15 minutes, an 800 s interval becomes due on the next poll, so its realized gaps cluster around the 900 s poll spacing.

## 5. Quota, HTTP status, bytes, tokens and LLM usage

A full-depth scan of all 1,161 distinct key paths in the log was probed by name. Token, prompt, completion, usage, LLM or model keys: `results[].article_evidence.high_confidence_articles[].llm_verdict`, `results[].article_evidence.top_sources[].llm_verdict`. Quota, rate-limit, retry or throttle keys: no key. HTTP status keys (`status_code`, `http_status`, `response_code`): no key. Byte or size keys: `results[].source_snapshots[].byte_length`, `results[].stored_readings_summary.latest[].byte_length`. Cost or billing keys: no key.

The only LLM-related key is `llm_verdict` on `article_evidence.top_sources[]` and `article_evidence.high_confidence_articles[]`; it is null in all 953 occurrences and non-null in 0, consistent with the detector's `ARTICLE_LLM_VERDICT_ENABLED` gate defaulting to off. No token counts or model names appear anywhere. HTTP status codes appear only as text inside error strings on failed requests ({"coordinator:400": 11, "coordinator:503": 22, "nws_alerts:503": 3, "usgs:400": 76, "usgs:503": 12}, from `fetch_errors[].error` and `failed_fetches[].failure_reason`); successful requests record no status code. The only byte quantity is `source_snapshots[].byte_length`, the UTF-8 length of the canonical JSON the runtime persists per official source snapshot (`len(canonical.encode("utf-8"))` in `county_flood_bridge`), not the size of any HTTP response: `src_county_flood_nwps` n = 656, median 7,692 B, max 17,503 B, sum 5,640,786 B; `src_county_flood_nws_alerts` n = 656, median 245 B, max 693 B, sum 162,202 B; `src_county_flood_usgs` n = 656, median 1,317 B, max 24,644 B, sum 3,191,678 B. `csv_truncated` flags by source: {"nwps:False": 378, "nwps:True": 278, "usgs:True": 656}. No `verification_trigger` key is present (False), so the trigger was off; no `ontology_ledger` key is present (False). The poll-level `cron` block records only scheduler state (fields: `ok`, `action`, `enabled`, `via`, `os_crontab`, `openclaw_agent_job`, `schedule`, `command`, `active_mission_count`).

## Reconciliation with `eval/results/deployment_summary.json`

| Check | This script | deployment_summary.json | Match |
|---|---|---|---|
| input.sha256 | 403fe1c3d70cec8246b5157444684ab6ad79eea512ddcb8270b4066e54756150 | 403fe1c3d70cec8246b5157444684ab6ad79eea512ddcb8270b4066e54756150 | yes |
| due_cycles | 689 | 689 | yes |
| heartbeat_polls | 2272 | 2272 | yes |
| source_fetch_error_total | 1811 | 1811 | yes |
| coordinator_failed_fetches | 33 | 33 | yes |
| county_api_fetch_errors | 1778 | 1778 | yes |
| no_iv_data | 1685 | 1685 | yes |
| extraction_failures_no_gauges | 288 | 288 | yes |
| report_deliveries | 602 | 602 | yes |
| report_messages_sent | 1054 | 1054 | yes |
| materiality_skips | 87 | 87 | yes |
| fold_deliveries | 1 | 1 | yes |

All scalar checks match: True. Mission windows (first and last poll `run_at`, opportunity counts) agree with the union of each mission's `configuration_epochs.mission_heartbeat` epochs: True.

