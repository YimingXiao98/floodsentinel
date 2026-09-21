"""Tests for Neo4j stage-delta persist gating."""

from __future__ import annotations

import json

from watcher.county_flood_bridge import _apply_gauge_stage_persist_gate


def _gauge_snap(*, stage: float, source: str = "usgs", fips: str = "48201") -> dict:
    payload = {
        "schema": "county-flood-data/1",
        "source": source,
        "gauges": [
            {
                "site_no": "08075000",
                "source": source,
                "observed_primary": stage,
                "flood_state_score": 0.0,
            }
        ],
    }
    raw = json.dumps(payload, sort_keys=True)
    return {
        "snapshot_id": f"snap_{source}_{stage}",
        "source_id": f"src_county_flood_{source}_{fips}",
        "raw_text": raw,
        "change_status": "changed",
        "content_fingerprint": f"fp-{stage}",
    }


def test_cold_start_keeps_persistable_when_no_prior():
    snaps = [_gauge_snap(stage=10.0)]
    out = _apply_gauge_stage_persist_gate(snaps, [])
    assert out[0]["change_status"] == "changed"


def test_sub_threshold_stage_marked_unchanged():
    prior = [
        {
            "site_no": "08075000",
            "source": "usgs",
            "observed_primary": 10.00,
            "flood_state_score": 0.0,
        }
    ]
    snaps = [_gauge_snap(stage=10.05)]
    out = _apply_gauge_stage_persist_gate(snaps, prior)
    assert out[0]["change_status"] == "unchanged"
    assert out[0]["skip_reason"] == "below_stage_delta"


def test_threshold_stage_stays_changed():
    prior = [
        {
            "site_no": "08075000",
            "source": "usgs",
            "observed_primary": 10.00,
            "flood_state_score": 0.0,
        }
    ]
    snaps = [_gauge_snap(stage=10.10)]
    out = _apply_gauge_stage_persist_gate(snaps, prior)
    assert out[0]["change_status"] == "changed"
    assert "skip_reason" not in out[0]


def test_nws_alerts_not_gated_by_stage_delta():
    snap = {
        "snapshot_id": "snap_alerts",
        "source_id": "src_county_flood_nws_alerts_48201",
        "raw_text": "{}",
        "change_status": "changed",
    }
    out = _apply_gauge_stage_persist_gate([snap], [{"site_no": "1", "observed_primary": 1.0}])
    assert out[0]["change_status"] == "changed"
