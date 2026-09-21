"""Tests for stage merge, classification, selection, and regional verdict."""

from __future__ import annotations

import unittest

from county_flood.sources.selection import gauge_priority_tier, sort_csv_rows
from county_flood.sources.stages import (
    datum_compatible,
    merge_stages,
    regional_verdict_from_gauges,
    resolve_flood_category,
    score_label,
    stages_from_nwps_payload,
)


class TestStages(unittest.TestCase):
    def test_merge_stages_csv_wins(self):
        csv = {"action_stage": 10.0, "flood_stage": None, "moderate_stage": None, "major_stage": None}
        api = {"action_stage": 9.0, "flood_stage": 12.0, "moderate_stage": None, "major_stage": None}
        merged, source = merge_stages(csv, api)
        self.assertEqual(merged["action_stage"], 10.0)
        self.assertEqual(merged["flood_stage"], 12.0)
        self.assertEqual(source, "mixed")

    def test_not_defined_with_stages_stage_derived(self):
        result = resolve_flood_category(
            observed_primary=15.0,
            nwps_category="not_defined",
            action_stage=10.0,
            flood_stage=12.0,
            moderate_stage=14.0,
            major_stage=16.0,
            threshold_source="nwps_api",
        )
        self.assertEqual(result.method, "stage_derived")
        self.assertEqual(result.category, "moderate_flooding")
        self.assertEqual(result.score, 3.0)

    def test_stage_thresholds_override_nwps_category(self):
        """Observed stage vs thresholds wins even if NWPS says no_flooding."""
        result = resolve_flood_category(
            observed_primary=13.0,
            nwps_category="no_flooding",
            action_stage=10.0,
            flood_stage=12.0,
            moderate_stage=14.0,
            major_stage=16.0,
            threshold_source="nwps_api",
        )
        self.assertEqual(result.method, "stage_derived")
        self.assertEqual(result.category, "minor_flooding")
        self.assertEqual(result.score, 2.0)

    def test_below_action_when_stage_under_thresholds(self):
        result = resolve_flood_category(
            observed_primary=72.03,
            nwps_category="action",
            action_stage=90.2,
            flood_stage=93.2,
            moderate_stage=None,
            major_stage=None,
            threshold_source="nwps_api",
        )
        self.assertEqual(result.method, "stage_derived")
        self.assertEqual(result.category, "no_flooding")
        self.assertEqual(result.score, 0.0)

    def test_not_defined_no_stages_observation_only(self):
        result = resolve_flood_category(
            observed_primary=5.0,
            nwps_category="not_defined",
            action_stage=None,
            flood_stage=None,
            moderate_stage=None,
            major_stage=None,
            threshold_source="none",
        )
        self.assertEqual(result.method, "observation_only")
        self.assertIsNone(result.score)
        self.assertEqual(result.unscored_reason, "not_defined_no_thresholds")

    def test_datum_mismatch_blocks_stage_derived(self):
        stages = {"action_stage": 8.0, "flood_stage": 9.0, "moderate_stage": 11.0, "major_stage": 13.0}
        self.assertFalse(datum_compatible(3199.0, stages))

    def test_nwps_payload_stages(self):
        payload = {
            "flood": {
                "categories": {
                    "action": {"stage": 3206},
                    "minor": {"stage": 3207},
                    "moderate": {"stage": -9999},
                    "major": {"stage": 3211},
                }
            }
        }
        stages = stages_from_nwps_payload(payload)
        self.assertEqual(stages["action_stage"], 3206.0)
        self.assertEqual(stages["flood_stage"], 3207.0)
        self.assertIsNone(stages["moderate_stage"])
        self.assertEqual(stages["major_stage"], 3211.0)


