"""Regression tests for the publication evaluation protocol."""

from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

import eval.run_study as study_module
from eval.baselines import (
    fixed_schedule_specs,
    matched_budget_interval_seconds,
    matched_budget_schedule_specs,
)
from eval.ground_truth import apply_threshold_compatibility, hydrologic_truth_at
from eval.metrics import (
    DEFAULT_EVALUATION_INTERVAL_SECONDS,
    annotate_run_on_grid,
    evaluate,
    rq1_detection,
    rq2_flood_episodes,
    rq2_timeliness,
    rq4_source_availability,
)
from eval.run_replay import run as run_replay
from eval.run_study import (
    FROZEN_REPLAY_ENVIRONMENT,
    _preflight,
    generate as generate_study,
)
from watcher.phase_engine import RECOVERY_MIN_CLEAR_HOURS_TO_FOLD


def _iso(hour: int) -> str:
    return f"2017-01-01T{hour:02d}:00:00Z"


def _corpus(stages=(0.0, 2.0, 2.0, 0.0, 0.0)):
    return {
        "schema": "sentinel-replay-corpus/1",
        "county": "Test",
        "state": "TX",
        "fips": "48000",
        "sites": [
            {
                "site_no": "A",
                "name": "Test River",
                "location_basis": "fips_verified",
                "action_stage": 1.0,
                "flood_stage": 2.0,
                "moderate_stage": 3.0,
                "major_stage": 4.0,
                "stage_unit": "ft",
                "threshold_source": "csv",
                "observations": [
                    {"t": _iso(hour), "stage": stage}
                    for hour, stage in enumerate(stages)
                ],
            }
        ],
    }


def _tick(
    t,
    *,
    flooding=False,
    flood_stage_observed=False,
    phase="early_watch",
    **extra,
):
    return {
        "t": t,
        "phase_before": phase,
        "phase_after": phase,
        "phase_changed": False,
        "alert_level": "none",
        "regional_verdict": "minor_flooding" if flooding else "no_flooding",
        "is_flooding": flooding,
        "flood_stage_observed": flood_stage_observed,
        "flood_stage_observed_sites": ["A"] if flood_stage_observed else [],
        "policy_observed_at": t,
        "gauges_with_data": 1,
        "gauges_scored": 1,
        "claims": [],
        "all_claims": [],
        "material_change": False,
        **extra,
    }


def test_common_grid_carries_policy_without_looking_ahead():
    run = {
        "window": {"start": _iso(0), "end": _iso(4)},
        "ticks": [
            _tick(_iso(0)),
            _tick(_iso(2), flooding=True, phase="elevated_watch"),
            _tick(_iso(4), phase="recovery"),
        ],
    }
    aligned = annotate_run_on_grid(
        run, corpus=_corpus(), events=[], interval_seconds=3600
    )

    assert [t["is_flooding"] for t in aligned] == [False, False, True, True, False]
    assert aligned[1]["policy_observed_at"] == _iso(0)
    assert aligned[3]["policy_observed_at"] == _iso(2)
    assert aligned[1]["truth_hydrologic_flooding"] is True
    scores = rq1_detection(aligned)["pooled"]
    assert (scores["tp"], scores["fn"], scores["fp"]) == (1, 1, 1)


def test_schedule_offset_is_scored_from_explicit_initial_state():
    run = {
        "window": {"start": _iso(0), "end": _iso(3)},
        "ticks": [_tick(_iso(2), flooding=True, phase="elevated_watch")],
    }
    aligned = annotate_run_on_grid(
        run, corpus=_corpus(), events=[], interval_seconds=3600
    )

    assert aligned[0]["policy_initialized"] is False
    assert aligned[1]["policy_initialized"] is False
    assert aligned[0]["regional_verdict"] == "insufficient_data"
    assert aligned[2]["policy_initialized"] is True


def test_finite_truth_age_marks_unobservable_all_clear_unknown():
    corpus = _corpus(stages=(0.0,))
    corpus["sites"].append(
        {
            **corpus["sites"][0],
            "site_no": "B",
            "observations": [{"t": _iso(0), "stage": 0.0}],
        }
    )
    corpus["sites"][0]["observations"].append({"t": _iso(2), "stage": 0.0})
    at_two = datetime(2017, 1, 1, 2, tzinfo=timezone.utc)

    finite = hydrologic_truth_at(corpus, at_two, max_age_hours=1.0)
    assert finite["truth_known"] is False
    assert finite["is_flooding"] is None
    assert finite["reference_gauges_fresh"] == 1

    legacy = hydrologic_truth_at(corpus, at_two)
    assert legacy["truth_known"] is True
    assert legacy["is_flooding"] is False

    corpus["sites"][0]["observations"][-1]["stage"] = 2.0
    positive = hydrologic_truth_at(corpus, at_two, max_age_hours=1.0)
    assert positive["truth_known"] is True
    assert positive["is_flooding"] is True


