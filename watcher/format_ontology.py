"""Shared belief-state and rule-layer ontology report sections."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from watcher.claims.confidence import build_confidence_breakdown, overall_confidence


def _confidence_label(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def infer_belief_method(claim: Dict[str, Any]) -> str:
    value = claim.get("value") or {}
    if not isinstance(value, dict):
        value = {}
    claim_type = claim.get("claim_type", "")
    if value.get("trend_source") == "unscored_delta" or claim_type == "gauge_trend":
        return "cold_start_delta"
    if any(value.get(k) is not None for k in ("flood_stage", "action_stage", "moderate_stage", "major_stage")):
        return "rated_threshold"
    if value.get("fema_declared"):
        return "fema_declared"
    if claim_type in ("warning", "forecast_shift", "Warning", "ForecastShift"):
        return "corroborated"
    if claim.get("claim_id", "").startswith("claim_art_"):
        return "corroborated"
    return "corroborated"


def infer_trust_tier_level(claim: Dict[str, Any]) -> int:
    value = claim.get("value") or {}
    if isinstance(value, dict):
        tier = value.get("evidence_tier")
        if tier == "T0":
            return 0
        if tier == "T1":
            return 1
        if tier == "T2":
            return 2
        if tier == "T3":
            return 3
    comp = claim.get("confidence_components") or {}
    source = float(comp.get("source") or 0)
    if source >= 0.88:
        return 0
    if source >= 0.72:
        return 1
    if source >= 0.55:
        return 2
    return 3


def synthesize_confidence_breakdown(claim: Dict[str, Any]) -> Dict[str, Any]:
    """Build ontology belief decomposition when extraction did not attach one."""
    components = dict(claim.get("confidence_components") or {})
    if not components:
        components = {"source": 0.8, "location": 0.8, "time": 0.75, "impact": 0.7}
    method = infer_belief_method(claim)
    trust_level = infer_trust_tier_level(claim)
    overall = claim.get("overall_confidence")
    if overall is None:
        overall = overall_confidence(components)
    method_confidence = float(overall)
    uncertainty: List[str] = []
    if method == "cold_start_delta":
        method_confidence = float(claim.get("overall_confidence") or 0.45)
        uncertainty = ["no rated threshold; delta-based"]
    elif method == "rated_threshold":
        method_confidence = max(float(components.get("impact", 0.85)), 0.85)
    elif claim.get("claim_id", "").startswith("claim_art_"):
        uncertainty = ["T2 curated news; page_score weighted"]
    elif claim.get("claim_type") in ("warning", "forecast_shift", "Warning", "ForecastShift"):
        value = claim.get("value") or {}
        if isinstance(value, dict) and value.get("evidence_tier") == "T0":
            breakdown_existing = claim.get("confidence_breakdown") or {}
            notes = breakdown_existing.get("uncertainty_notes") or []
            uncertainty = list(notes)
    return build_confidence_breakdown(
        components,
        trust_tier_level=trust_level,
        method=method,
        method_confidence=method_confidence,
        uncertainty_notes=uncertainty,
    )


def prepare_ontology_display(data: Dict[str, Any]) -> None:
    """Ensure claims and monitoring context are ready for ontology report sections."""
    claims = data.get("evidence_claims") or []
    for claim in claims:
        if not claim.get("confidence_breakdown"):
            breakdown = synthesize_confidence_breakdown(claim)
            claim["confidence_breakdown"] = breakdown
            claim.setdefault("source_reliability_prior", breakdown["source_reliability_prior"])

    if not data.get("monitoring_summary"):
        ms = data.get("mission_spec") or {}
        if ms.get("fips") or ms.get("county"):
            data["monitoring_summary"] = {
                "hazard": "flood",
                "phase": ms.get("current_event_phase") or "early_watch",
                "county_fips": ms.get("fips"),
                "county_name": f"{ms.get('county', '')} County, {ms.get('state', '')}".strip(),
                "heartbeat_interval_seconds": ms.get("heartbeat_interval_seconds", 800),
            }


def belief_method_label(method: Any) -> str:
    if not method:
        return "corroborated"
    return str(method).replace("_", " ")


def claim_belief_detail(claim: Dict[str, Any], *, compact: bool = False) -> Optional[str]:
    """Ontology belief decomposition for a single claim."""
    breakdown = claim.get("confidence_breakdown")
    if not breakdown:
        return None
    method = belief_method_label(breakdown.get("method"))
    method_conf = breakdown.get("method_confidence")
    prior = breakdown.get("source_reliability_prior")
    notes = breakdown.get("uncertainty_notes") or []
    claim_id = claim.get("claim_id", "")
    superseded = claim.get("supersedes_claim_id")
    parts = [
        f"belief method={method}",
        f"method_conf={_confidence_label(method_conf)}",
        f"source_prior={_confidence_label(prior)}",
    ]
    if superseded:
        parts.append(f"supersedes={superseded}")
    if notes:
        parts.append(f"uncertainty={notes[0]}")
    detail = ", ".join(parts)
    if compact:
        prefix = f"`{claim.get('claim_type')}`"
        if claim_id:
            prefix += f" `{claim_id}`"
        return f"• {prefix}: {detail}"
    prefix = claim.get("claim_type", "claim")
    if claim_id:
        prefix += f" ({claim_id})"
    return f"  • {prefix}: {detail}"


def ontology_belief_section(data: Dict[str, Any], *, compact: bool = False) -> List[str]:
    """Belief-state ontology: decomposed confidence + optional Intelligence evidence."""
    claims = data.get("evidence_claims") or []
    decomposed = [c for c in claims if c.get("confidence_breakdown")]
    intel = data.get("intelligence")
    ledger = data.get("ontology_ledger")

    if not claims and not intel and not ledger:
        return []

    lines: List[str] = []
    if compact:
        lines.append("*Belief-state ontology*")
    else:
        lines.extend(["", "-" * 56, "Belief-state ontology", "-" * 56, ""])

    if decomposed:
        if compact:
            lines.append("• Decomposed confidence (source prior ≠ method confidence):")
        else:
            lines.append("Decomposed confidence (source-tier prior separate from scoring method):")
        for claim in decomposed[:5]:
            detail = claim_belief_detail(claim, compact=compact)
            if detail:
                lines.append(detail)
    elif claims:
        if compact:
            lines.append("• No scored claims this cycle for belief decomposition.")
        else:
            lines.append("  No scored claims this cycle for belief decomposition.")

    if intel:
        assessment = intel.get("assessment", "")
        evidence = intel.get("evidence") or []
        conf = intel.get("confidence")
        if compact:
            lines.append(f"• Intelligence assessment: {assessment}")
            if evidence:
                lines.append(f"• Evidence claim IDs: {', '.join(f'`{e}`' for e in evidence)}")
            if conf is not None:
                lines.append(f"• Intelligence confidence: {conf}")
        else:
            lines.append(f"Intelligence assessment: {assessment}")
            if evidence:
                lines.append(f"  Evidence claim IDs: {', '.join(evidence)}")
            if conf is not None:
                lines.append(f"  Intelligence confidence: {conf}")

    if ledger:
        counts = ledger.get("record_counts") or {}
        if counts:
            count_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            if compact:
                lines.append(f"• Ledger records: {count_str}")
            else:
                lines.append(f"  Ledger records: {count_str}")
        path = ledger.get("ledger_path")
        if path:
            if compact:
                lines.append(f"• Ledger path: `{path}`")
            else:
                lines.append(f"  Ledger path: {path}")

    return lines


def monitoring_summary_context(data: Dict[str, Any], *, compact: bool = False) -> List[str]:
    """Rule-layer ontology: phase heartbeat and matched transition rule."""
    ms_summary = data.get("monitoring_summary") or {}
    mission_spec = data.get("mission_spec") or {}
    heartbeat = ms_summary.get("heartbeat_interval_seconds") or mission_spec.get(
        "heartbeat_interval_seconds"
    )
    phase_decision = data.get("phase_decision") or {}
    matched = phase_decision.get("matched_rule")
    ms = data.get("mission_spec") or {}
    phase = ms.get("current_event_phase") or (data.get("belief_state") or {}).get("event_phase")

    lines: List[str] = []
    if compact:
        lines.append("*Ontology monitoring context*")
    else:
        lines.extend(["", "Ontology monitoring context"])

    if phase:
        if compact:
            lines.append(f"• Event phase: `{phase}`")
        else:
            lines.append(f"  Event phase: {phase}")

    if heartbeat is not None:
        hours = round(int(heartbeat) / 3600, 1)
        if compact:
            lines.append(f"• Phase heartbeat: {heartbeat}s ({hours}h)")
        else:
            lines.append(f"  Phase heartbeat: {heartbeat}s ({hours}h)")

    if matched:
        rule_txt = (
            f"{matched.get('from')} → {matched.get('to')} "
            f"on {matched.get('claim_type')} ({matched.get('condition', '')})"
        )
        if compact:
            lines.append(f"• Matched rule: {rule_txt}")
        else:
            lines.append(f"  Matched transition rule: {rule_txt}")

    return lines


def _content_lines(lines: List[str]) -> List[str]:
    """Drop leading blank / markdown title lines so Section renderer owns the header."""
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    if out and (
        out[0].startswith("*")
        or out[0].startswith("-" * 8)
        or out[0] in ("Belief-state ontology", "Ontology monitoring context")
        or out[0].startswith("Ontology monitoring")
    ):
        # Drop a title line and any immediate underline / blank follow-ups
        out.pop(0)
        while out and (not out[0].strip() or set(out[0].strip()) <= {"-"}):
            out.pop(0)
    return out


def belief_state_section(data: Dict[str, Any], *, compact: bool = True):
    """BELIEF_STATE section for OutputReport (v1)."""
    from watcher.ontology.output_schema import DEFAULT_SECTION_PRIORITY, Section, SectionType

    raw = ontology_belief_section(data, compact=compact)
    lines = _content_lines(raw)
    if not lines and not (data.get("evidence_claims") or data.get("intelligence") or data.get("ontology_ledger")):
        return None
    if not lines:
        return None
    return Section(
        section_type=SectionType.BELIEF_STATE,
        title="Belief-state ontology",
        emoji=None,
        lines=lines,
        priority=DEFAULT_SECTION_PRIORITY[SectionType.BELIEF_STATE],
        atomic=False,
        splittable=True,
    )


def ontology_context_section(data: Dict[str, Any], *, compact: bool = True):
    """ONTOLOGY_CONTEXT section for OutputReport (v1)."""
    from watcher.ontology.output_schema import DEFAULT_SECTION_PRIORITY, Section, SectionType

    raw = monitoring_summary_context(data, compact=compact)
    lines = _content_lines(raw)
    if not lines:
        return None
    return Section(
        section_type=SectionType.ONTOLOGY_CONTEXT,
        title="Ontology monitoring context",
        emoji=None,
        lines=lines,
        priority=DEFAULT_SECTION_PRIORITY[SectionType.ONTOLOGY_CONTEXT],
        atomic=False,
        splittable=True,
    )
