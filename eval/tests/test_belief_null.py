"""Tests for the publication belief/news architectural-null artifact."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from eval.run_belief_null import (
    _claim_counts,
    compare_operational_outputs,
    generate,
)


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    start = "2017-08-24T06:00:00Z"
    sites = []
    for index in range(5):
        sites.append(
            {
                "site_no": f"0800000{index}",
                "shef_id": f"T0{index}TX",
                "name": f"Test gauge {index}",
                "location_basis": "fips_verified",
                "action_stage": 1.0,
                "flood_stage": 2.0,
                "moderate_stage": 3.0,
                "major_stage": 4.0,
                "stage_unit": "ft",
                "priority_tier": 0,
                "threshold_source": "csv",
                "observations": [{"t": start, "stage": 0.2, "q": ["P"]}],
            }
        )
    corpus = {
        "schema": "sentinel-replay-corpus/1",
        "county": "Harris",
        "state": "TX",
        "fips": "48201",
        "sites": sites,
    }
    corpus_path = tmp_path / "harris.json"
    _write_json(corpus_path, corpus)

    manifest = {
        "schema": "sentinel-study-corpus-manifest/1",
        "artifacts": [
            {
                "path": str(corpus_path),
                "county": "Harris",
                "state": "TX",
                "fips": "48201",
                "analysis_start": start,
                "analysis_end": "2017-08-24T18:00:00Z",
                "sha256": _sha256(corpus_path),
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)

    news = {
        "schema": "sentinel-news-corpus/2",
        "county": "Harris",
        "state": "TX",
        "window": {"start": "20170823", "end": "20170825"},
        "source": "gdelt_gkg",
        "as_of_field": "GKG_V2_1_DATE",
        "as_of_semantics": "test GKG publication timestamp",
        "place_terms": ["Harris County"],
        "slices_swept": 192,
        "slices_missing": 0,
        "missing_slices": [],
        "slice_coverage_fraction": 1.0,
        "articles": [
            {
                "url": "https://example.test/major-flooding-inundates-houston",
                "title": "Major flooding inundates Houston neighborhoods today",
                "domain": "example.test",
                "published_at": start,
            }
        ],
    }
    news_path = tmp_path / "news.json"
    _write_json(news_path, news)
    news_manifest = {
        "schema": "sentinel-belief-news-corpus-manifest/1",
        "artifact": {
            "path": str(news_path),
            "sha256": _sha256(news_path),
            "corpus_schema": "sentinel-news-corpus/2",
            "window": news["window"],
            "required_lookback_hours": 24.0,
            "maximum_missing_slice_fraction": 0.02,
            "as_of_field": "GKG_V2_1_DATE",
            "slices_swept": 192,
            "slices_missing": 0,
            "slice_coverage_fraction": 1.0,
        },
    }
    news_manifest_path = tmp_path / "news_manifest.json"
    _write_json(news_manifest_path, news_manifest)
    return manifest_path, news_path, news_manifest_path


def _tier_total(scenario, tier: str, field: str) -> int:
    return sum(
        row[field]
        for row in scenario["claims"]["by_tier_and_type"]
        if row["tier"] == tier
    )


def test_factorial_replay_exposes_admission_change_but_operational_null(tmp_path):
    manifest_path, news_path, news_manifest_path = _fixture_inputs(tmp_path)
    before = os.environ.get("SENTINEL_ABLATION")
    os.environ["SENTINEL_ABLATION"] = "none"
    try:
        artifact = generate(
            manifest_path=manifest_path,
            news_path=news_path,
            news_manifest_path=news_manifest_path,
        )
    finally:
        restored = os.environ.get("SENTINEL_ABLATION")
        if before is None:
            os.environ.pop("SENTINEL_ABLATION", None)
        else:
            os.environ["SENTINEL_ABLATION"] = before

    assert restored == "none"
    assert set(artifact["scenarios"]) == {
        "full_with_news",
        "no_belief_with_news",
        "full_without_news",
        "no_belief_without_news",
    }
    full = artifact["scenarios"]["full_with_news"]
    ablated = artifact["scenarios"]["no_belief_with_news"]
    generated = _tier_total(full, "T2", "generated_events")
    assert generated > 0
    assert _tier_total(full, "T2", "structurally_valid_events") == generated
    assert _tier_total(full, "T2", "admitted_events") == 0
    assert _tier_total(full, "T2", "rejected_events") == generated
    assert _tier_total(ablated, "T2", "admitted_events") == generated
    assert artifact["interpretation"]["news_operational_null_observed"] is True
    assert artifact["schema"] == "sentinel-belief-news-architectural-null/2"
    assert artifact["design"]["news_source_coverage"][
        "slice_coverage_fraction"
    ] == 1.0
    assert all(
        row["all_operational_outputs_exact_equal"]
        for row in artifact["comparisons"].values()
    )


def test_artifact_is_byte_deterministic_for_unchanged_inputs(tmp_path):
    manifest_path, news_path, news_manifest_path = _fixture_inputs(tmp_path)
    first = generate(
        manifest_path=manifest_path,
        news_path=news_path,
        news_manifest_path=news_manifest_path,
    )
    second = generate(
        manifest_path=manifest_path,
        news_path=news_path,
        news_manifest_path=news_manifest_path,
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_news_corpus_manifest_hash_is_fail_closed(tmp_path):
    manifest_path, news_path, news_manifest_path = _fixture_inputs(tmp_path)
    news_path.write_text(news_path.read_text(encoding="utf-8") + "\n")
    try:
        generate(
            manifest_path=manifest_path,
            news_path=news_path,
            news_manifest_path=news_manifest_path,
        )
    except ValueError as exc:
        assert "checksum mismatch" in str(exc)
    else:
        raise AssertionError("tampered news corpus should fail closed")


def test_news_corpus_must_cover_replay_lookback(tmp_path):
    manifest_path, news_path, news_manifest_path = _fixture_inputs(tmp_path)
    news = json.loads(news_path.read_text(encoding="utf-8"))
    news["window"] = {"start": "20170824", "end": "20170825"}
    news["slices_swept"] = 96
    _write_json(news_path, news)
    news_manifest = json.loads(news_manifest_path.read_text(encoding="utf-8"))
    news_manifest["artifact"]["window"] = news["window"]
    news_manifest["artifact"]["sha256"] = _sha256(news_path)
    news_manifest["artifact"]["slices_swept"] = 96
    _write_json(news_manifest_path, news_manifest)
    try:
        generate(
            manifest_path=manifest_path,
            news_path=news_path,
            news_manifest_path=news_manifest_path,
        )
    except ValueError as exc:
        assert "required lookback" in str(exc)
    else:
        raise AssertionError("incomplete lookback coverage should fail closed")


def test_claim_counts_separate_structural_and_epistemic_rejections():
    result = _claim_counts(
        [
            [
                {
                    "claim_id": "valid-rejected",
                    "claim_type": "inundation",
                    "tier": "T2",
                    "structurally_valid": True,
                    "admitted": False,
                    "rejection_reason": "below minimum_confidence 0.55",
                },
                {
                    "claim_id": "invalid",
                    "claim_type": "warning",
                    "tier": "T2",
                    "structurally_valid": False,
                    "admitted": False,
                    "rejection_reason": "missing keys",
                },
            ]
        ]
    )
    assert result["totals"] == {
        "generated_events": 2,
        "structurally_valid_events": 1,
        "structurally_invalid_events": 1,
        "admitted_events": 0,
        "rejected_events": 2,
        "valid_but_not_admitted_events": 1,
    }


def test_exact_comparison_reports_first_operational_difference():
    base = {
        "phase_timeline": [],
        "alert_timeline": [{"t": "2017-01-01T00:00:00Z", "alert_level": "none"}],
        "flood_verdict_timeline": [],
        "acquisition_cadence": {},
        "recovery_timeline": [],
        "termination": {"reason": "window_end"},
    }
    changed = json.loads(json.dumps(base))
    changed["alert_timeline"][0]["alert_level"] = "warning"
    compared = compare_operational_outputs("left", "right", base, changed)
    assert compared["all_operational_outputs_exact_equal"] is False
    assert compared["unequal_components"] == ["alert_timeline"]
    assert compared["components"]["alert_timeline"]["first_difference"] == {
        "path": "$[0].alert_level",
        "left": "none",
        "right": "warning",
    }