def test_episode_metrics_censor_unknown_and_measure_duration():
    truths = [False, True, True, None, True, True, False]
    predictions = [False, False, True, False, False, False, False]
    phases = [
        "early_watch",
        "early_watch",
        "elevated_watch",
        "elevated_watch",
        "recovery",
        "folded",
        "folded",
    ]
    ticks = [
        {
            **_tick(
                _iso(i),
                flooding=predictions[i],
                flood_stage_observed=predictions[i],
                phase=phases[i],
            ),
            "truth_hydrologic_flooding": truths[i],
            "evaluation_interval_hours": 0.0 if i == 6 else 1.0,
        }
        for i in range(7)
    ]
    result = rq2_flood_episodes(ticks)

    assert result["n_observed_flood_episodes"] == 1
    assert result["episodes_observed_by_schedule"] == 1
    assert result["episode_observation_rate"] == 1.0
    assert result["observed_flood_duration_hours"] == 4.0
    assert result["unobserved_flood_stage_duration_hours"] == 3.0
    assert result["unknown_truth_duration_hours"] == 1.0
    assert result["episodes"][0]["interval_censored"] is True
    assert result["episodes"][0]["internal_unknown_duration_hours"] == 1.0


def test_unknown_gap_does_not_split_episode_but_known_false_does():
    def make(values):
        return [
            {
                **_tick(_iso(index), flooding=True),
                "truth_hydrologic_flooding": value,
                "evaluation_interval_hours": 1.0,
            }
            for index, value in enumerate(values)
        ]

    uncertain = rq2_flood_episodes(make([True, None, True]))
    separated = rq2_flood_episodes(make([True, False, True]))
    assert uncertain["n_observed_flood_episodes"] == 1
    assert uncertain["episodes"][0]["interval_censored"] is True
    assert uncertain["episodes"][0]["internal_unknown_duration_hours"] == 1.0
    assert uncertain["known_clear_duration_hours"] == 0.0
    assert (
        uncertain["observed_flood_duration_hours"]
        + uncertain["known_clear_duration_hours"]
        + uncertain["unknown_truth_duration_hours"]
        == 3.0
    )
    assert separated["n_observed_flood_episodes"] == 2


def test_acquisition_during_censored_unknown_span_observes_episode():
    ticks = [
        {
            **_tick(_iso(0), flood_stage_observed=False),
            "truth_hydrologic_flooding": True,
            "evaluation_interval_hours": 1.0,
        },
        {
            **_tick(_iso(1), flood_stage_observed=True),
            "truth_hydrologic_flooding": None,
            "evaluation_interval_hours": 1.0,
        },
        {
            **_tick(_iso(2), flood_stage_observed=False),
            "truth_hydrologic_flooding": None,
            "evaluation_interval_hours": 1.0,
        },
        {
            **_tick(_iso(3), flood_stage_observed=False),
            "truth_hydrologic_flooding": False,
            "evaluation_interval_hours": 1.0,
        },
    ]
    result = rq2_flood_episodes(ticks)
    episode = result["episodes"][0]

    assert episode["interval_censored"] is True
    assert episode["trailing_unknown_duration_hours"] == 2.0
    assert episode["schedule_observed_episode"] is True
    assert episode["first_schedule_flood_stage_observation"] == _iso(1)
    assert episode["positive_duration_before_first_schedule_observation_hours"] == 1.0
    assert episode["unobserved_flood_stage_duration_hours"] == 1.0


def test_brief_off_grid_acquisition_in_final_unknown_interval_is_retained():
    grid = [
        {
            **_tick("2017-01-01T00:00:00Z", flood_stage_observed=False),
            "truth_hydrologic_flooding": True,
            "evaluation_interval_hours": 0.75,
        },
        {
            **_tick("2017-01-01T00:45:00Z", flood_stage_observed=False),
            "truth_hydrologic_flooding": None,
            "evaluation_interval_hours": 0.25,
        },
        {
            **_tick("2017-01-01T01:00:00Z", flood_stage_observed=False),
            "truth_hydrologic_flooding": False,
            "evaluation_interval_hours": 0.25,
        },
    ]
    acquisitions = [
        _tick("2017-01-01T00:50:00Z", flood_stage_observed=True),
        _tick("2017-01-01T00:55:00Z", flood_stage_observed=False),
        # A positive acquisition at the known-clear boundary must not credit
        # the preceding interval-censored episode.
        _tick("2017-01-01T01:00:00Z", flood_stage_observed=True),
    ]
    episode = rq2_flood_episodes(
        grid, acquisition_ticks=acquisitions
    )["episodes"][0]

    assert episode["schedule_observed_episode"] is True
    assert episode["first_schedule_flood_stage_observation"] == (
        "2017-01-01T00:50:00Z"
    )
    assert episode["first_schedule_flood_stage_observation_grid_time"] is None
    assert episode["schedule_flood_stage_observation_lag_hours"] == 0.8333


