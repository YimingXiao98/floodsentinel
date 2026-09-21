"""Unscored gauges are admitted as low-confidence observations, safely.

A gauge without action/minor/major thresholds cannot be scored, but it still
carries weak evidence. Dropping it entirely leaves gauge-sparse counties blind
after the first cycle. These claims must be admitted, yet must not be able to
escalate a phase or satisfy an all-clear on their own.
"""

from __future__ import annotations

import json
import unittest

from watcher.claims.extract_county_flood import extract_county_flood_claims
from watcher.claims.models import severity_rank
from watcher.phase_engine import _official_gauge_claims, evidence_supports_all_clear


def _snapshot(gauges):
    payload = {"schema": "county-flood-data/1", "gauges": gauges, "summary": {}}
    return {
        "source_id": "src_county_flood_usgs_48201",
        "snapshot_id": "snap_test_1",
        "snapshot_url": "https://example.test/usgs",
        "fetched_at": "2026-08-08T00:00:00Z",
        "raw_text": json.dumps(payload),
    }


def _gauge(signal, *, stage=12.34, score=None):
    # Real gauges carry how their flood state was established and where the
    # threshold came from; the credibility score reads both.
    return {
        "classification_method": "stage_derived" if score is not None else "observation_only",
        "threshold_source": "csv" if score is not None else "none",
        "site_no": "08074000",
        "shef_id": "HOUT2",
        "name": "Test Bayou",
        "observed_primary": stage,
        "streamflow_cfs": 500.0,
        "observed_valid_time": "2026-08-08T00:00:00Z",
        "flood_state_score": score,
        "hydrologic_signal": signal,
        "supplementary_confidence": 0.45,
    }


def _extract(gauges):
    claims, _ = extract_county_flood_claims(
        _snapshot(gauges), county="Harris", state="TX", location="Harris County, TX"
    )
    return claims


class TestUnscoredGaugeAdmission(unittest.TestCase):
    def test_stable_unscored_gauge_is_admitted(self):
        # Regression: previously dropped, leaving sparse counties blind on run 2+.
        claims = _extract([_gauge("stable")])
        self.assertEqual(len(claims), 1)
        value = claims[0]["value"]
        self.assertEqual(value["trend_source"], "unscored_observation")
        self.assertEqual(value["hydrologic_signal"], "stable")
        # The reading itself is credible: an official gauge in the monitored
        # county. What is missing is a flood classification, which is why the
        # claim asserts no severity rather than why it would be doubted.
        self.assertGreater(claims[0]["overall_confidence"], 0.5)
        self.assertEqual(claims[0]["severity"], "unknown")

    def test_signalless_unscored_gauge_is_admitted(self):
        claims = _extract([_gauge(None)])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["value"]["hydrologic_signal"], "observed")

    def test_cold_start_still_labelled_cold_start(self):
        claims = _extract([_gauge("cold_start")])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["value"]["trend_source"], "cold_start")
        self.assertEqual(
            claims[0]["confidence_breakdown"]["method"], "cold_start_delta"
        )

    def test_gauge_with_no_reading_is_not_admitted(self):
        g = _gauge("stable")
        g["observed_primary"] = None
        g["streamflow_cfs"] = None
        self.assertEqual(_extract([g]), [])

    def test_admitted_claim_cannot_escalate(self):
        # severity must rank below "minor" so phase rules never match it.
        claim = _extract([_gauge("stable")])[0]
        self.assertLess(severity_rank(claim.get("severity")), severity_rank("minor"))

    def test_admitted_claim_is_not_an_official_gauge(self):
        # Excluded from the official set, so it cannot satisfy an all-clear alone
        # (absence of a scorable gauge must not read as "no flooding").
        claims = _extract([_gauge("stable")])
        self.assertEqual(_official_gauge_claims(claims), [])
        self.assertFalse(evidence_supports_all_clear(claims))

    def test_scored_gauge_still_takes_the_scored_path(self):
        claims = _extract([_gauge("stable", score=2.0)])
        self.assertEqual(len(claims), 1)
        self.assertNotEqual(claims[0]["value"].get("trend_source"), "unscored_observation")
        # A classified gauge outranks a bare reading, because the flood state
        # was established rather than left open.
        unscored = _extract([_gauge("stable")])[0]["overall_confidence"]
        self.assertGreater(claims[0]["overall_confidence"], unscored)


if __name__ == "__main__":
    unittest.main()
