"""Integration tests for the hybrid verification-triggered replay schedule."""

from __future__ import annotations

import json
import os

import pytest

from eval.ground_truth import apply_threshold_compatibility
from eval.metrics import evaluate
from eval.run_replay import run as run_replay
from eval.run_study import FROZEN_REPLAY_ENVIRONMENT, _temporary_environment
from eval.vtec_trigger import (
    TRIGGER_CORPUS_SCHEMA,
    load_trigger_corpus,
    normalize_trigger_events,
    product_id_transmission_time,
    trigger_events_sorted,
    zone_name_matches_county,
)

WINDOW_START = "2017-01-01T00:00:00Z"
WINDOW_END = "2017-01-02T00:00:00Z"

AUDIT_KEYS = (
    "acquisition_due_reason",
    "verification_burst_active",
    "verification_burst_until",
    "trigger_new_event_keys",
)

TRIGGER = {
    "enabled": True,
    "qualifying": ["FF.W", "FA.W", "FL.W"],
    "burst_interval_seconds": 7200,
    "burst_duration_seconds": 21600,
}


def _iso(hour: int, minute: int = 0) -> str:
    return f"2017-01-01T{hour:02d}:{minute:02d}:00Z"


def _corpus(flood_hours=(13, 14, 15, 16)):
    return {
        "schema": "sentinel-replay-corpus/1",
        "county": "Test",
        "state": "TX",
        "fips": "48000",
        "window": {"start": "2017-01-01", "end": "2017-01-02"},
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
                    {
                        "t": _iso(hour),
                        "stage": 2.5 if hour in flood_hours else 0.5,
                    }
                    for hour in range(25)
                ],
            }
        ],
    }


def _trigger_corpus(events):
    return {
        "schema": TRIGGER_CORPUS_SCHEMA,
        "county": "Test",
        "state": "TX",
        "fips": "48000",
        "window": {"start": "2017-01-01", "end": "2017-01-02"},
        "events": events,
    }


def _event(key="2017-TST-FF-W-0001", available=_iso(13, 7), begins=None):
    return {
        "event_key": key,
        "wfo": "TST",
        "phenomena": "FF",
        "significance": "W",
        "etn": 1,
        "available_at": available,
        "begins_at": begins or available,
    }


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _run(tmp_path, *, corpus=None, trigger=None, trigger_events=None, **kwargs):
    corpus = corpus or _corpus()
    corpus_path = _write(tmp_path, "corpus.json", corpus)
    if trigger_events is not None:
        kwargs["trigger_corpus_path"] = _write(
            tmp_path, "triggers.json", _trigger_corpus(trigger_events)
        )
    if trigger is not None:
        kwargs["trigger"] = trigger
    with _temporary_environment(FROZEN_REPLAY_ENVIRONMENT):
        return run_replay(
            corpus_path=corpus_path,
            county="Test",
            state="TX",
            fips="48000",
            start=WINDOW_START,
            end=WINDOW_END,
            max_staleness_hours=6.0,
            **kwargs,
        )


def _normalized_ticks(run):
    out = []
    for tick in run["ticks"]:
        clean = {k: v for k, v in tick.items() if k not in AUDIT_KEYS}
        clean["claims"] = [
            {k: v for k, v in claim.items() if k != "claim_id"}
            for claim in tick["claims"]
        ]
        out.append(clean)
    return out


def test_trigger_disabled_poll_walk_equals_legacy_adaptive(tmp_path):
    legacy = _run(tmp_path)
    pollwalk = _run(tmp_path, trigger={"enabled": False})
    assert [t["t"] for t in legacy["ticks"]] == [t["t"] for t in pollwalk["ticks"]]
    assert _normalized_ticks(legacy) == _normalized_ticks(pollwalk)
    accounting = pollwalk["verification_trigger"]
    assert accounting["trigger_stream_consultations"] == 0
    assert accounting["trigger_attributable_acquisitions"] == 0
    assert pollwalk["schedule"]["kind"] == "phase_adaptive"


