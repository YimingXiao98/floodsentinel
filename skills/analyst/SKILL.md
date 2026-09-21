---
name: analyst
description: User-facing flood situation analyst — runs coordinator, produces plain-language situation summaries with confidence explanation, delivers via Slack
user-invocable: true
command-dispatch: tool
command-tool: exec
allowed-tools:
  - Bash(*run-analyst*)
  - Bash(<workspace>/bin/run-analyst *)
  - Bash(*sentinel-cli run-analyst*)
  - Bash(<workspace>/bin/sentinel-cli run-analyst *)
  - slack
---
version: 1.0.0
inputs:
  - task_type
  - mission_id
  - disaster_type
  - location
  - county
  - state
  - fips
outputs:
  - analyst_summary
  - analyst_slack_message
  - mission_spec
  - monitoring_summary
  - belief_state
  - evidence_claims
  - phase_decision
  - report_delta
  - county_flood_evidence
---

# analyst

**User-facing entry skill.** When invoked, runs the full coordinator monitoring cycle, then produces a **plain-language situation summary** with an explicit **confidence explanation**, and **posts to Slack**.

Use analyst (not coordinator) when the user wants a readable situation report for Slack or chat — not the full technical coordinator dump.

## Quick summary

| | |
|---|---|
| **Purpose** | `run-analyst` → coordinator cycle → situation summary + confidence narrative → Slack |
| **Underlying data** | Same as coordinator: county-flood-data (NWPS + USGS), flood-article-detector, Neo4j phase rules |
| **Output** | Situation at a glance, hydrology, alerts, confidence assessment, changes since last run |
| **Slack** | **Automatic** post to `channel:<channel-id>` on every successful run |
| **Never** | Fabricate readings, inflate confidence, or skip the CLI exec |

**Deterministic execution:** This skill uses `command-dispatch: tool` → `exec`. OpenClaw runs `run-analyst` directly.

| Symbol | Path |
|--------|------|
| `SENTINEL_ROOT` | `<workspace>` |
| `RUN_ANALYST` | `<workspace>/bin/run-analyst` |
| `SENTINEL_CLI` | `<workspace>/bin/sentinel-cli` |
| `SLACK_CHANNEL` | `channel:<channel-id>` (override: `SENTINEL_SLACK_CHANNEL`) |

## Invoke (OpenClaw TUI / chat)

```
/skill analyst Situation report for Harris County, Texas
/skill analyst What's the flood situation in Miami-Dade County, Florida?
/skill analyst --fips 48201
```

CLI stdout includes the human situation summary **and** a `Slack: delivered to channel:…` confirmation line.

**First-time setup:** Run the dispatch patch once on this host (includes analyst routing):

```bash
sudo bash <workspace>/scripts/patch-openclaw-county-flood-dispatch.sh
# then restart gateway: openclaw gateway restart
```

In the TUI, start a fresh session (`/new`) after restarting the gateway.

## Exec rules (mandatory)

| Do | Don't |
|----|-------|
| `exec` with `host=gateway`, `ask=off`, `security=full` | Use sandbox or skip exec |
| Run `RUN_ANALYST` or `SENTINEL_CLI run-analyst` | Fabricate situation facts or confidence |
| Return CLI stdout verbatim | Post coordinator technical report instead of analyst summary |
| Let the CLI post to Slack automatically | Call the `slack` tool separately when CLI confirms delivery |

## Workflow

Parse user input into `COUNTY` and `STATE` (or `--fips`).

### Step 1 — Run analyst cycle (primary path)

```bash
<workspace>/bin/run-analyst "COUNTY County, STATE"
```

Or directly:

```bash
<workspace>/bin/sentinel-cli run-analyst --county "COUNTY" --state "STATE"
```

Also: `--mission-id ID`, `--fips FIPS`. Skip Slack with `--no-slack`.

This single command:
1. Runs the **full coordinator cycle** (bootstrap if needed, fetch, extract, phase rules, delta)
2. Builds a **situation summary** grounded in coordinator JSON
3. Explains **confidence** (formula, components, per-claim scores, gaps)
4. **Posts the analyst summary to Slack** via `openclaw message send`
5. Prints human report + `---SLACK---` block + delivery confirmation

Debug JSON: `--format json`. Slack-only text: `--format slack`.

### Step 2 — Return report to user

Return the **situation summary** (everything before `---SLACK---`) plus the trailing `Slack: delivered to channel:…` line verbatim.

| Section | Content |
|---------|---------|
| **Situation at a glance** | Phase, alert level, bottom-line flooding verdict |
| **Hydrology** | Per-gauge stage/flow/category with confidence |
| **NWS alerts** | Active county-zone alerts or explicit none |
| **Confidence assessment** | Formula, API overall score, component breakdown, gaps |
| **Changes since last report** | Delta bullets from coordinator |
| **Slack confirmation** | `Slack: delivered to channel:<channel-id>` on success |

Do not summarize away confidence math or the Slack confirmation.

### Step 3 — Slack (automatic)

Analyst posts the **situation summary** (not the full coordinator technical report) to the configured channel.

| Config | Value |
|--------|-------|
| Default channel | `<channel-id>` |
| Override | `export SENTINEL_SLACK_CHANNEL=<channel-id>` |
| Skip posting | `--no-slack` |

For the full technical coordinator report with API URLs and raw snapshots, use `/skill coordinator` instead.

### Analyst vs coordinator

| Skill | Output | Slack content |
|-------|--------|---------------|
| **analyst** | Plain-language situation + confidence | Situation summary |
| **coordinator** | Full technical monitoring report | Compact coordinator update |

## Confidence scoring (reference)

```
overall = 0.35×source + 0.25×location + 0.20×time + 0.20×impact
```

The analyst report explains what each component means and cites per-claim scores from the coordinator cycle. Never invent scores not present in CLI output.

## Ontology integration

- **Rule layer:** Same `monitoring_summary` as coordinator (phase, heartbeat, claim thresholds). Reports include *Ontology monitoring context* (heartbeat interval, matched transition rules).
- **Belief decomposition:** Per-claim `confidence_breakdown` appears in reports as *Belief-state ontology* — `source_prior`, `method`, `method_confidence` for gauges (T0), NWS alerts (`claim_nws_*`), and news articles (`claim_art_*`, T2).
- **Belief-state ledger:** When `SENTINEL_ONTOLOGY_LEDGER=on`, ledger record counts and path appear in the report.
- **Structured analyst:** When `SENTINEL_STRUCTURED_ANALYST=on`, adds *Ontology intelligence* with evidence claim IDs (full report is preserved; no longer replaced by a one-liner).
- **JSON output:** `--format json` may include `ontology_ledger` and `intelligence` blocks when flags are on.

## Error handling

On CLI error, return error JSON/text only. **Do not post to Slack** on hard failure.

## Smoke test

```
/skill analyst Situation report for Harris County, Texas
```

Pass: report shows situation at a glance, hydrology lines, confidence assessment with formula + components, and `Slack: delivered to channel:<channel-id>`.
