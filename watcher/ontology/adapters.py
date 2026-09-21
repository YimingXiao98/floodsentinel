"""
adapters.py -- translate existing watcher internal shapes (dicts as produced
by claims/extract.py, claims/confidence.py, phase_engine.py, and the
SourceRecord/EvidenceClaim Neo4j node properties) into belief_schema
ontology types.

No I/O here. No Neo4j calls. Pure functions only.

Key name adjustments vs spec pseudocode (confirmed in watcher/claims/models.py
and watcher/county_flood_bridge.py):
  - EvidenceClaim uses ``observed_at`` (ISO string), not ``observed_at_utc``
  - SourceRecord ``trust_tier`` may be ``"T0"`` or ``{"level": 0, "name": "T0"}``
  - SourceRecord ``source_type`` is often ``"official"``; use ``ontology_source_type``
    when present for SourceClass mapping
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from .belief_schema import (
    Belief,
    BeliefMethod,
    Claim,
    ClaimStatus,
    DataSource,
    DisasterPhase,
    Phase,
    ReliabilityTier,
    SourceClass,
)

_TRUST_TIER_MAP = {
    "T0": ReliabilityTier.T0,
    "T1": ReliabilityTier.T1,
    "T2": ReliabilityTier.T2,
    "T3": ReliabilityTier.T3,
}

_ONTOLOGY_TYPE_CLASS_MAP = {
    "USGSGaugeSource": SourceClass.gauge,
    "NWSSource": SourceClass.official_feed,
    "NWSAlertSource": SourceClass.official_feed,
    "FEMADisasterSource": SourceClass.official_feed,
    "WeakSignalSource": SourceClass.crowd,
}

_SOURCE_CLASS_MAP = {
    "usgs": SourceClass.gauge,
    "nwps": SourceClass.official_feed,
    "nws": SourceClass.official_feed,
    "nws_alerts": SourceClass.official_feed,
    "fema_disasters": SourceClass.official_feed,
    "flood_article": SourceClass.crowd,
    "official": SourceClass.official_feed,
}


def _normalize_trust_tier(raw: Any) -> ReliabilityTier:
    if isinstance(raw, dict):
        name = str(raw.get("name") or "T1")
        return _TRUST_TIER_MAP.get(name, ReliabilityTier.T1)
    tier = str(raw or "T1")
    return _TRUST_TIER_MAP.get(tier, ReliabilityTier.T1)


def _source_class_from_record(source_record: dict) -> SourceClass:
    ont_type = source_record.get("ontology_source_type") or ""
    if ont_type in _ONTOLOGY_TYPE_CLASS_MAP:
        return _ONTOLOGY_TYPE_CLASS_MAP[ont_type]
    source_type = str(source_record.get("source_type") or "").lower()
    return _SOURCE_CLASS_MAP.get(source_type, SourceClass.model)


def _parse_timestamp(value: Union[str, datetime, None]) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not value:
        return datetime.now(timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return datetime.now(timezone.utc)


def source_record_to_datasource(source_record: dict) -> DataSource:
    """SourceRecord node properties -> DataSource."""
    source_class = _source_class_from_record(source_record)
    display = (
        source_record.get("display_name")
        or source_record.get("source_name")
        or source_record["source_id"]
    )
    return DataSource(
        id=source_record["source_id"],
        name=display,
        source_class=source_class,
        reliability_tier=_normalize_trust_tier(source_record.get("trust_tier")),
    )


def evidence_claim_to_claim(
    evidence_claim: dict,
    *,
    location_id: str,
    system_id: str | None = None,
) -> Claim:
    """EvidenceClaim dict -> Claim."""
    supersedes = evidence_claim.get("supersedes_claim_id")
    if not supersedes:
        rel = evidence_claim.get("relationships") or {}
        supers = rel.get("supersedes") or []
        if supers:
            supersedes = supers[0]

    status = ClaimStatus.active
    if supersedes:
        status = ClaimStatus.active
    elif evidence_claim.get("status") == "superseded":
        status = ClaimStatus.superseded

    timestamp = _parse_timestamp(
        evidence_claim.get("observed_at_utc") or evidence_claim.get("observed_at")
    )

    return Claim(
        id=evidence_claim["claim_id"],
        content=_render_content(evidence_claim),
        info_type=evidence_claim["claim_type"],
        system=system_id,
        location=location_id,
        timestamp=timestamp,
        source_id=evidence_claim["source_id"],
        raw_reliability=float(evidence_claim.get("source_reliability_prior", 0.8)),
        status=status,
        supersedes=supersedes,
    )


def confidence_to_belief(claim_id: str, confidence_breakdown: dict) -> Belief:
    """claims/confidence.py output -> Belief."""
    method_raw = confidence_breakdown.get("method", "corroborated")
    try:
        method = BeliefMethod(method_raw)
    except ValueError:
        method = BeliefMethod.corroborated

    return Belief(
        claim_id=claim_id,
        score=float(confidence_breakdown["overall_score"]),
        method=method,
        confidence=float(
            confidence_breakdown.get(
                "method_confidence", confidence_breakdown["overall_score"]
            )
        ),
        uncertainty=list(confidence_breakdown.get("uncertainty_notes") or []),
    )


def phase_transition_to_phase(transition: dict, trigger_claim_id: str) -> Phase:
    """phase_engine.evaluate_phase_transition() output -> Phase."""
    changed_from = transition.get("old_phase") or transition.get("current_phase")
    return Phase(
        current=DisasterPhase(transition["new_phase"]),
        changed_from=DisasterPhase(changed_from) if changed_from else None,
        changed_at=_parse_timestamp(transition["evaluated_at_utc"]),
        trigger=trigger_claim_id,
    )


def _render_content(evidence_claim: dict) -> str:
    """Human-readable one-line summary of a claim's value payload."""
    v = evidence_claim.get("value", {})
    if isinstance(v, dict):
        if "stage_ft" in v:
            return f"stage {v['stage_ft']} ft (flood_state_score={v.get('flood_state_score')})"
        if v.get("headline") or v.get("event"):
            parts = [v.get("headline") or v.get("event")]
            if v.get("severity"):
                parts.append(f"severity={v['severity']}")
            return " — ".join(str(p) for p in parts if p)
        if v.get("title"):
            score = v.get("page_score")
            score_s = f" page_score={score}" if score is not None else ""
            return f"{v['title']}{score_s}"
    return str(v)