def test_hybrid_observes_episode_the_adaptive_schedule_misses(tmp_path):
    corpus, _ = apply_threshold_compatibility(_corpus())
    adaptive = _run(tmp_path)
    hybrid = _run(tmp_path, trigger=TRIGGER, trigger_events=[_event()])
    with _temporary_environment(FROZEN_REPLAY_ENVIRONMENT):
        adaptive_eval = evaluate(adaptive, corpus=corpus, events=[])
        hybrid_eval = evaluate(hybrid, corpus=corpus, events=[])
    assert (
        adaptive_eval["rq2_event_episodes"]["episodes_observed_by_schedule"] == 0
    )
    assert hybrid_eval["rq2_event_episodes"]["episodes_observed_by_schedule"] == 1
    immediate = next(
        t
        for t in hybrid["ticks"]
        if t.get("acquisition_due_reason") == "verification_immediate"
    )
    # The warning became available at 13:07; the immediate verification lands
    # on the next 15-minute poll, never earlier.
    assert immediate["t"] == _iso(13, 15)
    assert immediate["trigger_new_event_keys"] == ["2017-TST-FF-W-0001"]
    assert immediate["verification_burst_active"] is True


def test_acquisition_reason_accounting_is_complete(tmp_path):
    hybrid = _run(tmp_path, trigger=TRIGGER, trigger_events=[_event()])
    accounting = hybrid["verification_trigger"]
    assert sum(accounting["acquisitions_by_reason"].values()) == hybrid["n_ticks"]
    assert accounting["trigger_attributable_acquisitions"] == (
        accounting["acquisitions_by_reason"].get("verification_immediate", 0)
        + accounting["acquisitions_by_reason"].get("verification_burst", 0)
    )
    assert accounting["trigger_stream_consultations"] == 96  # 24 h of 15-min polls
    assert accounting["authority"] == "acquisition_scheduling_only"
    for tick in hybrid["ticks"]:
        for key in AUDIT_KEYS:
            assert key in tick


def test_trigger_cannot_change_verdict_phase_or_alert_without_official_evidence(
    tmp_path,
):
    # Gauges never reach action stage; warnings alone must change nothing but
    # the acquisition schedule.
    quiet = _corpus(flood_hours=())
    hybrid = _run(
        tmp_path,
        corpus=quiet,
        trigger=TRIGGER,
        trigger_events=[_event(), _event(key="2017-TST-FF-W-0002", available=_iso(20, 7))],
    )
    assert hybrid["verification_trigger"]["trigger_attributable_acquisitions"] > 0
    for tick in hybrid["ticks"]:
        assert tick["phase_after"] == "early_watch"
        assert tick["alert_level"] in ("none", "informational")
        assert tick["is_flooding"] is False
        assert tick["n_alert_claims"] == 0


def test_no_lookahead_into_the_trigger_stream(tmp_path):
    hybrid = _run(tmp_path, trigger=TRIGGER, trigger_events=[_event()])
    available = _iso(13, 7)
    for tick in hybrid["ticks"]:
        if tick["t"] < available:
            assert tick["trigger_new_event_keys"] == []
            assert tick["verification_burst_active"] is False
    first_seen = hybrid["verification_trigger"]["events_first_seen"]
    assert len(first_seen) == 1
    assert first_seen[0]["first_seen_poll"] >= available
    assert hybrid["verification_trigger"]["ignored_future_events"] == 0


def test_event_beyond_window_never_participates(tmp_path):
    hybrid = _run(
        tmp_path,
        trigger=TRIGGER,
        trigger_events=[_event(available="2017-01-03T00:00:00Z")],
    )
    assert hybrid["verification_trigger"]["events_first_seen"] == []
    assert hybrid["verification_trigger"]["trigger_attributable_acquisitions"] == 0


def test_component_ablation_reports_only_fires_it_acted_on(tmp_path):
    events = [_event()]
    full = _run(tmp_path, trigger=TRIGGER, trigger_events=events)
    burst_only = _run(
        tmp_path,
        trigger={**TRIGGER, "immediate_fire_enabled": False},
        trigger_events=events,
    )
    assert full["verification_trigger"]["immediate_fires"] == 1
    # The suppressed component must not be credited to the arm that lacks it.
    assert burst_only["verification_trigger"]["immediate_fires"] == 0
    assert (
        burst_only["verification_trigger"][
            "immediate_fires_before_component_ablation"
        ]
        == 1
    )
    assert (
        burst_only["verification_trigger"]["acquisitions_by_reason"].get(
            "verification_immediate", 0
        )
        == 0
    )


