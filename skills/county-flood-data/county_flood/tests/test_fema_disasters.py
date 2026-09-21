"""Tests for FEMA disaster parsing and county filtering."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from county_flood.sources.fema_disasters import (
    _filter_disasters_for_county,
    _filter_recent_disasters,
    _parse_disaster_record,
)


SAMPLE_RECORD = {
    "femaDeclarationString": "DR-4781-TX",
    "disasterNumber": 4781,
    "state": "TX",
    "declarationDate": "2024-05-17T00:00:00.000Z",
    "incidentType": "Flood",
    "declarationTitle": "SEVERE STORMS AND FLOODING",
    "incidentBeginDate": "2024-04-26T00:00:00.000Z",
    "incidentEndDate": "2024-06-05T00:00:00.000Z",
    "fipsStateCode": "48",
    "fipsCountyCode": "201",
    "designatedArea": "Harris (County)",
    "iaProgramDeclared": True,
    "paProgramDeclared": True,
    "hmProgramDeclared": True,
}


class TestFemaDisasters(unittest.TestCase):
    def test_parse_disaster_record_recent_score(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        disaster = _parse_disaster_record(
            {**SAMPLE_RECORD, "declarationDate": f"{today}T00:00:00.000Z"}
        )
        self.assertEqual(disaster.flood_state_score, 2.0)

    def test_parse_disaster_record_old_no_score(self) -> None:
        disaster = _parse_disaster_record(SAMPLE_RECORD)
        self.assertIsNone(disaster.flood_state_score)

    def test_filter_recent_disasters(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        recent = _parse_disaster_record(
            {**SAMPLE_RECORD, "declarationDate": f"{today}T00:00:00.000Z"}
        )
        old = _parse_disaster_record(SAMPLE_RECORD)
        filtered = _filter_recent_disasters([recent, old])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].declaration_date, recent.declaration_date)

    def test_filter_disasters_state_fallback(self) -> None:
        harris = _parse_disaster_record(SAMPLE_RECORD)
        coke = _parse_disaster_record(
            {
                **SAMPLE_RECORD,
                "disasterNumber": 4879,
                "designatedArea": "Coke (County)",
                "fipsCountyCode": "081",
            }
        )
        filtered = _filter_disasters_for_county(
            [harris, coke], county="Harris", fips_county_code=None
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].designated_area, "Harris (County)")

    def test_filter_disasters_skipped_when_fips_present(self) -> None:
        harris = _parse_disaster_record(SAMPLE_RECORD)
        coke = _parse_disaster_record(
            {
                **SAMPLE_RECORD,
                "designatedArea": "Coke (County)",
            }
        )
        filtered = _filter_disasters_for_county(
            [harris, coke], county="Harris", fips_county_code="201"
        )
        self.assertEqual(len(filtered), 2)


if __name__ == "__main__":
    unittest.main()
