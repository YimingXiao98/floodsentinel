"""Tests for county → API parameter resolution."""

from __future__ import annotations

import unittest

from county_flood.api_resolution import (
    build_fema_disasters_url,
    build_nws_alerts_url,
    build_nws_zone_url,
    county_matches_area_desc,
    county_matches_fema_designated_area,
    derive_nws_zone,
    is_recent_disaster_date,
    resolve_api_urls,
    resolve_fema_fips_parts,
    resolve_nws_zone,
)


class TestApiResolution(unittest.TestCase):
    def test_derive_nws_zone(self) -> None:
        self.assertEqual(derive_nws_zone("48201", "TX"), "TXC201")
        self.assertEqual(derive_nws_zone("53033", "Washington"), "WAC033")

    def test_resolve_fema_fips_parts(self) -> None:
        self.assertEqual(resolve_fema_fips_parts("48201"), ("48", "201"))
        self.assertEqual(resolve_fema_fips_parts(None), (None, None))

    def test_resolve_nws_zone_derived(self) -> None:
        zone, method = resolve_nws_zone(fips="53033", state="WA", county="King")
        self.assertEqual(zone, "WAC033")
        self.assertEqual(method, "derived")

    def test_resolve_nws_zone_registry(self) -> None:
        zone, method = resolve_nws_zone(fips="48201", state="TX", county="Harris")
        self.assertEqual(zone, "TXC201")
        self.assertEqual(method, "registry")

    def test_build_nws_alerts_url_zone(self) -> None:
        url, method = build_nws_alerts_url(state="TX", nws_zone="TXC201")
        self.assertIn("zone=TXC201", url)
        self.assertIn("event=Flood+Warning", url)
        self.assertEqual(method, "zone")

    def test_build_nws_alerts_url_state_fallback(self) -> None:
        url, method = build_nws_alerts_url(state="TX", nws_zone=None)
        self.assertIn("area=TX", url)
        self.assertIn("event=Flood+Warning", url)
        self.assertEqual(method, "state_fallback")

    def test_build_fema_disasters_url_county(self) -> None:
        url, method = build_fema_disasters_url(state="TX", fips_county_code="201")
        self.assertIn("state+eq+%27TX%27", url)
        self.assertIn("incidentType+eq+%27Flood%27", url)
        self.assertIn("fipsCountyCode+eq+%27201%27", url)
        self.assertEqual(method, "county")

    def test_build_fema_disasters_url_recent(self) -> None:
        from datetime import date

        url, method = build_fema_disasters_url(
            state="TX",
            fips_county_code="201",
            declaration_start=date(2026, 6, 22),
            declaration_end=date(2026, 6, 23),
        )
        self.assertIn("declarationDate+ge+%272026-06-22%27", url)
        self.assertIn("declarationDate+lt+%272026-06-24%27", url)
        self.assertEqual(method, "county_recent")

    def test_build_nws_zone_url(self) -> None:
        self.assertEqual(
            build_nws_zone_url("TXC201"),
            "https://api.weather.gov/zones/county/TXC201",
        )

    def test_resolve_api_urls(self) -> None:
        resolved = resolve_api_urls(county="Harris", state="TX", fips="48201")
        self.assertEqual(resolved["nws_zone"], "TXC201")
        self.assertIn("api.weather.gov/alerts/active", resolved["nws_alerts_url"])
        self.assertIn("zone=TXC201", resolved["nws_alerts_url"])
        self.assertEqual(
            resolved["nws_zone_url"],
            "https://api.weather.gov/zones/county/TXC201",
        )
        self.assertIn("fipsCountyCode+eq+%27201%27", resolved["fema_disasters_url"])

    def test_is_recent_disaster_date(self) -> None:
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date().isoformat()
        self.assertTrue(
            is_recent_disaster_date(f"{today}T00:00:00.000Z")
        )
        self.assertFalse(is_recent_disaster_date("2020-01-01T00:00:00.000Z"))

    def test_build_fema_disasters_url_state_only(self) -> None:
        url, method = build_fema_disasters_url(state="TX", fips_county_code=None)
        self.assertIn("state+eq+%27TX%27", url)
        self.assertNotIn("fipsCountyCode", url)
        self.assertEqual(method, "state")

    def test_county_matches_area_desc(self) -> None:
        self.assertTrue(county_matches_area_desc("Lamar, TX", "Lamar"))
        self.assertFalse(county_matches_area_desc("Lamar, TX", "Harris"))

    def test_county_matches_fema_designated_area(self) -> None:
        self.assertTrue(
            county_matches_fema_designated_area("Harris (County)", "Harris")
        )
        self.assertFalse(
            county_matches_fema_designated_area("Coke (County)", "Harris")
        )


if __name__ == "__main__":
    unittest.main()