def test_action_stage_policy_does_not_count_as_schedule_flood_observation():
    ticks = []
    for hour, truth, policy, flood_observation in (
        (0, False, False, False),
        (1, False, True, False),  # action stage only
        (2, True, True, False),   # carried action-stage policy state
        (3, True, True, True),    # first acquisition at minor/flood stage
    ):
        ticks.append(
            {
                **_tick(
                    _iso(hour),
                    flooding=policy,
                    flood_stage_observed=flood_observation,
                ),
                "truth_hydrologic_flooding": truth,
                "evaluation_interval_hours": 1.0,
            }
        )
    episode = rq2_flood_episodes(ticks)["episodes"][0]
    timing = rq2_timeliness(ticks, [], acquisition_ticks=ticks)
    assert episode["schedule_observed_episode"] is True
    assert episode["first_schedule_flood_stage_observation"] == _iso(3)
    assert episode["schedule_flood_stage_observation_lag_hours"] == 1.0
    assert episode["positive_duration_before_first_schedule_observation_hours"] == 1.0
    assert timing["policy_positive_lead_lag_hours_vs_flood_stage"] == -1.0
    assert timing["schedule_flood_stage_observation_lag_hours_vs_reference"] == 1.0


def test_timeliness_uses_actual_acquisition_time_not_carried_grid_time():
    grid = [
        {
            **_tick("2017-08-27T02:30:00Z", flooding=False),
            "truth_hydrologic_flooding": True,
            "evaluation_interval_hours": 0.25,
        },
        {
            **_tick(
                "2017-08-27T02:45:00Z",
                flooding=True,
                flood_stage_observed=True,
            ),
            "truth_hydrologic_flooding": True,
            "evaluation_interval_hours": 0.25,
        },
    ]
    acquisitions = [
        _tick(
            "2017-08-27T02:36:00Z",
            flooding=True,
            flood_stage_observed=True,
        )
    ]
    timing = rq2_timeliness(grid, [], acquisition_ticks=acquisitions)

    assert timing["first_schedule_flood_stage_observation"] == (
        "2017-08-27T02:36:00Z"
    )
    assert timing["schedule_flood_stage_observation_lag_hours_vs_reference"] == 0.1
    assert timing["first_grid_state_with_flood_stage_observation"] == (
        "2017-08-27T02:45:00Z"
    )
    assert timing[
        "grid_state_flood_stage_observation_lag_hours_vs_reference"
    ] == 0.25
    assert "use rq2_event_episodes" in timing["window_first_timing_semantics"]


def test_prior_episode_acquisition_cannot_observe_later_episode():
    carried = [
        {
            **_tick(
                _iso(hour),
                flooding=True,
                flood_stage_observed=True,
            ),
            "truth_hydrologic_flooding": truth,
            "evaluation_interval_hours": 1.0,
            # Every carried state still comes from the episode-1 acquisition.
            "policy_observed_at": _iso(0),
        }
        for hour, truth in ((0, True), (1, False), (2, True), (3, True), (4, False))
    ]
    without_new_acquisition = rq2_flood_episodes(carried)
    assert without_new_acquisition["n_observed_flood_episodes"] == 2
    assert without_new_acquisition["episodes"][0]["schedule_observed_episode"] is True
    assert without_new_acquisition["episodes"][1]["schedule_observed_episode"] is False
    assert without_new_acquisition["episodes"][1][
        "unobserved_flood_stage_duration_hours"
    ] == 2.0

    with_new_acquisition = [dict(tick) for tick in carried]
    with_new_acquisition[3]["policy_observed_at"] = _iso(3)
    result = rq2_flood_episodes(with_new_acquisition)
    second = result["episodes"][1]
    assert second["schedule_observed_episode"] is True
    assert second["first_schedule_flood_stage_observation"] == _iso(3)
    assert second["schedule_flood_stage_observation_lag_hours"] == 1.0
    assert second["positive_duration_before_first_schedule_observation_hours"] == 1.0
    assert second["unobserved_flood_stage_duration_hours"] == 1.0


