"""Tests for ontology sections in coordinator report formatting."""

from __future__ import annotations

from watcher.format_report import format_coordinator_report, format_slack_report


def _payload_with_claim():
    return {
        "mission_id": "mission_test",
        "mission_spec": {
            "county": "Harris",
            "state": "TX",
            "fips": "48201",
            "current_event_phase": "early_watch",
            "heartbeat_interval_seconds": 21600,
        },
        "monitoring_summary": {
            "hazard": "flood",
            "phase": "early_watch",
            "heartbeat_interval_seconds": 21600,
        },
        "belief_state": {"event_phase": "early_watch", "alert_level": "none"},
        "phase_decision": {"alert_level": "none", "phase_changed": False},
        "evidence_claims": [
            {
                "claim_id": "claim_nws_abc123",
                "claim_type": "warning",
                "severity": "severe",
                "overall_confidence": 0.82,
                "confidence_components": {
                    "source": 0.9,
                    "location": 0.9,
                    "time": 0.85,
                    "impact": 0.85,
                },
                "confidence_breakdown": {
                    "overall_score": 0.82,
                    "source_reliability_prior": 0.95,
                    "method": "corroborated",
                    "method_confidence": 0.82,
                    "uncertainty_notes": [],
                },
                "excerpt": "Flood Warning for Harris County",
            },
            {
                "claim_id": "claim_art_xyz123",
                "claim_type": "inundation",
                "severity": "moderate",
                "overall_confidence": 0.79,
                "confidence_components": {
                    "source": 0.73,
                    "location": 0.75,
                    "time": 0.9,
                    "impact": 0.88,
                },
                "excerpt": "Local news reports street flooding",
                "value": {
                    "url": "https://example.com/flood-story",
                    "title": "Flooding downtown",
                    "page_score": 0.88,
                },
            },
        ],
        "article_evidence": {
            "status": "ok",
            "threshold": 0.75,
            "top_sources": [
                {
                    "title": "Flooding downtown",
                    "url": "https://example.com/flood-story",
                    "page_score": 0.88,
                    "indication": "flooding",
                },
                {
                    "title": "Weather update",
                    "url": "https://example.com/weather",
                    "page_score": 0.55,
                    "indication": "neutral",
                },
            ],
            "high_confidence_articles": [
                {
                    "title": "Flooding downtown",
                    "url": "https://example.com/flood-story",
                    "page_score": 0.88,
                    "indication": "flooding",
                }
            ],
        },
        "report_delta": {},
        "sources_attempted": 3,
        "sources_succeeded": 3,
        "failed_fetches": [],
        "run_at": "2026-07-07T12:00:00Z",
    }


def test_coordinator_report_shows_belief_ontology():
    text = format_coordinator_report(_payload_with_claim())
    assert "Belief-state ontology" in text
    assert "claim_nws_abc123" in text
    assert "source_prior=0.95" in text
    assert "News sources returned (validation input)" in text
    assert "https://example.com/flood-story" in text


def test_slack_report_shows_belief_ontology():
    text = format_slack_report(_payload_with_claim())
    assert "*Belief-state ontology*" in text
    assert "claim_nws_abc123" in text
    assert "*News sources returned (validation input)*" in text


def test_report_does_not_conflate_page_selection_and_claim_credibility():
    text = format_coordinator_report(_payload_with_claim())
    assert "detector page score > 0.75" in text
    assert "page_score=0.8800" in text
    assert "Page score is a detector relevance/recency score, not epistemic credibility" in text
    assert "confidence threshold" not in text


def test_report_renders_the_components_present_on_scored_gauge_claims():
    payload = _payload_with_claim()
    payload["evidence_claims"].insert(
        0,
        {
            "claim_id": "claim_cf_gauge01",
            "claim_type": "GaugeObservation",
            "severity": "minor",
            "overall_confidence": 0.89,
            "confidence_components": {
                "source": 0.95,
                "location": 0.92,
                "method": 0.85,
                "quality": 0.85,
            },
            "excerpt": "Stage derived from a published threshold",
        },
    )
    text = format_coordinator_report(payload)
    assert "src=0.95 loc=0.92 method=0.85 quality=0.85" in text
    assert "method=n/a" not in text
    assert "quality=n/a" not in text
    assert "County flood API (NWPS + USGS) — legacy display summary" not in text
