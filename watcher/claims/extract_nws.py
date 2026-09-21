"""Extract flood-related claims from NWS GeoJSON alerts."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional

from watcher.claims.confidence import (
    build_confidence_breakdown,
    overall_confidence,
    score_nws_alert_claim,
)
from watcher.claims.models import NWS_SEVERITY_MAP, new_claim

FLOOD_EVENT_RE = re.compile(
    r"flood|flash flood|coastal flood|river flood|areal flood|hydrolog",
    re.IGNORECASE,
)


def _county_matches(area_desc: str, county: str, state: str) -> bool:
    text = area_desc.lower()
    county_l = county.lower().replace(" county", "").strip()
    state_l = state.lower().strip()
    if county_l and county_l in text:
        return True
    if state_l and len(state_l) == 2 and state_l.upper() in area_desc.upper():
        return county_l in text if county_l else True
    return False


def _map_nws_severity(props: Dict[str, Any]) -> str:
    raw = (props.get("severity") or "unknown").lower()
    return NWS_SEVERITY_MAP.get(raw, "unknown")


def _claim_type_for_event(event: str) -> str:
    event_l = (event or "").lower()
    if "forecast" in event_l or "watch" in event_l:
        return "forecast_shift"
    if FLOOD_EVENT_RE.search(event_l):
        return "warning"
    return "warning"


def _claim_type_for_feature(props: Dict[str, Any]) -> str:
    return _claim_type_for_event(props.get("event") or "")


def _geometry_centroid(geometry: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    if not geometry:
        return None
    coords = geometry.get("coordinates")
    gtype = geometry.get("type")
    if not coords:
        return None

    points: List[List[float]] = []
    if gtype == "Point":
        points = [coords]
    elif gtype == "Polygon" and coords:
        ring = coords[0] if coords else []
        points = ring
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly and poly[0]:
                points.extend(poly[0])

    if not points:
        return None
    lons = [p[0] for p in points if len(p) >= 2]
    lats = [p[1] for p in points if len(p) >= 2]
    if not lons or not lats:
        return None
    return {"lat": sum(lats) / len(lats), "lon": sum(lons) / len(lons)}


def _attach_nws_breakdown(
    claim: Dict[str, Any],
    *,
    components: Dict[str, float],
    trust_level: int,
    zone_validated: bool,
    area_desc_match: bool,
) -> None:
    uncertainty: List[str] = []
    if not zone_validated and area_desc_match:
        uncertainty.append("areaDesc match only")
    elif not zone_validated and not area_desc_match:
        uncertainty.append("weak location match")
    overall = claim.get("overall_confidence") or overall_confidence(components)
    breakdown = build_confidence_breakdown(
        components,
        trust_tier_level=trust_level,
        method="corroborated",
        method_confidence=float(overall),
        uncertainty_notes=uncertainty,
    )
    claim["confidence_breakdown"] = breakdown
    claim["source_reliability_prior"] = breakdown["source_reliability_prior"]


def _build_nws_claim(
    *,
    source_id: str,
    snapshot_id: Optional[str],
    evidence_url: str,
    observed_at: str,
    county: str,
    state: str,
    location: str,
    event: str,
    headline: str,
    description: str,
    area_desc: str,
    severity: str,
    claim_type: str,
    published: Optional[str],
    urgency: Any,
    nws_zone: Optional[str],
    trust_level: int,
    zone_validated: bool,
    area_desc_match: bool,
    geo_ref: Optional[Dict[str, float]],
) -> Dict[str, Any]:
    components = score_nws_alert_claim(
        zone_validated=zone_validated,
        area_desc_match=area_desc_match,
        has_geometry=geo_ref is not None,
        published_at=str(published) if published else None,
        severity=severity,
        trust_tier_level=trust_level,
        now=observed_at,
    )
    excerpt = f"{headline}: {description[:200]}"
    claim_id = f"claim_nws_{uuid.uuid4().hex[:12]}"
    claim = new_claim(
        claim_id=claim_id,
        source_id=source_id,
        claim_type=claim_type,
        excerpt=excerpt,
        evidence_url=evidence_url,
        location_text=area_desc or location,
        observed_at=observed_at,
        published_at=str(published) if published else None,
        severity=severity,
        value={
            "event": event,
            "headline": headline,
            "severity": severity,
            "urgency": urgency,
            "nws_zone": nws_zone,
            "evidence_tier": "T0",
            "source_class": "official_feed",
        },
        snapshot_id=snapshot_id,
        confidence_components=components,
        geo_reference=geo_ref,
    )
    _attach_nws_breakdown(
        claim,
        components=components,
        trust_level=trust_level,
        zone_validated=zone_validated,
        area_desc_match=area_desc_match,
    )
    return claim


def extract_nws_claims_from_alert_dicts(
    snapshot: Dict[str, Any],
    *,
    county: str,
    state: str,
    location: str,
    source_record: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Parse county-flood-nws-alerts/1 snapshots into typed claims."""
    source_id = snapshot.get("source_id", "unknown")
    snapshot_id = snapshot.get("snapshot_id")
    evidence_url = snapshot.get("snapshot_url", "")
    observed_at = snapshot.get("fetched_at") or snapshot.get("fetched_at_utc", "")
    text = snapshot.get("raw_text") or snapshot.get("raw_excerpt", "")

    api_resolution = (source_record or {}).get("api_resolution") or {}
    zone_validated = bool(api_resolution.get("county_match"))
    trust_tier = (source_record or {}).get("trust_tier") or {}
    trust_level = trust_tier.get("level", 0)

    if not text or not text.strip():
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    if payload.get("schema") != "county-flood-nws-alerts/1":
        return []

    alerts = payload.get("alerts") or []
    claims: List[Dict[str, Any]] = []

    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        event = alert.get("event") or ""
        if not FLOOD_EVENT_RE.search(event):
            continue
        area_desc = alert.get("area_desc") or location
        area_desc_match = _county_matches(area_desc, county, state)
        if not zone_validated and not area_desc_match:
            continue
        severity = _map_nws_severity(alert)
        claim_type = _claim_type_for_event(event)
        published = alert.get("effective") or alert.get("published_at")
        headline = alert.get("headline") or event
        claims.append(
            _build_nws_claim(
                source_id=source_id,
                snapshot_id=snapshot_id,
                evidence_url=evidence_url,
                observed_at=observed_at,
                county=county,
                state=state,
                location=location,
                event=event,
                headline=headline,
                description=alert.get("description") or "",
                area_desc=area_desc,
                severity=severity,
                claim_type=claim_type,
                published=str(published) if published else None,
                urgency=alert.get("urgency"),
                nws_zone=alert.get("nws_zone") or payload.get("nws_zone"),
                trust_level=trust_level,
                zone_validated=zone_validated,
                area_desc_match=area_desc_match,
                geo_ref=None,
            )
        )

    return claims