def test_trigger_metrics_expose_fold_truncated_span(tmp_path):
    from eval.trigger_metrics import trigger_accounting_metrics

    corpus, _ = apply_threshold_compatibility(_corpus())
    hybrid = _run(tmp_path, trigger=TRIGGER, trigger_events=[_event()])
    metrics = trigger_accounting_metrics(hybrid, corpus=corpus)
    assert "run_terminated_at" in metrics
    assert "reference_episodes_after_termination" in metrics
    # This run reaches the window end rather than folding.
    assert metrics["run_terminated_at"] is None
    assert metrics["reference_episodes_after_termination"] == 0


def test_stale_official_sources_after_trigger_yield_no_verdict(tmp_path):
    # Gauges stop reporting at hour 8; a warning at 13:07 forces verification
    # acquisitions, but with every reading beyond the 6 h staleness window the
    # system must abstain (insufficient data), never assert or escalate.
    corpus = _corpus(flood_hours=())
    corpus["sites"][0]["observations"] = corpus["sites"][0]["observations"][:9]
    hybrid = _run(tmp_path, corpus=corpus, trigger=TRIGGER, trigger_events=[_event()])
    post = [t for t in hybrid["ticks"] if t["t"] >= _iso(14, 15)]
    assert post, "expected verification acquisitions after the trigger"
    for tick in post:
        assert tick["gauges_with_data"] == 0
        assert tick["regional_verdict"] == "insufficient_data"
        assert tick["is_flooding"] is False
        assert tick["phase_after"] == "early_watch"


def test_v3_manifest_validation_fails_closed(tmp_path):
    from eval.run_study_v3 import _validate_manifest

    protocol = tmp_path / "protocol.md"
    protocol.write_text("frozen", encoding="utf-8")
    import hashlib

    proto_hash = hashlib.sha256(b"frozen").hexdigest()
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_corpus()), encoding="utf-8")
    corpus_hash = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    trigger_path = tmp_path / "trig.json"
    trigger_path.write_text(json.dumps(_trigger_corpus([_event()])), "utf-8")
    trigger_hash = hashlib.sha256(trigger_path.read_bytes()).hexdigest()

    manifest = {
        "schema": "sentinel-study-corpus-manifest-v3/1",
        "protocol": str(protocol),
        "protocol_sha256": proto_hash,
        "selection": str(protocol),
        "selection_sha256": proto_hash,
        "dev_grid": str(protocol),
        "dev_grid_sha256": proto_hash,
        "frozen_trigger_policy": {
            "qualifying": ["FF.W"],
            "burst_interval_seconds": 7200,
            "burst_duration_seconds": 86400,
            "poll_interval_seconds": 900,
        },
        "artifacts": [
            {
                "path": str(corpus_path),
                "county": "Test",
                "state": "TX",
                "fips": "48000",
                "analysis_role": "episode_evaluation",
                "analysis_start": WINDOW_START,
                "analysis_end": WINDOW_END,
                "sha256": corpus_hash,
                "trigger_corpus": str(trigger_path),
                "trigger_corpus_sha256": trigger_hash,
            }
        ],
    }

    def _write_manifest(payload):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    assert _validate_manifest(_write_manifest(manifest))["artifacts"]

    tampered = json.loads(json.dumps(manifest))
    tampered["protocol_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum mismatch"):
        _validate_manifest(_write_manifest(tampered))

    tampered = json.loads(json.dumps(manifest))
    tampered["artifacts"][0]["trigger_corpus_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum mismatch"):
        _validate_manifest(_write_manifest(tampered))

    tampered = json.loads(json.dumps(manifest))
    del tampered["artifacts"][0]["trigger_corpus"]
    with pytest.raises(ValueError, match="missing"):
        _validate_manifest(_write_manifest(tampered))

    tampered = json.loads(json.dumps(manifest))
    del tampered["frozen_trigger_policy"]["burst_duration_seconds"]
    with pytest.raises(ValueError, match="frozen_trigger_policy"):
        _validate_manifest(_write_manifest(tampered))


