"""Tests for the ontology ablation switches used by the evaluation.

Four configurations must be reachable by environment variable alone, and the
default (unset) must behave exactly like the shipped system.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from watcher.claims.confidence import (
    apply_belief_ablation,
    overall_confidence,
    source_reliability_prior,
)
from watcher.config import (
    ABLATED_UNIFORM_CONFIDENCE,
    belief_layer_enabled,
    role_layer_enabled,
    sentinel_ablation,
)
from watcher.claims.models import new_claim
from watcher.claims.validate import filter_claims_by_phase
from watcher.phase_engine import evaluate_phase_transition

_COMPONENTS = {"source": 0.90, "location": 0.90, "time": 0.90, "impact": 0.90}

_RULES = {
    "phase_transition_rules": [
        {
            "from": "early_watch",
            "to": "elevated_watch",
            "claim_type": "GaugeObservation",
            "condition": "severity >= minor",
        }
    ]
}


def _env(mode=None):
    environ = {k: v for k, v in os.environ.items() if k != "SENTINEL_ABLATION"}
    if mode is not None:
        environ["SENTINEL_ABLATION"] = mode
    return mock.patch.dict(os.environ, environ, clear=True)


def _flood_claim():
    return new_claim(
        claim_id="claim_cf_test",
        source_id="src_test",
        claim_type="GaugeObservation",
        excerpt="gauge above flood stage",
        evidence_url="https://example.test",
        location_text="Harris County, TX",
        observed_at="2017-08-27T12:00:00Z",
        severity="moderate",
        value={"site_no": "08074000", "flood_state_score": 3.0, "source_class": "official_api"},
        confidence_components=dict(_COMPONENTS),
    )


class TestModeResolution(unittest.TestCase):
    def test_default_is_full(self):
        with _env():
            self.assertEqual(sentinel_ablation(), "full")
            self.assertTrue(belief_layer_enabled())
            self.assertTrue(role_layer_enabled())

    def test_unknown_mode_falls_back_to_full(self):
        with _env("nonsense"):
            self.assertEqual(sentinel_ablation(), "full")

    def test_each_mode_toggles_the_right_layers(self):
        expected = {
            "full": (True, True),
            "no_belief": (False, True),
            "no_role": (True, False),
            "none": (False, False),
        }
        for mode, (belief, role) in expected.items():
            with _env(mode):
                self.assertEqual(belief_layer_enabled(), belief, mode)
                self.assertEqual(role_layer_enabled(), role, mode)


class TestBeliefLayerAblation(unittest.TestCase):
    def test_full_mode_discriminates_by_tier(self):
        with _env("full"):
            self.assertGreater(source_reliability_prior(0), source_reliability_prior(2))

    def test_ablated_mode_collapses_tiers(self):
        with _env("no_belief"):
            self.assertEqual(source_reliability_prior(0), source_reliability_prior(2))
            self.assertEqual(source_reliability_prior(0), ABLATED_UNIFORM_CONFIDENCE)

    def test_ablated_composite_is_uniform(self):
        weak = {"source": 0.1, "location": 0.1, "time": 0.1, "impact": 0.1}
        with _env("no_belief"):
            self.assertEqual(overall_confidence(_COMPONENTS), overall_confidence(weak))

    def test_full_composite_is_not_uniform(self):
        weak = {"source": 0.1, "location": 0.1, "time": 0.1, "impact": 0.1}
        with _env("full"):
            self.assertNotEqual(overall_confidence(_COMPONENTS), overall_confidence(weak))

    def test_ablated_mode_bypasses_confidence_and_tier_admission(self):
        claim = _flood_claim()
        claim["overall_confidence"] = 0.01
        claim["source_reliability_prior"] = 0.01
        rules = {
            "required_claim_types": [
                {"type": "GaugeObservation", "minimum_confidence": 0.99}
            ]
        }
        with _env("no_belief"):
            accepted, rejected = filter_claims_by_phase([claim], rules)
        self.assertEqual(accepted, [claim])
        self.assertEqual(rejected, [])

    def test_ablated_mode_still_rejects_structurally_invalid_claims(self):
        claim = _flood_claim()
        del claim["confidence_components"]
        with _env("no_belief"):
            accepted, rejected = filter_claims_by_phase([claim], {})
        self.assertEqual(accepted, [])
        self.assertIn("missing keys", rejected[0]["rejection_reason"])


class TestRoleLayerAblation(unittest.TestCase):
    def test_full_mode_escalates(self):
        with _env("full"):
            decision = evaluate_phase_transition(
                current_phase="early_watch",
                monitoring_summary=_RULES,
                evidence_claims=[_flood_claim()],
            )
            self.assertTrue(decision["phase_changed"])
            self.assertEqual(decision["new_phase"], "elevated_watch")

    def test_ablated_mode_never_changes_phase(self):
        with _env("no_role"):
            decision = evaluate_phase_transition(
                current_phase="early_watch",
                monitoring_summary=_RULES,
                evidence_claims=[_flood_claim()],
            )
            self.assertFalse(decision["phase_changed"])
            self.assertEqual(decision["new_phase"], "early_watch")
            self.assertEqual(decision["ablated"], "no_role")

    def test_ablated_mode_still_reports_alert_level(self):
        # The naive baseline still surfaces severity; it just cannot phase-shift.
        with _env("no_role"):
            decision = evaluate_phase_transition(
                current_phase="early_watch",
                monitoring_summary=_RULES,
                evidence_claims=[_flood_claim()],
            )
            self.assertNotEqual(decision["alert_level"], "none")


class TestDirectAssignmentsRespectAblation(unittest.TestCase):
    """Regression: confidences assigned directly must also collapse.

    Cold-start and unscored gauge observations carry a fixed supplementary
    confidence instead of going through the weighted composite. Before this was
    routed through apply_belief_ablation, the no_belief configuration still
    distinguished those claims from scored ones, so the ablation was not clean.
    """

    def test_helper_collapses_when_ablated(self):
        with _env("no_belief"):
            self.assertEqual(apply_belief_ablation(0.45), ABLATED_UNIFORM_CONFIDENCE)

    def test_helper_is_transparent_when_full(self):
        with _env("full"):
            self.assertEqual(apply_belief_ablation(0.45), 0.45)

    def test_unscored_gauge_claim_collapses(self):
        from watcher.claims.extract_county_flood import extract_county_flood_claims

        payload = {
            "schema": "county-flood-data/1",
            "gauges": [
                {
                    "site_no": "08074000",
                    "name": "Test",
                    "observed_primary": 12.0,
                    "observed_valid_time": "2017-08-27T12:00:00Z",
                    "flood_state_score": None,
                    "hydrologic_signal": "stable",
                    "supplementary_confidence": 0.45,
                }
            ],
        }
        snap = {
            "source_id": "s",
            "snapshot_id": "snap",
            "snapshot_url": "https://example.test",
            "fetched_at": "2017-08-27T12:00:00Z",
            "raw_text": json.dumps(payload),
        }
        with _env("no_belief"):
            claims, _ = extract_county_flood_claims(
                snap, county="Harris", state="TX", location="Harris County, TX"
            )
            self.assertEqual(claims[0]["overall_confidence"], ABLATED_UNIFORM_CONFIDENCE)


class TestNaiveBaseline(unittest.TestCase):
    def test_none_mode_disables_both(self):
        with _env("none"):
            self.assertEqual(source_reliability_prior(0), source_reliability_prior(3))
            decision = evaluate_phase_transition(
                current_phase="early_watch",
                monitoring_summary=_RULES,
                evidence_claims=[_flood_claim()],
            )
            self.assertFalse(decision["phase_changed"])


if __name__ == "__main__":
    unittest.main()
