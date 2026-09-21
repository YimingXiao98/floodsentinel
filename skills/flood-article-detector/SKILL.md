---
name: flood-article-detector
description: Discover and score recent flood news articles for a US county; return top 5 sources and a flood-occurred verdict with heavy recency weighting
user-invocable: true
command-dispatch: tool
command-tool: exec
allowed-tools:
  - Bash(<workspace>/bin/run-flood-article-detector *)
---
version: 1.0.0
main: main.py
inputs:
  - disaster_type
  - county
  - state
  - location
outputs:
  - flood_outcome
  - top_sources
  - search_summary
  - coordinator_integration
---

# flood-article-detector

Deterministic skill that accepts a **disaster type** (flood) plus **county and state**, searches **Google News RSS** (primary) and web search (fallback) for recent articles, and scores only articles that are **published today or yesterday (UTC)** and **contain the requested disaster keyword**. Older articles or articles without the keyword are auto-scored to 0 and omitted.

**Deterministic by default:** Uses `command-dispatch: tool` → `exec`. No LLM step required for the default path. Deterministic and LLM-free by default; an optional, bounded (≤5 articles), flag-gated LLM read-and-conclude step is available for higher-fidelity county-specific verdicts — see `article_verdict.py`.

| Symbol | Path |
|--------|------|
| `SENTINEL_ROOT` | `<workspace>` |
| `RUN_FLOOD_ARTICLE_DETECTOR` | `<workspace>/bin/run-flood-article-detector` |

## Invoke (OpenClaw TUI / chat)

```
/skill flood-article-detector flood Miami-Dade County, Florida
/skill flood-article-detector flooding Harris County, Texas
```

**First-time setup:** Run the dispatch patch once on this host (routes args through `run-flood-article-detector` instead of shell-interpreting `flood` as a command):

```bash
sudo bash <workspace>/scripts/patch-openclaw-county-flood-dispatch.sh
# then restart gateway: openclaw gateway restart
```

In the TUI, start a fresh session (`/new`) after restarting the gateway.

## CLI (direct / debugging)

```bash
<workspace>/bin/run-flood-article-detector "flood Miami-Dade County, Florida"

python3 <workspace>/skills/flood-article-detector/main.py \
  --location "Miami-Dade County, Florida" --disaster flood --format json

# Optional: county-specific LLM verdict on top-N articles (up to 5 LLM calls)
ARTICLE_LLM_VERDICT_ENABLED=on python3 .../main.py --location "Harris County, Texas" --disaster flood
python3 .../main.py --location "Harris County, Texas" --disaster flood --llm-verdict
```

| Flag / env | Default | Effect |
|------------|---------|--------|
| `--llm-verdict` | off | Enable bounded LLM read-and-conclude on top-N articles |
| `ARTICLE_LLM_VERDICT_ENABLED=on` | off | Same as `--llm-verdict` when CLI flag not set |

When enabled, adds up to **5 additional LLM calls** per invocation (latency/cost). Articles about flooding in a **different county** are excluded from the aggregate verdict.

## Eligibility (hard filters)

Articles are **omitted** (page_score = 0, excluded from `top_sources`) when:

| Filter | Rule |
|--------|------|
| Recency | Publish date must be **today or yesterday** (UTC calendar dates) |
| Keyword | Title/body must contain the requested disaster keyword (e.g. `flood`, `flooding`, `flash flood`) |
| Publish date | Missing publish date → omitted |

`search_summary.omitted` reports counts: `outdated`, `missing_keyword`, `missing_publish_date`.

## Page scoring (recency-heavy, eligible articles only)

```
page_score =
  0.60 × recency_score
+ 0.20 × flood_mention_score
+ 0.12 × indication_normalized
+ 0.08 × location_score
```

| Age since publish | recency_score |
|-------------------|---------------|
| Today, ≤ 6 hours | 1.00 |
| Today, > 6 hours | 0.92 |
| Yesterday | 0.75 |
| Older than yesterday | omitted (score 0) |
| No date found | omitted (score 0) |

**Indication values:** `confirms_flood` (+1.0), `suggests_flood_risk` (+0.5), `neutral` (0.0), `denies_or_clear` (−0.8).

**Flood verdict** aggregates top 5 articles: `outcome_score = Σ(page_score × indication) / Σ(page_score)`.

| outcome_score | verdict |
|---------------|---------|
| ≥ 0.55 | flood_confirmed |
| ≥ 0.30 | flood_likely |
| ≥ 0.10 | flood_possible |
| ≤ −0.15 | no_flood_evidence |
| else | inconclusive |

## Output JSON

`schema_version`: **1.1** (additive; `llm_verdict` on each `top_sources[]` item, optional `outcome_method` on `flood_outcome`).

Returns `top_sources[]` (up to 5), `flood_outcome`, `search_summary`, and `coordinator_integration` (`merge_key` = `article_evidence`, threshold **0.75**). The **coordinator** skill merges this automatically on every `run-coordinator` cycle.

Each `top_sources[]` item includes `llm_verdict` (object or `null`). When `--llm-verdict` is enabled and the call succeeds, `llm_verdict` contains `county_relevance`, `flood_status`, `confidence`, `rationale`, `supporting_quote`, `final_verdict` (code-derived), and `text_source` (`fetched_full_page` or `snippet_fallback`).

`flood_outcome` may include `outcome_method: "llm_verified"` when the LLM aggregation path was used; absent on the legacy rule-based path.

## Ontology integration

- **Input:** `disaster_type` (flood only in v1), `county`, `state`, or natural-language `location`.
- **Bridge:** No Neo4j writes in v1. Output `coordinator_integration.merge_key` = `article_evidence`; coordinator includes articles with `page_score` > **0.75** in JSON output and Slack report.
- **Persistence:** None in v1.

## Exec rules

| Do | Don't |
|----|-------|
| Run `RUN_FLOOD_ARTICLE_DETECTOR` for flood article requests | Fabricate article content or scores |
| Return CLI stdout verbatim | Skip web fetch and invent sources |
