---
name: watcher
description: Monitor county-scoped NWS/USGS flood sources — resolve API IDs, fetch, persist to Neo4j, detect changes since last run
user-invocable: true
allowed-tools:
  - Bash(*sentinel-cli*)
  - Bash(<workspace>/bin/sentinel-cli *)
---
version: 2.0.0
inputs:
  - mission_id
  - mission_spec
  - monitoring_summary
  - belief_state
  - source_records
  - county
  - state
  - fips
  - prior_cycle_state
outputs:
  - source_records
  - source_snapshots
  - failed_fetches
  - county_api_mapping
  - neo4j_persistence
---

# watcher

You are the watcher agent.

HTTP-fetch **county-scoped flood readings** via the county-flood-data skill for a bootstrapped flood mission. The coordinator invokes you (via `fetch-sources` or the full `run-coordinator` cycle).

## Quick summary

| | |
|---|---|
| **Purpose** | county-flood-data fetch → Neo4j persist → change detection vs prior snapshot |
| **Sources** | NOAA NWPS (`--source nws`) · USGS NWIS IV (`--source usgs`) · both (default) |
| **Neo4j** | `SourceRecord` + append-only `SourceSnapshot` with `change_status` + timestamps |
| **Delta** | `content_fingerprint` on county-flood JSON; coordinator tracks gauge value deltas |
| **Upstream** | Coordinator auto-bootstraps mission; watcher runs fetch-only via `fetch-sources` |
| **Never** | Raw stub HTTP fetch (replaced by county-flood-data skill) |

## OpenClaw execution

| Symbol | Absolute path |
|--------|----------------|
| `SENTINEL_ROOT` | `<workspace>` |
| `SENTINEL_CLI` | `<workspace>/bin/sentinel-cli` |

Use `host=gateway`, `workdir=SENTINEL_ROOT`, run steps in one pass.

## How to invoke

```
/skill watcher monitor mission evt_king_flood_20260607T154155Z
/skill watcher Monitor King County, Washington
/skill watcher --fips 53033
```

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `mission_id` | one of* | From Neo4j / coordinator |
| `county` + `state` | one of* | Neo4j lookup if no mission_id |
| `fips` | one of* | Neo4j lookup if no mission_id |
| `mission_spec` | no | Skip get-mission if coordinator passed full envelope |
| `monitoring_summary` | no | From coordinator or get-mission |
| `belief_state` | no | From coordinator or get-mission |
| `source_records` | no | From coordinator discover-sources (optional) |
| `prior_cycle_state` | no | Prior fingerprints for change detection |

\*At least one lookup key unless coordinator passed complete `mission_spec.mission_id`.

## Workflow

### Step 0 — Resolve lookup key

Accept **one** of: `mission_id`, `fips`, or `county`+`state`.

If coordinator passed `mission_spec.mission_id`, use that — skip redundant parsing.

### Step 1 — Load mission context (skip if coordinator provided)

If `monitoring_summary` or `mission_spec.fips` missing, load from Neo4j:

```bash
<workspace>/bin/sentinel-cli get-mission --mission-id MISSION_ID
```

Or by FIPS / county+state (same args as coordinator).

### Step 2 — Fetch county-scoped sources

Primary action:

```bash
<workspace>/bin/sentinel-cli fetch-sources --county "King" --state WA
```

`fetch-sources` automatically:

1. **Resolves county API IDs** — USGS site numbers via `countyCd`, NWS zone via FIPS derivation + validation
2. **Builds county-specific URLs** — NWIS IV and NWS zone-filtered alerts
3. Loads **prior readings** from Neo4j (`prior_readings`)
4. Compares `content_fingerprint` → `change_status`: `new` | `changed` | `unchanged`
5. **Persists** to Neo4j: updates `SourceRecord` (including `api_resolution_json`), appends `SourceSnapshot`
6. Returns `county_api_mapping`, `api_resolution` per source, `neo4j_persistence`

### Step 3 — Query stored readings (optional)

```bash
<workspace>/bin/sentinel-cli list-source-readings --county "King" --state WA
```

### Step 4 — Return result

Return `fetch-sources` JSON only. Do not extract claims (coordinator owns that).

## Coordinator + cron integration

The **coordinator** runs the full cycle in one CLI call:

```bash
sentinel-cli run-coordinator --county "Harris" --state TX
```

Pipeline: bootstrap (if needed) → **watcher fetch** (county resolve + Neo4j persist) → extract claims → phase rules → **delta report** → Slack.

After first coordinator run, register cron heartbeat (`*/15 * * * *`):

```bash
sentinel-cli run-heartbeat
```

Each cron tick loads every active `MissionSpec`, runs the full coordinator cycle per county, and posts Slack reports with:
- County API sources (USGS sites, NWS zone)
- Hydrology changes since last run (gage height deltas)
- Source fingerprint change status
- Per-source and per-claim confidence scores

## County API resolution

| Source | Strategy | Resolved ID | Fetch URL |
|--------|----------|-------------|-----------|
| `USGSGaugeSource` | `usgs_nwis_sites_by_county_fips` | comma-separated `site_no` | `waterservices.usgs.gov/nwis/iv/...` |
| `NWSSource` | `nws_alerts_by_county_zone` | validated UGC zone | `api.weather.gov/alerts/active?zone={zone}` |

Cache: `watcher/state/gauge_cache_{fips}.json` (24h TTL).

Optional overrides: `skills/disaster-scout/counties/{fips}.yaml` with `usgs_sites` / `nws_zones`.

## Rules

- Resolve county/FIPS/phase from Neo4j when not provided by coordinator.
- Do not invoke disaster_coordinator, scout, or verify-sources.
- Do not extract claims or update belief state (coordinator only).
- Return valid JSON only.

## Success output shape

```json
{
  "mission_id": "evt_harris_flood_20260609T160000Z",
  "mission_spec": { "county": "Harris", "state": "TX", "fips": "48201" },
  "discovery_mode": "ontology_stub_with_county_resolution",
  "county_api_mapping": {
    "county_fips": "48201",
    "usgs_sites": [{"site_no": "08067505", "name": "Cedar Bayou..."}],
    "nws_zone": {"zone": "TXC201", "validated": true}
  },
  "source_records": [
    {
      "source_id": "src_usgsgaugesource_48201",
      "source_url": "https://waterservices.usgs.gov/nwis/iv/?format=json&sites=08067505,...",
      "api_resolution": {
        "strategy": "usgs_nwis_sites_by_county_fips",
        "county_match": true,
        "resolved_ids": {"sites": ["08067505"]}
      }
    }
  ],
  "source_snapshots": [
    {"source_id": "src_usgsgaugesource_48201", "change_status": "changed"}
  ],
  "neo4j_persistence": {
    "ok": true,
    "snapshots_created": 2
  }
}
```
