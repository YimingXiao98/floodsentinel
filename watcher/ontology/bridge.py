"""Ontology bridge — stub monitoring summaries and source records."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from watcher.sources.county_api_resolver import (
    build_source_url,
    resolve_county_flood_sources,
)

_STUB_PATH = Path(__file__).resolve().parent / "stub_monitoring.json"
_SOURCES_STUB_PATH = Path(__file__).resolve().parent / "stub_sources.json"
_PHASE_IRI = {
    "early_watch": "ds:EarlyWatch",
    "elevated_watch": "ds:ElevatedWatch",
    "imminent": "ds:Imminent",
    "active_incident": "ds:ActiveIncident",
    "recovery": "ds:Recovery",
    "folded": "ds:Folded",
}


def _load_stub() -> Dict[str, Any]:
    with _STUB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_sources_stub() -> Dict[str, Any]:
    with _SOURCES_STUB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def get_monitoring_summary(
    hazard: str,
    phase: str,
    county_fips: Optional[str] = None,
    county_name: Optional[str] = None,
    aoi_id: Optional[str] = None,
) -> Dict[str, Any]:
    stub = _load_stub()
    if hazard != "flood":
        raise ValueError(f"unsupported hazard: {hazard}")
    if phase not in stub:
        raise KeyError(f"unknown phase: {phase}")
    summary = deepcopy(stub[phase])
    if county_fips:
        summary["county_fips"] = county_fips
    if county_name:
        summary["county_name"] = county_name
    if aoi_id:
        summary["aoi_id"] = aoi_id
    return summary


def phase_to_iri(phase: str) -> str:
    return _PHASE_IRI.get(phase, f"ds:{phase}")


def _apply_county_resolution(
    record: Dict[str, Any],
    template: Dict[str, Any],
    mapping: Any,
    req: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Resolve county-specific URL and attach api_resolution metadata."""
    source_type = record.get("ontology_source_type", "")
    url_template = (
        req.get("url_template")
        or template.get("url_template")
        or template.get("source_url")
    )
    fallback_url = template.get("fallback_url") or template.get("source_url")

    resolved_url, api_resolution = build_source_url(source_type, url_template, mapping)

    if resolved_url:
        record["source_url"] = resolved_url
        record["health_status"] = "verified"
    else:
        record["source_url"] = fallback_url
        record["health_status"] = "degraded"
        api_resolution["fallback_url"] = fallback_url

    record["api_resolution"] = api_resolution
    record["stub"] = False
    return record, (
        {
            "source_type": source_type,
            "reason": api_resolution.get("resolution_error", "resolution_failed"),
            "county_fips": mapping.county_fips,
        }
        if record["health_status"] == "degraded"
        else None
    )


