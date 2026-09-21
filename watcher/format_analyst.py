"""Plain-language situation summaries and confidence explanations for analyst delivery."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from watcher.config import sentinel_structured_analyst
from watcher.claims.confidence import build_confidence_breakdown, overall_confidence
from watcher.format_ontology import (
    claim_belief_detail,
    monitoring_summary_context,
    ontology_belief_section,
    prepare_ontology_display,
    synthesize_confidence_breakdown,
)
from watcher.format_report import (
    _confidence_components_text,
    _confidence_label,
    _excerpt,
    _phase_label,
    format_error_report,
)

# Backward-compatible aliases for tests and internal references
_prepare_ontology_display = prepare_ontology_display
_synthesize_confidence_breakdown = synthesize_confidence_breakdown
_claim_belief_detail = claim_belief_detail
_ontology_belief_section = ontology_belief_section
_monitoring_summary_context = monitoring_summary_context


def _is_insufficient_data(data: Dict[str, Any]) -> bool:
    api_ev = data.get("county_flood_evidence") or {}
    summary = (data.get("county_flood_payload") or {}).get("summary") or {}
    return bool(
        api_ev.get("insufficient_data")
        or api_ev.get("regional_verdict") == "insufficient_data"
        or summary.get("regional_verdict") == "insufficient_data"
    )


def _flooding_verdict(data: Dict[str, Any]) -> str:
    if _is_insufficient_data(data):
        return "insufficient_data"
    api_ev = data.get("county_flood_evidence") or {}
    payload = data.get("county_flood_payload") or {}
    summary = payload.get("summary") or {}
    verdict = api_ev.get("regional_verdict") or summary.get("regional_verdict")
    is_flooding = api_ev.get("is_flooding", summary.get("is_flooding"))
    if is_flooding is True:
        return f"active flooding indicated ({verdict or 'elevated gauges'})"
    if is_flooding is False:
        return f"no active flooding ({verdict or 'gauges below action stage'})"
    return f"flood status inconclusive ({verdict or 'insufficient gauge signal'})"


def _confidence_tier(score: Any) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 0.85:
        return "high"
    if value >= 0.70:
        return "moderate"
    if value >= 0.55:
        return "low"
    return "very low"


def _gauge_lines(claims: List[Dict[str, Any]], *, compact: bool = False) -> List[str]:
    from watcher.format_report import (
        _format_gauge_reading_bullet,
        _gauge_readings_empty_message,
        _iter_displayable_gauge_claims,
    )

    gauge_claims = _iter_displayable_gauge_claims(claims, limit=10)
    if not gauge_claims:
        return [_gauge_readings_empty_message(claims)]

    lines: List[str] = []
    for claim in gauge_claims:
        value = claim.get("value") or {}
        if not isinstance(value, dict):
            continue
        bullet = _format_gauge_reading_bullet(value)
        conf = claim.get("overall_confidence", "n/a")
        if compact:
            lines.append(f"• {bullet} (credibility {conf})")
        else:
            lines.append(f"  • {bullet} (credibility {conf})")
    return lines


def _alert_lines(claims: List[Dict[str, Any]], *, compact: bool = False) -> List[str]:
    alert_claims = [c for c in claims if c.get("claim_type") in ("warning", "forecast_shift", "Warning")]
    if not alert_claims:
        return ["No active NWS flood or hydrology alerts for the county zone."]
    lines: List[str] = []
    for claim in alert_claims[:4]:
        excerpt = _excerpt(claim.get("excerpt"), 90 if compact else 120)
        conf = claim.get("overall_confidence", "n/a")
        if compact:
            lines.append(f"• {claim.get('severity', 'alert')}: {excerpt} (credibility {conf})")
        else:
            lines.append(f"  • {claim.get('severity', 'alert')}: {excerpt} (credibility {conf})")
    return lines


def _confidence_explanation(data: Dict[str, Any], *, compact: bool = False) -> List[str]:
    api_ev = data.get("county_flood_evidence") or {}
    payload = data.get("county_flood_payload") or {}
    summary = payload.get("summary") or {}
    overall = api_ev.get("overall_confidence") or summary.get("overall_confidence")
    components = api_ev.get("confidence_components") or summary.get("confidence_components") or {}
    weight = api_ev.get("weight_hint") or summary.get("api_weight_hint") or "medium"

    lines: List[str] = []
    if compact:
        lines.append("*Confidence assessment*")
    else:
        lines.extend(["", "-" * 56, "Confidence assessment", "-" * 56, ""])

    if compact:
        lines.append(
            "• County display formula (legacy, non-operational): "
            "`0.35×source + 0.25×location + 0.20×time + 0.20×impact`"
        )
        lines.append(
            "• Scored-gauge credibility: "
            "`0.40×source + 0.25×location + 0.20×method + 0.15×quality`"
        )
    else:
        lines.extend(
            [
                "County display formula (legacy): "
                "0.35×source + 0.25×location + 0.20×time + 0.20×impact.",
                "  This aggregate is display-only; it does not control claim admission or phase changes.",
                "Scored-gauge credibility: "
                "0.40×source + 0.25×location + 0.20×method + 0.15×quality.",
                "  NWS/news legacy claim credibility uses normalized source + location only.",
            ]
        )

    if overall is not None:
        tier = _confidence_tier(overall)
        if compact:
            lines.append(
                f"• Official county display score: *{overall}* "
                f"({tier}; {weight} display label)"
            )
            lines.append(
                f"• Components: src={_confidence_label(components.get('source'))} "
                f"loc={_confidence_label(components.get('location'))} "
                f"time={_confidence_label(components.get('time'))} "
                f"impact={_confidence_label(components.get('impact'))}"
            )
        else:
            lines.extend(
                [
                    "",
                    f"Official county display score: {overall} "
                    f"({tier}; {weight} display label)",
                    (
                        f"  source={_confidence_label(components.get('source'))} "
                        f"location={_confidence_label(components.get('location'))} "
                        f"time={_confidence_label(components.get('time'))} "
                        f"impact={_confidence_label(components.get('impact'))}"
                    ),
                ]
            )

    if compact:
        lines.append("• Gauge currency is enforced by an age limit, outside the credibility score")
        lines.append("• Severity is reported separately from claim credibility")
    else:
        lines.extend(
            [
                "",
                "Interpretation:",
                "  • Source and county resolution contribute to credibility.",
                "  • Gauge method and data-quality terms describe how the threshold state was established.",
                "  • Gauge currency is enforced by an age limit, outside the credibility score.",
                "  • Severity is reported separately from claim credibility.",
            ]
        )

    claims = data.get("evidence_claims") or []
    scored = [c for c in claims if c.get("overall_confidence") is not None]
    if scored:
        if compact:
            lines.append("*Per-claim confidence (top)*")
        else:
            lines.append("")
            lines.append("Per-claim confidence (top signals):")
        for claim in scored[:5]:
            belief_detail = _claim_belief_detail(claim, compact=compact)
            if compact:
                lines.append(
                    f"• `{claim.get('claim_type')}`: {_confidence_label(claim.get('overall_confidence'))} "
                    f"({_confidence_components_text(claim.get('confidence_components') or {})})"
                )
                if belief_detail:
                    lines.append(f"  ↳ {belief_detail.split(': ', 1)[-1]}")
            else:
                lines.append(
                    f"  • {claim.get('claim_type')}: overall={_confidence_label(claim.get('overall_confidence'))} "
                    f"[{_confidence_components_text(claim.get('confidence_components') or {})}]"
                )
                if belief_detail:
                    lines.append(f"    {belief_detail.strip()}")

    article_ev = data.get("article_evidence") or {}
    articles = article_ev.get("high_confidence_articles") or []
    all_sources = article_ev.get("top_sources") or []
    if articles:
        threshold = article_ev.get("threshold", 0.75)
        if compact:
            lines.append(
                f"• News corroboration: {len(articles)} article(s) above "
                f"page-selection threshold {threshold:.2f}"
            )
        else:
            lines.append(
                f"  • News corroboration: {len(articles)} article(s) above "
                f"page-selection threshold {threshold:.2f}"
            )
    if all_sources:
        if compact:
            lines.append(f"• News sources returned: {len(all_sources)}")
            for src in all_sources[:3]:
                title = _excerpt(src.get("title") or "Untitled", 50)
                url = src.get("url") or ""
                score = src.get("page_score", 0)
                lines.append(f"• <{url}|{title}> ({float(score):.2f})")
        else:
            lines.append("  • News sources returned (flood-article-detector):")
            for src in all_sources[:5]:
                title = src.get("title") or "Untitled"
                url = src.get("url") or ""
                score = src.get("page_score", 0)
                lines.append(f"    - {title} ({float(score):.2f})")
                lines.append(f"      {url}")

    gaps: List[str] = []
    failed = data.get("failed_fetches") or []
    if failed:
        gaps.append(f"{len(failed)} source fetch failure(s)")
    if not scored:
        gaps.append("no scored gauge or alert claims this cycle")
    if api_ev.get("overall_confidence") is None and not summary.get("overall_confidence"):
        gaps.append("county flood API display score unavailable")

    if gaps:
        if compact:
            lines.append("*Uncertainty / gaps*")
            for gap in gaps:
                lines.append(f"• {gap}")
        else:
            lines.append("")
            lines.append("Uncertainty / gaps:")
            for gap in gaps:
                lines.append(f"  • {gap}")

    return lines


def _bottom_line(data: Dict[str, Any]) -> str:
    ms = data.get("mission_spec") or {}
    belief = data.get("belief_state") or {}
    phase = ms.get("current_event_phase") or belief.get("event_phase") or "unknown"
    alert = belief.get("alert_level") or (data.get("phase_decision") or {}).get("alert_level", "none")
    verdict_text = _flooding_verdict(data)
    county = ms.get("county") or data.get("county_under_monitor") or "the county"
    state = ms.get("state") or data.get("state_under_monitor") or ""

    phase_decision = data.get("phase_decision") or {}
    if phase_decision.get("phase_changed"):
        return (
            f"{county} County, {state} is in {_phase_label(phase)} with alert level {alert}. "
            f"Phase auto-changed this run. Hydrology indicates {verdict_text}."
        )
    return (
        f"{county} County, {state} remains in {_phase_label(phase)} (alert: {alert}). "
        f"Current evidence indicates {verdict_text}."
    )


def _build_structured_intelligence_summary(data: Dict[str, Any]) -> None:
    """Populate data['intelligence'] from ledger or in-memory claims."""
    mission_id = data.get("mission_id")
    if not mission_id:
        return
    try:
        from watcher.ontology.belief_schema import Belief, Claim, Intelligence
        from watcher.ontology.ledger import read_all, write as ledger_write

        claims: List[Any] = []
        beliefs: Dict[str, Any] = {}
        try:
            records = read_all(mission_id)
            claims = [r for r in records if r.type == "Claim" and r.status == "active"]
            beliefs = {r.claim_id: r for r in records if r.type == "Belief"}
        except (FileNotFoundError, OSError):
            records = []

        if not claims:
            for raw in data.get("evidence_claims") or []:
                breakdown = raw.get("confidence_breakdown") or {}
                claim_id = raw.get("claim_id", "")
                if not claim_id:
                    continue
                claims.append(
                    Claim(
                        id=claim_id,
                        content=(raw.get("excerpt") or str(raw.get("value", "")))[:200],
                        info_type=raw.get("claim_type", "unknown"),
                        location="inline",
                        timestamp=raw.get("observed_at") or "2026-01-01T00:00:00Z",
                        source_id=raw.get("source_id", "unknown"),
                        raw_reliability=float(breakdown.get("source_reliability_prior", 0.8)),
                    )
                )
                beliefs[claim_id] = Belief(
                    claim_id=claim_id,
                    score=float(raw.get("overall_confidence", breakdown.get("overall_score", 0.5))),
                    method=breakdown.get("method", "corroborated"),
                    confidence=float(
                        breakdown.get("method_confidence", raw.get("overall_confidence", 0.5))
                    ),
                    uncertainty=list(breakdown.get("uncertainty_notes") or []),
                )

        if not claims:
            return

        evidence_lines = "\n".join(
            f"- [{c.id}] {c.content} (source {c.source_id}, "
            f"belief {beliefs[c.id].score:.2f}, method {beliefs[c.id].method})"
            for c in claims
            if c.id in beliefs
        )
        if not evidence_lines.strip():
            return

        intel: Optional[Intelligence] = None
        try:
            import anthropic

            client = anthropic.Anthropic()
            resp = client.messages.parse(
                model=os.environ.get("SENTINEL_ANALYST_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=4000,
                system=(
                    "You are a disaster-intelligence analyst. From the scored claims, "
                    "produce ONE assessment. Put the exact claim ids you relied on in "
                    "`evidence`. Do not invent claims."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": f"Mission {mission_id}. Claims:\n{evidence_lines}",
                    }
                ],
                output_format=Intelligence,
            )
            intel = resp.parsed_output
        except Exception:
            top = claims[0]
            belief = beliefs.get(top.id)
            intel = Intelligence(
                assessment=_bottom_line(data),
                confidence=belief.score if belief else 0.5,
                evidence=[c.id for c in claims[:3] if c.id in beliefs],
                location=getattr(top, "location", "inline"),
            )

        valid_claim_ids = {c.id for c in claims}
        if any(e not in valid_claim_ids for e in intel.evidence):
            return

        if records:
            try:
                ledger_write(mission_id, intel)
            except Exception:
                pass
        data["intelligence"] = intel.model_dump(mode="json")
    except Exception:
        return


def _maybe_attach_structured_intelligence(data: Dict[str, Any]) -> None:
    """Populate data['intelligence'] when structured analyst flag is on."""
    if sentinel_structured_analyst().lower() != "on":
        return
    _build_structured_intelligence_summary(data)


def format_situation_summary(data: Dict[str, Any]) -> str:
    """Human-readable situation summary with confidence explanation."""
    if data.get("error"):
        return format_error_report(data)
    _prepare_ontology_display(data)
    _maybe_attach_structured_intelligence(data)
    return _build_legacy_situation_summary(data)


def _build_legacy_situation_summary(data: Dict[str, Any]) -> str:
    ms = data.get("mission_spec") or {}
    belief = data.get("belief_state") or {}
    delta = data.get("report_delta") or {}
    claims = data.get("evidence_claims") or []

    county = ms.get("county") or data.get("county_under_monitor") or "unknown"
    state = ms.get("state") or data.get("state_under_monitor") or ""
    location = ms.get("location") or f"{county} County, {state}".strip(", ")
    phase = ms.get("current_event_phase") or belief.get("event_phase") or "unknown"
    alert = belief.get("alert_level") or (data.get("phase_decision") or {}).get("alert_level", "none")
    run_at = data.get("run_at", "now")

    lines: List[str] = [
        "=" * 56,
        "  Disaster Sentinel — Situation Analysis",
        "=" * 56,
        "",
        f"Location:  {location}",
        f"Mission:   {data.get('mission_id', 'unknown')}",
        f"Run at:    {run_at}",
        f"Phase:     {_phase_label(phase)}",
        f"Alert:     {alert}",
        "",
        "Situation at a glance",
        "-" * 56,
        _bottom_line(data),
        "",
        "Hydrology (USGS / NWPS)",
    ]
    lines.extend(_gauge_lines(claims))
    lines.extend(["", "NWS alerts"])
    lines.extend(_alert_lines(claims))
    lines.extend(_confidence_explanation(data))

    intel = data.get("intelligence")
    if intel and intel.get("assessment"):
        lines.extend(["", "Ontology intelligence (structured)"])
        lines.append(f"  • {intel.get('assessment')}")
        evidence = intel.get("evidence") or []
        if evidence:
            lines.append(f"  • Evidence: {', '.join(evidence)}")

    lines.extend(_ontology_belief_section(data))
    lines.extend(_monitoring_summary_context(data))

    lines.extend(["", "Changes since last report"])
    if delta.get("first_run"):
        lines.append("  • First analyst report for this mission.")
    else:
        for bullet in delta.get("summary_bullets") or ["No material changes recorded."]:
            lines.append(f"  • {bullet}")

    phase_decision = data.get("phase_decision") or {}
    if phase_decision.get("phase_changed"):
        lines.extend(
            [
                "",
                "Phase evaluation",
                f"  • Transition: {_phase_label(phase_decision.get('current_phase'))} → "
                f"{_phase_label(phase_decision.get('new_phase'))}",
            ]
        )
        if phase_decision.get("matched_rule"):
            rule = phase_decision["matched_rule"]
            lines.append(f"  • Rule: {rule.get('from')} → {rule.get('to')} ({rule.get('condition', '')})")

    lines.extend(["", "Raw coordinator data is available via `sentinel-cli run-coordinator --format json`."])
    return "\n".join(lines)


def format_analyst_slack(data: Dict[str, Any]) -> str:
    """Compact Slack markdown for analyst situation summary."""
    if data.get("error"):
        return format_error_report(data)

    _prepare_ontology_display(data)
    _maybe_attach_structured_intelligence(data)

    ms = data.get("mission_spec") or {}
    belief = data.get("belief_state") or {}
    delta = data.get("report_delta") or {}
    claims = data.get("evidence_claims") or []

    county = ms.get("county") or data.get("county_under_monitor") or "unknown"
    state = ms.get("state") or data.get("state_under_monitor") or ""
    phase = ms.get("current_event_phase") or belief.get("event_phase") or "unknown"
    alert = belief.get("alert_level") or (data.get("phase_decision") or {}).get("alert_level", "none")
    run_at = data.get("run_at", "now")

    lines = [
        "*Disaster Sentinel — Situation Analysis*",
        f"*County:* {county} County, {state} (FIPS {ms.get('fips', 'n/a')})",
        f"*Phase:* {_phase_label(phase)} | *Alert:* {alert}",
        f"*Mission:* `{data.get('mission_id', 'unknown')}`",
        f"*Run at:* {run_at}",
        "",
        "*Bottom line*",
        _bottom_line(data),
        "",
        "*Hydrology*",
    ]
    lines.extend(_gauge_lines(claims, compact=True))
    lines.append("")
    lines.append("*NWS alerts*")
    lines.extend(_alert_lines(claims, compact=True))
    lines.append("")
    lines.extend(_confidence_explanation(data, compact=True))

    intel = data.get("intelligence")
    if intel and intel.get("assessment"):
        lines.append("")
        lines.append("*Ontology intelligence*")
        lines.append(f"• {intel.get('assessment')}")
        evidence = intel.get("evidence") or []
        if evidence:
            lines.append(f"• Evidence: {', '.join(f'`{e}`' for e in evidence)}")

    belief_lines = _ontology_belief_section(data, compact=True)
    if belief_lines:
        lines.append("")
        lines.extend(belief_lines)

    ctx_lines = _monitoring_summary_context(data, compact=True)
    if ctx_lines:
        lines.append("")
        lines.extend(ctx_lines)

    lines.append("")
    lines.append("*Changes since last report*")
    if delta.get("first_run"):
        lines.append("• First analyst report for this mission")
    else:
        for bullet in delta.get("summary_bullets") or ["No material changes"]:
            lines.append(f"• {bullet}")

    phase_decision = data.get("phase_decision") or {}
    if phase_decision.get("phase_changed"):
        lines.append("")
        lines.append(
            f":warning: *Phase changed:* "
            f"{_phase_label(phase_decision.get('current_phase'))} → "
            f"{_phase_label(phase_decision.get('new_phase'))}"
        )

    lines.append(f"\n_Analyst · {county} County · {_phase_label(phase)} · {run_at}_")
    return "\n".join(lines)


def format_analyst_report_with_slack_block(data: Dict[str, Any]) -> str:
    """Human analyst summary plus ---SLACK--- block."""
    human = format_situation_summary(data)
    slack_msg = data.get("analyst_slack_message") or format_analyst_slack(data)
    return f"{human}\n\n---SLACK---\n{slack_msg}"



def intelligence_section(data: Dict[str, Any]):
    """INTELLIGENCE section for OutputReport (v1)."""
    from watcher.ontology.output_schema import DEFAULT_SECTION_PRIORITY, Section, SectionType

    intel = data.get("intelligence")
    if not intel or not intel.get("assessment"):
        return None
    lines = [f"• {intel.get('assessment')}"]
    evidence = intel.get("evidence") or []
    if evidence:
        lines.append(f"• Evidence: {', '.join(f'`{e}`' for e in evidence)}")
    if intel.get("confidence") is not None:
        lines.append(f"• Confidence: {intel.get('confidence')}")
    return Section(
        section_type=SectionType.INTELLIGENCE,
        title="Ontology intelligence",
        lines=lines,
        priority=DEFAULT_SECTION_PRIORITY[SectionType.INTELLIGENCE],
    )


def build_analyst_output_report(data: Dict[str, Any]):
    """Assemble a typed OutputReport for analyst Slack/human renderers (v1)."""
    from watcher.format_ontology import belief_state_section, ontology_context_section
    from watcher.ontology.output_schema import (
        DEFAULT_SECTION_PRIORITY,
        OutputReport,
        Section,
        SectionType,
    )

    if data.get("error"):
        return OutputReport(
            mission_id=str(data.get("mission_id") or "unknown"),
            report_kind="analyst",
            generated_at_utc=str(data.get("run_at") or ""),
            sections=[
                Section(
                    section_type=SectionType.MISSION_HEADER,
                    title="Analyst Error",
                    lines=format_error_report(data).splitlines(),
                    priority=0,
                    atomic=True,
                    splittable=False,
                )
            ],
        )

    _prepare_ontology_display(data)
    _maybe_attach_structured_intelligence(data)

    ms = data.get("mission_spec") or {}
    belief = data.get("belief_state") or {}
    delta = data.get("report_delta") or {}
    claims = data.get("evidence_claims") or []
    phase_decision = data.get("phase_decision") or {}

    county = ms.get("county") or data.get("county_under_monitor") or "unknown"
    state = ms.get("state") or data.get("state_under_monitor") or ""
    phase = ms.get("current_event_phase") or belief.get("event_phase") or "unknown"
    alert = belief.get("alert_level") or phase_decision.get("alert_level", "none")
    run_at = data.get("run_at", "now")
    mission_id = str(data.get("mission_id") or "unknown")

    report = OutputReport(
        mission_id=mission_id,
        report_kind="analyst",
        generated_at_utc=str(run_at),
    )

    report.append(
        Section(
            section_type=SectionType.MISSION_HEADER,
            title="Disaster Sentinel — Situation Analysis",
            lines=[
                f"*County:* {county} County, {state} (FIPS {ms.get('fips', 'n/a')})",
                f"*Phase:* {_phase_label(phase)} | *Alert:* {alert}",
                f"*Mission:* `{mission_id}`",
                f"*Run at:* {run_at}",
            ],
            priority=DEFAULT_SECTION_PRIORITY[SectionType.MISSION_HEADER],
            atomic=True,
            splittable=False,
        )
    )

    report.append(
        Section(
            section_type=SectionType.PHASE_STATUS,
            title="Bottom line",
            lines=[_bottom_line(data)],
            priority=DEFAULT_SECTION_PRIORITY[SectionType.PHASE_STATUS],
            atomic=True,
            splittable=False,
        )
    )

    official: List[str] = ["*Hydrology*"]
    official.extend(_gauge_lines(claims, compact=True))
    official.append("*NWS alerts*")
    official.extend(_alert_lines(claims, compact=True))
    conf = _confidence_explanation(data, compact=True)
    # Drop leading blank / *Confidence* title if present
    while conf and not conf[0].strip():
        conf = conf[1:]
    if conf and conf[0].startswith("*"):
        conf = conf[1:]
        while conf and not conf[0].strip():
            conf = conf[1:]
    if conf:
        official.append("*Confidence*")
        official.extend(conf)
    report.append(
        Section(
            section_type=SectionType.OFFICIAL_EVIDENCE,
            title="Official Hydrology",
            lines=official,
            priority=DEFAULT_SECTION_PRIORITY[SectionType.OFFICIAL_EVIDENCE],
        )
    )

    intel = intelligence_section(data)
    if intel:
        report.append(intel)

    belief_sec = belief_state_section(data, compact=True)
    if belief_sec:
        report.append(belief_sec)
    ctx = ontology_context_section(data, compact=True)
    if ctx:
        report.append(ctx)

    if delta.get("first_run"):
        delta_lines = ["• First analyst report for this mission"]
    else:
        delta_lines = [f"• {b}" for b in (delta.get("summary_bullets") or ["No material changes"])]
    if phase_decision.get("phase_changed"):
        delta_lines.append(
            f":warning: *Phase changed:* "
            f"{_phase_label(phase_decision.get('current_phase'))} → "
            f"{_phase_label(phase_decision.get('new_phase'))}"
        )
    report.append(
        Section(
            section_type=SectionType.REPORT_DELTA,
            title="Changes since last report",
            lines=delta_lines,
            priority=DEFAULT_SECTION_PRIORITY[SectionType.REPORT_DELTA],
        )
    )

    report.append(
        Section(
            section_type=SectionType.DELIVERY_FOOTER,
            title="Delivery",
            lines=[f"_Analyst · {county} County · {_phase_label(phase)} · {run_at}_"],
            priority=DEFAULT_SECTION_PRIORITY[SectionType.DELIVERY_FOOTER],
            atomic=True,
            splittable=False,
        )
    )
    return report


def format_analyst_slack_v1(data: Dict[str, Any]) -> tuple:
    """Return (primary_slack_message, slack_messages_list) via OutputReport pipeline."""
    from watcher.format_slack import build_slack_messages

    report = build_analyst_output_report(data)
    messages = build_slack_messages(report)
    primary = "\n\n".join(messages) if messages else ""
    return primary, messages
