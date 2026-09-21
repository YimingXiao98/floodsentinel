"""Tests for supplementary scoring merge in aggregator."""

from __future__ import annotations

import unittest

from county_flood.aggregator import _merge_verdict_with_supplementary, _supplementary_scores


class TestAggregatorSupplementary(unittest.TestCase):
    def test_supplementary_scores_from_alerts_and_disasters(self) -> None:
        scores = _supplementary_scores(
            alerts=[{"flood_state_score": 2.0}],
            disasters=[{"flood_state_score": 2.0}],
        )
        self.assertEqual(scores["nws_alerts"], 2.0)
        self.assertEqual(scores["fema_disasters"], 2.0)

    def test_merge_verdict_raises_is_flooding(self) -> None:
        gauge_verdict = {
            "max_flood_state": 0.0,
            "regional_verdict": "no_flooding",
            "is_flooding": False,
            "average_flood_state": None,
        }
        merged = _merge_verdict_with_supplementary(
            gauge_verdict,
            {"fema_disasters": 2.0},
        )
        self.assertTrue(merged["is_flooding"])
        self.assertEqual(merged["max_flood_state"], 2.0)
        self.assertEqual(merged["regional_verdict"], "minor_flooding")


if __name__ == "__main__":
    unittest.main()
