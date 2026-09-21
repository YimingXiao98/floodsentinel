---
name: county-flood-data
description: Fetch county-scoped NOAA NWPS, USGS NWIS, NWS Flood Warnings, and FEMA flood disaster declarations
user-invocable: true
command-dispatch: tool
command-tool: exec
allowed-tools:
  - Bash(*sentinel-cli*)
  - Bash(<workspace>/bin/sentinel-cli *)
  - Bash(<workspace>/bin/run-county-flood *)
---
version: 2.5.0
main: main.py
inputs:
  - county
  - state
  - fips
  - location
outputs:
  - average_flood_state
  - average_primary_reading
  - gauges
  - alerts
  - disasters
  - discovered_flood_categories
---

# county-flood-data

Fetch live flood status for a U.S. county from **NOAA NWPS** river gauges, **USGS NWIS IV** stream gauges, **NWS active Flood Warnings** (`api.weather.gov`), and **FEMA flood disaster declarations** (OpenFEMA v2). Resolves gauge IDs from the local NWPS catalog CSV and county FIPS (for USGS site discovery, NWS zone, and FEMA county filter), queries live APIs, and returns county-level **average flood state** and **average primary reading**.

**Deterministic execution:** This skill uses `command-dispatch: tool` → `exec`. OpenClaw runs the wrapper script directly — **no LLM step required**.

| Symbol | Path |
|--------|------|
| `SENTINEL_ROOT` | `<workspace>` |
| `RUN_COUNTY_FLOOD` | `<workspace>/bin/run-county-flood` |
| `SENTINEL_CLI` | `<workspace>/bin/sentinel-cli` |

## Invoke (OpenClaw TUI / chat)

```
/skill county-flood-data Harris County, Texas
/county_flood_data Travis County, Texas
```

Both forms return **all four default sources** (NWPS + USGS + NWS alerts + FEMA disasters). Optional: `--source both` for gauges only, or `--source nws` / `--source usgs` for a single gauge API.

**Note:** `--source nws` means **NOAA NWPS hydrology** (`api.water.noaa.gov`), not NWS public alerts. Use source id `nws_alerts` (included in default `all`) for Flood Warnings from `api.weather.gov`.

Both forms bypass the model when `command-dispatch` is active and return CLI stdout verbatim.

**First-time setup:** OpenClaw needs a one-time patch for skill dispatch on this host:

```bash
sudo bash <workspace>/scripts/patch-openclaw-county-flood-dispatch.sh
# then restart gateway: pkill -f openclaw-gateway; openclaw gateway &
```

In the TUI, start a fresh session (`/new`) after restarting the gateway.

## CLI (direct / debugging)

```bash
<workspace>/bin/run-county-flood "Harris County, Texas"
```

Or via sentinel-cli:

```bash
<workspace>/bin/sentinel-cli county-flood-data --county "Harris" --state "TX"
```

JSON:

```bash
<workspace>/bin/run-county-flood "Harris County, Texas"  # human default
python3 <workspace>/skills/county-flood-data/main.py --location "Harris County, Texas" --format json
python3 <workspace>/skills/county-flood-data/main.py --location "Harris County, Texas" --source nws
python3 <workspace>/skills/county-flood-data/main.py --location "Harris County, Texas" --source usgs
python3 <workspace>/skills/county-flood-data/main.py --location "Harris County, Texas" --source both
python3 <workspace>/skills/county-flood-data/main.py --location "Harris County, Texas" --source all
python3 <workspace>/skills/county-flood-data/main.py --location "Harris County, Texas" --sources nws_alerts,fema_disasters
```

## Exec rules (when agent path is used)

| Do | Don't |
|----|-------|
| `exec` with `host=gateway`, `ask=off`, `security=full` | Fabricate gauge readings |
| Run `RUN_COUNTY_FLOOD` or `SENTINEL_CLI county-flood-data` | Skip CLI and summarize from memory |
| Return CLI stdout verbatim | Return empty response |

## What the output includes

1. **`regional_verdict`** / **`is_flooding`** — worst gauge wins; any score ≥ 1.0 (action+) → flooding (gauge-only; alerts/disasters are supplementary)
2. **`average_flood_state`** — mean numeric flood severity across scored gauges
3. **`average_primary_reading`** — mean observed stage (`status.observed.primary` for NWPS, gage height for USGS)
4. **`alerts`** — active NWS Flood Warnings for the county (zone-scoped via FIPS)
5. **`disasters`** — recent FEMA flood disaster declarations for the county
6. **Per-gauge detail** — all in-service county gauges (scorable-first priority; optional `COUNTY_FLOOD_MAX_GAUGES` cap)

## JSON shape (summary)

```json
{
  "county": "Harris",
  "state": "TX",
  "fips": "48201",
  "alerts": [],
  "disasters": [
    {
      "disaster_number": 4781,
      "fema_declaration_string": "DR-4781-TX",
      "declaration_title": "SEVERE STORMS...",
      "declaration_date": "2024-05-17T00:00:00.000Z",
      "designated_area": "Harris (County)"
    }
  ],
  "summary": {
    "average_flood_state": 0.0,
    "average_primary_reading": 49.481,
    "average_primary_unit": "ft",
    "gauges_queried": 25,
    "gauges_with_data": 25
  }
}
```

## Extensibility

Additional flood APIs can be registered in `county_flood/sources/__init__.py` (`SOURCE_REGISTRY`). Active sources today:

| Source | API | County resolution |
|--------|-----|-------------------|
| `nwps` | `https://api.water.noaa.gov/nwps/v1/gauges/{shef_id}` | CSV filter by county + state |
| `usgs` | `https://waterservices.usgs.gov/nwis/iv/` | CSV `usgs id` + optional FIPS site discovery + registry overrides |
| `usgs_ogc` | `https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous/items` | Same site list as `usgs`; modernized replacement for NWIS IV |
| `nws_alerts` | `https://api.weather.gov/alerts/active?zone={zone}&event=Flood%20Warning` | FIPS → NWS UGC zone (`TXC201`) or registry override |
| `fema_disasters` | `https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries` | FIPS → `fipsCountyCode` OData filter |

### Source selection (`--source`)

| Value | APIs | Notes |
|-------|------|-------|
| `all` | NWPS + USGS + NWS alerts + FEMA disasters (default) | Full county flood picture |
| `both` | NOAA NWPS + USGS NWIS IV | Gauges only (backward compatible) |
| `nws` | NOAA NWPS hydrology | **Not** NWS public alerts |
| `usgs` | USGS NWIS IV | Stream gauges only |

Examples:

| Goal | Command |
|------|---------|
| All sources (default) | `--source all` |
| Gauges only | `--source both` |
| NOAA NWPS only | `--source nws` |
| USGS NWIS IV only | `--source usgs` |
| Alerts + disasters only | `--sources nws_alerts,fema_disasters` |

Advanced: `--sources nwps,usgs,usgs_ogc,nws_alerts,fema_disasters` overrides `--source`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Empty reply or model tries to `read` SKILL.md | Run `scripts/patch-openclaw-county-flood-dispatch.sh`, restart gateway, `/new` in TUI |
| `❌ Tool not available: exec` | Same patch (adds direct subprocess for this skill) |
| `command not found: Harris` | Patch missing — location was passed as shell command instead of `run-county-flood` |
| No gauges for county | Verify county name matches CSV (`Harris` + `TX`); try `--gauge-ids` |
