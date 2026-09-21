---
name: coordinator
description: On-demand county flood monitoring — resolve APIs, fetch readings, score confidence, check phase, compile report
user-invocable: true
command-dispatch: tool
command-tool: exec
allowed-tools:
  - Bash(*sentinel-cli*)
  - Bash(<workspace>/bin/sentinel-cli *)
  - Bash(<workspace>/bin/run-coordinator *)
  - slack
---
version: 3.1.0
inputs:
  - task_type
  - mission_id
  - disaster_type
  - location
  - county
  - state
  - fips
outputs:
  - mission_spec
  - monitoring_summary
  - belief_state
  - evidence_claims
  - article_evidence
  - phase_decision
  - report_delta
  - source_snapshots
  - county_api_mapping
---

# coordinator

When invoked, immediately runs the full flood monitoring cycle for the requested county: resolve APIs → fetch → score confidence → check phase → compile a report with endpoints, readings, and deltas.

**Lifecycle:** Slack / first invoke bootstraps (or reuses) a MissionSpec, then enables the global heartbeat cron. Cron polls every 15 minutes and re-runs only missions whose phase `heartbeat_interval_seconds` is due. After `recovery` stays all-clear for **2** due cycles, phase becomes **`folded`**, the mission is deactivated, and cron disables itself when no active missions remain.

## Quick summary

| | |
|---|---|
| **Purpose** | One CLI call: bootstrap/reuse mission → county-flood-data fetch → **flood-article-detector** → Neo4j persist → phase + delta report → enable heartbeat cron |
| **Sources** | NOAA NWPS · USGS NWIS IV · **NWS active alerts** (`nws_alerts`, default `both`) via county-flood-data · **Google News / web** via flood-article-detector (T2 ontology claims when above threshold) |
| **Bootstrap** | Auto-creates Neo4j mission if county has no active `MissionSpec`; duplicates **reuse** and **re-run** (never a second MissionSpec) |
| **End** | Auto-fold after recovery all-clear streak of 2 → `active=false` |
| **Output** | Human report with phase, API endpoints, flood readings, **belief-state ontology** sections, confidence formula, deltas |
| **Slack** | **Automatic** post to `channel:<channel-id>` on every successful run |
| **Never** | Fabricate readings, skip CLI exec, create a second active MissionSpec for the same FIPS |

**Deterministic execution:** This skill uses `command-dispatch: tool` → `exec`. OpenClaw runs `run-coordinator` directly — **no LLM step required** for the monitoring cycle or Slack delivery.

| Symbol | Path |
|--------|------|
| `SENTINEL_ROOT` | `<workspace>` |
| `RUN_COORDINATOR` | `<workspace>/bin/run-coordinator` |
| `SENTINEL_CLI` | `<workspace>/bin/sentinel-cli` |
| `SLACK_CHANNEL` | `channel:<channel-id>` (override: `SENTINEL_SLACK_CHANNEL`) |

## Invoke (OpenClaw TUI / chat)

```
/skill coordinator Monitor Harris County, Texas
/skill coordinator Check flood status for Miami-Dade County, Florida
/skill coordinator --fips 48201
```

Both forms bypass the model when `command-dispatch` is active. CLI stdout includes the human report **and** a `Slack: delivered to channel:…` confirmation line.

## Invoke (Slack `/openclaw`)

In Slack, send natural-language flood monitoring requests via the `/openclaw` slash command. Bare text (no `/skill` prefix) routes deterministically to `run-coordinator`:

```
/openclaw there is a flood approaching harris county in texas
/openclaw Monitor Miami-Dade County, Florida
/openclaw Check flood status for King County, Washington
```

Explicit skill commands still work (prefix with `/`):

```
/openclaw /skill coordinator Harris County, Texas
/openclaw /skill analyst Situation report for Miami-Dade County, Florida
/openclaw /help
```

Replies are posted to the channel (not ephemeral). The coordinator also auto-posts a summary to `channel:<channel-id>` on every successful run.

**First-time setup:** Run the dispatch patch once on this host:

```bash
sudo bash <workspace>/scripts/patch-openclaw-county-flood-dispatch.sh
# then restart gateway: pkill -f openclaw-gateway; openclaw gateway &
```

In the TUI, start a fresh session (`/new`) after restarting the gateway.

## Exec rules (mandatory)

| Do | Don't |
|----|-------|
| `exec` with `host=gateway`, `ask=off`, `security=full` | Use sandbox or skip exec |
| Run `RUN_COORDINATOR` or `SENTINEL_CLI run-coordinator` | Fabricate API results or confidence |
| Return CLI stdout verbatim (human section + Slack confirmation) | Register cron jobs |
| Let the CLI post to Slack automatically | Manually reformat or omit the Slack block |

## Workflow

Parse user input into `COUNTY` and `STATE` (or `--fips`).

### Step 0 — Neo4j health (debug only)

```bash
<workspace>/bin/sentinel-cli neo4j-health
```

Stop on failure when running steps manually.

### Step 1 — Run full coordinator cycle (primary path)

```bash
<workspace>/bin/run-coordinator "COUNTY County, STATE"
```

Or directly:

