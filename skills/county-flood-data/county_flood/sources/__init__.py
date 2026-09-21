"""Flood source registry — extensible plugin point for county flood data."""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol, Sequence

from county_flood.models import SourceResult
from county_flood.sources.fema_disasters import (
    FemaDisastersFloodSource,
    fetch_fema_disasters,
)
from county_flood.sources.nws_alerts import NwsAlertsFloodSource, fetch_nws_alerts
from county_flood.sources.nwps import NwpsFloodSource, fetch_nwps_county_gauges
from county_flood.sources.usgs import UsgsFloodSource, fetch_usgs_county_gauges
from county_flood.sources.usgs_ogc import UsgsOgcFloodSource, fetch_usgs_ogc_county_gauges


class FloodSource(Protocol):
    source_id: str

    def fetch(
        self,
        *,
        county: str,
        state: str,
        fips: Optional[str] = None,
        gauge_ids: Optional[Sequence[str]] = None,
        site_ids: Optional[Sequence[str]] = None,
        discover_categories: bool = False,
    ) -> SourceResult: ...


SOURCE_REGISTRY: Dict[str, FloodSource] = {
    "nwps": NwpsFloodSource(),
    "usgs": UsgsFloodSource(),
    "usgs_ogc": UsgsOgcFloodSource(),
    "nws_alerts": NwsAlertsFloodSource(),
    "fema_disasters": FemaDisastersFloodSource(),
}


def active_sources() -> List[FloodSource]:
    return list(SOURCE_REGISTRY.values())


__all__ = [
    "FloodSource",
    "NwpsFloodSource",
    "UsgsFloodSource",
    "UsgsOgcFloodSource",
    "NwsAlertsFloodSource",
    "FemaDisastersFloodSource",
    "SOURCE_REGISTRY",
    "active_sources",
    "fetch_nwps_county_gauges",
    "fetch_usgs_county_gauges",
    "fetch_usgs_ogc_county_gauges",
    "fetch_nws_alerts",
    "fetch_fema_disasters",
]