def test_v3_generator_refuses_nonempty_output_dir(tmp_path):
    from eval.run_study_v3 import generate

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="absent or empty"):
        generate(manifest_path=tmp_path / "missing.json", out_dir=out_dir)


def test_boundary_trigger_at_scheduled_instant_counts_as_scheduled(tmp_path):
    hybrid = _run(tmp_path, trigger=TRIGGER, trigger_events=[_event(available=_iso(6))])
    at_six = next(t for t in hybrid["ticks"] if t["t"] == _iso(6))
    assert at_six["acquisition_due_reason"] == "scheduled"
    assert at_six["trigger_new_event_keys"] == ["2017-TST-FF-W-0001"]
    at_eight = next(t for t in hybrid["ticks"] if t["t"] == _iso(8))
    assert at_eight["acquisition_due_reason"] == "verification_burst"


def test_enabled_trigger_requires_corpus(tmp_path):
    with pytest.raises(ValueError, match="frozen trigger corpus"):
        _run(tmp_path, trigger=TRIGGER)


def test_trigger_rejects_fixed_interval_override(tmp_path):
    with pytest.raises(ValueError, match="phase-adaptive"):
        _run(
            tmp_path,
            trigger=TRIGGER,
            trigger_events=[_event()],
            fixed_interval_seconds=3600,
        )


def test_trigger_requires_role_layer(tmp_path):
    saved = os.environ.get("SENTINEL_ABLATION")
    os.environ["SENTINEL_ABLATION"] = "no_role"
    try:
        with pytest.raises(ValueError, match="role-layer"):
            run_replay(
                corpus_path=_write(tmp_path, "corpus.json", _corpus()),
                county="Test",
                state="TX",
                fips="48000",
                start=WINDOW_START,
                end=WINDOW_END,
                trigger=TRIGGER,
                trigger_corpus_path=_write(
                    tmp_path, "triggers.json", _trigger_corpus([_event()])
                ),
            )
    finally:
        if saved is None:
            os.environ.pop("SENTINEL_ABLATION", None)
        else:
            os.environ["SENTINEL_ABLATION"] = saved


def test_trigger_corpus_loader_rejects_wrong_schema(tmp_path):
    bad = _trigger_corpus([_event()])
    bad["schema"] = "something-else/9"
    path = _write(tmp_path, "bad.json", bad)
    with pytest.raises(ValueError, match="schema"):
        load_trigger_corpus(path)


def test_trigger_events_sorted_requires_identity_fields():
    with pytest.raises(ValueError, match="event_key/available_at"):
        trigger_events_sorted(_trigger_corpus([{"phenomena": "FF"}]))


def test_product_id_transmission_time_parsing():
    parsed = product_id_transmission_time("201708261043-KHGX-WGUS54-FFWHGX")
    assert parsed is not None
    assert parsed.isoformat() == "2017-08-26T10:43:00+00:00"
    assert product_id_transmission_time("garbage") is None
    assert product_id_transmission_time(None) is None


def test_normalize_trigger_events_dedupes_and_counts_anomalies():
    raw = [
        {
            "phenomena": "FF",
            "significance": "W",
            "eventid": 43,
            "wfo": "HGX",
            "ugc": "TXC201",
            "issue": "2017-08-26T10:43:00Z",
            "expire": "2017-08-26T13:45:00Z",
            "product_id": "201708261043-KHGX-WGUS54-FFWHGX",
        },
        {  # same event via a zone UGC, later product: merged, earliest kept
            "phenomena": "FF",
            "significance": "W",
            "eventid": 43,
            "wfo": "HGX",
            "ugc": "TXZ213",
            "issue": "2017-08-26T10:43:00Z",
            "expire": "2017-08-26T13:45:00Z",
            "product_id": "201708261100-KHGX-WGUS54-FFWHGX",
        },
        {"phenomena": "TO", "significance": "W", "eventid": 9, "wfo": "HGX"},
        {
            "phenomena": "FL",
            "significance": "W",
            "eventid": 40,
            "wfo": "HGX",
            "issue": "2017-08-26T11:58:00Z",
            "expire": "2017-08-26T22:44:00Z",
            "product_id": "no-timestamp-here",
        },
    ]
    events, anomalies = normalize_trigger_events(raw)
    assert len(events) == 1
    assert events[0]["available_at"] == "2017-08-26T10:43:00Z"
    assert sorted(events[0]["ugcs"]) == ["TXC201", "TXZ213"]
    assert events[0]["begins_at"] == "2017-08-26T10:43:00Z"
    assert anomalies["non_flood_phenomena"] == 1
    assert anomalies["missing_product_timestamp"] == 1


