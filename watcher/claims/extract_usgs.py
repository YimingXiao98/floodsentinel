"""Extract gauge claims from USGS NWIS IV JSON."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from watcher.claims.confidence import (
    build_confidence_breakdown,
    overall_confidence,
    score_usgs_hydrology_claim,
)
from watcher.claims.models import new_claim

USGS_DISCHARGE_CODE = "00060"
USGS_GAGE_HEIGHT_CODE = "00065"


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_nwis_iv(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse NWIS IV timeSeries into per-station observations."""
    stations: Dict[str, Dict[str, Any]] = {}
    for item in (payload.get("value") or {}).get("timeSeries") or []:
        site_codes = item.get("sourceInfo", {}).get("siteCode") or []
        if not site_codes:
            continue
        site_no = str(site_codes[0].get("value") or "")
        if not site_no:
            continue

        if site_no not in stations:
            geo = item.get("sourceInfo", {}).get("geoLocation", {}).get("geogLocation", {})
            stations[site_no] = {
                "site_no": site_no,
                "station_name": item.get("sourceInfo", {}).get("siteName") or site_no,
                "observation_time": None,
                "streamflow_cfs": None,
                "gage_height_ft": None,
                "lat": _safe_float(geo.get("latitude")),
                "lon": _safe_float(geo.get("longitude")),
            }

        variable_codes = item.get("variable", {}).get("variableCode") or []
        parameter_code = variable_codes[0].get("value") if variable_codes else None
        values = item.get("values") or []
        if not values:
            continue
        observations = values[0].get("value") or []
        if not observations:
            continue

        latest = observations[-1]
        observed_value = _safe_float(latest.get("value"))
        observed_time = latest.get("dateTime")
        if observed_time:
            stations[site_no]["observation_time"] = observed_time

        if parameter_code == USGS_DISCHARGE_CODE:
            stations[site_no]["streamflow_cfs"] = observed_value
        elif parameter_code == USGS_GAGE_HEIGHT_CODE:
            stations[site_no]["gage_height_ft"] = observed_value

    return list(stations.values())


def _derive_severity(
    gage_height_ft: Optional[float],
    streamflow_cfs: Optional[float],
) -> str:
    """Heuristic severity from available hydrology measurements."""
    if gage_height_ft is not None and gage_height_ft >= 20:
        return "moderate"
    if streamflow_cfs is not None and streamflow_cfs >= 10000:
        return "moderate"
    if gage_height_ft is not None or streamflow_cfs is not None:
        return "minor"
    return "unknown"


def _build_excerpt(station: Dict[str, Any]) -> str:
    parts = [f"USGS {station.get('station_name')} ({station.get('site_no')})"]
    if station.get("gage_height_ft") is not None:
        parts.append(f"gage height {station['gage_height_ft']:.2f} ft")
    if station.get("streamflow_cfs") is not None:
        parts.append(f"streamflow {station['streamflow_cfs']:.0f} cfs")
    if station.get("observation_time"):
        parts.append(f"observed {station['observation_time']}")
    return "; ".join(parts)


def extract_usgs_claims(
    snapshot: Dict[str, Any],
    *,
    county: str,
    state: str,
    location: str,
    raw_text: Optional[str] = None,
    source_record: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return (claims, extraction_failure_or_none)."""
    source_id = snapshot.get("source_id", "unknown")
    snapshot_id = snapshot.get("snapshot_id")
    evidence_url = snapshot.get("snapshot_url", "")
    observed_at = snapshot.get("fetched_at") or snapshot.get("fetched_at_utc", "")
    text = raw_text if raw_text is not None else snapshot.get("raw_text") or snapshot.get("raw_excerpt", "")
    byte_length = snapshot.get("byte_length", 0)

    api_resolution = (source_record or {}).get("api_resolution") or {}
    county_match = bool(api_resolution.get("county_match"))
    trust_tier = (source_record or {}).get("trust_tier") or {}
    trust_level = trust_tier.get("level", 0)

    if not text or not text.strip() or byte_length == 0:
        return [], {
            "source_id": source_id,
            "source_url": evidence_url,
            "snapshot_id": snapshot_id,
            "failure_reason": "empty_response_body",
        }

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [], {
            "source_id": source_id,
            "source_url": evidence_url,
            "snapshot_id": snapshot_id,
            "failure_reason": "invalid_json",
        }

    stations = _parse_nwis_iv(payload)
    if not stations:
        return [], {
            "source_id": source_id,
            "source_url": evidence_url,
            "snapshot_id": snapshot_id,
            "failure_reason": "no_nwis_timeseries",
        }

    claims: List[Dict[str, Any]] = []
    for station in stations:
        gage_height = station.get("gage_height_ft")
        streamflow = station.get("streamflow_cfs")
        obs_time = station.get("observation_time")
        severity = _derive_severity(gage_height, streamflow)

        components = score_usgs_hydrology_claim(
            county_match=county_match,
            observation_time=obs_time,
            gage_height_ft=gage_height,
            streamflow_cfs=streamflow,
            trust_tier_level=trust_level,
        )

        geo_ref = None
        if station.get("lat") is not None and station.get("lon") is not None:
            geo_ref = {"lat": station["lat"], "lon": station["lon"]}

        claim_id = f"claim_usgs_{uuid.uuid4().hex[:12]}"
        overall = overall_confidence(components)
        breakdown = build_confidence_breakdown(
            components,
            trust_tier_level=trust_level,
            method="corroborated",
            method_confidence=overall,
        )
        claim = new_claim(
            claim_id=claim_id,
            source_id=source_id,
            claim_type="gauge_rising",
            excerpt=_build_excerpt(station),
            evidence_url=evidence_url,
            location_text=location,
            observed_at=obs_time or observed_at,
            published_at=obs_time,
            severity=severity,
            value={
                "site_no": station.get("site_no"),
                "gage_height_ft": gage_height,
                "streamflow_cfs": streamflow,
                "observation_time": obs_time,
            },
            snapshot_id=snapshot_id,
            confidence_components=components,
            geo_reference=geo_ref,
        )
        claim["confidence_breakdown"] = breakdown
        claim["source_reliability_prior"] = breakdown["source_reliability_prior"]
        claims.append(claim)

    return claims, None
