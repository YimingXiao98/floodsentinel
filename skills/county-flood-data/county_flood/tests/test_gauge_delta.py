"""Tests for unscored gauge delta and supplementary signals."""

from __future__ import annotations

import unittest

from county_flood.sources.gauge_delta import (
    apply_supplementary_signals,
    compute_hydrologic_signal,
    gauge_lookup_key,
    gauges_require_persist,
    index_prior_gauges,
)


class TestGaugeDelta(unittest.TestCase):
    def test_gauge_lookup_key_prefers_site(self):
        gauge = {"site_no": "08075000", "shef_id": "HCCT2"}
        self.assertEqual(gauge_lookup_key(gauge), "site:08075000")

    def test_cold_start_without_prior(self):
        result = compute_hydrologic_signal(
            observed_primary=12.5,
            streamflow_cfs=1000.0,
            prior=None,
        )
        self.assertEqual(result["hydrologic_signal"], "cold_start")
        self.assertIsNone(result["supplementary_confidence"])

    def test_rising_on_stage_delta(self):
        prior = {"observed_primary": 10.0, "streamflow_cfs": 500.0}
        result = compute_hydrologic_signal(
            observed_primary=12.0,
            streamflow_cfs=500.0,
            prior=prior,
        )
        self.assertEqual(result["hydrologic_signal"], "rising")
        self.assertEqual(result["delta_primary_ft"], 2.0)
        self.assertIsNotNone(result["supplementary_confidence"])

    def test_falling_on_stage_delta(self):
        prior = {"observed_primary": 15.0, "streamflow_cfs": 800.0}
        result = compute_hydrologic_signal(
            observed_primary=13.0,
            streamflow_cfs=800.0,
            prior=prior,
        )
        self.assertEqual(result["hydrologic_signal"], "falling")

    def test_stable_small_change(self):
        prior = {"observed_primary": 10.0, "streamflow_cfs": 500.0}
        result = compute_hydrologic_signal(
            observed_primary=10.3,
            streamflow_cfs=510.0,
            prior=prior,
        )
        self.assertEqual(result["hydrologic_signal"], "stable")
        self.assertIsNone(result["supplementary_confidence"])

    def test_scored_gauge_gets_delta_but_not_supplementary(self):
        prior = [{"site_no": "1", "shef_id": "A", "observed_primary": 14.0}]
        gauges = [
            {
                "site_no": "1",
                "shef_id": "A",
                "flood_state_score": 2.0,
                "observed_primary": 15.0,
            }
        ]
        updated, supplementary = apply_supplementary_signals(gauges, prior)
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].get("delta_primary_ft"), 1.0)
        self.assertEqual(updated[0].get("prior_observed_primary"), 14.0)
        self.assertEqual(updated[0].get("hydrologic_signal"), "rising")
        # Scored gauges get delta fields but do not emit supplementary trend claims.
        self.assertEqual(supplementary, [])

    def test_scored_gauge_cold_start_without_prior(self):
        gauges = [
            {
                "site_no": "1",
                "shef_id": "A",
                "flood_state_score": 2.0,
                "observed_primary": 15.0,
            }
        ]
        updated, supplementary = apply_supplementary_signals(gauges, [])
        self.assertEqual(updated[0].get("hydrologic_signal"), "cold_start")
        self.assertEqual(supplementary, [])

    def test_unscored_cold_start_emits_supplementary(self):
        gauges = [
            {
                "site_no": "99",
                "shef_id": "X",
                "name": "Test Creek",
                "source": "usgs",
                "flood_state_score": None,
                "observed_primary": 5.0,
            }
        ]
        updated, supplementary = apply_supplementary_signals(gauges, [])
        self.assertEqual(updated[0]["hydrologic_signal"], "cold_start")
        self.assertEqual(len(supplementary), 1)
        self.assertEqual(supplementary[0]["hydrologic_signal"], "cold_start")
        self.assertIsNotNone(updated[0].get("supplementary_confidence"))

    def test_unscored_gets_supplementary_list_on_rise(self):
        prior = [{"site_no": "99", "shef_id": "X", "observed_primary": 5.0}]
        gauges = [
            {
                "site_no": "99",
                "shef_id": "X",
                "name": "Test Creek",
                "source": "usgs",
                "flood_state_score": None,
                "observed_primary": 7.5,
            }
        ]
        updated, supplementary = apply_supplementary_signals(gauges, prior)
        self.assertEqual(updated[0]["hydrologic_signal"], "rising")
        self.assertEqual(len(supplementary), 1)
        self.assertEqual(supplementary[0]["hydrologic_signal"], "rising")

    def test_index_prior_gauges(self):
        prior = [
            {"site_no": "1", "observed_primary": 1.0},
            {"shef_id": "ABC", "observed_primary": 2.0},
        ]
        idx = index_prior_gauges(prior)
        self.assertIn("site:1", idx)
        self.assertIn("shef:ABC", idx)

    def test_gauges_require_persist_cold_start(self):
        gauges = [{"site_no": "1", "observed_primary": 10.0}]
        self.assertTrue(gauges_require_persist(gauges, None))
        self.assertTrue(gauges_require_persist(gauges, []))

    def test_gauges_require_persist_sub_threshold_skipped(self):
        prior = [{"site_no": "1", "observed_primary": 10.00}]
        current = [{"site_no": "1", "observed_primary": 10.05}]
        self.assertFalse(gauges_require_persist(current, prior))

    def test_gauges_require_persist_at_threshold(self):
        prior = [{"site_no": "1", "observed_primary": 10.00}]
        current = [{"site_no": "1", "observed_primary": 10.10}]
        self.assertTrue(gauges_require_persist(current, prior))

    def test_gauges_require_persist_score_change(self):
        prior = [{"site_no": "1", "observed_primary": 10.00, "flood_state_score": 0.0}]
        current = [{"site_no": "1", "observed_primary": 10.02, "flood_state_score": 2.0}]
        self.assertTrue(gauges_require_persist(current, prior))


if __name__ == "__main__":
    unittest.main()
