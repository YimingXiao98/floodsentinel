"""How a claim reached the county grades how precisely it is placed.

The location term asks whether a claim resolves to the area being monitored, so
what should grade it is the strength of that resolution. A gauge returned by a
query keyed on the county's FIPS code is one the agency places in the county. A
gauge matched only on the county's name is usually the same gauge, but name
matching is fallible in a way FIPS is not: county names repeat across states,
and an independent city can share a name with a separate county.

Without this the term was constant for every in-county gauge, and a composite
with a constant term is a composite with one fewer term.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from watcher.claims.confidence import score_credibility
from watcher.claims.extract_county_flood import extract_county_flood_claims


def _no_ablation():
    env = {k: v for k, v in os.environ.items() if k != "SENTINEL_ABLATION"}
    return mock.patch.dict(os.environ, env, clear=True)


def _gauge(**kw):
    base = {
        "site_no": "08074000",
        "name": "Test Bayou",
        "source": "usgs",
        "observed_primary": 12.0,
        "observed_valid_time": "2026-08-08T00:00:00Z",
        "observed_flood_category": "no_flooding",
        "flood_state_score": 0.0,
        "flood_stage": 28.0,
        "classification_method": "stage_derived",
        "threshold_source": "csv",
    }
    base.update(kw)
    return base


def _claim(gauge):
    snap = {
        "source_id": "src", "snapshot_id": "s", "snapshot_url": "u",
        "fetched_at": "2026-08-08T01:00:00Z",
        "raw_text": json.dumps({"schema": "county-flood-data/1", "gauges": [gauge]}),
    }
    claims, _ = extract_county_flood_claims(
        snap, county="Harris", state="TX", location="Harris County, TX")
    return claims[0]


class TestBasisOrdersLocation(unittest.TestCase):
    def _loc(self, basis):
        with _no_ablation():
            return score_credibility(location_basis=basis)["location"]

    def test_fips_beats_a_name_match(self):
        self.assertGreater(self._loc("fips_verified"), self._loc("name_matched"))

    def test_a_name_match_beats_a_gauge_in_another_county(self):
        self.assertGreater(self._loc("name_matched"), self._loc("shared_watershed"))

    def test_a_substitute_still_beats_an_unresolved_location(self):
        self.assertGreater(self._loc("shared_watershed"), self._loc("unresolved"))

    def test_an_unknown_basis_falls_back_to_the_match_flag(self):
        with _no_ablation():
            self.assertEqual(score_credibility(location_basis="something_else")["location"],
                             score_credibility(county_match=True)["location"])


class TestPrecedence(unittest.TestCase):
    def test_an_explicit_precision_outranks_the_basis(self):
        with _no_ablation():
            got = score_credibility(location_basis="fips_verified",
                                    location_precision=0.40)["location"]
            self.assertAlmostEqual(got, 0.40)

    def test_the_basis_outranks_the_bare_match_flag(self):
        with _no_ablation():
            got = score_credibility(county_match=True, location_basis="name_matched")
            self.assertAlmostEqual(got["location"], 0.85)


class TestItReachesTheClaim(unittest.TestCase):
    def test_a_fips_verified_gauge_scores_above_a_name_matched_one(self):
        with _no_ablation():
            fips = _claim(_gauge(location_basis="fips_verified"))
            name = _claim(_gauge(location_basis="name_matched"))
            self.assertGreater(fips["confidence_components"]["location"],
                               name["confidence_components"]["location"])
            self.assertGreater(fips["overall_confidence"], name["overall_confidence"])

    def test_a_gauge_with_no_recorded_basis_is_unchanged(self):
        # Corpora built before this existed must score exactly as they did.
        with _no_ablation():
            self.assertAlmostEqual(
                _claim(_gauge())["confidence_components"]["location"], 0.90)


if __name__ == "__main__":
    unittest.main()
