"""NWS active Flood Warning alerts source."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from county_flood.api_resolution import (
    ACTIVE_FLOOD_WARNING_SCORE,
    build_nws_alerts_url,
    build_nws_zone_url,
    county_matches_area_desc,
    resolve_nws_zone,
)
from county_flood.geo import normalize_county_name, normalize_state
from county_flood.models import FloodAlert, SourceResult

REQUEST_TIMEOUT = 30

_FETCH_HEADERS = {
    "Accept": "application/geo+json",
    "User-Agent": "OpenClaw-county-flood-data/1.0",
}


def _request_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _parse_alert_feature(feature: Dict[str, Any]) -> FloodAlert:
    props = feature.get("properties") or {}
    geocode = props.get("geocode") or {}
    ugc = geocode.get("UGC") or []
    return FloodAlert(
        id=str(props.get("id") or feature.get("id") or ""),
        event=str(props.get("event") or ""),
        headline=str(props.get("headline") or ""),
        severity=str(props.get("severity") or ""),
        urgency=str(props.get("urgency") or ""),
        area_desc=str(props.get("areaDesc") or ""),
        effective=props.get("effective"),
        onset=props.get("onset"),
        expires=props.get("expires"),
        status=str(props.get("status") or ""),
        nws_zone=str(ugc[0]) if ugc else None,
        flood_state_score=ACTIVE_FLOOD_WARNING_SCORE,
    )


def _filter_alerts_for_county(
    alerts: List[FloodAlert],
    *,
    county: str,
    nws_zone: Optional[str],
) -> List[FloodAlert]:
    if not alerts:
        return alerts
    if not nws_zone:
        return [
            alert
            for alert in alerts
            if county_matches_area_desc(alert.area_desc, county)
        ]
    zone_upper = nws_zone.upper()
    filtered = [
        alert
        for alert in alerts
        if (alert.nws_zone or "").upper() == zone_upper
        or county_matches_area_desc(alert.area_desc, county)
    ]
    return filtered or alerts


def fetch_nws_alerts(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
) -> SourceResult:
    county_norm = normalize_county_name(county)
    state_norm = normalize_state(state)
    nws_zone, zone_resolution = resolve_nws_zone(
        fips=fips, state=state_norm, county=county_norm
    )
    api_url, url_resolution = build_nws_alerts_url(
        state=state_norm, nws_zone=nws_zone
    )

    alerts: List[FloodAlert] = []
    fetch_errors: List[Dict[str, str]] = []

    try:
        payload = _request_json(api_url)
        features = payload.get("features") or []
        alerts = [_parse_alert_feature(feature) for feature in features]
        if url_resolution == "state_fallback":
            alerts = _filter_alerts_for_county(
                alerts, county=county_norm, nws_zone=nws_zone
            )
    except urllib.error.HTTPError as exc:
        fetch_errors.append({"source": "nws_alerts", "error": f"HTTP {exc.code}"})
    except Exception as exc:
        fetch_errors.append({"source": "nws_alerts", "error": str(exc)})

    return SourceResult(
        source_id="nws_alerts",
        alerts=alerts,
        fetch_errors=fetch_errors,
        metadata={
            "api_url": api_url,
            "nws_alerts_url": api_url,
            "nws_zone": nws_zone,
            "nws_zone_url": build_nws_zone_url(nws_zone) if nws_zone else None,
            "zone_resolution": zone_resolution,
            "url_resolution": url_resolution,
            "alert_count": len(alerts),
            "scored_alert_count": sum(
                1 for alert in alerts if alert.flood_state_score is not None
            ),
        },
    )


class NwsAlertsFloodSource:
    source_id = "nws_alerts"

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
        return fetch_nws_alerts(county=county, state=state, fips=fips)
