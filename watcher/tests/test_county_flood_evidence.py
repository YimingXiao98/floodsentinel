"""Tests for coordinator county flood evidence envelope."""

from __future__ import annotations

import unittest

from watcher.county_flood_evidence import build_county_flood_evidence


class TestCountyFloodEvidence(unittest.TestCase):
    def test_missing_payload(self):
        result = build_county_flood_evidence(None)
        self.assertEqual(result["status"], "missing")
        self.assertIsNone(result["overall_confidence"])

    def test_ok_payload(self):
        payload = {
            "county": "Harris",
            "state": "TX",
            "generated_at": "2026-06-20T12:00:00Z",
            "summary": {
                "overall_confidence": 0.862,
                "confidence_components": {"source": 0.9, "location": 0.9, "time": 0.85, "impact": 0.8},
                "regional_verdict": "no_flooding",
                "is_flooding": False,
                "verdict_gauge_count": 29,
                "api_weight_hint": "high",
            },
        }
        result = build_county_flood_evidence(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["overall_confidence"], 0.862)
        self.assertEqual(result["merge_key"], "county_flood_evidence")


if __name__ == "__main__":
    unittest.main()
