# US County Flood API Registry

## Do you need a full nationwide mapping?

**Usually no.** For most US counties the system auto-resolves:

| Field | Auto-resolution | When to add to registry |
|-------|-----------------|-------------------------|
| **NWS zone** | `{ST}C{last 3 FIPS}` e.g. `48201` → `TXC201` | Rare edge cases only |
| **USGS sites** | `nwis/site/?countyCd={fips}` → up to 10 stream gauges | Curate priority gauges, or county has 0 auto-discovered sites |

Add a county to the registry when you want **specific gauges** (not just the first 10 USGS returns) or when auto-discovery fails.

## File

[`county_flood_registry.json`](county_flood_registry.json) — FIPS-keyed JSON loaded at runtime.

Priority order:
1. **Registry** (this file)
2. Per-county YAML (`{fips}.yaml`)
3. **Live API discovery** (USGS + NWS)

## Schema (per county)

```json
{
  "48201": {
    "county_name": "Harris",
    "state": "TX",
    "nws_zone": "TXC201",
    "usgs_sites": ["08075000", "08067505"],
    "notes": "optional"
  }
}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `county_name` | recommended | Display / validation |
| `state` | recommended | 2-letter or full state name |
| `nws_zone` | optional* | NWS UGC county zone for alerts API |
| `nws_zones` | optional* | Alias; first zone used if `nws_zone` omitted |
| `usgs_sites` | optional* | List of USGS `site_no` strings (max 10 used) |
| `notes` | optional | Human comment |

\*At least one of `usgs_sites` or `nws_zone` should be set for a registry entry to be useful; omitted fields fall back to API discovery.

### Rich USGS site objects (optional)

```json
"usgs_sites": [
  {"site_no": "08075000", "name": "Buffalo Bayou at Houston", "lat": 29.76, "lon": -95.38}
]
```

## Bulk import format

If you prepare a spreadsheet or CSV, columns:

```
fips,county_name,state,nws_zone,usgs_sites
48201,Harris,TX,TXC201,"08075000;08067505"
53033,King,WA,WAC033,"12122000;12115500"
```

`usgs_sites`: semicolon-separated USGS site numbers.

We can convert CSV → `county_flood_registry.json` when you provide the file.

## What to pick per county

### USGS (`usgs_sites`)

Prefer **stream gauges (ST)** on rivers/creeks with:
- Active instantaneous values (IV) — gage height + streamflow
- Flood-relevant locations (main stem, not tiny tributaries unless only option)

Find candidates: https://waterdata.usgs.gov/ or API:
`https://waterservices.usgs.gov/nwis/site/?format=rdb&countyCd={FIPS}&siteType=ST&siteStatus=active`

### NWS (`nws_zone`)

Almost always `{STATE_ABBR}C{COUNTY_FIPS_LAST_3}`:
- Harris TX `48201` → `TXC201`
- King WA `53033` → `WAC033`

Verify: `https://api.weather.gov/zones/county/{ZONE}`

## After you provide the mapping

1. Place or merge into `county_flood_registry.json`
2. Run: `sentinel-cli county-flood-data --fips {FIPS}`
3. Report shows `resolution_source: registry` when curated data is used
