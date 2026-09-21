"""Merge flood source results and compute county-level averages."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from county_flood.api_resolution import resolve_api_urls
from county_flood.confidence import compute_county_flood_confidence
from county_flood.models import CountyFloodResult, CountyFloodSummary, GaugeReading, SourceResult
from county_flood.sources import active_sources
from county_flood.sources.gauge_delta import apply_supplementary_signals
from county_flood.sources.stages import (
    is_flooding_score,
    regional_verdict_from_gauges,
    score_label,
)

DEFAULT_SOURCE_IDS = ("nwps", "usgs", "nws_alerts", "fema_disasters")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _gauge_to_dict(gauge: GaugeReading) -> Dict[str, Any]:
    return {
        "source": gauge.source,
        "shef_id": gauge.shef_id,
        "site_no": gauge.site_no,
        "name": gauge.name,
        "observed_primary": gauge.observed_primary,
        "observed_primary_unit": gauge.observed_primary_unit,
        "observed_flood_category": gauge.observed_flood_category,
        "observed_valid_time": gauge.observed_valid_time,
        "forecast_primary": gauge.forecast_primary,
        "forecast_flood_category": gauge.forecast_flood_category,
        "flood_state_score": gauge.flood_state_score,
        "streamflow_cfs": gauge.streamflow_cfs,
        "action_stage": gauge.action_stage,
        "flood_stage": gauge.flood_stage,
        "moderate_stage": gauge.moderate_stage,
        "major_stage": gauge.major_stage,
        "error": gauge.error,
        "classification_method": gauge.classification_method,
        "threshold_source": gauge.threshold_source,
        "data_qualifier": gauge.data_qualifier,
        "location_relation": gauge.location_relation,
        "location_basis": gauge.location_basis,
        "unscored_reason": gauge.unscored_reason,
        "priority_tier": gauge.priority_tier,
    }


def _most_common_unit(units: List[str]) -> Optional[str]:
    if not units:
        return None
    return Counter(units).most_common(1)[0][0]


def _gauge_key(gauge: GaugeReading) -> Optional[str]:
    site = (gauge.site_no or "").strip()
    shef = (gauge.shef_id or "").strip().upper()
    if site and shef and site != shef:
        return f"site:{site}"
    if site:
        return f"site:{site}"
    if shef:
        return f"shef:{shef}"
    return None


def _dedupe_gauges(gauges: List[GaugeReading]) -> List[GaugeReading]:
    """Prefer NWPS reading when NWPS and USGS refer to the same physical gauge."""
    by_key: Dict[str, GaugeReading] = {}
    order: List[str] = []

    for gauge in gauges:
        if gauge.error:
            continue
        key = _gauge_key(gauge)
        if not key:
            continue
        if key not in by_key:
            by_key[key] = gauge
            order.append(key)
            continue
        existing = by_key[key]
        if existing.source == "nwps":
            continue
        if gauge.source == "nwps":
            by_key[key] = gauge
            continue
        if gauge.flood_state_score is not None and existing.flood_state_score is None:
            by_key[key] = gauge

    errors = [g for g in gauges if g.error]
    deduped = [by_key[k] for k in order]
    return deduped + errors


def _supplementary_scores(
    alerts: List[Dict[str, Any]],
    disasters: List[Dict[str, Any]],
) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    alert_scores = [
        float(alert["flood_state_score"])
        for alert in alerts
        if alert.get("flood_state_score") is not None
    ]
    disaster_scores = [
        float(disaster["flood_state_score"])
        for disaster in disasters
        if disaster.get("flood_state_score") is not None
    ]
    if alert_scores:
        scores["nws_alerts"] = max(alert_scores)
    if disaster_scores:
        scores["fema_disasters"] = max(disaster_scores)
    return scores


def _merge_verdict_with_supplementary(
    gauge_verdict: Dict[str, Any],
    supplementary: Dict[str, float],
) -> Dict[str, Any]:
    if not supplementary:
        return gauge_verdict

    merged = dict(gauge_verdict)
    gauge_max = float(merged.get("max_flood_state") or 0.0)
    supplemental_max = max(supplementary.values())
    combined_max = max(gauge_max, supplemental_max)
    merged["max_flood_state"] = round(combined_max, 3)
    merged["regional_verdict"] = score_label(combined_max)
    merged["is_flooding"] = is_flooding_score(combined_max)
    merged["supplementary_scores"] = supplementary
    if merged.get("average_flood_state") is None and supplementary:
        merged["average_flood_state"] = round(
            sum(supplementary.values()) / len(supplementary), 3
        )
    return merged


def aggregate_county_flood(
    *,
    county: str,
    state: str,
    fips: Optional[str],
    location: str,
    gauge_ids: Optional[Sequence[str]] = None,
    site_ids: Optional[Sequence[str]] = None,
    discover_categories: bool = False,
    sources: Optional[Sequence[str]] = None,
    prior_gauges: Optional[Sequence[Dict[str, Any]]] = None,
) -> CountyFloodResult:
    api_resolution = resolve_api_urls(county=county, state=state, fips=fips)
    all_gauges: List[GaugeReading] = []
    all_alerts: List[Dict[str, Any]] = []
    all_disasters: List[Dict[str, Any]] = []
    sources_meta: Dict[str, Any] = {}
    csv_truncated = False
    active = active_sources()
    if sources:
        wanted = {source_id.strip().lower() for source_id in sources if source_id.strip()}
        active = [source for source in active if source.source_id in wanted]
    else:
        active = [source for source in active if source.source_id in DEFAULT_SOURCE_IDS]

    for source in active:
        result: SourceResult = source.fetch(
            county=county,
            state=state,
            fips=fips,
            gauge_ids=gauge_ids,
            site_ids=site_ids,
            discover_categories=discover_categories,
        )
        all_gauges.extend(result.gauges)
        all_alerts.extend(alert.to_dict() for alert in result.alerts)
        all_disasters.extend(disaster.to_dict() for disaster in result.disasters)
        if result.metadata.get("csv_truncated"):
            csv_truncated = True
        sources_meta[result.source_id] = {
            **result.metadata,
            "discovered_flood_categories": result.discovered_flood_categories,
            "fetch_errors": result.fetch_errors,
        }

    deduped = _dedupe_gauges(all_gauges)
    gauge_dicts = [_gauge_to_dict(g) for g in deduped if not g.error]
    prior_list = list(prior_gauges) if prior_gauges else None
    gauge_dicts, supplementary_signals = apply_supplementary_signals(gauge_dicts, prior_list)
    verdict = regional_verdict_from_gauges(gauge_dicts)
    supplemental = _supplementary_scores(all_alerts, all_disasters)
    verdict = _merge_verdict_with_supplementary(verdict, supplemental)
    scored_alert_count = sum(
        1 for alert in all_alerts if alert.get("flood_state_score") is not None
    )
    scored_disaster_count = sum(
        1 for disaster in all_disasters if disaster.get("flood_state_score") is not None
    )

    with_data = [g for g in deduped if not g.error]
    scored_gauges = [g for g in with_data if g.flood_state_score is not None]
    scores = [g.flood_state_score for g in scored_gauges]
    primaries = [g.observed_primary for g in scored_gauges if g.observed_primary is not None]
    units = [g.observed_primary_unit for g in scored_gauges if g.observed_primary_unit]

    avg_primary = round(sum(primaries) / len(primaries), 3) if primaries else None
    generated_at = _utc_now()

    summary_dict = {
        "average_flood_state": verdict.get("average_flood_state"),
        "gauges_scored": len(scores),
        "gauges_with_data": len(with_data),
        "verdict_gauge_count": verdict.get("verdict_gauge_count", 0),
        "confidence": verdict.get("confidence"),
        "is_flooding": bool(verdict.get("is_flooding")),
        "max_flood_state": verdict.get("max_flood_state"),
    }
    api_confidence = compute_county_flood_confidence(
        summary=summary_dict,
        gauges=gauge_dicts,
        generated_at=generated_at,
    )

    summary = CountyFloodSummary(
        average_flood_state=verdict.get("average_flood_state"),
        average_primary_reading=avg_primary,
        average_primary_unit=_most_common_unit(units),
        gauges_queried=len(all_gauges),
        gauges_with_data=len(with_data),
        gauges_scored=len(scores),
        max_flood_state=verdict.get("max_flood_state"),
        regional_verdict=verdict.get("regional_verdict"),
        is_flooding=bool(verdict.get("is_flooding")),
        confidence=verdict.get("confidence"),
        evaluation_mode=verdict.get("evaluation_mode"),
        scoring_coverage=verdict.get("scoring_coverage"),
        gauges_unscored=verdict.get("gauges_unscored", 0),
        verdict_gauge_count=verdict.get("verdict_gauge_count", 0),
        gauges_excluded=verdict.get("gauges_excluded", 0),
        min_gauges_required=verdict.get("min_gauges_required"),
        classification_methods=verdict.get("classification_methods"),
        flooding_gauge_ids=verdict.get("flooding_gauge_ids"),
        supplementary_signals=supplementary_signals or None,
        overall_confidence=api_confidence.get("overall_confidence"),
        confidence_components=api_confidence.get("confidence_components"),
        evidence_tier=api_confidence.get("evidence_tier"),
        api_weight_hint=api_confidence.get("weight_hint"),
        csv_truncated=csv_truncated,
        supplementary_scores=verdict.get("supplementary_scores"),
        scored_disaster_count=scored_disaster_count,
        scored_alert_count=scored_alert_count,
    )

    return CountyFloodResult(
        county=county,
        state=state,
        fips=fips,
        location=location,
        generated_at=generated_at,
        summary=summary,
        sources=sources_meta,
        gauges=gauge_dicts,
        alerts=all_alerts,
        disasters=all_disasters,
        api_resolution=api_resolution,
    )
