"""Tests for T2 article claim extraction."""

from __future__ import annotations

import os
from unittest import mock

from watcher.claims.extract import extract_claims_from_snapshots
from watcher.claims.extract_articles import extract_article_claims
from watcher.claims.validate import filter_claims_by_phase


def test_article_claim_t2_prior():
    article_evidence = {
        "high_confidence_articles": [
            {
                "title": "Major flooding reported downtown",
                "url": "https://news.example.com/flood-story",
                "page_score": 0.88,
                "indication": "flooding",
                "published_at": "2026-07-07T08:00:00Z",
            }
        ]
    }
    claims = extract_article_claims(
        article_evidence,
        county="Harris",
        state="TX",
        location="Harris County, TX",
    )
    assert len(claims) == 1
    claim = claims[0]
    assert claim["claim_id"].startswith("claim_art_")
    assert claim["claim_type"] == "inundation"
    assert claim["confidence_breakdown"]["source_reliability_prior"] == 0.45
    assert claim["value"]["evidence_tier"] == "T2"
    assert claim["value"]["weight_hint"] == "low"
    # Stable claim id from URL
    again = extract_article_claims(
        article_evidence,
        county="Harris",
        state="TX",
        location="Harris County, TX",
    )
    assert again[0]["claim_id"] == claim["claim_id"]


def test_article_below_threshold_not_in_evidence():
    claims = extract_article_claims({"high_confidence_articles": []}, county="Harris", state="TX", location="")
    assert claims == []


def test_detector_indications_map_only_positive_assertions_to_claims():
    articles = [
        {
            "title": indication,
            "url": f"https://news.example.com/{indication}",
            "page_score": 0.9,
            "indication": indication,
            "published_at": "2026-07-07T08:00:00Z",
        }
        for indication in (
            "confirms_flood",
            "suggests_flood_risk",
            "neutral",
            "denies_or_clear",
        )
    ]
    claims = extract_article_claims(
        {"high_confidence_articles": articles},
        county="Harris",
        state="TX",
        location="Harris County, TX",
    )

    by_indication = {claim["value"]["indication"]: claim for claim in claims}
    assert set(by_indication) == {"confirms_flood", "suggests_flood_risk"}
    assert by_indication["confirms_flood"]["claim_type"] == "inundation"
    assert by_indication["suggests_flood_risk"]["claim_type"] == "warning"
    assert (
        by_indication["confirms_flood"]["confidence_components"]["impact"]
        > by_indication["suggests_flood_risk"]["confidence_components"]["impact"]
    )


def test_live_extraction_and_replay_gate_article_claims_the_same_way():
    article_claims = extract_article_claims(
        {
            "high_confidence_articles": [
                {
                    "title": "Flood risk remains",
                    "url": "https://news.example.com/risk",
                    "page_score": 0.9,
                    "indication": "suggests_flood_risk",
                    "published_at": "2026-07-07T08:00:00Z",
                }
            ]
        },
        county="Harris",
        state="TX",
        location="Harris County, TX",
    )
    summary = {
        "required_claim_types": [
            {"type": "warning", "minimum_confidence": 0.55}
        ]
    }

    with mock.patch.dict(os.environ, {"SENTINEL_ABLATION": "full"}):
        replay_accepted, replay_rejected = filter_claims_by_phase(article_claims, summary)
        live = extract_claims_from_snapshots(
            [],
            monitoring_summary=summary,
            county="Harris",
            state="TX",
            location="Harris County, TX",
            additional_claims=article_claims,
        )

    assert replay_accepted == []
    assert len(replay_rejected) == 1
    assert [c["claim_id"] for c in live["evidence_claims"]] == [
        c["claim_id"] for c in replay_accepted
    ]
    assert [c["claim_id"] for c in live["rejected_claims"]] == [
        c["claim_id"] for c in replay_rejected
    ]
    assert live["claims_extracted"] == len(article_claims)