```bash
<workspace>/bin/sentinel-cli run-coordinator --county "COUNTY" --state "STATE"
```

Also: `--mission-id ID`, `--fips FIPS`. Skip Slack with `--no-slack`.

This single command:
1. Bootstraps mission in Neo4j if missing (mission-bootstrap geography + `bootstrap_mission`)
2. Loads **current phase** from `BeliefState` / `MissionSpec`
3. Runs **county-flood-data skill** for **both** NWPS and USGS readings (always; no flags)
4. Runs **flood-article-detector** and merges `article_evidence` (articles with `page_score` > **0.75**)
5. Persists timestamped snapshots to Neo4j (`SourceRecord` + `SourceSnapshot`)
6. Extracts gauge claims with **confidence decomposition**
7. Evaluates **phase transition rules** for the current phase
8. Computes **delta vs last run** (gauge changes, fingerprint changes)
9. Prints compiled report (includes high-confidence news articles)
10. **Posts the `---SLACK---` summary to `channel:<channel-id>` via `openclaw message send`**

Debug JSON: `--format json`. Slack-only text: `--format slack`.

### Step 2 — Return report to user

Return the **human report** (everything before `---SLACK---`) plus the trailing `Slack: delivered to channel:…` line verbatim.

| Section | Content |
|---------|---------|
| **Current phase** | Phase name, alert level, phase interval |
| **API endpoints called** | Full URLs for USGS NWIS IV and NWS zone alerts |
| **Flood readings** | Per-gauge gage height + streamflow; NWS alerts if any |
| **Confidence scoring** | Formula + per-claim src/loc/time/impact breakdown |
| **Changes since last run** | Phase, gauge deltas, source fingerprint changes |
| **News article evidence** | Articles with confidence > 0.75: link + page_score in human report and Slack |
| **Phase evaluation** | Stay or transition; matched rule if changed |
| **Slack confirmation** | `Slack: delivered to channel:<channel-id>` on success |

Do not summarize away endpoint URLs, confidence math, or the Slack confirmation.

### Step 3 — Slack (automatic)

Slack delivery is **built into** `run-coordinator` / `sentinel-cli run-coordinator`. The CLI sends the compact report (same content as the `---SLACK---` block) to the configured channel using OpenClaw's Slack integration.

| Config | Value |
|--------|-------|
| Default channel | `<channel-id>` |
| Override | `export SENTINEL_SLACK_CHANNEL=<channel-id>` |
| Skip posting | `--no-slack` |

Do **not** call the `slack` tool separately when the CLI already confirms delivery.

### Step 4 — Do NOT register cron

Cron heartbeat is **disabled**. Skip `openclaw cron add` entirely.

## Confidence scoring (reference)

```
overall = 0.35×source + 0.25×location + 0.20×time + 0.20×impact
```

- **USGS (T0):** county-resolved site → location 0.90; gage height present → impact 0.85
- **NWS (T0):** validated zone → location 0.90; severity drives impact 0.80–0.90

Full breakdown appears in the CLI report under "Confidence scoring".

## Two flood sources

| Source | Endpoint | County resolution |
|--------|----------|-------------------|
| NOAA NWPS | `api.water.noaa.gov/nwps/v1/gauges/{shef_id}` | county-flood-data CSV + `--source nws` |
| USGS NWIS IV | `waterservices.usgs.gov/nwis/iv/` | county-flood-data FIPS/CSV + `--source usgs` |

Standalone diagnostic (no Neo4j): `/skill county-flood-data`.

## Ontology integration

Two ontology layers (do not merge):

| Layer | Source | Role in this skill |
|-------|--------|-------------------|
| **Rule layer** | `monitoring_summary` from `stub_monitoring.json` via bridge | Phase rules, heartbeat, required source/claim types |
| **Belief-state layer** | JSONL ledger (`state/ledger/{mission_id}.jsonl`) | Typed `Claim`, `Belief`, `Phase` audit trail |

- **Rule layer:** Loaded automatically during bootstrap; `run-coordinator` uses `phase_transition_rules` and `heartbeat_interval_seconds`. Human and Slack reports include **Ontology monitoring context** (phase, heartbeat, matched rules).
- **Belief-state layer:** NWS alerts (`claim_nws_*`, T0) and high-confidence news (`claim_art_*`, T2) are extracted as `evidence_claims` with `confidence_breakdown`. Reports include **Belief-state ontology** (source_prior, method, method_confidence per claim).
- **Ledger:** Gated by `SENTINEL_ONTOLOGY_LEDGER=on` (default `off`). When on, `--format json` includes `ontology_ledger` with record counts.

## If county resolution fails

Ask the user for:
- Explicit **FIPS** (`--fips 48201`), or
- Manual gauge IDs / NWS zone in `skills/disaster-scout/counties/{fips}.yaml`

## Error handling

On CLI error, return error JSON/text only. **Do not post to Slack** on hard failure.

## Smoke test

```
/skill coordinator Monitor Harris County, Texas
```

Pass: report shows current phase, two API endpoint URLs, USGS gauge readings with ft/cfs, confidence formula + per-claim scores, and `Slack: delivered to channel:<channel-id>`.
