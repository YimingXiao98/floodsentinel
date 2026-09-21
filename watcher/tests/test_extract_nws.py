"""Tests for NWS alert claim extraction."""

from __future__ import annotations

import json

from watcher.claims.extract_nws import extract_nws_claims_from_alert_dicts


def _snapshot(alerts: list) -> dict:
    payload = {
        "schema": "county-flood-nws-alerts/1",
        "alerts": alerts,
        "nws_zone": "TXZ213",
        "api_url": "https://api.weather.gov/alerts/active?zone=TXZ213",
    }
    return {
        "source_id": "src_county_flood_nws_alerts_48201",
        "snapshot_id": "snap_test",
        "snapshot_url": payload["api_url"],
        "fetched_at": "2026-07-07T12:00:00Z",
        "raw_text": json.dumps(payload),
    }


def test_nws_alert_dicts_produce_confidence_breakdown():
    alerts = [
        {
            "event": "Flood Warning",
            "headline": "Flood Warning for Harris County",
            "severity": "Severe",
            "urgency": "Immediate",
            "area_desc": "Harris, TX",
            "effective": "2026-07-07T10:00:00Z",
            "nws_zone": "TXZ213",
        }
    ]
    source_record = {
        "trust_tier": {"level": 0, "name": "T0"},
        "api_resolution": {"county_match": True},
    }
    claims = extract_nws_claims_from_alert_dicts(
        _snapshot(alerts),
        county="Harris",
        state="TX",
        location="Harris County, TX",
        source_record=source_record,
    )
    assert len(claims) == 1
    claim = claims[0]
    assert claim["claim_id"].startswith("claim_nws_")
    assert claim["claim_type"] == "warning"
    breakdown = claim["confidence_breakdown"]
    assert breakdown["method"] == "corroborated"
    assert breakdown["source_reliability_prior"] == 0.95
    assert claim["value"]["evidence_tier"] == "T0"


def test_nws_skips_non_flood_events():
    alerts = [{"event": "Heat Advisory", "area_desc": "Harris, TX"}]
    claims = extract_nws_claims_from_alert_dicts(
        _snapshot(alerts),
        county="Harris",
        state="TX",
        location="Harris County, TX",
    )
    assert claims == []
