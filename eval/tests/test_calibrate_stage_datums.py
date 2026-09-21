"""Focused checks for the frozen Duval stage-datum calibration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from eval.calibrate_stage_datums import (
    CALIBRATION_END_UTC,
    CALIBRATION_START_UTC,
    SCHEMA,
    SITE_CONFIG,
    STUDY_END_UTC,
    STUDY_START_UTC,
    _summary,
    apply_calibration,
    nwps_crest_wall_time_to_utc,
    validate_calibration_artifact,
)


def _artifact():
    sites = []
    base = datetime(2017, 7, 1, 4, tzinfo=timezone.utc)
    for site_no, config in SITE_CONFIG.items():
        offset = float(config["expected_offset_ft"])
        anchors = []
        for index in range(int(config["expected_anchors"])):
            when = base + timedelta(hours=index)
            anchors.append(
                {
                    "matched_time_utc": when.isoformat().replace("+00:00", "Z"),
                    "calibration_segment": "pre_event",
                    "nwps_stage_ft": 2.0,
                    "usgs_00065_ft": 2.0 + offset,
                    "offset_ft": offset,
                }
            )
        sites.append(
            {
                "site_no": site_no,
                "nwps_lid": config["lid"],
                "prespecified_offset_ft": offset,
                "offset_summary_ft": _summary(
                    [float(anchor["offset_ft"]) for anchor in anchors]
                ),
                "calibration_anchors": anchors,
            }
        )
    return {
        "schema": SCHEMA,
        "study_window_utc_half_open": [STUDY_START_UTC, STUDY_END_UTC],
        "calibration_window_utc_half_open": [
            CALIBRATION_START_UTC,
            CALIBRATION_END_UTC,
        ],
        "sites": sites,
    }


def _corpus():
    return {
        "schema": "sentinel-replay-corpus/1",
        "county": "Duval",
        "state": "FL",
        "fips": "12031",
        "sites": [
            {
                "site_no": site_no,
                "observations": [
                    {
                        "t": "2017-09-08T04:00:00Z",
                        "stage": float(config["expected_offset_ft"]) + 2.0,
                    },
                    {"t": "2017-09-08T04:15:00Z", "stage": None, "flow": 10.0},
                ],
            }
            for site_no, config in SITE_CONFIG.items()
        ],
    }


def test_nwps_historic_crest_clock_is_interpreted_in_station_zone():
    converted = nwps_crest_wall_time_to_utc("2017-10-01T20:30:00Z")
    assert converted.isoformat() == "2017-10-02T00:30:00+00:00"


def test_verifier_recomputes_prespecified_offsets_and_rejects_leakage():
    artifact = _artifact()
    result = validate_calibration_artifact(artifact)
    assert result["sites"] == 8
    assert result["anchors"] == sum(
        int(config["expected_anchors"]) for config in SITE_CONFIG.values()
    )

    leaked = deepcopy(artifact)
    leaked["sites"][0]["calibration_anchors"][0]["matched_time_utc"] = (
        "2017-09-08T04:00:00Z"
    )
    with pytest.raises(ValueError, match="study window"):
        validate_calibration_artifact(leaked)

    tampered = deepcopy(artifact)
    tampered["sites"][0]["offset_summary_ft"]["maximum"] += 0.1
    with pytest.raises(ValueError, match="summary field maximum"):
        validate_calibration_artifact(tampered)

    incomplete = deepcopy(artifact)
    incomplete["sites"][0]["calibration_anchors"].pop()
    with pytest.raises(ValueError, match="frozen artifact requires"):
        validate_calibration_artifact(incomplete)


def test_apply_retains_raw_stage_and_is_idempotent():
    artifact = _artifact()
    source = _corpus()
    transformed = apply_calibration(
        source,
        artifact,
        calibration_path="eval/corpus/study/calibration.json",
        calibration_sha256="abc123",
    )

    assert "stage_raw" not in source["sites"][0]["observations"][0]
    assert transformed["datum_calibration"]["transformed_sites"] == 8
    assert transformed["datum_calibration"]["transformed_stage_observations"] == 8
    for site in transformed["sites"]:
        observation = site["observations"][0]
        offset = float(SITE_CONFIG[site["site_no"]]["expected_offset_ft"])
        assert observation["stage_raw"] == pytest.approx(offset + 2.0)
        assert observation["stage"] == pytest.approx(2.0)
        assert site["stage_transform"]["offset_ft"] == offset

    repeated = apply_calibration(
        transformed,
        artifact,
        calibration_path="eval/corpus/study/calibration.json",
        calibration_sha256="abc123",
    )
    assert repeated == transformed


def test_apply_rejects_an_uncovered_duval_site():
    corpus = _corpus()
    corpus["sites"][0]["site_no"] = "uncovered"
    with pytest.raises(ValueError, match="site mismatch"):
        apply_calibration(
            corpus,
            _artifact(),
            calibration_path="calibration.json",
            calibration_sha256="abc123",
        )
