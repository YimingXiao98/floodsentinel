"""County-level overall confidence for official NWPS/USGS API evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


CONFIDENCE_LABEL_TO_IMPACT = {
    "high": 0.90,
    "medium": 0.80,
    "low": 0.65,
}

T0_SOURCE_SCORE = 0.95


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if str(value).endswith("Z"):
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _hours_since(timestamp: Optional[str]) -> Optional[float]:
    dt = _parse_time(timestamp)
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _freshness_score(gauges: List[Dict[str, Any]], generated_at: Optional[str]) -> float:
    hours_list = [_hours_since(g.get("observed_valid_time")) for g in gauges]
    hours_list = [h for h in hours_list if h is not None]
    if hours_list:
        freshest = min(hours_list)
        if freshest < 2:
            return 0.90
        if freshest < 6:
            return 0.85
        if freshest < 24:
            return 0.75
        return 0.70
    gen_hours = _hours_since(generated_at)
    if gen_hours is not None and gen_hours < 6:
        return 0.85
    return 0.70


def _location_score(verdict_gauge_count: int, gauges_with_data: int) -> float:
    if verdict_gauge_count >= 3:
        return 0.90
    if verdict_gauge_count == 2:
        return 0.85
    if verdict_gauge_count == 1:
        return 0.75
    if gauges_with_data > 0:
        return 0.55
    return 0.40


def _impact_score(
    *,
    verdict_confidence: Optional[str],
    is_flooding: bool,
    verdict_gauge_count: int,
    max_flood_state: Optional[float],
) -> float:
    base = CONFIDENCE_LABEL_TO_IMPACT.get(str(verdict_confidence or "low").lower(), 0.65)
    if verdict_gauge_count == 0:
        return 0.50
    if is_flooding and max_flood_state is not None and max_flood_state >= 2.0:
        return min(0.95, base + 0.05)
    if is_flooding:
        return min(0.92, base + 0.03)
    return base


def overall_from_components(components: Dict[str, float]) -> float:
    return round(
        0.35 * components.get("source", 0)
        + 0.25 * components.get("location", 0)
        + 0.20 * components.get("time", 0)
        + 0.20 * components.get("impact", 0),
        3,
    )


def compute_county_flood_confidence(
    *,
    summary: Dict[str, Any],
    gauges: List[Dict[str, Any]],
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Aggregate T0 official API confidence for coordinator weighting.

    Uses the same 0.35/0.25/0.20/0.20 decomposition as per-claim scoring.
    """
    scored = [g for g in gauges if g.get("flood_state_score") is not None and not g.get("error")]
    with_data = [g for g in gauges if not g.get("error") and g.get("observed_primary") is not None]

    verdict_count = summary.get("verdict_gauge_count") or summary.get("gauges_scored") or len(scored)
    gauges_with_data = summary.get("gauges_with_data") or len(with_data)

    components = {
        "source": T0_SOURCE_SCORE,
        "location": round(_location_score(int(verdict_count), int(gauges_with_data)), 2),
        "time": round(_freshness_score(with_data or gauges, generated_at), 2),
        "impact": round(
            _impact_score(
                verdict_confidence=summary.get("confidence"),
                is_flooding=bool(summary.get("is_flooding")),
                verdict_gauge_count=int(verdict_count),
                max_flood_state=summary.get("max_flood_state"),
            ),
            2,
        ),
    }
    overall = overall_from_components(components)

    return {
        "overall_confidence": overall,
        "confidence_components": components,
        "evidence_tier": "T0",
        "source_class": "official_api",
        "weight_hint": "high" if overall >= 0.80 else "medium" if overall >= 0.65 else "low",
        "verdict_gauge_count": verdict_count,
        "gauges_with_data": gauges_with_data,
    }