def test_acquisitions_and_materiality_gated_messages_are_not_conflated():
    grid = [
        {
            **_tick(_iso(i)),
            "truth_hydrologic_flooding": False,
            "evaluation_interval_hours": 0.0 if i == 4 else 1.0,
        }
        for i in range(5)
    ]
    acquisitions = [
        _tick(_iso(0), material_acquisition=True, material_change=True),
        _tick(_iso(3), material_acquisition=False, material_change=False),
    ]
    result = rq2_timeliness(grid, [], acquisition_ticks=acquisitions)

    assert result["evaluation_grid_points"] == 5
    assert result["acquisitions"] == 2
    assert result[
        "current_runtime_logical_report_delivery_opportunities"
    ] == 2
    assert result["material_acquisitions"] == 1
    assert result[
        "hypothetical_logical_report_delivery_opportunities_if_materiality_gated"
    ] == 1
    assert result["report_delivery_opportunity_semantics"] == (
        "one logical coordinator report is generated per due acquisition; "
        "Slack rendering may split one report into multiple transport messages, "
        "and replay measures neither attempted nor successful delivery"
    )
    assert "current_runtime_messages" not in result
    assert result["legacy_metric_aliases"]["current_runtime_messages"] == (
        "current_runtime_logical_report_delivery_opportunities"
    )


def test_source_availability_separates_missing_from_stale_opportunities():
    ticks = [
        {
            "source_availability": {
                "all_selected_sites": {
                    "sources_total": 3,
                    "no_prior_observation": 1,
                    "latest_observation_missing_stage": 0,
                    "stale_stage": 1,
                    "fresh_stage": 1,
                },
                "threshold_compatible_sites": {
                    "sources_total": 2,
                    "no_prior_observation": 0,
                    "latest_observation_missing_stage": 0,
                    "stale_stage": 1,
                    "fresh_stage": 1,
                },
            }
        },
        {
            "source_availability": {
                "all_selected_sites": {
                    "sources_total": 3,
                    "no_prior_observation": 0,
                    "latest_observation_missing_stage": 1,
                    "stale_stage": 0,
                    "fresh_stage": 2,
                },
                "threshold_compatible_sites": {
                    "sources_total": 2,
                    "no_prior_observation": 0,
                    "latest_observation_missing_stage": 1,
                    "stale_stage": 0,
                    "fresh_stage": 1,
                },
            }
        },
    ]
    result = rq4_source_availability(ticks)

    assert result["unit"] == "acquisition_source_opportunity"
    assert result["all_selected_sites"]["source_opportunities"] == 6
    assert result["all_selected_sites"]["missing_stage_total"] == 2
    assert result["all_selected_sites"]["stale_stage"] == 1
    assert result["all_selected_sites"]["fresh_stage_rate"] == 0.5
    assert result["threshold_compatible_sites"]["stale_stage_rate"] == 0.25


def test_fixed_baselines_cover_cadences_and_clock_phase_offsets():
    specs = fixed_schedule_specs()
    assert DEFAULT_EVALUATION_INTERVAL_SECONDS == 900
    assert len(specs) == 4 + 8 + 12 + 24 + 48
    assert {s["fixed_interval_seconds"] for s in specs} == {
        3600,
        7200,
        10800,
        21600,
        43200,
    }
    six_hour_offsets = [
        s["schedule_offset_seconds"]
        for s in specs
        if s["fixed_interval_seconds"] == 21600
    ]
    assert six_hour_offsets == list(range(0, 21600, 900))
    assert all(s["phase_reasoning_enabled"] for s in specs)


def test_matched_budget_interval_uses_same_nominal_acquisition_count():
    assert matched_budget_interval_seconds(
        start="2017-08-25T00:00:00Z",
        end="2017-09-01T00:00:00Z",
        target_acquisitions=65,
    ) == 9305


def test_matched_budget_enumerates_clock_offsets_and_selects_without_outcomes():
    specs = matched_budget_schedule_specs(
        start="2017-08-25T00:00:00Z",
        end="2017-09-01T00:00:00Z",
        target_acquisitions=65,
    )
    assert len(specs) == 11
    assert {spec["fixed_interval_seconds"] for spec in specs} == {9305}
    assert [spec["schedule_offset_seconds"] for spec in specs] == list(
        range(0, 9305, 900)
    )


