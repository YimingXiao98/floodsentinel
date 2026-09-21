"""Evidence claim models and ontology type mapping."""

from __future__ import annotations

from typing import Any, Dict, Optional

CLAIM_TYPE_IRI: Dict[str, str] = {
    "warning": "ds:Warning",
    "gauge_rising": "ds:GaugeObservation",
    "GaugeObservation": "ds:GaugeObservation",
    "gauge_trend": "ds:GaugeObservation",
    "forecast_shift": "ds:ForecastShift",
    "ForecastShift": "ds:ForecastShift",
    "road_closure": "ds:RoadClosureClaim",
    "outage": "ds:PowerOutageClaim",
    "inundation": "ds:InundationClaim",
    "evacuation": "ds:EvacuationClaim",
    "shelter": "ds:ShelterClaim",
}

SEVERITY_RANK: Dict[str, int] = {
    "unknown": 0,
    "none": 0,
    "minor": 1,
    "moderate": 2,
    "severe": 3,
    "extreme": 4,
}

NWS_SEVERITY_MAP: Dict[str, str] = {
    "minor": "minor",
    "moderate": "moderate",
    "severe": "severe",
    "extreme": "extreme",
    "unknown": "unknown",
}


def claim_type_iri(claim_type: str) -> str:
    return CLAIM_TYPE_IRI.get(claim_type, f"ds:{claim_type}")


def severity_rank(severity: Optional[str]) -> int:
    if not severity:
        return 0
    return SEVERITY_RANK.get(severity.lower().strip(), 0)


def new_claim(
    *,
    claim_id: str,
    source_id: str,
    claim_type: str,
    excerpt: str,
    evidence_url: str,
    location_text: str,
    observed_at: str,
    published_at: Optional[str] = None,
    severity: Optional[str] = None,
    value: Any = None,
    snapshot_id: Optional[str] = None,
    confidence_components: Optional[Dict[str, float]] = None,
    geo_reference: Optional[Dict[str, Any]] = None,
    overall_confidence: Optional[float] = None,
) -> Dict[str, Any]:
    components = confidence_components or {
        "source": 0.90,
        "location": 0.85,
        "time": 0.80,
        "impact": 0.75,
    }
    # Delegate rather than inline the weighted composite: it keeps one
    # definition of the formula, and routes through the belief-layer ablation
    # so a claim built here cannot bypass it.
    from watcher.claims.confidence import overall_confidence as _composite

    overall_computed = _composite(components)
    overall_final = overall_confidence if overall_confidence is not None else overall_computed
    return {
        "claim_id": claim_id,
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "observed_at": observed_at,
        "published_at": published_at,
        "location_text": location_text,
        "geo_reference": geo_reference,
        "claim_type": claim_type,
        "claim_type_iri": claim_type_iri(claim_type),
        "severity": severity,
        "value": value,
        "excerpt": excerpt[:500],
        "evidence_url": evidence_url,
        "confidence_components": components,
        "overall_confidence": overall_final,
        "relationships": {
            "supports": [],
            "contradicts": [],
            "supersedes": [],
            "duplicates": [],
        },
    }
