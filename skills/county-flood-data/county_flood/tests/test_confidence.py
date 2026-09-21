"""Tests for county-level API confidence scoring."""

from __future__ import annotations

import unittest

from county_flood.confidence import compute_county_flood_confidence, overall_from_components


class TestCountyFloodConfidence(unittest.TestCase):
    def test_high_confidence_with_multiple_scored_gauges(self):
        summary = {
            "verdict_gauge_count": 5,
            "gauges_scored": 5,
            "gauges_with_data": 20,
            "confidence": "high",
            "is_flooding": False,
            "max_flood_state": 0.0,
        }
        gauges = [
            {"observed_primary": 10.0, "observed_valid_time": "2026-06-20T12:00:00Z", "flood_state_score": 0.0},
        ]
        result = compute_county_flood_confidence(summary=summary, gauges=gauges)
        self.assertGreaterEqual(result["overall_confidence"], 0.80)
        self.assertEqual(result["evidence_tier"], "T0")
        self.assertEqual(result["weight_hint"], "high")

    def test_low_confidence_without_scored_gauges(self):
        summary = {
            "verdict_gauge_count": 0,
            "gauges_scored": 0,
            "gauges_with_data": 10,
            "confidence": "low",
            "is_flooding": False,
            "max_flood_state": 0.0,
        }
        result = compute_county_flood_confidence(summary=summary, gauges=[])
        self.assertLess(result["overall_confidence"], 0.75)
        self.assertIn(result["weight_hint"], ("low", "medium"))

    def test_overall_formula(self):
        components = {"source": 0.90, "location": 0.90, "time": 0.85, "impact": 0.80}
        self.assertAlmostEqual(overall_from_components(components), 0.87, places=3)


if __name__ == "__main__":
    unittest.main()
