"""A county with too few gauges may borrow from the watershed it drains.

Counties whose own gauges cannot be scored currently report insufficient
evidence on every interval. A gauge upstream in the same subbasin is genuinely
informative about that county, unlike one in a neighbouring county across a
divide, so shared drainage rather than shared border is what admits it.

The evidence is weaker and is scored that way, and it is deliberately asymmetric:
rising water somewhere connected is worth acting on, but a calm gauge in the next
county says nothing about whether this one is clear.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from watcher.claims.extract_county_flood import extract_county_flood_claims
from watcher.phase_engine import _official_gauge_claims, evidence_supports_all_clear


def _no_ablation():
    env = {k: v for k, v in os.environ.items() if k != "SENTINEL_ABLATION"}
    return mock.patch.dict(os.environ, env, clear=True)


def _gauge(
    *,
    site_no="08074000",
    relation=None,
    score=0.0,
    category="no_flooding",
    stage=12.0,
):
    return {
        "site_no": site_no,
        "name": "Test Bayou",
        "source": "usgs",
        "observed_primary": stage,
        "observed_valid_time": "2026-08-08T00:00:00Z",
        "observed_flood_category": category,
        "flood_state_score": score,
        "flood_stage": 28.0,
        "classification_method": "stage_derived",
        "threshold_source": "csv",
        "location_relation": relation,
    }


def _claims(gauges, fetched="2026-08-08T01:00:00Z"):
    snap = {
        "source_id": "src_county_flood_usgs_48201",
        "snapshot_id": "snap_1",
        "snapshot_url": "https://example.test/usgs",
        "fetched_at": fetched,
        "raw_text": json.dumps({"schema": "county-flood-data/1", "gauges": gauges}),
    }
    claims, _ = extract_county_flood_claims(
        snap, county="Harris", state="TX", location="Harris County, TX"
    )
    return claims


class TestLocationCredibility(unittest.TestCase):
    def test_a_neighbour_is_placed_less_precisely_than_an_in_county_gauge(self):
        with _no_ablation():
            here = _claims([_gauge()])[0]
            over_there = _claims([_gauge(relation="shared_watershed")])[0]
            self.assertLess(
                over_there["confidence_components"]["location"],
                here["confidence_components"]["location"],
            )
            self.assertLess(
                over_there["overall_confidence"], here["overall_confidence"]
            )

    def test_a_neighbour_still_outranks_an_unresolved_location(self):
        with _no_ablation():
            over_there = _claims([_gauge(relation="shared_watershed")])[0]
            self.assertGreater(over_there["confidence_components"]["location"], 0.55)

    def test_the_precision_is_configurable(self):
        with mock.patch.dict(
            os.environ, {"SENTINEL_WATERSHED_LOCATION_PRECISION": "0.40"}
        ):
            claim = _claims([_gauge(relation="shared_watershed")])[0]
            self.assertAlmostEqual(claim["confidence_components"]["location"], 0.40)

    def test_the_relation_is_recorded_on_the_claim(self):
        claim = _claims([_gauge(relation="shared_watershed")])[0]
        self.assertEqual(claim["value"]["location_relation"], "shared_watershed")


class TestAsymmetry(unittest.TestCase):
    """Rising water elsewhere is actionable; calm water elsewhere is not."""

    def test_a_neighbour_cannot_stand_an_event_down_on_its_own(self):
        claims = _claims([_gauge(relation="shared_watershed")])
        self.assertEqual(_official_gauge_claims(claims), [])
        self.assertFalse(evidence_supports_all_clear(claims))

    def test_one_in_county_gauge_cannot_stand_an_event_down(self):
        claims = _claims([_gauge()])
        self.assertEqual(len(_official_gauge_claims(claims)), 1)
        self.assertFalse(evidence_supports_all_clear(claims))

    def test_a_calm_neighbour_does_not_dilute_an_in_county_all_clear(self):
        in_county = [
            _gauge(site_no=f"0807400{index}") for index in range(5)
        ]
        claims = _claims(
            in_county
            + [_gauge(site_no="08074999", relation="shared_watershed")]
        )
        self.assertEqual(len(_official_gauge_claims(claims)), 5)
        self.assertTrue(evidence_supports_all_clear(claims))

    def test_a_flooding_neighbour_blocks_an_all_clear(self):
        claims = _claims([
            _gauge(),
            _gauge(relation="shared_watershed", score=4.0, category="major_flooding"),
        ])
        self.assertFalse(evidence_supports_all_clear(claims))


class TestDiscoveryIsOptIn(unittest.TestCase):
    def test_neighbours_are_not_fetched_by_default(self):
        from watcher.config import watershed_neighbours_enabled

        self.assertFalse(watershed_neighbours_enabled())

    def test_the_flag_turns_them_on(self):
        from watcher.config import watershed_neighbours_enabled

        with mock.patch.dict(os.environ, {"SENTINEL_WATERSHED_NEIGHBOURS": "on"}):
            self.assertTrue(watershed_neighbours_enabled())


if __name__ == "__main__":
    unittest.main()
