"""Tests for analyst situation summary formatting."""

from watcher.format_analyst import format_analyst_slack, format_situation_summary


def _sample_coordinator_result() -> dict:
    return {
        "mission_id": "evt_harris_flood_test",
        "run_at": "2026-06-26T12:00:00Z",
        "mission_spec": {
            "county": "Harris",
            "state": "TX",
            "fips": "48201",
            "location": "Harris County, TX",
            "current_event_phase": "early_watch",
        },
        "belief_state": {"alert_level": "none", "event_phase": "early_watch"},
        "county_flood_evidence": {
            "overall_confidence": 0.82,
            "weight_hint": "high",
            "regional_verdict": "watch",
            "is_flooding": False,
            "verdict_gauge_count": 3,
            "confidence_components": {
                "source": 0.9,
                "location": 0.9,
                "time": 0.85,
                "impact": 0.65,
            },
        },
        "evidence_claims": [
            {
                "claim_type": "GaugeObservation",
                "severity": "normal",
                "overall_confidence": 0.81,
                "confidence_components": {
                    "source": 0.9,
                    "location": 0.9,
                    "time": 0.85,
                    "impact": 0.65,
                },
                "value": {
                    "site_no": "08073700",
                    "gage_height_ft": 12.5,
                    "prior_observed_primary": 12.3,
                    "delta_primary_ft": 0.2,
                    "streamflow_cfs": 1500,
                    "flood_category": "no_flooding",
                    "action_stage": 20.0,
                    "flood_stage": 25.0,
                },
            }
        ],
        "report_delta": {"first_run": True},
        "phase_decision": {"phase_changed": False, "alert_level": "none"},
        "sources_succeeded": 2,
        "article_evidence": {
            "threshold": 0.75,
            "top_sources": [
                {
                    "title": "Flooding closes roads",
                    "url": "https://example.com/flood-news",
                    "page_score": 0.86,
                    "indication": "flooding",
                }
            ],
            "high_confidence_articles": [
                {
                    "title": "Flooding closes roads",
                    "url": "https://example.com/flood-news",
                    "page_score": 0.86,
                    "indication": "flooding",
                }
            ],
        },
    }


def test_situation_summary_includes_bottom_line_and_confidence():
    text = format_situation_summary(_sample_coordinator_result())
    assert "Situation Analysis" in text
    assert "Harris County, TX" in text
    assert "0.35×source" in text
    assert "0.82" in text
    assert "08073700" in text
    assert "https://example.com/flood-news" in text
    assert "First analyst report" in text
    assert "display-only" in text
    assert "page-selection threshold" in text


def test_analyst_slack_compact_sections():
    text = format_analyst_slack(_sample_coordinator_result())
    assert "*Disaster Sentinel — Situation Analysis*" in text
    assert "*Confidence assessment*" in text
    assert "08073700" in text
    assert "flood-news" in text
    assert "_Analyst · Harris County" in text


def test_analyst_shows_belief_section_without_precomputed_breakdown():
    """Live runs may lack confidence_breakdown until format time synthesis."""
    payload = _sample_coordinator_result()
    payload["evidence_claims"][0]["claim_id"] = "claim_cf_live01"
    payload["evidence_claims"][0]["value"]["flood_stage"] = 12.0
    assert "confidence_breakdown" not in payload["evidence_claims"][0]
    text = format_situation_summary(payload)
    assert "Belief-state ontology" in text
    assert "rated threshold" in text
    assert "Ontology monitoring context" in text


def test_analyst_shows_ontology_belief_decomposition():
    payload = _sample_coordinator_result()
    payload["evidence_claims"][0]["claim_id"] = "claim_cf_abc123"
    payload["evidence_claims"][0]["confidence_breakdown"] = {
        "overall_score": 0.81,
        "source_reliability_prior": 0.95,
        "method": "rated_threshold",
        "method_confidence": 0.88,
        "uncertainty_notes": [],
    }
    payload["monitoring_summary"] = {"heartbeat_interval_seconds": 21600}
    text = format_situation_summary(payload)
    assert "Belief-state ontology" in text
    assert "rated threshold" in text
    assert "source_prior=0.95" in text
    assert "method_conf=0.88" in text
    assert "21600s" in text


def test_analyst_slack_shows_belief_section():
    payload = _sample_coordinator_result()
    payload["evidence_claims"][0]["confidence_breakdown"] = {
        "overall_score": 0.81,
        "source_reliability_prior": 0.95,
        "method": "cold_start_delta",
        "method_confidence": 0.45,
        "uncertainty_notes": ["no rated threshold; delta-based"],
    }
    text = format_analyst_slack(payload)
    assert "*Belief-state ontology*" in text
    assert "cold start delta" in text
    assert "method_conf=0.45" in text


def test_error_passthrough():
    err = {"error": "input_error", "message": "missing county"}
    assert "input_error" in format_situation_summary(err)
    assert "input_error" in format_analyst_slack(err)


def test_insufficient_data_verdict_lines_not_no_flooding():
    payload = _sample_coordinator_result()
    payload["county_flood_evidence"] = {
        "regional_verdict": "insufficient_data",
        "insufficient_data": True,
        "is_flooding": False,
        "overall_confidence": 0.2,
        "weight_hint": "low",
        "verdict_gauge_count": 2,
        "confidence_components": {
            "source": 0.9,
            "location": 0.5,
            "time": 0.7,
            "impact": 0.2,
        },
    }
    text = format_situation_summary(payload)
    assert "insufficient_data" in text
    assert "no active flooding" not in text
    assert "Situation Analysis" in text

    from watcher.format_report import format_slack_report

    slack = format_slack_report(payload)
    assert "Verdict: `insufficient_data`" in slack
    assert "flooding=insufficient_data" in slack
    assert "flooding=False" not in slack
    assert "no_flooding" not in slack
