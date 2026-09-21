"""Tests for coordinator report gauge-change noise floor."""

from __future__ import annotations

from watcher.report_delta import _compare_gauge_readings, compute_delta


def test_sub_threshold_stage_delta_is_ignored(monkeypatch):
    monkeypatch.delenv("REPORT_GAUGE_STAGE_THRESHOLD_FT", raising=False)
    prior = {
        "08068275": {"gage_height_ft": 139.09, "streamflow_cfs": 100.0},
        "08068720": {"gage_height_ft": 145.75, "streamflow_cfs": 200.0},
    }
    current = {
        "08068275": {"gage_height_ft": 139.08, "streamflow_cfs": 100.0},
        "08068720": {"gage_height_ft": 145.74, "streamflow_cfs": 200.0},
    }
    assert _compare_gauge_readings(prior, current) == []


def test_stage_delta_at_threshold_is_reported(monkeypatch):
    monkeypatch.delenv("REPORT_GAUGE_STAGE_THRESHOLD_FT", raising=False)
    prior = {"08068275": {"gage_height_ft": 139.00, "streamflow_cfs": 100.0}}
    current = {"08068275": {"gage_height_ft": 139.10, "streamflow_cfs": 100.0}}
    changes = _compare_gauge_readings(prior, current)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "value_changed"
    assert changes[0]["gage_height_from"] == 139.00
    assert changes[0]["gage_height_to"] == 139.10


def test_flow_only_change_still_reported(monkeypatch):
    monkeypatch.delenv("REPORT_GAUGE_STAGE_THRESHOLD_FT", raising=False)
    prior = {"08068275": {"gage_height_ft": 139.09, "streamflow_cfs": 100.0}}
    current = {"08068275": {"gage_height_ft": 139.08, "streamflow_cfs": 150.0}}
    changes = _compare_gauge_readings(prior, current)
    assert len(changes) == 1
    assert "gage_height_from" not in changes[0]
    assert changes[0]["streamflow_from"] == 100.0
    assert changes[0]["streamflow_to"] == 150.0


def test_compute_delta_omits_noise_stage_bullets(monkeypatch):
    monkeypatch.delenv("REPORT_GAUGE_STAGE_THRESHOLD_FT", raising=False)
    prior = {
        "phase": "early_watch",
        "alert_level": "none",
        "claim_ids": [],
        "claims": [],
        "source_change_status": {},
        "flood_signal_count": 0,
        "gauge_readings": {
            "08068275": {"gage_height_ft": 139.09, "streamflow_cfs": 10.0},
        },
        "api_resolution": {},
    }
    current = {
        "phase": "early_watch",
        "alert_level": "none",
        "claim_ids": [],
        "claims": [],
        "source_change_status": {},
        "flood_signal_count": 0,
        "gauge_readings": {
            "08068275": {"gage_height_ft": 139.08, "streamflow_cfs": 10.0},
        },
        "api_resolution": {},
    }
    delta = compute_delta(current, prior)
    assert delta["gauge_changes"] == []
    assert not any("USGS 08068275" in b for b in delta["summary_bullets"])


def test_sub_threshold_flow_delta_is_ignored(monkeypatch):
    monkeypatch.delenv("REPORT_GAUGE_FLOW_THRESHOLD_PCT", raising=False)
    prior = {"08068275": {"gage_height_ft": 5.0, "streamflow_cfs": 1000.0}}
    current = {"08068275": {"gage_height_ft": 5.0, "streamflow_cfs": 1050.0}}
    assert _compare_gauge_readings(prior, current) == []


def test_large_flow_delta_is_reported(monkeypatch):
    monkeypatch.delenv("REPORT_GAUGE_FLOW_THRESHOLD_PCT", raising=False)
    prior = {"08068275": {"gage_height_ft": 5.0, "streamflow_cfs": 1000.0}}
    current = {"08068275": {"gage_height_ft": 5.0, "streamflow_cfs": 1300.0}}
    assert len(_compare_gauge_readings(prior, current)) == 1


def test_flow_noise_no_longer_defeats_the_stage_floor(monkeypatch):
    # Both deltas sit under their floors, so nothing material happened. Before
    # streamflow had a floor, the flow move alone reported a change and the
    # stage floor had no practical effect.
    monkeypatch.delenv("REPORT_GAUGE_STAGE_THRESHOLD_FT", raising=False)
    monkeypatch.delenv("REPORT_GAUGE_FLOW_THRESHOLD_PCT", raising=False)
    prior = {"08068275": {"gage_height_ft": 5.00, "streamflow_cfs": 1000.0}}
    current = {"08068275": {"gage_height_ft": 5.02, "streamflow_cfs": 1010.0}}
    assert _compare_gauge_readings(prior, current) == []


def test_appearing_flow_reading_is_reported(monkeypatch):
    monkeypatch.delenv("REPORT_GAUGE_FLOW_THRESHOLD_PCT", raising=False)
    prior = {"08068275": {"gage_height_ft": 5.0, "streamflow_cfs": None}}
    current = {"08068275": {"gage_height_ft": 5.0, "streamflow_cfs": 900.0}}
    assert len(_compare_gauge_readings(prior, current)) == 1