def test_threshold_exclusion_disables_only_datum_dependent_thresholds():
    original = _corpus(stages=(2.0,))
    prepared, exclusions = apply_threshold_compatibility(
        original, excluded_site_ids=["A"]
    )
    assert original["sites"][0]["flood_stage"] == 2.0
    assert prepared["sites"][0]["flood_stage"] is None
    assert prepared["sites"][0]["observations"] == original["sites"][0]["observations"]
    assert exclusions[0]["site_no"] == "A"


def test_manifest_excluded_high_site_cannot_enter_replay_or_oracle_truth(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SENTINEL_ABLATION", "full")
    corpus = _corpus(stages=(0.5, 0.5))
    corpus["sites"].append(
        {
            **corpus["sites"][0],
            "site_no": "BAD",
            "observations": [
                {"t": _iso(0), "stage": 12.0},
                {"t": _iso(1), "stage": 12.0},
            ],
        }
    )
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    run = run_replay(
        corpus_path=str(corpus_path),
        county="Test",
        state="TX",
        fips="48000",
        start=_iso(0),
        end=_iso(2),
        fixed_interval_seconds=3600,
        threshold_excluded_site_ids=["BAD"],
    )
    metrics = evaluate(run, corpus=corpus, events=[])
    assert [row["site_no"] for row in run["threshold_exclusions"]] == ["BAD"]
    assert metrics["protocol"]["threshold_exclusions"][0]["site_no"] == "BAD"
    assert metrics["protocol"]["threshold_exclusions"][0]["original_thresholds"][
        "flood_stage"
    ] == 2.0
    assert metrics["rq2_event_episodes"]["n_observed_flood_episodes"] == 0
    assert metrics["rq1_policy_state_agreement"]["pooled"]["fn"] == 0


def _long_corpus():
    start = datetime(2017, 1, 1, tzinfo=timezone.utc)
    observations = []
    for hour in range(73):
        observations.append(
            {
                "t": (start + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
                "stage": 2.0 if hour < 6 else 0.5,
            }
        )
    corpus = _corpus(stages=())
    corpus["sites"][0]["observations"] = observations
    for site_no in ("B", "C", "D", "E"):
        corpus["sites"].append(
            {
                **corpus["sites"][0],
                "site_no": site_no,
                "observations": list(observations),
            }
        )
    return corpus


def test_replay_uses_virtual_elapsed_time_for_recovery_fold(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_ABLATION", "full")
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_long_corpus()), encoding="utf-8")

    adaptive = run_replay(
        corpus_path=str(corpus_path),
        county="Test",
        state="TX",
        fips="48000",
        start=_iso(0),
        end="2017-01-04T00:00:00Z",
    )
    fixed = run_replay(
        corpus_path=str(corpus_path),
        county="Test",
        state="TX",
        fips="48000",
        start=_iso(0),
        end="2017-01-04T00:00:00Z",
        fixed_interval_seconds=3600,
    )

    assert adaptive["schedule"]["kind"] == "phase_adaptive"
    assert fixed["schedule"]["kind"] == "fixed_override"
    assert fixed["schedule"]["phase_reasoning_enabled"] is True
    assert adaptive["termination"]["reason"] == "recovery_continuous_clear"
    assert fixed["termination"]["reason"] == "recovery_continuous_clear"
    assert adaptive["ticks"][-1]["t"] == fixed["ticks"][-1]["t"]
    assert adaptive["ticks"][-1]["recovery_clear_elapsed_hours"] == pytest.approx(
        RECOVERY_MIN_CLEAR_HOURS_TO_FOLD
    )
    assert fixed["ticks"][-1]["recovery_clear_elapsed_hours"] == pytest.approx(
        RECOVERY_MIN_CLEAR_HOURS_TO_FOLD
    )


def test_truth_reference_is_not_an_implicit_nws_system_input(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_ABLATION", "full")
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_corpus(stages=(0.0,))), encoding="utf-8")
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "phenomena": "FF",
                        "significance": "W",
                        "name": "Flash Flood Warning",
                        "issue": _iso(0),
                        "expire": _iso(1),
                        "eventid": "1",
                        "wfo": "TST",
                        "ugc": "TXC000",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    common = {
        "corpus_path": str(corpus_path),
        "truth_path": str(truth_path),
        "county": "Test",
        "state": "TX",
        "fips": "48000",
        "start": _iso(0),
        "end": _iso(1),
    }

    held_out = run_replay(**common)
    injected = run_replay(**common, nws_input_path=str(truth_path))

    assert held_out["truth_reference"] == str(truth_path)
    assert held_out["nws_system_input"] is None
    assert held_out["ticks"][0]["n_active_alerts"] == 0
    assert injected["nws_system_input"] == str(truth_path)
    assert injected["ticks"][0]["n_active_alerts"] == 1
    assert injected["nws_reference_is_system_input"] is True