def get_stub_source_records(
    monitoring_summary: Dict[str, Any],
    *,
    mission_id: Optional[str] = None,
    county_fips: Optional[str] = None,
    county_name: Optional[str] = None,
    location: Optional[str] = None,
    state: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Materialize verified T0 source records from ontology stub templates.

    Resolves county-specific USGS site IDs and NWS zone codes before fetch.
    """
    templates = _load_sources_stub()
    fips = county_fips or monitoring_summary.get("county_fips") or "00000"
    county = county_name or monitoring_summary.get("county_name") or "unknown"
    loc = location or f"{county} County (FIPS {fips})"
    state_val = state or monitoring_summary.get("state") or ""

    mapping = None
    if fips and fips != "00000":
        mapping = resolve_county_flood_sources(fips, county, state_val)

    source_records: List[Dict[str, Any]] = []
    credibility_assessments: List[Dict[str, Any]] = []
    degraded_sources: List[Dict[str, Any]] = []

    for req in monitoring_summary.get("required_source_types") or []:
        source_type = req.get("type")
        if not source_type:
            continue
        template = templates.get(source_type)
        if not template:
            continue

        source_id = f"src_{_slug(source_type)}_{fips}"
        record = {
            "source_id": source_id,
            "source_name": template["source_name"],
            "source_url": template.get("source_url"),
            "source_type": template.get("source_type", "official"),
            "source_type_iri": template.get("source_type_iri"),
            "coverage": {
                "county_fips": fips,
                "county_name": county,
                "location": loc,
                "scope": template.get("coverage_scope", "county"),
            },
            "trust_tier": {
                "level": template.get("trust_tier_level", 0),
                "label": template.get("trust_tier", "T0"),
                "rationale": "ontology_official_registry",
            },
            "update_cadence": template.get("update_cadence", "15m"),
            "parser_adapter": template.get("parser_adapter"),
            "health_status": "verified",
            "fetch_headers": template.get("fetch_headers", {}),
            "ontology_source_type": source_type,
            "discovery_hint": req.get("discovery_hint") or template.get("resolution_strategy"),
            "resolution_strategy": template.get("resolution_strategy"),
            "stub": True,
        }
        if mission_id:
            record["mission_id"] = mission_id

        if mapping:
            record, degraded = _apply_county_resolution(record, template, mapping, req)
            if degraded:
                degraded_sources.append(degraded)

        source_records.append(record)

        loc_conf = 0.90
        api_res = record.get("api_resolution") or {}
        if api_res.get("county_match"):
            loc_conf = 0.90
        elif record.get("health_status") == "degraded":
            loc_conf = 0.55

        credibility_assessments.append(
            {
                "credibility_assessment_id": f"cred_{source_id}",
                "target_type": "source",
                "target_id": source_id,
                "source_confidence": 0.90,
                "location_confidence": loc_conf,
                "time_confidence": 0.80,
                "impact_confidence": 0.75,
                "rationale": "T0 official source with county API resolution",
                "stub": record.get("stub", False),
            }
        )

    return source_records, credibility_assessments, degraded_sources


def discover_sources_from_stub(
    monitoring_summary: Dict[str, Any],
    *,
    mission_id: Optional[str] = None,
    county_fips: Optional[str] = None,
    county_name: Optional[str] = None,
    location: Optional[str] = None,
    state: Optional[str] = None,
) -> Dict[str, Any]:
    """Scout-equivalent: return verified source_records with county-resolved URLs."""
    source_records, credibility_assessments, degraded_sources = get_stub_source_records(
        monitoring_summary,
        mission_id=mission_id,
        county_fips=county_fips,
        county_name=county_name,
        location=location,
        state=state,
    )

    county_mapping = None
    fips = county_fips or monitoring_summary.get("county_fips")
    if fips and fips != "00000":
        county_mapping = resolve_county_flood_sources(
            fips,
            county_name or monitoring_summary.get("county_name") or "unknown",
            state or monitoring_summary.get("state") or "",
        ).to_dict()

    return {
        "source_records": source_records,
        "credibility_assessments": credibility_assessments,
        "dropped_sources": degraded_sources,
        "degraded_sources": degraded_sources,
        "county_api_mapping": county_mapping,
        "discovery_mode": "ontology_stub_with_county_resolution",
        "stub_sources_file": str(_SOURCES_STUB_PATH),
    }


_KNOWN_CLAIM_TYPES = {
    "GaugeObservation",
    "gauge_rising",
    "gauge_trend",
    "ForecastShift",
    "forecast_shift",
    "Warning",
    "warning",
    "RoadClosureClaim",
    "road_closure",
    "PowerOutageClaim",
    "outage",
    "InundationClaim",
    "inundation",
    "EvacuationClaim",
    "evacuation",
    "ShelterClaim",
    "shelter",
}

_TRUST_TIER_BASE = {
    "USGSGaugeSource": {"trust_tier": "T0", "base_confidence": 0.90},
    "NWPSSource": {"trust_tier": "T0", "base_confidence": 0.90},
    "NWSSource": {"trust_tier": "T0", "base_confidence": 0.90},
    "NWSAlertSource": {"trust_tier": "T0", "base_confidence": 0.90},
    "FEMADisasterSource": {"trust_tier": "T0", "base_confidence": 0.95},
    "WeakSignalSource": {"trust_tier": "T3", "base_confidence": 0.35},
}


def validate_claim_type(claim_type: str) -> bool:
    """Q4 — is this a known claim class (OWL local name or legacy alias)?"""
    return claim_type in _KNOWN_CLAIM_TYPES


def get_trust_tier_for_source_type(source_type: str) -> Dict[str, Any]:
    """Q5 — tier and base confidence for an ontology source type."""
    return dict(_TRUST_TIER_BASE.get(source_type, {"trust_tier": "T2", "base_confidence": 0.60}))


def get_phase_transition_rules(hazard: str) -> List[Dict[str, Any]]:
    """Q2 — aggregate phase rules across stub phases for a hazard."""
    if hazard != "flood":
        raise ValueError(f"unsupported hazard: {hazard}")
    stub = _load_stub()
    rules: List[Dict[str, Any]] = []
    for phase_name, phase_blob in stub.items():
        if not isinstance(phase_blob, dict):
            continue
        for rule in phase_blob.get("phase_transition_rules") or []:
            enriched = dict(rule)
            enriched.setdefault("phase", phase_name)
            rules.append(enriched)
    return rules