class TestRegionalVerdict(unittest.TestCase):
    def _gauges(self, n_scored: int, *, score: float = 0.0, extra_unscored: int = 0):
        gauges = []
        for i in range(n_scored):
            gauges.append(
                {
                    "shef_id": f"S{i}",
                    "flood_state_score": score,
                    "observed_primary": float(i + 1),
                    "classification_method": "nwps_api_category",
                }
            )
        for i in range(extra_unscored):
            gauges.append(
                {
                    "shef_id": f"U{i}",
                    "flood_state_score": None,
                    "observed_primary": 50.0 + i,
                }
            )
        return gauges

    def test_any_one_floods(self):
        gauges = self._gauges(4, score=0.0) + [
            {"shef_id": "B", "flood_state_score": 2.0, "observed_primary": 15.0},
        ]
        v = regional_verdict_from_gauges(gauges)
        self.assertTrue(v["is_flooding"])
        self.assertEqual(v["regional_verdict"], "minor_flooding")

    def test_sparse_coverage_does_not_suppress_a_detection(self):
        # Three gauges reading minor flooding is evidence of flooding even
        # though coverage is below the minimum. Suppressing it would turn thin
        # coverage into a missed flood, which is the costlier error.
        gauges = self._gauges(3, score=2.0)
        v = regional_verdict_from_gauges(gauges)
        self.assertEqual(v["regional_verdict"], "minor_flooding")
        self.assertTrue(v["is_flooding"])
        self.assertEqual(v["evaluation_mode"], "scored_gauges_sparse")
        self.assertEqual(v["confidence"], "low")
        self.assertEqual(v["gauges_with_data"], 3)
        self.assertEqual(v["min_gauges_required"], 5)

    def test_sparse_coverage_blocks_an_all_clear(self):
        # Nothing is flooding, but too few gauges are reporting to have seen it
        # if it were, so no all-clear is asserted.
        gauges = self._gauges(3, score=0.0)
        v = regional_verdict_from_gauges(gauges)
        self.assertEqual(v["regional_verdict"], "insufficient_data")
        self.assertFalse(v["is_flooding"])
        self.assertEqual(v["evaluation_mode"], "insufficient_data")

    def test_no_scorable_gauge_is_insufficient_data(self):
        # Gauges are reporting, but without thresholds none can be judged, so
        # the state is unknown rather than clear.
        gauges = self._gauges(0, extra_unscored=5)
        v = regional_verdict_from_gauges(gauges)
        self.assertEqual(v["regional_verdict"], "insufficient_data")
        self.assertFalse(v["is_flooding"])
        self.assertEqual(v["evaluation_mode"], "unscored_gauges")
        self.assertEqual(v["confidence"], "low")

    def test_adequate_coverage_can_assert_all_clear(self):
        gauges = self._gauges(6, score=0.0)
        v = regional_verdict_from_gauges(gauges)
        self.assertEqual(v["regional_verdict"], "no_flooding")
        self.assertFalse(v["is_flooding"])
        self.assertEqual(v["evaluation_mode"], "scored_gauges")

    def test_all_no_flooding(self):
        gauges = self._gauges(5, score=0.0)
        v = regional_verdict_from_gauges(gauges)
        self.assertFalse(v["is_flooding"])
        self.assertEqual(v["regional_verdict"], "no_flooding")

    def test_unscored_gauges_do_not_satisfy_all_clear_minimum(self):
        gauges = [
            {"shef_id": "A", "flood_state_score": 0.0, "observed_primary": 1.0, "classification_method": "nwps_api_category"},
            {"shef_id": "B", "flood_state_score": 0.0, "observed_primary": 2.0, "classification_method": "nwps_api_category"},
            {"shef_id": "C", "flood_state_score": 0.0, "observed_primary": 3.0, "classification_method": "nwps_api_category"},
            {"shef_id": "U1", "flood_state_score": None, "observed_primary": 50.0},
            {"shef_id": "U2", "flood_state_score": None, "observed_primary": 60.0},
        ]
        v = regional_verdict_from_gauges(gauges)
        self.assertEqual(v["regional_verdict"], "insufficient_data")
        self.assertEqual(v["evaluation_mode"], "insufficient_data")
        self.assertEqual(v["confidence"], "low")
        self.assertEqual(v["verdict_gauge_count"], 3)
        self.assertEqual(v["gauges_excluded"], 2)


class TestSelection(unittest.TestCase):
    def test_priority_tiers(self):
        row_flood = {"action stage": "10", "flood stage": "12", "nws shef id": "X", "usgs id": "1"}
        row_shef = {"action stage": "-9999", "flood stage": "-9999", "nws shef id": "Y", "usgs id": ""}
        self.assertLess(gauge_priority_tier(row_flood), gauge_priority_tier(row_shef))

    def test_sort_scorable_first(self):
        rows = [
            {"location name": "Z", "action stage": "-9999", "flood stage": "-9999", "nws shef id": "", "usgs id": ""},
            {"location name": "A", "action stage": "10", "flood stage": "12", "nws shef id": "X", "usgs id": "1"},
        ]
        sorted_rows = sort_csv_rows(rows)
        self.assertEqual(sorted_rows[0]["location name"], "A")


class TestScoreLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(score_label(0.0), "no_flooding")
        self.assertEqual(score_label(2.0), "minor_flooding")


if __name__ == "__main__":
    unittest.main()