def test_replay_acquisitions_use_half_open_window(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_ABLATION", "full")
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_long_corpus()), encoding="utf-8")
    result = run_replay(
        corpus_path=str(corpus_path),
        county="Test",
        state="TX",
        fips="48000",
        start=_iso(0),
        end=_iso(4),
        fixed_interval_seconds=3600,
    )
    assert [tick["t"] for tick in result["ticks"]] == [
        _iso(0),
        _iso(1),
        _iso(2),
        _iso(3),
    ]
    assert result["analysis_interval_convention"] == "[start,end)"
    assert result["ticks"][0]["flood_stage_observed"] is True
    assert result["ticks"][0]["flood_stage_observed_sites"] == ["A", "B", "C", "D", "E"]
    assert result["ticks"][0]["source_availability"][
        "threshold_compatible_sites"
    ]["fresh_stage"] == 5


def test_study_generator_writes_county_aggregate_and_table_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SENTINEL_ABLATION", "full")
    corpus = _corpus(stages=(0.5, 0.5, 0.5, 0.5))
    corpus_path = tmp_path / "study_county.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    digest = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "sentinel-study-corpus-manifest/1",
                "artifacts": [
                    {
                        "path": str(corpus_path),
                        "county": "Test",
                        "state": "TX",
                        "fips": "48000",
                        "event": "Synthetic",
                        "analysis_role": "episode_evaluation",
                        "analysis_start": _iso(0),
                        "analysis_end": _iso(4),
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "artifacts"
    result = generate_study(
        manifest_path=manifest_path,
        out_dir=out_dir,
        cadence_hours=(1.0,),
        offset_step_seconds=3600,
        evaluation_interval_seconds=900,
    )
    assert (out_dir / "counties/test_48000/result.json").is_file()
    assert (out_dir / "aggregate.json").is_file()
    assert (out_dir / "tables.json").is_file()
    assert (out_dir / "preflight_qc.json").is_file()
    assert (out_dir / "artifact_manifest.json").is_file()
    assert result["schema"] == "sentinel-study-artifact-manifest/3"
    assert result["config"]["analysis_interval_convention"] == "[analysis_start,analysis_end)"
    tables = json.loads((out_dir / "tables.json").read_text(encoding="utf-8"))
    aggregate = json.loads(
        (out_dir / "aggregate.json").read_text(encoding="utf-8")
    )
    assert tables["county_primary"][0]["analysis_role"] == "episode_evaluation"
    assert aggregate["n_episode_evaluation_counties"] == 1
    assert aggregate["n_coverage_only_counties"] == 0
    assert aggregate[
        "episode_evaluation_primary_truth_known_duration_proportion"
    ] == aggregate["all_county_primary_truth_known_duration_proportion"]
    assert tables["matched_budget_offset_ranges"][0]["n_phase_offsets"] >= 1
    assert "episodes_observed_by_schedule" in tables[
        "fixed_cadence_offset_ranges"
    ][0]
    assert "episodes_observed_by_schedule" in tables[
        "matched_budget_offset_ranges"
    ][0]
    assert tables["matched_budget_selected"][0][
        "selected_nearest_realised_acquisition_count"
    ] is True
    assert {
        row["sensitivity_value"]
        for row in tables["clear_gauge_requirement_sensitivity"]
    } == {1, 3, 5, 8}
    assert {
        row["sensitivity_value"]
        for row in tables["policy_staleness_sensitivity"]
    } == {3.0, 6.0, 12.0}
    sensitivity_rows = (
        tables["recovery_fold_sensitivity"]
        + tables["clear_gauge_requirement_sensitivity"]
        + tables["policy_staleness_sensitivity"]
    )
    matched_sensitivity_rows = [
        row
        for row in sensitivity_rows
        if row["policy"] == "matched_budget_selected"
    ]
    assert matched_sensitivity_rows
    for row in matched_sensitivity_rows:
        assert row["target_acquisitions"] is not None
        assert row["realised_acquisitions"] is not None
        assert row["budget_gap"] == (
            row["realised_acquisitions"] - row["target_acquisitions"]
        )
        assert row["selected_nearest_realised_acquisition_count"] is True
        assert row["matched_budget_selection"][
            "selected_realised_acquisitions"
        ] == row["realised_acquisitions"]
        offset_rows = row["matched_budget_offset_rows"]
        offset_ranges = row["matched_budget_offset_outcome_ranges"]
        assert len(offset_rows) == row["matched_budget_selection"][
            "n_phase_offsets"
        ]
        assert offset_ranges["n_phase_offsets"] == len(offset_rows)
        assert sum(
            candidate["selected_nearest_realised_acquisition_count"] is True
            for candidate in offset_rows
        ) == 1
        assert offset_ranges["realised_acquisitions"]["min"] == min(
            candidate["realised_acquisitions"] for candidate in offset_rows
        )
        assert offset_ranges["realised_acquisitions"]["max"] == max(
            candidate["realised_acquisitions"] for candidate in offset_rows
        )
        assert "outcomes never affect selection" in offset_ranges["selection_rule"]
    assert "watcher/claims/validate.py" in result["implementation_sha256"]
    assert (
        "skills/county-flood-data/county_flood/sources/stages.py"
        in result["implementation_sha256"]
    )
    assert result["python"]["version"]
    with pytest.raises(FileExistsError, match="must be absent or empty"):
        generate_study(
            manifest_path=manifest_path,
            out_dir=out_dir,
            cadence_hours=(1.0,),
            offset_step_seconds=3600,
            evaluation_interval_seconds=900,
        )


def test_preflight_fails_closed_on_declared_input_hash_pairing(tmp_path):
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_corpus()), encoding="utf-8")
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps({"anchors": []}), encoding="utf-8")
    base = {
        "path": str(corpus_path),
        "county": "Test",
        "state": "TX",
        "fips": "48000",
        "event": "Synthetic",
        "analysis_role": "episode_evaluation",
        "analysis_start": _iso(0),
        "analysis_end": _iso(4),
        "sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
    }

    with pytest.raises(ValueError, match="truth reference path requires"):
        _preflight(
            {**base, "truth_path": str(truth_path)}, interval_seconds=900
        )
    with pytest.raises(ValueError, match="hash declared without a path"):
        _preflight(
            {**base, "truth_sha256": "0" * 64}, interval_seconds=900
        )
    with pytest.raises(ValueError, match="checksum mismatch for truth reference"):
        _preflight(
            {
                **base,
                "truth_path": str(truth_path),
                "truth_sha256": "0" * 64,
            },
            interval_seconds=900,
        )
    with pytest.raises(ValueError, match="datum calibration path requires"):
        _preflight(
            {
                **base,
                "datum_qc": {"calibration_path": str(calibration_path)},
            },
            interval_seconds=900,
        )


