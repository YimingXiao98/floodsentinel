"""Tests for NWS snapshot emission in county_flood_bridge."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from watcher.county_flood_bridge import fetch_county_flood_sources

_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "county-flood-data"
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))


def test_both_mode_includes_nws_alerts_source_record():
    payload = {
        "generated_at": "2026-07-07T12:00:00Z",
        "county": "Harris",
        "state": "TX",
        "fips": "48201",
        "gauges": [
            {"source": "nwps", "site_no": "08073700", "shef_id": "HFDC1"},
            {"source": "usgs", "site_no": "08073700"},
        ],
        "alerts": [{"event": "Flood Warning", "area_desc": "Harris, TX"}],
        "sources": {
            "nwps": {"api_base": "https://api.water.noaa.gov/nwps/v1/"},
            "usgs": {"api_base": "https://waterservices.usgs.gov/nwis/iv/"},
            "nws_alerts": {
                "api_url": "https://api.weather.gov/alerts/active?zone=TXZ213",
                "nws_zone": "TXZ213",
            },
        },
        "summary": {},
    }
    mapping = MagicMock()
    mapping.nws_zone = {"zone": "TXZ213", "validated": True}
    mapping.to_dict.return_value = {"nws_zone": "TXZ213"}

    with patch("county_flood.runner.run_county_flood", return_value=payload), patch(
        "watcher.county_flood_bridge.resolve_county_flood_sources",
        return_value=mapping,
    ), patch("watcher.county_flood_bridge.get_prior_county_flood_gauges", return_value=[]):
        result = fetch_county_flood_sources(
            county="Harris",
            state="TX",
            fips="48201",
            location="Harris County, TX",
            mission_id="mission_test",
            source_mode="both",
        )

    records = result["source_records"]
    ont_types = {r["ontology_source_type"] for r in records}
    assert "NWSSource" in ont_types
    assert "NWPSSource" in ont_types
    assert "USGSGaugeSource" in ont_types
    assert len(records) == 3

    nws_snap = next(
        s for s in result["source_snapshots"] if "nws_alerts" in s.get("source_id", "")
    )
    body = json.loads(nws_snap["raw_text"])
    assert body["schema"] == "county-flood-nws-alerts/1"
    assert len(body["alerts"]) == 1
