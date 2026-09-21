"""Confidence measures credibility; severity is reported separately.

The composite previously included the severity of the reported condition, which
made the system more confident the worse the news was. A certain but mild
reading then ranked below an uncertain but severe one, and a request for
high-confidence material returned the worst news rather than the most reliable.

Freshness is also measured against the moment the system looked, not the wall
clock, so a replay of a past event does not score every observation as stale.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from watcher.claims.confidence import (
    _hours_since,
    overall_confidence,
    score_article_claim,
    score_usgs_hydrology_claim,
    severity_score,
)

_CREDIBLE = {"source": 0.90, "location": 0.90, "time": 0.85}


def _no_ablation():
    env = {k: v for k, v in os.environ.items() if k != "SENTINEL_ABLATION"}
    return mock.patch.dict(os.environ, env, clear=True)


class TestSeverityIsNotConfidence(unittest.TestCase):
    def test_severity_does_not_change_confidence(self):
        with _no_ablation():
            mild = overall_confidence({**_CREDIBLE, "impact": 0.62})
            severe = overall_confidence({**_CREDIBLE, "impact": 0.95})
            self.assertEqual(mild, severe)

    def test_severity_is_still_available(self):
        self.assertEqual(severity_score({**_CREDIBLE, "impact": 0.95}), 0.95)
        self.assertEqual(severity_score({**_CREDIBLE, "impact": 0.62}), 0.62)

    def test_credibility_terms_still_move_confidence(self):
        with _no_ablation():
            good = overall_confidence({"source": 0.90, "location": 0.90, "time": 0.90})
            weak_source = overall_confidence({"source": 0.35, "location": 0.90, "time": 0.90})
            wrong_place = overall_confidence({"source": 0.90, "location": 0.55, "time": 0.90})
            self.assertLess(weak_source, good)
            self.assertLess(wrong_place, good)
            # Source is weighted more heavily than location, as before.
            self.assertLess(weak_source, wrong_place)

    def test_age_no_longer_moves_confidence(self):
        # Age is enforced by the validity window, not by discounting a truth
        # score: a timestamped reading does not become false as it ages.
        with _no_ablation():
            fresh = overall_confidence({"source": 0.90, "location": 0.90, "time": 0.90})
            old = overall_confidence({"source": 0.90, "location": 0.90, "time": 0.55})
            self.assertEqual(fresh, old)

    def test_confidence_stays_in_range(self):
        with _no_ablation():
            self.assertEqual(overall_confidence({"source": 1.0, "location": 1.0, "time": 1.0}), 1.0)
            self.assertEqual(overall_confidence({"source": 0.0, "location": 0.0, "time": 0.0}), 0.0)

    def test_ablation_still_collapses_confidence(self):
        with mock.patch.dict(os.environ, {"SENTINEL_ABLATION": "no_belief"}):
            self.assertEqual(
                overall_confidence({**_CREDIBLE, "impact": 0.62}),
                overall_confidence({"source": 0.1, "location": 0.1, "time": 0.1}),
            )


class TestFreshnessUsesTheObservingMoment(unittest.TestCase):
    def test_age_is_measured_against_the_reference(self):
        self.assertAlmostEqual(
            _hours_since("2017-08-27T12:00:00Z", "2017-08-27T15:00:00Z"), 3.0
        )

    def test_a_replayed_reading_is_not_reported_as_years_stale(self):
        # Without a reference the same reading is measured against the wall
        # clock, which in a replay of a past event is meaningless.
        fresh = _hours_since("2017-08-27T12:00:00Z", "2017-08-27T13:00:00Z")
        wall_clock = _hours_since("2017-08-27T12:00:00Z")
        self.assertLess(fresh, 2)
        self.assertGreater(wall_clock, 24)

    def test_gauge_freshness_varies_with_the_reference(self):
        recent = score_usgs_hydrology_claim(
            county_match=True, observation_time="2017-08-27T12:00:00Z",
            gage_height_ft=45.0, now="2017-08-27T13:00:00Z")
        old = score_usgs_hydrology_claim(
            county_match=True, observation_time="2017-08-27T12:00:00Z",
            gage_height_ft=45.0, now="2017-08-28T12:00:00Z")
        self.assertGreater(recent["time"], old["time"])

    def test_article_freshness_varies_with_the_reference(self):
        recent = score_article_claim(page_score=0.9, published_at="2017-08-27T12:00:00Z",
                                     indication="flooding", now="2017-08-27T13:00:00Z")
        old = score_article_claim(page_score=0.9, published_at="2017-08-27T12:00:00Z",
                                  indication="flooding", now="2017-08-31T12:00:00Z")
        self.assertGreater(recent["time"], old["time"])

    def test_no_reference_falls_back_to_the_wall_clock(self):
        self.assertIsNotNone(_hours_since("2017-08-27T12:00:00Z"))


class TestValidityWindow(unittest.TestCase):
    """A gauge that stops reporting must not keep asserting a flood state.

    Without a window, a frozen sensor's last value counts as live evidence for
    as long as the sensor stays down. The reading is kept in the record with its
    age, but carries no severity, so it cannot move the event.
    """

    def _claims(self, observed_at, fetched_at):
        import json

        from watcher.claims.extract_county_flood import extract_county_flood_claims

        payload = {"schema": "county-flood-data/1", "gauges": [{
            "site_no": "08074000", "name": "Test", "observed_primary": 45.0,
            "observed_valid_time": observed_at, "observed_flood_category": "major_flooding",
            "flood_state_score": 4.0, "flood_stage": 28.0}]}
        snap = {"source_id": "s", "snapshot_id": "sn", "snapshot_url": "u",
                "fetched_at": fetched_at, "raw_text": json.dumps(payload)}
        claims, _ = extract_county_flood_claims(
            snap, county="Harris", state="TX", location="Harris County, TX")
        return claims

    def test_a_current_reading_asserts_its_flood_state(self):
        c = self._claims("2017-08-27T12:00:00Z", "2017-08-27T14:00:00Z")[0]
        self.assertEqual(c["severity"], "severe")
        self.assertTrue(c["value"]["reading_current"])

    def test_a_frozen_reading_asserts_nothing(self):
        c = self._claims("2017-08-27T12:00:00Z", "2017-08-28T12:00:00Z")[0]
        self.assertEqual(c["severity"], "unknown")
        self.assertFalse(c["value"]["reading_current"])

    def test_the_stale_reading_is_still_recorded_with_its_age(self):
        c = self._claims("2017-08-27T12:00:00Z", "2017-08-28T12:00:00Z")[0]
        self.assertEqual(c["value"]["gage_height_ft"], 45.0)
        self.assertAlmostEqual(c["value"]["observation_age_hours"], 24.0)

    def test_a_stale_reading_cannot_escalate(self):
        from watcher.claims.models import severity_rank

        c = self._claims("2017-08-27T12:00:00Z", "2017-08-28T12:00:00Z")[0]
        self.assertLess(severity_rank(c["severity"]), severity_rank("minor"))

    def test_the_window_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"SENTINEL_READING_MAX_AGE_HOURS": "0"}):
            c = self._claims("2017-08-27T12:00:00Z", "2017-08-30T12:00:00Z")[0]
            self.assertEqual(c["severity"], "severe")


if __name__ == "__main__":
    unittest.main()
