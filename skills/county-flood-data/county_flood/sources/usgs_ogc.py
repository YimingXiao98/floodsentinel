"""USGS Water Data OGC API flood source — latest-continuous gage-height scoring."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from county_flood.models import GaugeReading, SourceResult
from county_flood.sources.stages import USGS_IV_BATCH_SIZE
from county_flood.sources.usgs import (
    USGS_DISCHARGE_CODE,
    USGS_GAGE_HEIGHT_CODE,
    _enrich_sites_from_nwps,
    _site_to_reading,
    select_county_usgs_sites,
)

USGS_OGC_BASE = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
USGS_OGC_LATEST_CONTINUOUS = f"{USGS_OGC_BASE}/latest-continuous/items"
REQUEST_TIMEOUT = 30

_FETCH_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OpenClaw-county-flood-data/1.0",
}


def _clean_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number in (-999, -9999):
        return None
    return number


def _site_no_from_monitoring_location_id(monitoring_location_id: str) -> str:
    value = (monitoring_location_id or "").strip()
    if value.upper().startswith("USGS-"):
        return value.split("-", 1)[1]
    return value


def _monitoring_location_ids(site_nos: Sequence[str]) -> str:
    return ",".join(f"USGS-{site_no}" for site_no in site_nos if site_no)


def _request_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _parse_latest_continuous(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    stations: Dict[str, Dict[str, Any]] = {}
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        site_no = _site_no_from_monitoring_location_id(props.get("monitoring_location_id") or "")
        if not site_no:
            continue

        if site_no not in stations:
            stations[site_no] = {
                "site_no": site_no,
                "observation_time": None,
                "streamflow_cfs": None,
                "gage_height_ft": None,
                "stage_unit": "ft",
            }

        parameter_code = props.get("parameter_code")
        observed_value = _clean_number(props.get("value"))
        observed_time = props.get("time")
        if observed_time:
            stations[site_no]["observation_time"] = observed_time

        unit = (props.get("unit_of_measure") or "").strip()
        if unit:
            stations[site_no]["stage_unit"] = unit

        if parameter_code == USGS_DISCHARGE_CODE:
            stations[site_no]["streamflow_cfs"] = observed_value
        elif parameter_code == USGS_GAGE_HEIGHT_CODE:
            stations[site_no]["gage_height_ft"] = observed_value

    return stations


def fetch_usgs_ogc_latest_continuous(site_nos: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not site_nos:
        return {}

    merged: Dict[str, Dict[str, Any]] = {}
    ids = list(site_nos)
    for start in range(0, len(ids), USGS_IV_BATCH_SIZE):
        chunk = ids[start : start + USGS_IV_BATCH_SIZE]
        params = urllib.parse.urlencode(
            {
                "f": "json",
                "monitoring_location_id": _monitoring_location_ids(chunk),
                "parameter_code": f"{USGS_DISCHARGE_CODE},{USGS_GAGE_HEIGHT_CODE}",
                "limit": max(50, len(chunk) * 4),
            }
        )
        payload = _request_json(f"{USGS_OGC_LATEST_CONTINUOUS}?{params}")
        merged.update(_parse_latest_continuous(payload))
    return merged


def fetch_usgs_ogc_county_gauges(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
    site_ids: Optional[Sequence[str]] = None,
) -> SourceResult:
    selected_sites, site_meta = select_county_usgs_sites(
        county=county,
        state=state,
        fips=fips,
        site_ids=site_ids,
    )

    for site in selected_sites:
        if site.get("threshold_source") is None:
            site["threshold_source"] = "csv"

    _enrich_sites_from_nwps(selected_sites)

    site_nos = [site["site_no"] for site in selected_sites if site.get("site_no")]
    fetch_errors: List[Dict[str, str]] = []
    gauges: List[GaugeReading] = []

    observations: Dict[str, Dict[str, Any]] = {}
    if site_nos:
        try:
            observations = fetch_usgs_ogc_latest_continuous(site_nos)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            fetch_errors.append({"source": "usgs_ogc", "error": str(exc)})
            for site in selected_sites:
                gauges.append(_site_to_reading(site, None, error=str(exc)))
            return SourceResult(
                source_id="usgs_ogc",
                gauges=gauges,
                fetch_errors=fetch_errors,
                metadata={
                    "api_base": USGS_OGC_LATEST_CONTINUOUS,
                    **site_meta,
                },
            )

    for site in selected_sites:
        site_no = site["site_no"]
        observation = observations.get(site_no)
        if not observation:
            gauges.append(_site_to_reading(site, None, error="no_ogc_data"))
            fetch_errors.append({"site_no": site_no, "error": "no_ogc_data"})
            continue
        gauges.append(_site_to_reading(site, observation))

    gauges.sort(key=lambda gauge: (gauge.priority_tier or 99, gauge.site_no or ""))

    return SourceResult(
        source_id="usgs_ogc",
        gauges=gauges,
        fetch_errors=fetch_errors,
        metadata={
            "api_base": USGS_OGC_LATEST_CONTINUOUS,
            **site_meta,
        },
    )


class UsgsOgcFloodSource:
    source_id = "usgs_ogc"

    def fetch(
        self,
        *,
        county: str,
        state: str,
        fips: Optional[str] = None,
        gauge_ids: Optional[Sequence[str]] = None,
        site_ids: Optional[Sequence[str]] = None,
        discover_categories: bool = False,
    ) -> SourceResult:
        selected_site_ids = site_ids or gauge_ids
        return fetch_usgs_ogc_county_gauges(
            county=county,
            state=state,
            fips=fips,
            site_ids=selected_site_ids,
        )
