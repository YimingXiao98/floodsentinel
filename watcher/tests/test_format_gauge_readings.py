"""Tests for gauge reading display formatting."""

from __future__ import annotations

from watcher.format_report import (
    _format_gauge_reading_bullet,
    _is_new_gauge_reading,
    _iter_displayable_gauge_claims,
    _should_display_gauge_reading,
    _significant_display_delta,
    _threshold_status_for_display,
)


def test_no_na_flow_suffix():
    bullet = _format_gauge_reading_bullet(
        {
            "site_no": "08072500",
            "gage_height_ft": 71.53,
            "prior_observed_primary": 71.33,
            "delta_primary_ft": 0.20,
            "streamflow_cfs": None,
            "flood_category": "no_flooding",
            "action_stage": 90.2,
            "flood_stage": 93.2,
        }
    )
    assert "/ n/a" not in bullet
    assert "n/a" not in bullet
    assert "71.33→71.53ft" in bullet
    assert "status=below_action" in bullet


def test_flow_shown_when_present():
    bullet = _format_gauge_reading_bullet(
        {
            "site_no": "08072500",
            "gage_height_ft": 71.53,
            "prior_observed_primary": 71.33,
            "delta_primary_ft": 0.20,
            "streamflow_cfs": 1200.0,
            "flood_category": "no_flooding",
            "action_stage": 90.2,
        }
    )
    assert "1200cfs" in bullet
    assert "/ n/a" not in bullet


def test_small_delta_gauge_not_displayed():
    val = {
        "site_no": "08072500",
        "gage_height_ft": 72.03,
        "prior_observed_primary": 72.06,
        "delta_primary_ft": -0.03,
        "flood_category": "no_flooding",
        "action_stage": 90.2,
    }
    assert _should_display_gauge_reading(val) is False
    claims = [
        {"claim_type": "GaugeObservation", "value": val},
        {
            "claim_type": "GaugeObservation",
            "value": {
                "site_no": "08074000",
                "gage_height_ft": 5.50,
                "prior_observed_primary": 5.30,
                "delta_primary_ft": 0.20,
                "flood_category": "no_flooding",
                "action_stage": 17.0,
            },
        },
    ]
    shown = _iter_displayable_gauge_claims(claims)
    assert len(shown) == 1
    assert shown[0]["value"]["site_no"] == "08074000"


def test_cold_start_gauge_is_displayed():
    val = {
        "site_no": "09402000",
        "gage_height_ft": 4.25,
        "hydrologic_signal": "cold_start",
        "flood_category": "action",
        "flood_state_score": 1.0,
        "action_stage": 4.0,
        "flood_stage": 6.0,
    }
    assert _is_new_gauge_reading(val) is True
    assert _should_display_gauge_reading(val) is True
    bullet = _format_gauge_reading_bullet(val)
    assert "first reading" in bullet
    assert "4.25ft" in bullet


def test_new_tracking_shows_top_10_by_severity():
    """First monitoring cycle: no priors → show up to 10 gauges ranked by severity."""
    claims = []
    # 12 cold-start gauges with mixed severity
    for i, (score, cat) in enumerate(
        [
            (0.0, "no_flooding"),
            (2.0, "minor_flooding"),
            (0.0, "no_flooding"),
            (4.0, "major_flooding"),
            (1.0, "action"),
            (0.0, "no_flooding"),
            (3.0, "moderate_flooding"),
            (0.0, "no_flooding"),
            (0.0, "no_flooding"),
            (1.0, "action"),
            (0.0, "no_flooding"),
            (2.0, "minor_flooding"),
        ]
    ):
        claims.append(
            {
                "claim_type": "GaugeObservation",
                "value": {
                    "site_no": f"site_{i:02d}",
                    "gage_height_ft": 10.0 + i,
                    "hydrologic_signal": "cold_start",
                    "flood_category": cat,
                    "flood_state_score": score,
                },
            }
        )
    shown = _iter_displayable_gauge_claims(claims, limit=10)
    assert len(shown) == 10
    scores = [c["value"]["flood_state_score"] for c in shown]
    assert scores == sorted(scores, reverse=True)
    assert shown[0]["value"]["site_no"] == "site_03"  # major
    # The two lowest no_flooding (among 12) are dropped when capping at 10
    shown_sites = {c["value"]["site_no"] for c in shown}
    assert "site_03" in shown_sites
    assert len(shown_sites) == 10


def test_mixed_existing_and_new_gauges():
    """Existing small-delta hidden; new gauge and significant delta shown."""
    claims = [
        {
            "claim_type": "GaugeObservation",
            "value": {
                "site_no": "old_quiet",
                "gage_height_ft": 5.01,
                "prior_observed_primary": 5.00,
                "delta_primary_ft": 0.01,
                "flood_category": "no_flooding",
            },
        },
        {
            "claim_type": "GaugeObservation",
            "value": {
                "site_no": "old_moved",
                "gage_height_ft": 5.50,
                "prior_observed_primary": 5.20,
                "delta_primary_ft": 0.30,
                "flood_category": "no_flooding",
            },
        },
        {
            "claim_type": "GaugeObservation",
            "value": {
                "site_no": "brand_new",
                "gage_height_ft": 8.0,
                "hydrologic_signal": "cold_start",
                "flood_category": "minor_flooding",
                "flood_state_score": 2.0,
            },
        },
    ]
    shown = _iter_displayable_gauge_claims(claims)
    sites = [c["value"]["site_no"] for c in shown]
    assert sites == ["brand_new", "old_moved"]


def test_significant_display_delta_threshold():
    assert _significant_display_delta(0.09) is False
    assert _significant_display_delta(0.10) is True
    assert _significant_display_delta(-0.11) is True


def test_status_at_minor():
    status = _threshold_status_for_display(
        {
            "gage_height_ft": 94.0,
            "flood_category": "minor_flooding",
            "action_stage": 90.2,
            "flood_stage": 93.2,
            "moderate_stage": 100.0,
        }
    )
    assert status.startswith("minor")
    assert "below mod" in status
