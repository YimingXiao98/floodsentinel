"""Finding gauges that share a county's watershed.

USGS reports a hydrologic unit code per gauge at several resolutions in one
column. Eight digits is the subbasin, which is the scale a flood signal travels
across, so that is the level neighbours are matched on.

The extra queries are only worth issuing for a county that cannot be decided on
from its own gauges, so discovery is gated on how few of those there are.
"""

from __future__ import annotations

import unittest
from unittest import mock

from county_flood.sources import usgs


_RDB = "\n".join([
    "agency_cd\tsite_no\tstation_nm\tdec_lat_va\tdec_long_va\thuc_cd",
    "5s\t15s\t50s\t16s\t16s\t16s",
    "USGS\t02290000\tBarron River\t25.90\t-80.19\t03090202",
    "USGS\t02291000\tTurner River\t25.95\t-80.25\t03090202",
    "USGS\t02999999\tFar Away Creek\t28.50\t-82.00\t03090202",
])


class TestSubbasinCodes(unittest.TestCase):
    def test_a_longer_code_is_truncated_to_the_subbasin(self):
        self.assertEqual(usgs.subbasin_codes([{"huc_cd": "030902021200"}]), ["03090202"])

    def test_codes_are_deduplicated(self):
        codes = usgs.subbasin_codes(
            [{"huc_cd": "030902021200"}, {"huc_cd": "03090202"}]
        )
        self.assertEqual(codes, ["03090202"])

    def test_a_code_coarser_than_a_subbasin_is_ignored(self):
        self.assertEqual(usgs.subbasin_codes([{"huc_cd": "0309"}]), [])

    def test_missing_codes_are_ignored(self):
        self.assertEqual(usgs.subbasin_codes([{"huc_cd": ""}, {}]), [])


class TestNeighbourDiscovery(unittest.TestCase):
    _OWN = [{"site_no": "02290000", "huc_cd": "03090202",
             "lat": 25.90, "lon": -80.19}]

    def test_the_county_s_own_gauges_are_not_returned_as_neighbours(self):
        with mock.patch.object(usgs, "_request_text", return_value=_RDB):
            found = usgs.discover_watershed_neighbour_sites(own_sites=self._OWN)
        self.assertIn("02291000", [s["site_no"] for s in found])
        self.assertNotIn("02290000", [s["site_no"] for s in found])

    def test_neighbours_are_marked_so_they_can_be_weighed_differently(self):
        with mock.patch.object(usgs, "_request_text", return_value=_RDB):
            found = usgs.discover_watershed_neighbour_sites(own_sites=self._OWN)
        self.assertEqual(found[0]["location_relation"], "shared_watershed")

    def test_a_gauge_too_far_away_is_not_a_substitute(self):
        # Shared drainage is not enough: a subbasin in flat terrain can span
        # more than a hundred kilometres.
        with mock.patch.object(usgs, "_request_text", return_value=_RDB):
            found = usgs.discover_watershed_neighbour_sites(own_sites=self._OWN)
        self.assertNotIn("02999999", [s["site_no"] for s in found])

    def test_the_distance_ceiling_can_be_widened(self):
        with mock.patch.object(usgs, "_request_text", return_value=_RDB):
            found = usgs.discover_watershed_neighbour_sites(
                own_sites=self._OWN, max_distance_km=500.0
            )
        self.assertIn("02999999", [s["site_no"] for s in found])

    def test_a_candidate_without_coordinates_is_not_used(self):
        no_coords = "\n".join([
            "agency_cd\tsite_no\tstation_nm\thuc_cd",
            "5s\t15s\t50s\t16s",
            "USGS\t02291000\tTurner River\t03090202",
        ])
        with mock.patch.object(usgs, "_request_text", return_value=no_coords):
            found = usgs.discover_watershed_neighbour_sites(own_sites=self._OWN)
        self.assertEqual(found, [])

    def test_a_county_with_no_usable_code_queries_nothing(self):
        with mock.patch.object(usgs, "_request_text") as req:
            found = usgs.discover_watershed_neighbour_sites(
                own_sites=[{"site_no": "1", "huc_cd": ""}]
            )
        self.assertEqual(found, [])
        req.assert_not_called()


class TestDiscoveryIsGated(unittest.TestCase):
    """What decides a county is how many gauges can be scored, not how many exist.

    A county can be dense with sensors and still have nothing to compare a
    reading against, which is the case that needs substitutes most.
    """

    def _fetch(self, own, *, enrich_gives_thresholds=True, **kw):
        def enrich(sites):
            if enrich_gives_thresholds:
                for s in sites:
                    s.setdefault("flood_stage", None)
                    if s.get("location_relation") == "shared_watershed":
                        s["flood_stage"] = 15.0
        with mock.patch.object(usgs, "select_county_usgs_sites",
                               return_value=(list(own), {})), \
             mock.patch.object(usgs, "_enrich_sites_from_nwps", side_effect=enrich), \
             mock.patch.object(usgs, "fetch_usgs_iv", return_value={}), \
             mock.patch.object(usgs, "_request_text", return_value=_RDB):
            return usgs.fetch_usgs_county_gauges(
                county="Test", state="FL", fips="12086", **kw
            )

    @staticmethod
    def _site(i, *, flood_stage):
        return {"site_no": str(i), "huc_cd": "03090202", "flood_stage": flood_stage,
                "name": f"site {i}", "stage_unit": "ft",
                "lat": 25.90, "lon": -80.19}

    def test_a_county_with_enough_scorable_gauges_does_not_reach_out(self):
        own = [self._site(i, flood_stage=20.0) for i in range(5)]
        res = self._fetch(own, include_watershed_neighbours=True)
        self.assertEqual(res.metadata["watershed_neighbour_sites"], 0)

    def test_a_county_dense_with_unscorable_gauges_still_reaches_out(self):
        # The Miami-Dade case: plenty of sensors, none with a flood threshold.
        own = [self._site(i, flood_stage=None) for i in range(10)]
        res = self._fetch(own, include_watershed_neighbours=True)
        self.assertGreater(res.metadata["watershed_neighbour_sites"], 0)

    def test_an_unscorable_neighbour_is_not_carried(self):
        own = [self._site(i, flood_stage=None) for i in range(10)]
        # Enrichment leaves the neighbours without thresholds too, so they add a
        # fetch and tell the county nothing.
        res = self._fetch(own, include_watershed_neighbours=True,
                          enrich_gives_thresholds=False)
        self.assertEqual(res.metadata["watershed_neighbour_sites"], 0)
        self.assertGreater(res.metadata["watershed_neighbours_found"], 0)
        for gauge in res.gauges:
            if gauge.location_relation == "shared_watershed":
                self.fail("kept a neighbour that cannot be scored")

    def test_nothing_is_fetched_unless_asked_for(self):
        own = [self._site(i, flood_stage=None) for i in range(10)]
        with mock.patch.object(usgs, "discover_watershed_neighbour_sites") as disc:
            res = self._fetch(own)
        self.assertEqual(res.metadata["watershed_neighbour_sites"], 0)
        disc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