def test_study_generation_freezes_and_restores_hostile_replay_environment(
    tmp_path, monkeypatch
):
    corpus = _corpus(stages=(0.5, 0.5, 0.5, 0.5))
    corpus_path = tmp_path / "environment_county.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    manifest_path = tmp_path / "environment_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "sentinel-study-corpus-manifest/1",
                "artifacts": [
                    {
                        "path": str(corpus_path),
                        "county": "Test",
                        "state": "TX",
                        "fips": "48000",
                        "event": "Synthetic",
                        "analysis_start": _iso(0),
                        "analysis_end": _iso(4),
                        "sha256": hashlib.sha256(
                            corpus_path.read_bytes()
                        ).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    for name in FROZEN_REPLAY_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    baseline_dir = tmp_path / "baseline"
    baseline = generate_study(
        manifest_path=manifest_path,
        out_dir=baseline_dir,
        cadence_hours=(1.0,),
        offset_step_seconds=3600,
        evaluation_interval_seconds=900,
    )
    assert all(name not in os.environ for name in FROZEN_REPLAY_ENVIRONMENT)

    hostile = {
        name: f"hostile-{index}"
        for index, name in enumerate(FROZEN_REPLAY_ENVIRONMENT, start=1)
    }
    hostile.update(
        {
            "SENTINEL_ABLATION": "none",
            "SENTINEL_TIER_WEIGHT": "0",
            "SENTINEL_READING_MAX_AGE_HOURS": "0",
            "SENTINEL_ABLATED_CONFIDENCE": "0.01",
            "SENTINEL_WATERSHED_NEIGHBOURS": "on",
            "SENTINEL_WATERSHED_LOCATION_PRECISION": "0.01",
            "COUNTY_FLOOD_MIN_GAUGES": "99",
            "COUNTY_FLOOD_MAX_GAUGES": "1",
            "COUNTY_FLOOD_STAGE_RISE_FT": "999",
            "COUNTY_FLOOD_STAGE_FALL_FT": "999",
            "COUNTY_FLOOD_STAGE_PERSIST_FT": "999",
            "COUNTY_FLOOD_FLOW_RISE_PCT": "999",
            "COUNTY_FLOOD_FLOW_FALL_PCT": "999",
            "COUNTY_FLOOD_SUPPLEMENTARY_CONFIDENCE": "0.99",
            "REPORT_GAUGE_STAGE_THRESHOLD_FT": "999",
            "REPORT_GAUGE_FLOW_THRESHOLD_PCT": "999",
        }
    )
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    hostile_dir = tmp_path / "hostile"
    hardened = generate_study(
        manifest_path=manifest_path,
        out_dir=hostile_dir,
        cadence_hours=(1.0,),
        offset_step_seconds=3600,
        evaluation_interval_seconds=900,
    )

    assert {name: os.environ.get(name) for name in hostile} == hostile
    assert hardened == baseline
    for relative in (
        "aggregate.json",
        "tables.json",
        "counties/test_48000/result.json",
        "artifact_manifest.json",
    ):
        assert (hostile_dir / relative).read_bytes() == (
            baseline_dir / relative
        ).read_bytes()
    contract = hardened["config"]["frozen_replay_environment"]
    assert contract == FROZEN_REPLAY_ENVIRONMENT
    assert hardened["config"]["environment_sensitivity_overrides"] == {
        "COUNTY_FLOOD_MIN_GAUGES": [1, 3, 5, 8],
        "SENTINEL_READING_MAX_AGE_HOURS": [3.0, 6.0, 12.0],
    }
    assert hardened["config"]["replay_argument_sensitivity_overrides"] == {
        "max_staleness_hours": [3.0, 6.0, 12.0],
    }


def test_policy_staleness_moves_payload_and_claim_freshness_together(
    tmp_path, monkeypatch
):
    corpus = _corpus(stages=(0.5, 0.5, 0.5, 0.5))
    corpus_path = tmp_path / "staleness_county.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    manifest_path = tmp_path / "staleness_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "sentinel-study-corpus-manifest/1",
                "artifacts": [
                    {
                        "path": str(corpus_path),
                        "county": "Test",
                        "state": "TX",
                        "fips": "48000",
                        "event": "Synthetic",
                        "analysis_start": _iso(0),
                        "analysis_end": _iso(4),
                        "sha256": hashlib.sha256(
                            corpus_path.read_bytes()
                        ).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    real_replay_run = study_module.replay_run
    observed_freshness = []

    def recording_replay_run(**kwargs):
        observed_freshness.append(
            (
                kwargs.get("max_staleness_hours"),
                os.environ.get("SENTINEL_READING_MAX_AGE_HOURS"),
            )
        )
        return real_replay_run(**kwargs)

    monkeypatch.setattr(study_module, "replay_run", recording_replay_run)
    result = study_module.generate(
        manifest_path=manifest_path,
        out_dir=tmp_path / "artifacts",
        cadence_hours=(1.0,),
        offset_step_seconds=3600,
        evaluation_interval_seconds=900,
    )

    observed_arms = {
        (float(argument_hours), float(environment_hours))
        for argument_hours, environment_hours in observed_freshness
    }
    assert {(3.0, 3.0), (6.0, 6.0), (12.0, 12.0)} <= observed_arms
    assert all(argument == environment for argument, environment in observed_arms)
    county_result = json.loads(
        (
            tmp_path
            / "artifacts"
            / result["county_results"][0]["result_path"]
        ).read_text(encoding="utf-8")
    )
    for row in county_result["policy_staleness_sensitivity"]:
        effective = row["effective_policy_parameters"]
        assert effective["policy_max_staleness_hours"] == row[
            "sensitivity_value"
        ]
        assert effective["claim_reading_max_age_hours"] == row[
            "sensitivity_value"
        ]


def test_limited_truth_coverage_requires_manifest_review_and_rationale(tmp_path):
    corpus = _corpus(stages=(0.5,))
    corpus_path = tmp_path / "limited.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    base = {
        "path": str(corpus_path),
        "county": "Test",
        "state": "TX",
        "fips": "48000",
        "event": "Synthetic",
        "analysis_start": _iso(0),
        "analysis_end": _iso(4),
        "sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
    }

    unreviewed = _preflight(base, interval_seconds=900)
    assert unreviewed["blocked"] is True
    assert unreviewed["warnings"][0]["code"] == (
        "limited_primary_truth_coverage_unreviewed"
    )

    reviewed = _preflight(
        {
            **base,
            "limited_truth_coverage_reviewed": True,
            "limited_truth_coverage_rationale": (
                "Conservative all-reference-gauges-fresh rule censors dropouts."
            ),
        },
        interval_seconds=900,
    )
    assert reviewed["blocked"] is False
    assert reviewed["warnings"][0]["blocking"] is False
    assert reviewed["warnings"][0]["code"] == (
        "limited_primary_truth_coverage_reviewed"
    )
