"""Tests for the retrospective replay harness.

The load-bearing property is no lookahead: at virtual time t the replay must
never expose an observation timestamped after t. If that leaks, every
evaluation number is invalid.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import patch

from watcher.replay import (
    _request_json,
    corpus_stats,
    iter_ticks,
    observation_age_hours,
    observation_as_of,
    parse_iso,
    payload_at,
    snapshot_at,
)


def _site(site_no="08074000", *, thresholds=True, observations=None):
    site = {
        "site_no": site_no,
        "shef_id": "TSTT2",
        "name": "Test Bayou",
        "action_stage": 10.0 if thresholds else None,
        "flood_stage": 20.0 if thresholds else None,
        "moderate_stage": 30.0 if thresholds else None,
        "major_stage": 40.0 if thresholds else None,
        "stage_unit": "ft",
        "priority_tier": 1,
        "threshold_source": "csv",
        "observations": observations
        if observations is not None
        else [
            {"t": "2017-08-26T00:00:00Z", "stage": 5.0, "flow": 100.0},
            {"t": "2017-08-26T01:00:00Z", "stage": 15.0, "flow": 900.0},
            {"t": "2017-08-26T02:00:00Z", "stage": 35.0, "flow": 5000.0},
        ],
    }
    return site


def _corpus(sites=None):
    return {
        "schema": "sentinel-replay-corpus/1",
        "county": "Harris",
        "state": "TX",
        "fips": "48201",
        "window": {"start": "2017-08-26T00:00:00Z", "end": "2017-08-26T03:00:00Z"},
        "sites": sites if sites is not None else [_site()],
    }


def _at(text):
    return parse_iso(text)


class TestNoLookahead(unittest.TestCase):
    def test_returns_latest_observation_at_or_before_t(self):
        site = _site()
        obs = observation_as_of(site, _at("2017-08-26T01:30:00Z"))
        self.assertEqual(obs["t"], "2017-08-26T01:00:00Z")
        self.assertEqual(obs["stage"], 15.0)

    def test_exact_timestamp_is_included(self):
        obs = observation_as_of(_site(), _at("2017-08-26T01:00:00Z"))
        self.assertEqual(obs["t"], "2017-08-26T01:00:00Z")

    def test_before_first_observation_returns_none(self):
        self.assertIsNone(observation_as_of(_site(), _at("2017-08-25T23:59:59Z")))

    def test_never_exposes_a_future_observation(self):
        # Sweep the whole window: no returned observation may postdate t.
        site = _site()
        for tick in iter_ticks(
            "2017-08-25T23:00:00Z", "2017-08-26T04:00:00Z", 600
        ):
            obs = observation_as_of(site, tick)
            if obs is not None:
                self.assertLessEqual(parse_iso(obs["t"]), tick)

    def test_payload_never_reflects_future_flooding(self):
        # At 00:30 only the 5.0 ft reading exists; the 35.0 ft spike is later.
        payload = payload_at(_corpus(), _at("2017-08-26T00:30:00Z"))
        gauge = payload["gauges"][0]
        self.assertEqual(gauge["observed_primary"], 5.0)
        self.assertEqual(gauge["observed_flood_category"], "no_flooding")
        self.assertFalse(payload["summary"]["is_flooding"])

    def test_payload_reflects_flooding_once_reached(self):
        # Needs >= COUNTY_FLOOD_MIN_GAUGES (default 5) reporting gauges for a
        # county-level verdict to be issued at all; see TestMinGaugeRule.
        sites = [_site(site_no=f"0807400{i}") for i in range(5)]
        payload = payload_at(_corpus(sites), _at("2017-08-26T02:30:00Z"))
        gauge = payload["gauges"][0]
        self.assertEqual(gauge["observed_primary"], 35.0)
        self.assertEqual(gauge["observed_flood_category"], "moderate_flooding")
        self.assertTrue(payload["summary"]["is_flooding"])
        self.assertEqual(payload["summary"]["regional_verdict"], "moderate_flooding")


class TestCorpusFetchRetries(unittest.TestCase):
    @patch("watcher.replay.time.sleep")
    @patch("watcher.replay.urllib.request.urlopen")
    def test_retries_transient_http_failure(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.HTTPError("https://example.test", 503, "busy", None, None),
            BytesIO(b'{"ok": true}'),
        ]
        self.assertEqual(_request_json("https://example.test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("watcher.replay.time.sleep")
    @patch("watcher.replay.urllib.request.urlopen")
    def test_does_not_retry_permanent_http_failure(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example.test", 404, "missing", None, None
        )
        with self.assertRaises(urllib.error.HTTPError):
            _request_json("https://example.test")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


class TestStaleness(unittest.TestCase):
    def test_stale_reading_is_dropped(self):
        # Sensor last reported at 02:00; by 12:00 it is long stale.
        payload = payload_at(
            _corpus(), _at("2017-08-26T12:00:00Z"), max_staleness_hours=6.0
        )
        self.assertIsNone(payload["gauges"][0]["observed_primary"])

    def test_staleness_disabled_keeps_last_reading(self):
        payload = payload_at(
            _corpus(), _at("2017-08-26T12:00:00Z"), max_staleness_hours=None
        )
        self.assertEqual(payload["gauges"][0]["observed_primary"], 35.0)

    def test_observation_age(self):
        obs = {"t": "2017-08-26T00:00:00Z"}
        self.assertAlmostEqual(
            observation_age_hours(obs, _at("2017-08-26T06:00:00Z")), 6.0
        )


class TestPayloadShape(unittest.TestCase):
    def test_snapshot_is_extractable(self):
        from watcher.claims.extract_county_flood import extract_county_flood_claims

        snap = snapshot_at(_corpus(), _at("2017-08-26T02:30:00Z"))
        claims, failure = extract_county_flood_claims(
            snap, county="Harris", state="TX", location="Harris County, TX"
        )
        self.assertIsNone(failure)
        self.assertTrue(claims)
        self.assertEqual(json.loads(snap["raw_text"])["schema"], "county-flood-data/1")

    def test_sensor_dropout_yields_insufficient_data_not_all_clear(self):
        # The Whiteoak Bayou case: the only gauge dies mid-event.
        payload = payload_at(
            _corpus([_site(thresholds=False)]), _at("2017-08-26T12:00:00Z")
        )
        self.assertEqual(payload["summary"]["regional_verdict"], "insufficient_data")
        self.assertFalse(payload["summary"]["is_flooding"])

    def test_corpus_stats(self):
        stats = corpus_stats(_corpus())
        self.assertEqual(stats["sites"], 1)
        self.assertEqual(stats["observations_total"], 3)
        self.assertEqual(stats["sites_scorable"], 1)


class TestMinGaugeRule(unittest.TestCase):
    """COUNTY_FLOOD_MIN_GAUGES applies asymmetrically, which shapes RQ4.

    Sparse coverage blocks an all-clear but never suppresses a detection: a
    missed flood is the costlier error.
    """

    def test_sparse_coverage_still_reports_flooding(self):
        sites = [_site(site_no=f"0807400{i}") for i in range(4)]
        payload = payload_at(_corpus(sites), _at("2017-08-26T02:30:00Z"))
        self.assertEqual(payload["gauges"][0]["observed_flood_category"], "moderate_flooding")
        self.assertEqual(payload["summary"]["regional_verdict"], "moderate_flooding")
        self.assertTrue(payload["summary"]["is_flooding"])
        self.assertEqual(payload["summary"]["evaluation_mode"], "scored_gauges_sparse")

    def test_full_coverage_produces_a_verdict(self):
        sites = [_site(site_no=f"0807400{i}") for i in range(5)]
        payload = payload_at(_corpus(sites), _at("2017-08-26T02:30:00Z"))
        self.assertTrue(payload["summary"]["is_flooding"])
        self.assertEqual(payload["summary"]["evaluation_mode"], "scored_gauges")


class TestVirtualClock(unittest.TestCase):
    def test_tick_count_and_bounds(self):
        ticks = list(iter_ticks("2017-08-26T00:00:00Z", "2017-08-26T02:00:00Z", 3600))
        self.assertEqual(len(ticks), 3)
        self.assertEqual(ticks[0], datetime(2017, 8, 26, 0, tzinfo=timezone.utc))
        self.assertEqual(ticks[-1], datetime(2017, 8, 26, 2, tzinfo=timezone.utc))

    def test_rejects_nonpositive_interval(self):
        with self.assertRaises(ValueError):
            list(iter_ticks("2017-08-26T00:00:00Z", "2017-08-26T02:00:00Z", 0))


if __name__ == "__main__":
    unittest.main()
