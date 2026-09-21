"""Extract gauge claims from county-flood-data JSON snapshots."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from watcher.config import watershed_location_precision
from watcher.claims.confidence import (
    apply_belief_ablation,
    build_confidence_breakdown,
    overall_confidence,
    reading_age_hours,
    reading_is_current,
    score_credibility,
    score_usgs_hydrology_claim,
)
from watcher.claims.models import new_claim

FLOOD_CATEGORY_SEVERITY = {
    "no_flooding": "unknown",
    "action": "minor",
    "minor_flooding": "minor",
    "moderate_flooding": "moderate",
    "major_flooding": "severe",
    "record_flood": "extreme",
}


def _severity_from_category(category: Optional[str]) -> str:
    if not category:
        return "unknown"
    return FLOOD_CATEGORY_SEVERITY.get(str(category).strip().lower(), "unknown")


def _build_excerpt(gauge: Dict[str, Any]) -> str:
    label = gauge.get("name") or gauge.get("shef_id") or gauge.get("site_no") or "gauge"
    parts = [f"{gauge.get('source', 'county-flood')} {label}"]
    obs = gauge.get("observed_primary")
    unit = gauge.get("observed_primary_unit") or "ft"
    if obs is not None:
        parts.append(f"stage {obs} {unit}")
    if gauge.get("streamflow_cfs") is not None:
        parts.append(f"flow {gauge['streamflow_cfs']} cfs")
    cat = gauge.get("observed_flood_category")
    if cat:
        parts.append(f"category={cat}")
    flood_stage = gauge.get("flood_stage")
    if flood_stage is not None:
        parts.append(f"flood_stage={flood_stage} {unit}")
    if gauge.get("observed_valid_time"):
        parts.append(f"observed {gauge['observed_valid_time']}")
    return "; ".join(parts)


def _credibility_for(gauge: Dict[str, Any], *, trust_level: int, county_match: bool) -> Dict[str, float]:
    """Credibility terms drawn from what the pipeline already established.

    A category the authority declared outranks one derived from a threshold,
    which outranks a bare reading; and a derived category is only as firm as the
    threshold behind it.
    """
    # A gauge outside the county but in its watershed describes connected water
    # rather than water here, so it is placed less precisely than an in-county
    # reading and more precisely than one that does not resolve at all.
    location_precision = None
    if gauge.get("location_relation") == "shared_watershed":
        location_precision = watershed_location_precision()

    return score_credibility(
        trust_tier_level=trust_level,
        county_match=county_match,
        classification_method=gauge.get("classification_method"),
        threshold_source=gauge.get("threshold_source"),
        data_qualifier=gauge.get("data_qualifier"),
        location_precision=location_precision,
        location_basis=gauge.get("location_basis"),
    )


def extract_county_flood_claims(
    snapshot: Dict[str, Any],
    *,
    county: str,
    state: str,
    location: str,
    raw_text: Optional[str] = None,
    source_record: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return (claims, extraction_failure_or_none) from county-flood-data snapshot JSON."""
    source_id = snapshot.get("source_id", "unknown")
    snapshot_id = snapshot.get("snapshot_id")
    evidence_url = snapshot.get("snapshot_url", "")
    fetched_at = snapshot.get("fetched_at") or snapshot.get("fetched_at_utc", "")
    text = raw_text if raw_text is not None else snapshot.get("raw_text") or snapshot.get("raw_excerpt", "")

    api_resolution = (source_record or {}).get("api_resolution") or {}
    county_match = bool(api_resolution.get("county_match", True))
    trust_tier = (source_record or {}).get("trust_tier") or {}
    trust_level = trust_tier.get("level", 0)

    if not text or not str(text).strip():
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

    if payload.get("schema") != "county-flood-data/1":
        return [], {
            "source_id": source_id,
            "source_url": evidence_url,
            "snapshot_id": snapshot_id,
            "failure_reason": "unsupported_schema",
        }

    gauges = payload.get("gauges") or []
    if not gauges:
        return [], {
            "source_id": source_id,
            "source_url": evidence_url,
            "snapshot_id": snapshot_id,
            "failure_reason": "no_gauges",
        }

    claims: List[Dict[str, Any]] = []
    for gauge in gauges:
        gage_height = gauge.get("observed_primary")
        streamflow = gauge.get("streamflow_cfs")
        obs_time = gauge.get("observed_valid_time")
        category = gauge.get("observed_flood_category")
        flood_score = gauge.get("flood_state_score")
        hydrologic_signal = gauge.get("hydrologic_signal")
        is_scored = flood_score is not None

        if not is_scored and hydrologic_signal in ("rising", "falling"):
            site_no = gauge.get("site_no") or gauge.get("shef_id")
            trend_excerpt_parts = [
                f"{gauge.get('source', 'county-flood')} {gauge.get('name') or site_no}",
                f"signal={hydrologic_signal}",
            ]
            if gauge.get("delta_primary_ft") is not None:
                trend_excerpt_parts.append(f"Δstage {gauge['delta_primary_ft']:+.1f} ft")
            if gauge.get("delta_streamflow_pct") is not None:
                trend_excerpt_parts.append(f"Δflow {gauge['delta_streamflow_pct']:+.0f}%")
            trend_components = _credibility_for(
                gauge, trust_level=trust_level, county_match=county_match
            )
            supp_conf = apply_belief_ablation(overall_confidence(trend_components))
            breakdown = build_confidence_breakdown(
                trend_components,
                trust_tier_level=trust_level,
                method="cold_start_delta",
                method_confidence=supp_conf,
                uncertainty_notes=["no rated threshold; delta-based"],
            )
            claim = new_claim(
                claim_id=f"claim_cf_{uuid.uuid4().hex[:12]}",
                source_id=source_id,
                claim_type="gauge_trend",
                    excerpt="; ".join(trend_excerpt_parts),
                    evidence_url=evidence_url,
                    location_text=location,
                    observed_at=obs_time or fetched_at,
                    published_at=obs_time,
                    severity="minor" if hydrologic_signal == "rising" else "unknown",
                    value={
                        "site_no": site_no,
                        "shef_id": gauge.get("shef_id"),
                        "gage_height_ft": gage_height,
                        "streamflow_cfs": streamflow,
                        "observation_time": obs_time,
                        "hydrologic_signal": hydrologic_signal,
                        "delta_primary_ft": gauge.get("delta_primary_ft"),
                        "delta_streamflow_pct": gauge.get("delta_streamflow_pct"),
                        "trend_source": "unscored_delta",
                        "supplementary": True,
                    },
                    snapshot_id=snapshot_id,
                    confidence_components=trend_components,
                    overall_confidence=supp_conf,
                )
            claim["confidence_breakdown"] = breakdown
            claim["source_reliability_prior"] = breakdown["source_reliability_prior"]
            claims.append(claim)
            continue

        # Unscored gauges (no action/minor/major thresholds) still carry weak
        # evidence. Admit the observation at low confidence rather than dropping
        # it, so gauge-sparse counties are not blind after the first cycle.
        # These claims are excluded from the official-gauge set (no
        # source_class/source_backend/flood_state_score), so they can neither
        # escalate a phase nor satisfy an all-clear on their own.
        if not is_scored and (gage_height is not None or streamflow is not None):
            site_no = gauge.get("site_no") or gauge.get("shef_id")
            observation_signal = hydrologic_signal or "observed"
            is_cold_start = observation_signal == "cold_start"
            trend_components = _credibility_for(
                gauge, trust_level=trust_level, county_match=county_match
            )
            supp_conf = apply_belief_ablation(overall_confidence(trend_components))
            breakdown = build_confidence_breakdown(
                trend_components,
                trust_tier_level=trust_level,
                method="cold_start_delta" if is_cold_start else "observation_only",
                method_confidence=supp_conf,
                uncertainty_notes=[
                    "no rated threshold; cold-start baseline"
                    if is_cold_start
                    else "no rated threshold; observation only"
                ],
            )
            claim = new_claim(
                claim_id=f"claim_cf_{uuid.uuid4().hex[:12]}",
                source_id=source_id,
                claim_type="GaugeObservation",
                excerpt=_build_excerpt(gauge) + f"; signal={observation_signal}",
                evidence_url=evidence_url,
                location_text=location,
                observed_at=obs_time or fetched_at,
                published_at=obs_time,
                severity="unknown",
                value={
                    "site_no": site_no,
                    "shef_id": gauge.get("shef_id"),
                    "gage_height_ft": gage_height,
                    "streamflow_cfs": streamflow,
                    "observation_time": obs_time,
                    "observation_age_hours": reading_age_hours(obs_time, fetched_at),
                    "hydrologic_signal": observation_signal,
                    "trend_source": "cold_start" if is_cold_start else "unscored_observation",
                    "supplementary": True,
                    "unscored_reason": gauge.get("unscored_reason"),
                    "prior_observed_primary": gauge.get("prior_observed_primary"),
                    "delta_primary_ft": gauge.get("delta_primary_ft"),
                },
                snapshot_id=snapshot_id,
                confidence_components=trend_components,
                overall_confidence=supp_conf,
            )
            claim["confidence_breakdown"] = breakdown
            claim["source_reliability_prior"] = breakdown["source_reliability_prior"]
            claims.append(claim)
            continue

        if not is_scored:
            continue

        # A gauge that has stopped reporting keeps returning its last value.
        # Such a reading is kept in the record, with its age, but must not
        # assert anything about the present, so it carries no flood severity
        # and cannot move the event.
        age_hours = reading_age_hours(obs_time, fetched_at)
        current = reading_is_current(obs_time, fetched_at)
        severity = _severity_from_category(category) if current else "unknown"

        components = _credibility_for(
            gauge, trust_level=trust_level, county_match=county_match
        )
        # Severity is kept out of the confidence composite and recorded beside
        # it, so a serious reading is not mistaken for a reliable one.
        components["impact"] = score_usgs_hydrology_claim(
            county_match=county_match,
            observation_time=obs_time,
            gage_height_ft=gage_height,
            streamflow_cfs=streamflow,
            trust_tier_level=trust_level,
            flood_category=category,
            flood_state_score=None if flood_score is None else float(flood_score),
            action_stage=gauge.get("action_stage"),
            flood_stage=gauge.get("flood_stage"),
            now=fetched_at,
        )["impact"]

        site_no = gauge.get("site_no") or gauge.get("shef_id")
        claim_id = f"claim_cf_{uuid.uuid4().hex[:12]}"
        from watcher.claims.confidence import overall_confidence as compute_overall

        overall = max(compute_overall(components), 0.72)
        has_rated_threshold = any(
            gauge.get(k) is not None
            for k in ("flood_stage", "action_stage", "moderate_stage", "major_stage")
        )
        if has_rated_threshold:
            breakdown = build_confidence_breakdown(
                components,
                trust_tier_level=trust_level,
                method="rated_threshold",
                method_confidence=max(components.get("impact", 0.85), 0.85),
            )
        else:
            breakdown = build_confidence_breakdown(
                components,
                trust_tier_level=trust_level,
                method="corroborated",
            )
        claim = new_claim(
            claim_id=claim_id,
            source_id=source_id,
            claim_type="GaugeObservation",
            excerpt=_build_excerpt(gauge),
            evidence_url=evidence_url,
            location_text=location,
            observed_at=obs_time or fetched_at,
            published_at=obs_time,
            severity=severity,
            value={
                "site_no": site_no,
                "shef_id": gauge.get("shef_id"),
                "gage_height_ft": gage_height,
                "streamflow_cfs": streamflow,
                "observation_time": obs_time,
                "observation_age_hours": age_hours,
                "reading_current": current,
                "flood_category": category,
                "flood_state_score": flood_score,
                "action_stage": gauge.get("action_stage"),
                "flood_stage": gauge.get("flood_stage"),
                "moderate_stage": gauge.get("moderate_stage"),
                "major_stage": gauge.get("major_stage"),
                "prior_observed_primary": gauge.get("prior_observed_primary"),
                "delta_primary_ft": gauge.get("delta_primary_ft"),
                "delta_streamflow_pct": gauge.get("delta_streamflow_pct"),
                "hydrologic_signal": gauge.get("hydrologic_signal"),
                "source_backend": gauge.get("source"),
                "source_class": "official_api",
                "evidence_tier": "T0",
                # Present when the gauge lies outside the county but drains the
                # same watershed, so downstream rules can weigh it accordingly.
                "location_relation": gauge.get("location_relation"),
            },
            snapshot_id=snapshot_id,
            confidence_components=components,
            overall_confidence=overall,
        )
        claim["confidence_breakdown"] = breakdown
        claim["source_reliability_prior"] = breakdown["source_reliability_prior"]
        claims.append(claim)

    return claims, None