def test_zone_name_matching_enforces_word_boundaries():
    assert zone_name_matches_county("Harris", "Harris")
    assert zone_name_matches_county("Inland Harris", "Harris")
    assert zone_name_matches_county("Coastal Harris", "Harris")
    assert not zone_name_matches_county("Harrison", "Harris")
    assert not zone_name_matches_county("North Harrison", "Harris")
    assert not zone_name_matches_county("Somewhere Else Harris County Line", "Harris")


ADDITIVE = {**TRIGGER, "anchor_mode": "additive"}


def test_additive_mode_never_moves_the_base_schedule(tmp_path):
    # Quiet corpus: no escalation, so the phase trajectory is identical and
    # the additive hybrid's acquisitions must be a strict superset of the
    # adaptive schedule at exactly the adaptive instants.
    quiet = _corpus(flood_hours=())
    adaptive = _run(tmp_path, corpus=quiet)
    additive = _run(tmp_path, corpus=quiet, trigger=ADDITIVE, trigger_events=[_event()])
    adaptive_times = {t["t"] for t in adaptive["ticks"]}
    additive_times = {t["t"] for t in additive["ticks"]}
    assert adaptive_times <= additive_times
    assert len(additive_times) > len(adaptive_times)
    # Every adaptive instant is credited to the base clock in the hybrid.
    by_time = {t["t"]: t for t in additive["ticks"]}
    for when in sorted(adaptive_times):
        assert by_time[when]["acquisition_due_reason"] in ("first_run", "scheduled")
    assert additive["verification_trigger"]["policy"]["anchor_mode"] == "additive"


def test_additive_trigger_disabled_equals_adaptive(tmp_path):
    legacy = _run(tmp_path)
    pollwalk = _run(tmp_path, trigger={"enabled": False, "anchor_mode": "additive"})
    assert [t["t"] for t in legacy["ticks"]] == [t["t"] for t in pollwalk["ticks"]]
    assert _normalized_ticks(legacy) == _normalized_ticks(pollwalk)


def test_unknown_anchor_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="anchor_mode"):
        _run(tmp_path, trigger={**TRIGGER, "anchor_mode": "sideways"},
             trigger_events=[_event()])


def test_trigger_refuses_fixed_base_unless_opted_in(tmp_path):
    with pytest.raises(ValueError, match="fixed-interval override"):
        _run(tmp_path, trigger=TRIGGER, trigger_events=[_event()], fixed_interval_seconds=21600)


def test_phase_free_control_keeps_fixed_base_and_adds_trigger_looks(tmp_path):
    quiet = _corpus(flood_hours=())
    control = _run(
        tmp_path,
        corpus=quiet,
        trigger={**TRIGGER, "base_schedule": "fixed"},
        trigger_events=[],
        fixed_interval_seconds=21600,
    )
    times = [tick["t"] for tick in control["ticks"]]
    assert times == [_iso(0), _iso(6), _iso(12), _iso(18)]
    policy = control["verification_trigger"]["policy"]
    assert policy["base_schedule"] == "fixed"
    assert policy["base_fixed_interval_seconds"] == 21600

    with_warning = _run(
        tmp_path,
        corpus=quiet,
        trigger={**TRIGGER, "base_schedule": "fixed"},
        trigger_events=[_event(available=_iso(7))],
        fixed_interval_seconds=21600,
    )
    reasons = with_warning["verification_trigger"]["acquisitions_by_reason"]
    assert reasons.get("first_run", 0) + reasons.get("scheduled", 0) == 4
    assert any(k.startswith("verification") for k in reasons)
    assert len(with_warning["ticks"]) > len(control["ticks"])
