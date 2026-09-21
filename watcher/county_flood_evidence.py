"""Build coordinator-mergeable envelope from county-flood-data API payload."""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_county_flood_evidence(
    payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Expose the legacy county display score and hydrologic verdict for reports.

    The aggregate score is not consumed by claim admission or phase decisions;
    operational claim credibility is computed separately on each extracted claim.
    """
    if not payload or not isinstance(payload, dict):
        return {
            "merge_key": "county_flood_evidence",
            "skill": "county-flood-data",
            "status": "missing",
            "overall_confidence": None,
            "confidence_components": None,
            "evidence_tier": None,
            "weight_hint": "low",
            "regional_verdict": None,
            "is_flooding": False,
            "verdict_gauge_count": 0,
        }

    summary = payload.get("summary") or {}
    components = summary.get("confidence_components") or {}
    overall = summary.get("overall_confidence")

    return {
        "merge_key": "county_flood_evidence",
        "skill": "county-flood-data",
        "status": "ok",
        "overall_confidence": overall,
        "confidence_components": components,
        "evidence_tier": summary.get("evidence_tier") or "T0",
        "weight_hint": summary.get("api_weight_hint") or "medium",
        "regional_verdict": summary.get("regional_verdict"),
        "is_flooding": bool(summary.get("is_flooding")),
        "verdict_confidence": summary.get("confidence"),
        "verdict_gauge_count": summary.get("verdict_gauge_count") or summary.get("gauges_scored", 0),
        "gauges_with_data": summary.get("gauges_with_data", 0),
        "gauges_excluded": summary.get("gauges_excluded", 0),
        "evaluation_mode": summary.get("evaluation_mode"),
        "min_gauges_required": summary.get("min_gauges_required"),
        "max_flood_state": summary.get("max_flood_state"),
        "generated_at": payload.get("generated_at"),
        "county": payload.get("county"),
        "state": payload.get("state"),
        "fips": payload.get("fips"),
        "supplementary_signal_count": len(summary.get("supplementary_signals") or []),
        "insufficient_data": summary.get("regional_verdict") == "insufficient_data",
    }