def extract_nws_claims(
    snapshot: Dict[str, Any],
    *,
    county: str,
    state: str,
    location: str,
    raw_text: Optional[str] = None,
    source_record: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Parse NWS active alerts; filter to county flood-related features."""
    source_id = snapshot.get("source_id", "unknown")
    snapshot_id = snapshot.get("snapshot_id")
    evidence_url = snapshot.get("snapshot_url", "")
    observed_at = snapshot.get("fetched_at") or snapshot.get("fetched_at_utc", "")
    text = raw_text if raw_text is not None else snapshot.get("raw_text") or snapshot.get("raw_excerpt", "")

    api_resolution = (source_record or {}).get("api_resolution") or {}
    zone_validated = bool(api_resolution.get("county_match"))
    trust_tier = (source_record or {}).get("trust_tier") or {}
    trust_level = trust_tier.get("level", 0)

    if not text or not text.strip():
        return []

    try:
        payload = json.loads(text) if text.lstrip().startswith("{") else None
    except json.JSONDecodeError:
        return []

    if not payload:
        return []

    if payload.get("schema") == "county-flood-nws-alerts/1":
        return extract_nws_claims_from_alert_dicts(
            snapshot,
            county=county,
            state=state,
            location=location,
            source_record=source_record,
        )

    features = payload.get("features") or []
    claims: List[Dict[str, Any]] = []

    for feature in features:
        props = feature.get("properties") or {}
        event = props.get("event") or ""
        if not FLOOD_EVENT_RE.search(event):
            continue

        area_desc = props.get("areaDesc") or location
        area_desc_match = _county_matches(area_desc, county, state)
        if not zone_validated and not area_desc_match:
            continue

        severity = _map_nws_severity(props)
        claim_type = _claim_type_for_feature(props)
        published = props.get("sent") or props.get("effective") or props.get("onset")
        headline = props.get("headline") or event
        geo_ref = _geometry_centroid(feature.get("geometry"))
        geocode = props.get("geocode") or {}
        ugc = geocode.get("UGC") or []
        nws_zone = ugc[0] if ugc else None

        claims.append(
            _build_nws_claim(
                source_id=source_id,
                snapshot_id=snapshot_id,
                evidence_url=evidence_url,
                observed_at=observed_at,
                county=county,
                state=state,
                location=location,
                event=event,
                headline=headline,
                description=(props.get("description") or "")[:200],
                area_desc=area_desc,
                severity=severity,
                claim_type=claim_type,
                published=str(published) if published else None,
                urgency=props.get("urgency"),
                nws_zone=nws_zone,
                trust_level=trust_level,
                zone_validated=zone_validated,
                area_desc_match=area_desc_match,
                geo_ref=geo_ref,
            )
        )

    return claims
