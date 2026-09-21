"""Unit tests for the shared verification-trigger decision function."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from watcher.verification_trigger import (
    TriggerPolicy,
    acquisition_due,
    candidate_events_from_live_alerts,
    empty_trigger_state,
    evaluate_verification_trigger,
    normalize_trigger_state,
    trigger_policy_from_env,
)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2017, 1, 1, hour, minute, tzinfo=timezone.utc)


def _iso(hour: int, minute: int = 0) -> str:
    return _dt(hour, minute).isoformat().replace("+00:00", "Z")


POLICY = TriggerPolicy(
    enabled=True,
    qualifying=(("FF", "W"), ("FA", "W"), ("FL", "W")),
    burst_interval_seconds=7200,
    burst_duration_seconds=21600,
)


def _event(key="2017-HGX-FF-W-0001", available=6, begins=None, ph="FF", sig="W"):
    return {
        "event_key": key,
        "phenomena": ph,
        "significance": sig,
        "available_at": _iso(available),
        "begins_at": _iso(begins) if begins is not None else None,
    }


class TestTriggerDecision(unittest.TestCase):
    def test_disabled_policy_never_fires_or_caps(self):
        policy = TriggerPolicy(enabled=False)
        decision = evaluate_verification_trigger(
            state=None, events=[_event()], now=_dt(7), policy=policy
        )
        self.assertFalse(decision["fired_immediate"])
        self.assertFalse(decision["burst_active"])
        self.assertIsNone(decision["effective_interval_cap_seconds"])
        self.assertEqual(decision["new_qualifying_events"], [])

    def test_new_warning_fires_once_then_deduplicates(self):
        first = evaluate_verification_trigger(
            state=None, events=[_event()], now=_dt(6), policy=POLICY
        )
        self.assertTrue(first["fired_immediate"])
        self.assertTrue(first["burst_active"])
        self.assertEqual(
            first["effective_interval_cap_seconds"], POLICY.burst_interval_seconds
        )
        second = evaluate_verification_trigger(
            state=first["state"], events=[_event()], now=_dt(7), policy=POLICY
        )
        self.assertFalse(second["fired_immediate"])
        self.assertTrue(second["burst_active"])
        self.assertEqual(second["new_qualifying_events"], [])

    def test_stale_event_is_seen_but_activates_nothing(self):
        # Available at 00:00 with a 6 h duration, first seen at 07:00.
        decision = evaluate_verification_trigger(
            state=None, events=[_event(available=0)], now=_dt(7), policy=POLICY
        )
        self.assertFalse(decision["fired_immediate"])
        self.assertFalse(decision["burst_active"])
        self.assertEqual(len(decision["new_qualifying_events"]), 1)
        self.assertTrue(decision["new_qualifying_events"][0]["stale_at_first_sight"])
        self.assertIn("2017-HGX-FF-W-0001", decision["state"]["seen_event_keys"])

    def test_advance_issued_warning_arms_window_at_published_begin(self):
        event = _event(available=2, begins=12)
        early = evaluate_verification_trigger(
            state=None, events=[event], now=_dt(3), policy=POLICY
        )
        # Known and deduplicated, but the hazard window has not begun.
        self.assertFalse(early["burst_active"])
        self.assertFalse(early["fired_immediate"])
        self.assertIn(event["event_key"], early["state"]["seen_event_keys"])
        at_begin = evaluate_verification_trigger(
            state=early["state"], events=[event], now=_dt(12), policy=POLICY
        )
        self.assertTrue(at_begin["burst_active"])
        self.assertTrue(at_begin["fired_immediate"])
        after_end = evaluate_verification_trigger(
            state=at_begin["state"], events=[event], now=_dt(18, 30), policy=POLICY
        )
        self.assertFalse(after_end["burst_active"])

    def test_future_available_event_is_ignored_and_counted(self):
        decision = evaluate_verification_trigger(
            state=None, events=[_event(available=9)], now=_dt(6), policy=POLICY
        )
        self.assertEqual(decision["ignored_future_events"], 1)
        self.assertFalse(decision["burst_active"])
        self.assertNotIn(
            "2017-HGX-FF-W-0001", decision["state"]["seen_event_keys"]
        )

    def test_non_qualifying_events_filtered(self):
        decision = evaluate_verification_trigger(
            state=None,
            events=[
                _event(key="a", ph="FF", sig="A"),
                _event(key="b", ph="TO", sig="W"),
                _event(key="c", ph="FA", sig="Y"),
            ],
            now=_dt(7),
            policy=POLICY,
        )
        self.assertEqual(decision["new_qualifying_events"], [])
        self.assertFalse(decision["burst_active"])

    def test_overlapping_windows_extend_burst(self):
        first = evaluate_verification_trigger(
            state=None, events=[_event(key="a", available=6)], now=_dt(6), policy=POLICY
        )
        second = evaluate_verification_trigger(
            state=first["state"],
            events=[_event(key="a", available=6), _event(key="b", available=10)],
            now=_dt(10),
            policy=POLICY,
        )
        self.assertTrue(second["burst_active"])
        self.assertFalse(second["fired_immediate"])  # burst already active
        self.assertEqual(second["burst_until"], _iso(16))

    def test_exact_boundaries(self):
        # Available exactly at now participates; a window ending exactly at
        # now is inactive (half-open [start, until)).
        at_now = evaluate_verification_trigger(
            state=None, events=[_event(available=6)], now=_dt(6), policy=POLICY
        )
        self.assertTrue(at_now["burst_active"])
        at_end = evaluate_verification_trigger(
            state=None, events=[_event(available=0)], now=_dt(6), policy=POLICY
        )
        self.assertFalse(at_end["burst_active"])

    def test_decision_carries_no_evidence_channel(self):
        decision = evaluate_verification_trigger(
            state=None, events=[_event()], now=_dt(6), policy=POLICY
        )
        self.assertEqual(decision["authority"], "acquisition_scheduling_only")
        for forbidden in (
            "claims",
            "severity",
            "verdict",
            "alert_level",
            "phase",
            "is_flooding",
        ):
            self.assertNotIn(forbidden, decision)

    def test_state_roundtrips_through_json(self):
        decision = evaluate_verification_trigger(
            state=None, events=[_event(available=2, begins=12)], now=_dt(3), policy=POLICY
        )
        restored = normalize_trigger_state(json.loads(json.dumps(decision["state"])))
        self.assertEqual(restored, normalize_trigger_state(decision["state"]))
        replayed = evaluate_verification_trigger(
            state=restored,
            events=[_event(available=2, begins=12)],
            now=_dt(12),
            policy=POLICY,
        )
        self.assertTrue(replayed["fired_immediate"])

    def test_normalize_handles_missing_and_garbage_state(self):
        self.assertEqual(normalize_trigger_state(None), empty_trigger_state())
        self.assertEqual(normalize_trigger_state("junk"), empty_trigger_state())
        garbage = {
            "seen_events": {"a": _iso(1), "b": "nope", "": _iso(1)},
            "burst_windows": [{"start": "nope", "until": "nope"}, "junk"],
            "immediate_fires": "many",
        }
        normalized = normalize_trigger_state(garbage)
        self.assertEqual(normalized["seen_event_keys"], ["a"])
        self.assertEqual(normalized["burst_windows"], [])
        self.assertEqual(normalized["immediate_fires"], 0)

    def test_legacy_keyed_state_migrates_without_wall_clock(self):
        legacy = {"schema": "sentinel-verification-trigger-state/1",
                  "seen_event_keys": ["2017-HGX-FF-W-0001"]}
        first = normalize_trigger_state(legacy)
        self.assertEqual(first["seen_event_keys"], ["2017-HGX-FF-W-0001"])
        # Deterministic: normalizing twice cannot differ, and no wall clock is
        # consulted, so replay stays reproducible.
        self.assertEqual(first, normalize_trigger_state(legacy))

    def test_seen_keys_prune_by_recorded_age_not_key_format(self):
        # An opaque, non-VTEC identifier (as the live CAP stream produces)
        # must prune on age like any other key.
        opaque = {
            "event_key": "urn:oid:2.49.0.1.840.0.abc",
            "phenomena": "FF",
            "significance": "W",
            "available_at": _iso(6),
        }
        first = evaluate_verification_trigger(
            state=None, events=[opaque], now=_dt(6), policy=POLICY
        )
        self.assertIn("urn:oid:2.49.0.1.840.0.abc", first["state"]["seen_events"])
        much_later = datetime(2017, 1, 30, tzinfo=timezone.utc)
        pruned = evaluate_verification_trigger(
            state=first["state"], events=[], now=much_later, policy=POLICY
        )
        self.assertEqual(pruned["state"]["seen_events"], {})
        self.assertEqual(pruned["state"]["seen_event_keys"], [])


class TestAcquisitionDue(unittest.TestCase):
    def test_first_run_is_due(self):
        due, reason = acquisition_due(
            now=_dt(6), last_acquisition_at=None, phase_interval_seconds=21600
        )
        self.assertTrue(due)
        self.assertEqual(reason, "first_run")

    def test_scheduled_at_exact_interval(self):
        due, reason = acquisition_due(
            now=_dt(6), last_acquisition_at=_dt(0), phase_interval_seconds=21600
        )
        self.assertTrue(due)
        self.assertEqual(reason, "scheduled")

    def test_not_due_without_trigger(self):
        due, reason = acquisition_due(
            now=_dt(3), last_acquisition_at=_dt(0), phase_interval_seconds=21600
        )
        self.assertFalse(due)
        self.assertEqual(reason, "not_due")

    def test_burst_cap_shortens_interval(self):
        decision = {"effective_interval_cap_seconds": 7200, "fired_immediate": False}
        due, reason = acquisition_due(
            now=_dt(2),
            last_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=decision,
        )
        self.assertTrue(due)
        self.assertEqual(reason, "verification_burst")

    def test_immediate_fire_is_due_and_attributed(self):
        decision = {"effective_interval_cap_seconds": 7200, "fired_immediate": True}
        due, reason = acquisition_due(
            now=_dt(0, 15),
            last_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=decision,
        )
        self.assertTrue(due)
        self.assertEqual(reason, "verification_immediate")

    def test_scheduled_takes_precedence_over_attribution(self):
        decision = {"effective_interval_cap_seconds": 7200, "fired_immediate": True}
        due, reason = acquisition_due(
            now=_dt(6),
            last_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=decision,
        )
        self.assertTrue(due)
        self.assertEqual(reason, "scheduled")

    def test_trigger_can_never_delay(self):
        # A burst cap longer than the phase interval must not stretch it.
        decision = {"effective_interval_cap_seconds": 86400, "fired_immediate": False}
        due, reason = acquisition_due(
            now=_dt(6),
            last_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=decision,
        )
        self.assertTrue(due)
        self.assertEqual(reason, "scheduled")


class TestPolicyFromEnv(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "SENTINEL_VERIFICATION_TRIGGER",
                "SENTINEL_TRIGGER_QUALIFYING",
                "SENTINEL_TRIGGER_BURST_INTERVAL_SECONDS",
                "SENTINEL_TRIGGER_BURST_DURATION_SECONDS",
            )
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_disabled_by_default(self):
        os.environ.pop("SENTINEL_VERIFICATION_TRIGGER", None)
        self.assertFalse(trigger_policy_from_env().enabled)

    def test_hostile_values_fall_back_to_defaults(self):
        os.environ["SENTINEL_VERIFICATION_TRIGGER"] = "on"
        os.environ["SENTINEL_TRIGGER_QUALIFYING"] = ",,garbage,,"
        os.environ["SENTINEL_TRIGGER_BURST_INTERVAL_SECONDS"] = "NaN?"
        os.environ["SENTINEL_TRIGGER_BURST_DURATION_SECONDS"] = "-5"
        policy = trigger_policy_from_env()
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.qualifying, (("FF", "W"), ("FA", "W"), ("FL", "W")))
        self.assertEqual(policy.burst_interval_seconds, 3600)
        self.assertEqual(policy.burst_duration_seconds, 60)

    def test_non_finite_values_cannot_raise(self):
        # int(float("inf")) raises OverflowError, which would otherwise escape
        # into the heartbeat loop and deny every scheduled acquisition.
        os.environ["SENTINEL_VERIFICATION_TRIGGER"] = "on"
        for bad in ("inf", "-inf", "Infinity", "nan", "1e400"):
            os.environ["SENTINEL_TRIGGER_BURST_INTERVAL_SECONDS"] = bad
            os.environ["SENTINEL_TRIGGER_BURST_DURATION_SECONDS"] = bad
            policy = trigger_policy_from_env()
            self.assertEqual(policy.burst_interval_seconds, 3600)
            self.assertEqual(policy.burst_duration_seconds, 21600)

    def test_explicit_configuration(self):
        os.environ["SENTINEL_VERIFICATION_TRIGGER"] = "on"
        os.environ["SENTINEL_TRIGGER_QUALIFYING"] = "FF.W , fl.w"
        os.environ["SENTINEL_TRIGGER_BURST_INTERVAL_SECONDS"] = "7200"
        os.environ["SENTINEL_TRIGGER_BURST_DURATION_SECONDS"] = "86400"
        policy = trigger_policy_from_env()
        self.assertEqual(policy.qualifying, (("FF", "W"), ("FL", "W")))
        self.assertEqual(policy.burst_interval_seconds, 7200)
        self.assertEqual(policy.burst_duration_seconds, 86400)


class TestLiveAlertMapping(unittest.TestCase):
    def test_maps_actual_flood_alerts_only(self):
        alerts = [
            {
                "id": "urn:oid:1",
                "event": "Flash Flood Warning",
                "effective": _iso(6),
                "status": "Actual",
            },
            {
                "id": "urn:oid:2",
                "event": "Tornado Warning",
                "effective": _iso(6),
                "status": "Actual",
            },
            {
                "id": "urn:oid:3",
                "event": "Flood Warning",
                "effective": _iso(6),
                "status": "Test",
            },
            {"id": "", "event": "Flood Warning", "effective": _iso(6)},
        ]
        events = candidate_events_from_live_alerts(alerts)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_key"], "urn:oid:1")
        self.assertEqual(events[0]["phenomena"], "FF")
        self.assertEqual(events[0]["significance"], "W")
        self.assertEqual(events[0]["available_at"], _iso(6))
        self.assertEqual(events[0]["begins_at"], _iso(6))

    def test_published_onset_anchors_the_live_burst_window(self):
        events = candidate_events_from_live_alerts(
            [
                {
                    "id": "urn:oid:9",
                    "event": "Flood Warning",
                    "effective": _iso(2),
                    "onset": _iso(12),
                    "status": "Actual",
                }
            ]
        )
        self.assertEqual(events[0]["available_at"], _iso(2))
        self.assertEqual(events[0]["begins_at"], _iso(12))
        # Same anchoring the replay corpus produces from the VTEC begin time.
        decision = evaluate_verification_trigger(
            state=None, events=events, now=_dt(3), policy=POLICY
        )
        self.assertFalse(decision["burst_active"])
        at_onset = evaluate_verification_trigger(
            state=decision["state"], events=events, now=_dt(12), policy=POLICY
        )
        self.assertTrue(at_onset["burst_active"])


class TestLiveDueParity(unittest.TestCase):
    def test_mission_cycle_due_uses_shared_rule(self):
        from watcher.cli import _mission_cycle_due

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        mission = {
            "mission_id": "m1",
            "current_event_phase": "early_watch",
            "last_heartbeat_at": recent,
        }
        due, reason = _mission_cycle_due(mission)
        self.assertFalse(due)
        burst = {"effective_interval_cap_seconds": 7200, "fired_immediate": False}
        due, reason = _mission_cycle_due(mission, burst)
        self.assertFalse(due)  # 1 h elapsed < 2 h burst cap
        fired = {"effective_interval_cap_seconds": 7200, "fired_immediate": True}
        due, reason = _mission_cycle_due(mission, fired)
        self.assertTrue(due)
        self.assertIn("verification_immediate", reason)
        stale = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        mission["last_heartbeat_at"] = stale
        due, reason = _mission_cycle_due(mission, burst)
        self.assertTrue(due)
        self.assertIn("verification_burst", reason)




class TestAcquisitionDueAdditive(unittest.TestCase):
    def _decision(self, *, cap=7200, fired=False):
        return {"effective_interval_cap_seconds": cap, "fired_immediate": fired}

    def test_first_run_credits_base(self):
        from watcher.verification_trigger import acquisition_due_additive

        due, reason, credits = acquisition_due_additive(
            now=_dt(0),
            last_any_acquisition_at=None,
            last_base_acquisition_at=None,
            phase_interval_seconds=21600,
        )
        self.assertEqual((due, reason, credits), (True, "first_run", True))

    def test_base_clock_ignores_verification_acquisitions(self):
        from watcher.verification_trigger import acquisition_due_additive

        # Base ran at 00:00; a verification acquisition ran at 05:00. The base
        # clock must still fire at 06:00, not at 11:00.
        due, reason, credits = acquisition_due_additive(
            now=_dt(6),
            last_any_acquisition_at=_dt(5),
            last_base_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=self._decision(),
        )
        self.assertEqual((due, reason, credits), (True, "scheduled", True))

    def test_burst_caps_gap_since_any_acquisition(self):
        from watcher.verification_trigger import acquisition_due_additive

        due, reason, credits = acquisition_due_additive(
            now=_dt(4),
            last_any_acquisition_at=_dt(2),
            last_base_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=self._decision(),
        )
        self.assertEqual((due, reason, credits), (True, "verification_burst", False))
        # A recent verification acquisition suppresses the next burst slot.
        due, reason, credits = acquisition_due_additive(
            now=_dt(3),
            last_any_acquisition_at=_dt(2),
            last_base_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=self._decision(),
        )
        self.assertEqual((due, reason, credits), (False, "not_due", False))

    def test_immediate_fire_never_credits_base(self):
        from watcher.verification_trigger import acquisition_due_additive

        due, reason, credits = acquisition_due_additive(
            now=_dt(0, 15),
            last_any_acquisition_at=_dt(0),
            last_base_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=self._decision(fired=True),
        )
        self.assertEqual((due, reason, credits), (True, "verification_immediate", False))

    def test_scheduled_takes_precedence_and_credits_base(self):
        from watcher.verification_trigger import acquisition_due_additive

        due, reason, credits = acquisition_due_additive(
            now=_dt(6),
            last_any_acquisition_at=_dt(5),
            last_base_acquisition_at=_dt(0),
            phase_interval_seconds=21600,
            trigger_decision=self._decision(fired=True),
        )
        self.assertEqual((due, reason, credits), (True, "scheduled", True))


if __name__ == "__main__":
    unittest.main()


class TestLiveAdditiveDueParity(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("SENTINEL_TRIGGER_ANCHOR_MODE")
        os.environ["SENTINEL_TRIGGER_ANCHOR_MODE"] = "additive"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SENTINEL_TRIGGER_ANCHOR_MODE", None)
        else:
            os.environ["SENTINEL_TRIGGER_ANCHOR_MODE"] = self._saved

    def test_base_clock_fires_despite_recent_verification_acquisition(self):
        from watcher.cli import _mission_cycle_due

        now = datetime.now(timezone.utc)
        mission = {
            "mission_id": "m1",
            "current_event_phase": "early_watch",
            # A verification acquisition ran one hour ago...
            "last_heartbeat_at": (now - timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        decision = {
            "effective_interval_cap_seconds": 7200,
            "fired_immediate": False,
            # ...but the base clock last ran seven hours ago.
            "state": {
                "last_base_acquisition_at": (now - timedelta(hours=7))
                .isoformat()
                .replace("+00:00", "Z")
            },
        }
        due, reason = _mission_cycle_due(mission, decision)
        self.assertTrue(due)
        self.assertIn("scheduled", reason)
        self.assertTrue(decision["_credits_base_clock"])

    def test_verification_acquisition_does_not_credit_base(self):
        from watcher.cli import _mission_cycle_due

        now = datetime.now(timezone.utc)
        stamp = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")
        mission = {
            "mission_id": "m1",
            "current_event_phase": "early_watch",
            "last_heartbeat_at": stamp(3),
        }
        decision = {
            "effective_interval_cap_seconds": 7200,
            "fired_immediate": False,
            "state": {"last_base_acquisition_at": stamp(4)},
        }
        due, reason = _mission_cycle_due(mission, decision)
        self.assertTrue(due)
        self.assertIn("verification_burst", reason)
        self.assertFalse(decision["_credits_base_clock"])
