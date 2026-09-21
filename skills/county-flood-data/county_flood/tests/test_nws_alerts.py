"""Tests for NWS alerts parsing and county filtering."""

from __future__ import annotations

import unittest

from county_flood.sources.nws_alerts import _filter_alerts_for_county, _parse_alert_feature


SAMPLE_FEATURE = {
    "id": "https://api.weather.gov/alerts/urn:oid:test",
    "properties": {
        "id": "urn:oid:test",
        "event": "Flood Warning",
        "headline": "Flood Warning for Lamar County",
        "severity": "Severe",
        "urgency": "Immediate",
        "areaDesc": "Lamar, TX",
        "effective": "2026-06-23T10:54:00-05:00",
        "expires": "2026-06-23T14:00:00-05:00",
        "status": "Actual",
        "geocode": {"UGC": ["TXC277"], "SAME": ["048277"]},
    },
}


class TestNwsAlerts(unittest.TestCase):
    def test_parse_alert_feature(self) -> None:
        alert = _parse_alert_feature(SAMPLE_FEATURE)
        self.assertEqual(alert.event, "Flood Warning")
        self.assertEqual(alert.nws_zone, "TXC277")
        self.assertEqual(alert.area_desc, "Lamar, TX")

    def test_filter_alerts_state_fallback(self) -> None:
        alert = _parse_alert_feature(SAMPLE_FEATURE)
        other = _parse_alert_feature(
            {
                **SAMPLE_FEATURE,
                "properties": {
                    **SAMPLE_FEATURE["properties"],
                    "areaDesc": "Harris, TX",
                    "geocode": {"UGC": ["TXC201"]},
                },
            }
        )
        filtered = _filter_alerts_for_county(
            [alert, other], county="Lamar", nws_zone=None
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].area_desc, "Lamar, TX")


if __name__ == "__main__":
    unittest.main()
