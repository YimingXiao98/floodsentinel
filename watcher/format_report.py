"""Human-readable and Slack report formatting for coordinator output."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from watcher.format_ontology import (
    monitoring_summary_context,
    ontology_belief_section,
    prepare_ontology_display,
)


def _phase_label(phase: Optional[str]) -> str:
    if not phase:
        return "unknown"
    return phase.replace("_", " ").title()


def _change_label(status: Optional[str]) -> str:
    labels = {
        "new": "New reading",
        "changed": "Content changed",
        "unchanged": "Unchanged since last fetch",
    }
    return labels.get(status or "", status or "unknown")


def _format_bytes(n: Any) -> str:
    try:
        size = int(n)
    except (TypeError, ValueError):
        return "unknown"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f} KB"
    return f"{size} bytes"


def _excerpt(text: Optional[str], limit: int = 120) -> str:
    if not text:
        return "(empty response)"
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


def format_error_report(data: Dict[str, Any]) -> str:
    lines = [
        "Disaster Sentinel — Coordinator Error",
        "",
        f"Error:   {data.get('error', 'unknown')}",
        f"Message: {data.get('message', 'No details')}",
    ]
    if data.get("hint"):
        lines.append(f"Hint:    {data['hint']}")
    for key in ("county", "state", "fips", "mission_id"):
        if data.get(key):
            lines.append(f"{key.replace('_', ' ').title()}: {data[key]}")
    return "\n".join(lines)


def _is_insufficient_data(data: Dict[str, Any]) -> bool:
    """True when county flood verdict is insufficient_data (< min gauges)."""
    api_ev = data.get("county_flood_evidence") or {}
    summary = (data.get("county_flood_payload") or {}).get("summary") or {}
    return bool(
        api_ev.get("insufficient_data")
        or api_ev.get("regional_verdict") == "insufficient_data"
        or summary.get("regional_verdict") == "insufficient_data"
    )


def _regional_verdict_label(data: Dict[str, Any], *, fallback: str = "n/a") -> str:
    """Regional verdict for display; never collapse sparse counties to no_flooding."""
    if _is_insufficient_data(data):
        return "insufficient_data"
    api_ev = data.get("county_flood_evidence") or {}
    summary = (data.get("county_flood_payload") or {}).get("summary") or {}
    return (
        api_ev.get("regional_verdict")
        or summary.get("regional_verdict")
        or fallback
    )


def _flooding_status_label(data: Dict[str, Any]) -> Any:
    """is_flooding for display; use insufficient_data instead of False/no_flooding."""
    if _is_insufficient_data(data):
        return "insufficient_data"
    api_ev = data.get("county_flood_evidence") or {}
    summary = (data.get("county_flood_payload") or {}).get("summary") or {}
    if "is_flooding" in api_ev:
        return api_ev.get("is_flooding")
    return summary.get("is_flooding")


def _delta_section(delta: Dict[str, Any]) -> List[str]:
    lines = ["", "-" * 56, "Changes since last report", "-" * 56, ""]
    if delta.get("first_run"):
        lines.append("• First coordinator report for this mission.")
        return lines
    for bullet in delta.get("summary_bullets") or []:
        lines.append(f"• {bullet}")
    return lines


def _article_evidence_block(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return data.get("article_evidence")


def _article_evidence_section(data: Dict[str, Any], *, compact: bool = False) -> List[str]:
    """Human or Slack lines for articles selected by the detector page score."""
    evidence = _article_evidence_block(data)
    if not evidence:
        return []

    threshold = evidence.get("threshold", 0.75)
    articles = evidence.get("high_confidence_articles") or []
    outcome = evidence.get("flood_outcome") or {}
    lines: List[str] = []

    if compact:
        lines.extend(["", "*News article evidence*"])
        if evidence.get("status") != "ok":
            lines.append(f"• Article scan failed: {evidence.get('detail', 'unknown error')}")
            return lines
        if not articles:
            lines.append(f"• No articles above page-selection threshold ({threshold:.2f})")
            return lines
        verdict = outcome.get("verdict", "n/a")
        outcome_score = outcome.get("outcome_score")
        if outcome_score is not None:
            lines.append(
                f"• Verdict: *{verdict}* (aggregate score {float(outcome_score):.2f})"
            )
        for article in articles:
            title = _excerpt(article.get("title") or "Untitled", 70)
            url = article.get("url") or ""
            score = article.get("page_score", 0)
            lines.append(f"• <{url}|{title}> — detector page score *{float(score):.2f}*")
        return lines

    lines.extend(
        [
            "",
            "-" * 56,
            f"News article evidence (detector page score > {threshold:.2f})",
            "-" * 56,
            "",
        ]
    )
    if evidence.get("status") != "ok":
        lines.append(f"Article scan failed: {evidence.get('detail', 'unknown error')}")
        lines.append("")
        return lines
    if not articles:
        lines.append(f"No articles above page-selection threshold ({threshold:.2f}).")
        lines.append("")
        return lines

    if outcome:
        lines.append(
            f"Aggregate verdict: {outcome.get('verdict', 'n/a')} "
            f"(outcome_score={outcome.get('outcome_score', 'n/a')}, "
            f"detector_confidence={outcome.get('confidence', 'n/a')})"
        )
        rationale = outcome.get("rationale")
        if rationale:
            lines.append(f"  {rationale}")
        lines.append("")

    for article in articles:
        rank = article.get("rank", "?")
        title = article.get("title") or "Untitled"
        url = article.get("url") or ""
        score = article.get("page_score", 0)
        published = article.get("published_at") or "no date"
        indication = article.get("indication", "neutral")
        lines.append(f"#{rank} page_score={float(score):.4f} [{indication}] {title}")
        lines.append(f"   Published: {published}")
        lines.append(f"   URL: {url}")
        lines.append("")

    return lines


def _article_sources_section(data: Dict[str, Any], *, compact: bool = False) -> List[str]:
    """Show all flood-article-detector sources and validation usage."""
    evidence = _article_evidence_block(data)
    if not evidence:
        return []

    top_sources = evidence.get("top_sources") or []
    threshold = float(evidence.get("threshold", 0.75) or 0.75)
    article_claim_urls = {
        (c.get("value") or {}).get("url")
        for c in (data.get("evidence_claims") or [])
        if isinstance(c.get("value"), dict) and c.get("claim_id", "").startswith("claim_art_")
    }
    article_claim_urls.discard(None)

    if compact:
        lines: List[str] = ["", "*News sources returned (validation input)*"]
        if not top_sources:
            lines.append("• No sources returned by flood-article-detector.")
            return lines
        lines.append(
            f"• Sources returned: {len(top_sources)} (page-selection threshold > {threshold:.2f})"
        )
        for src in top_sources[:5]:
            url = src.get("url") or ""
            title = _excerpt(src.get("title") or "Untitled", 70)
            score = float(src.get("page_score") or 0)
            used = "used" if url and url in article_claim_urls else "not_used"
            lines.append(f"• <{url}|{title}> — page score {score:.2f} ({used})")
        return lines

    lines = [
        "",
        "-" * 56,
        "News sources returned (validation input)",
        "-" * 56,
        "",
    ]
    if not top_sources:
        lines.append("No sources returned by flood-article-detector.")
        lines.append("")
        return lines
    article_urls = evidence.get("article_urls") or [
        str(src.get("url")) for src in top_sources if src.get("url")
    ]
    if article_urls:
        lines.append("Fetched article URLs:")
        for url in article_urls:
            lines.append(f"  • {url}")
        lines.append("")
    lines.append(
        f"Sources returned: {len(top_sources)} "
        f"(positive indications become candidate claims when page score > {threshold:.2f})"
    )
    lines.append("")
    for idx, src in enumerate(top_sources, start=1):
        url = src.get("url") or ""
        title = src.get("title") or "Untitled"
        score = float(src.get("page_score") or 0)
        indication = src.get("indication") or "neutral"
        used = "used_for_validation" if url and url in article_claim_urls else "not_used"
        lines.append(f"{idx}. page_score={score:.4f} [{indication}] {title}")
        lines.append(f"   URL: {url}")
        lines.append(f"   Validation use: {used}")
        lines.append("")
    return lines


def _claims_section(claims: List[Dict[str, Any]]) -> List[str]:
    lines = ["", "-" * 56, "Evidence claims (this cycle)", "-" * 56, ""]
    if not claims:
        lines.append("No accepted evidence claims this cycle.")
        lines.append("")
        return lines
    for idx, claim in enumerate(claims[:10], start=1):
        lines.extend(
            [
                f"{idx}. {claim.get('claim_type', 'unknown')} — {claim.get('severity', 'n/a')}",
                f"   Claim ID:    {claim.get('claim_id')}",
                f"   Credibility: {claim.get('overall_confidence', 'n/a')}",
                f"   Location:    {claim.get('location_text', '')}",
                f"   Excerpt:     {_excerpt(claim.get('excerpt'), 100)}",
            ]
        )
        evidence_url = claim.get("evidence_url")
        if evidence_url:
            lines.append(f"   URL:         {evidence_url}")
        lines.append("")
    return lines


def _api_endpoints_section(data: Dict[str, Any]) -> List[str]:
    """List every flood API URL called this run."""
    records = data.get("source_records") or []
    mapping = data.get("county_api_mapping") or {}
    if not records and not mapping:
        return []

    lines = ["", "-" * 56, "API endpoints called", "-" * 56, ""]
    lines.append("Predefined T0 sources (resolved per county FIPS):")
    lines.append("")

    for rec in records:
        name = rec.get("source_name") or rec.get("ontology_source_type") or "unknown"
        url = rec.get("source_url") or ""
        strategy = rec.get("resolution_strategy") or rec.get("discovery_hint") or ""
        health = rec.get("health_status", "unknown")
        api_res = rec.get("api_resolution") or {}
        resolved = api_res.get("resolved_ids") or {}

        lines.append(f"• {name}")
        lines.append(f"  Strategy:  {strategy}")
        lines.append(f"  Endpoint:  {url}")
        lines.append(f"  Health:    {health}")
        if resolved.get("sites"):
            lines.append(f"  Site IDs:  {', '.join(resolved['sites'])}")
        if resolved.get("zone"):
            lines.append(f"  NWS zone:  {resolved['zone']}")
        lines.append("")

    if mapping.get("resolved_at"):
        lines.append(f"Resolution cached at: {mapping.get('resolved_at')}")
    lines.append("")
    return lines


def _confidence_scoring_section(data: Dict[str, Any]) -> List[str]:
    """Distinguish operational claim credibility from the legacy display score."""
    lines = [
        "",
        "-" * 56,
        "Credibility and display scores",
        "-" * 56,
        "",
        "Claim credibility is used by the admission gate in belief-enabled runs; ablations bypass that gate.",
        "  Scored gauges: 0.40×source + 0.25×location + 0.20×method + 0.15×quality.",
        "  NWS/news legacy claims: normalized source + location only; time and impact are descriptive.",
        "",
    ]

    api_ev = data.get("county_flood_evidence") or {}
    payload = data.get("county_flood_payload") or {}
    summary = payload.get("summary") or {}
    overall = api_ev.get("overall_confidence") or summary.get("overall_confidence")
    components = api_ev.get("confidence_components") or summary.get("confidence_components") or {}

    if overall is not None:
        comp = components
        lines.extend(
            [
                "County flood API (NWPS + USGS) — legacy display summary:",
                "  Display formula: 0.35×source + 0.25×location + 0.20×time + 0.20×impact.",
                "  This aggregate is not used for claim admission or phase decisions.",
                f"  display_score={overall}  display_label={api_ev.get('weight_hint', summary.get('api_weight_hint', 'n/a'))}",
                f"  regional_verdict={_regional_verdict_label(data)}  "
                f"is_flooding={_flooding_status_label(data)}  "
                f"verdict_gauges={api_ev.get('verdict_gauge_count', summary.get('verdict_gauge_count', 'n/a'))}",
                f"  [src={_confidence_label(comp.get('source'))} "
                f"loc={_confidence_label(comp.get('location'))} "
                f"time={_confidence_label(comp.get('time'))} "
                f"impact={_confidence_label(comp.get('impact'))}]",
                "",
                "",
            ]
        )

    claims = data.get("evidence_claims") or []
    article_ev = data.get("article_evidence") or {}
    top_sources = article_ev.get("top_sources") or []
    threshold = float(article_ev.get("threshold", 0.75) or 0.75)
    article_claims = [c for c in claims if str(c.get("claim_id", "")).startswith("claim_art_")]
    if top_sources:
        lines.extend(
            [
                "News/web validation:",
                f"  flood-article-detector returned {len(top_sources)} source(s); "
                f"{len(article_claims)} accepted positive T2 claim(s) remain after "
                f"page selection (> {threshold:.2f}) and claim admission.",
                "  Page score is a detector relevance/recency score, not epistemic credibility.",
                "",
            ]
        )

    if not claims:
        lines.append("No claims scored this cycle.")
        lines.append("")
        return lines

    lines.append("Per-claim scores:")
    for claim in claims[:12]:
        overall = claim.get("overall_confidence", "n/a")
        ctype = claim.get("claim_type", "unknown")
        lines.append(
            f"  • {ctype} overall={overall} "
            f"[{_confidence_components_text(claim.get('confidence_components') or {})}]"
        )
    lines.append("")
    return lines


def _stage_delta_ft(val: Dict[str, Any]) -> Optional[float]:
    prior = val.get("prior_observed_primary")
    stage = val.get("gage_height_ft")
    delta = val.get("delta_primary_ft")
    if delta is not None:
        try:
            return float(delta)
        except (TypeError, ValueError):
            return None
    if prior is None or stage is None:
        return None
    try:
        return float(stage) - float(prior)
    except (TypeError, ValueError):
        return None


def _significant_display_delta(delta: Optional[float], threshold: float = 0.1) -> bool:
    if delta is None:
        return False
    try:
        return round(abs(float(delta)), 3) >= threshold
    except (TypeError, ValueError):
        return False


_CATEGORY_DISPLAY_RANK = {
    "record_flood": 5.0,
    "major_flooding": 4.0,
    "moderate_flooding": 3.0,
    "minor_flooding": 2.0,
    "action": 1.0,
    "no_flooding": 0.0,
}


def _is_new_gauge_reading(val: Dict[str, Any]) -> bool:
    """True when no prior baseline exists (cold start / newly tracked gauge)."""
    if str(val.get("hydrologic_signal") or "").strip().lower() == "cold_start":
        return True
    if val.get("gage_height_ft") is None:
        return False
    return val.get("prior_observed_primary") is None and val.get("delta_primary_ft") is None


def _should_display_gauge_reading(val: Dict[str, Any], threshold: float = 0.1) -> bool:
    """
    New / cold-start gauges: always eligible for display (flood status from absolute stage).
    Existing gauges with a prior: only when |Δstage| >= threshold.
    """
    if _is_new_gauge_reading(val):
        return True
    return _significant_display_delta(_stage_delta_ft(val), threshold=threshold)


def _gauge_display_sort_key(claim: Dict[str, Any]) -> tuple:
    """Prefer higher flood severity, then larger |Δstage|, then first-reading gauges."""
    val = claim.get("value") if isinstance(claim.get("value"), dict) else {}
    score = val.get("flood_state_score")
    try:
        severity = float(score) if score is not None else None
    except (TypeError, ValueError):
        severity = None
    if severity is None:
        cat = str(val.get("flood_category") or "").strip().lower()
        severity = _CATEGORY_DISPLAY_RANK.get(cat, 0.0)
    delta = _stage_delta_ft(val)
    try:
        abs_delta = abs(float(delta)) if delta is not None else 0.0
    except (TypeError, ValueError):
        abs_delta = 0.0
    is_new = 1 if _is_new_gauge_reading(val) else 0
    return (severity, abs_delta, is_new)


def _threshold_status_for_display(val: Dict[str, Any]) -> str:
    """Classify current stage vs action/minor/mod/major for Slack bullets."""
    category = str(val.get("flood_category") or "").strip().lower()
    label_map = {
        "no_flooding": "below_action",
        "action": "action",
        "minor_flooding": "minor",
        "moderate_flooding": "moderate",
        "major_flooding": "major",
        "record_flood": "record",
    }
    status = label_map.get(category, "unclassified")
    stage = val.get("gage_height_ft")
    if stage is None:
        return status
    try:
        obs = float(stage)
    except (TypeError, ValueError):
        return status

    ladder = [
        ("action", val.get("action_stage")),
        ("minor", val.get("flood_stage")),
        ("mod", val.get("moderate_stage")),
        ("major", val.get("major_stage")),
    ]
    for name, thresh in ladder:
        if thresh is None:
            continue
        try:
            t = float(thresh)
        except (TypeError, ValueError):
            continue
        if obs < t:
            return f"{status} ({t - obs:.1f}ft below {name})"
    highest = next((name for name, thresh in reversed(ladder) if thresh is not None), None)
    if highest:
        return f"{status} (at/above {highest})"
    return status


def _format_gauge_reading_bullet(val: Dict[str, Any]) -> str:
    """Slack/human one-liner: stage + delta, status, thresholds (no empty flow)."""
    site = val.get("site_no") or val.get("shef_id") or "?"
    stage = val.get("gage_height_ft")
    flow = val.get("streamflow_cfs")
    stage_s = f"{stage:.2f}ft" if stage is not None else "n/a"

    prior = val.get("prior_observed_primary")
    delta = _stage_delta_ft(val)
    if prior is not None and stage is not None and _significant_display_delta(delta):
        try:
            stage_s = f"{float(prior):.2f}→{float(stage):.2f}ft (Δ{float(delta):+.2f})"
        except (TypeError, ValueError):
            pass
    elif _is_new_gauge_reading(val) and stage is not None:
        try:
            stage_s = f"{float(stage):.2f}ft (first reading)"
        except (TypeError, ValueError):
            pass

    parts = [f"USGS `{site}`: {stage_s}"]
    if flow is not None:
        try:
            parts.append(f"{float(flow):.0f}cfs")
        except (TypeError, ValueError):
            pass
    parts.append(f"status={_threshold_status_for_display(val)}")
    thresholds = []
    if val.get("action_stage") is not None:
        thresholds.append(f"action>={val['action_stage']}")
    if val.get("flood_stage") is not None:
        thresholds.append(f"minor>={val['flood_stage']}")
    if val.get("moderate_stage") is not None:
        thresholds.append(f"mod>={val['moderate_stage']}")
    if val.get("major_stage") is not None:
        thresholds.append(f"major>={val['major_stage']}")
    if thresholds:
        parts.append(f"[{', '.join(thresholds)}]")
    return " ".join(parts)


def _iter_displayable_gauge_claims(
    claims: List[Dict[str, Any]],
    *,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    GaugeObservation claims for display, capped at `limit`.

    - Cold-start / newly tracked gauges (no prior): included so flood status can be
      assessed from absolute stage on first monitoring.
    - Existing gauges: only when |Δstage| >= 0.1 ft.
    Ranked by flood severity, then |Δ|, then first-reading.
    """
    eligible: List[Dict[str, Any]] = []
    for claim in claims:
        if claim.get("claim_type") not in ("gauge_rising", "GaugeObservation"):
            continue
        value = claim.get("value") or {}
        if not isinstance(value, dict):
            continue
        if not _should_display_gauge_reading(value):
            continue
        eligible.append(claim)
    eligible.sort(key=_gauge_display_sort_key, reverse=True)
    return eligible[:limit]


def _gauge_readings_empty_message(claims: List[Dict[str, Any]]) -> str:
    """Context-aware empty line when no gauges pass the display filter."""
    has_existing_prior = False
    for claim in claims:
        if claim.get("claim_type") not in ("gauge_rising", "GaugeObservation"):
            continue
        value = claim.get("value") or {}
        if not isinstance(value, dict):
            continue
        if value.get("prior_observed_primary") is not None or value.get("delta_primary_ft") is not None:
            has_existing_prior = True
            break
    if has_existing_prior:
        return "No gauges with |Δstage| ≥ 0.1 ft this run (existing baselines unchanged)."
    return "No gauge readings available for display."


def _flood_readings_section(
    claims: List[Dict[str, Any]],
    county_flood_payload: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Current hydrology and alert readings from the two flood sources."""
    gauge_claims = _iter_displayable_gauge_claims(claims, limit=10)
    alert_claims = [c for c in claims if c.get("claim_type") in ("warning", "forecast_shift", "Warning")]

    lines = ["", "-" * 56, "Flood readings (this run)", "-" * 56, ""]

    lines.append("USGS / NWPS county flood gauges (county-flood-data skill):")
    if not gauge_claims:
        lines.append(f"  ({_gauge_readings_empty_message(claims)})")
    else:
        for claim in gauge_claims:
            value = claim.get("value") or {}
            if not isinstance(value, dict):
                continue
            obs = value.get("observation_time") or claim.get("observed_at", "")
            conf = claim.get("overall_confidence", "n/a")
            lines.append(f"  • {_format_gauge_reading_bullet(value)} @ {obs} (conf {conf})")

    payload = county_flood_payload
    if payload and payload.get("summary"):
        summary = payload["summary"]
        lines.append("")
        lines.append("County flood aggregates:")
        overall = summary.get("overall_confidence")
        if overall is not None:
            lines.append(
                f"  legacy_display_score={overall} "
                f"(display_label={summary.get('api_weight_hint', 'n/a')}; non-operational)"
            )
        display_verdict = summary.get("regional_verdict")
        display_flooding = summary.get("is_flooding")
        if display_verdict == "insufficient_data":
            display_flooding = "insufficient_data"
        lines.append(
            f"  regional_verdict={display_verdict} "
            f"is_flooding={display_flooding} "
            f"verdict_confidence={summary.get('confidence')}"
        )
        lines.append(
            f"  average_flood_state={summary.get('average_flood_state')} "
            f"({summary.get('verdict_gauge_count', summary.get('gauges_scored', 0))} scored gauges)"
        )
        if summary.get("average_primary_reading") is not None:
            unit = summary.get("average_primary_unit") or "ft"
            lines.append(
                f"  average_primary_reading={summary.get('average_primary_reading')} {unit} "
                f"({summary.get('gauges_with_data', 0)} gauges with data)"
            )
        lines.append(f"  generated_at={payload.get('generated_at', 'n/a')}")

    lines.append("")
    lines.append("NWS alerts — county zone:")
    if not alert_claims:
        lines.append("  (no active flood/hydrology alerts for this county)")
    else:
        for claim in alert_claims[:5]:
            lines.append(
                f"  • {claim.get('severity', 'n/a')}: {_excerpt(claim.get('excerpt'), 100)} "
                f"(conf {claim.get('overall_confidence', 'n/a')})"
            )
    lines.append("")
    return lines


def _phase_section(phase: str, phase_decision: Dict[str, Any]) -> List[str]:
    lines = ["", "-" * 56, f"Phase: {_phase_label(phase)}", "-" * 56, ""]
    decision = phase_decision.get("phase_decision", "stay")
    lines.append(f"Decision: {decision}")
    if phase_decision.get("matched_rule"):
        rule = phase_decision["matched_rule"]
        lines.append(f"Rule: {rule.get('from')} → {rule.get('to')} ({rule.get('condition', '')})")
    if phase_decision.get("phase_changed"):
        lines.append(
            f"Phase updated: {_phase_label(phase_decision.get('current_phase'))} → "
            f"{_phase_label(phase_decision.get('new_phase'))}"
        )
    lines.append("")
    return lines


def format_coordinator_report(data: Dict[str, Any]) -> str:
    if data.get("error"):
        return format_error_report(data)

    prepare_ontology_display(data)

    ms = data.get("mission_spec") or {}
    belief = data.get("belief_state") or {}
    summary = data.get("monitoring_summary") or {}
    persist = data.get("neo4j_persistence") or {}
    claim_persist = data.get("neo4j_claim_persistence") or {}
    stored = data.get("stored_readings_summary") or {}
    delta = data.get("report_delta") or {}
    phase_decision = data.get("phase_decision") or {}
    claims = data.get("evidence_claims") or []

    county = ms.get("county") or "unknown"
    state = ms.get("state") or ""
    fips = ms.get("fips") or data.get("fips") or "unknown"
    location = ms.get("location") or f"{county} County, {state}".strip(", ")

    phase = ms.get("current_event_phase") or belief.get("event_phase") or summary.get("phase")
    alert = belief.get("alert_level") or phase_decision.get("alert_level", "none")
    hazard = summary.get("hazard") or ms.get("disaster_type") or "flood"

    attempted = data.get("sources_attempted", 0)
    succeeded = data.get("sources_succeeded", 0)
    failed = len(data.get("failed_fetches") or [])

    lines: List[str] = [
        "=" * 56,
        "  Disaster Sentinel — Flood Monitoring Report",
        "=" * 56,
        "",
    ]
    if data.get("auto_bootstrapped"):
        boot = data.get("bootstrap_result") or {}
        verify = boot.get("neo4j_verification") or {}
        lines.extend(
            [
                "Mission bootstrap: NEW (auto-created in Neo4j this run)",
                f"  Mission ID:   {boot.get('mission_id') or data.get('mission_id', 'unknown')}",
                f"  Neo4j verify: {'ok' if verify.get('ok') else 'FAILED'}",
                "",
            ]
        )
    hb = summary.get("heartbeat_interval_seconds")
    hb_label = f"{int(hb) // 3600}h" if hb else "n/a"

    lines.extend(
        [
        f"Mission:      {data.get('mission_id', 'unknown')}",
        f"Location:     {location}",
        f"FIPS:         {fips}",
        f"Hazard:       {hazard}",
        "",
        f"Current phase:     {_phase_label(phase)}",
        f"Alert level:       {alert}",
        f"Phase interval:    {hb_label} (on-demand only; cron disabled)",
        f"Discovery:         {data.get('discovery_mode', 'unknown')} "
        f"(source={data.get('county_flood_source_mode', 'both')})",
        "",
        f"Sources:      {attempted} attempted · {succeeded} succeeded · {failed} failed",
        f"Claims:       {data.get('claims_accepted', len(claims))} accepted · "
        f"{len(data.get('extraction_failures') or [])} extraction failures",
        ]
    )

    api_ev = data.get("county_flood_evidence") or {}
    if api_ev.get("overall_confidence") is not None:
        lines.append(
            f"County display score: {api_ev.get('overall_confidence')} "
            f"({api_ev.get('weight_hint', 'medium')} label · "
            f"verdict={_regional_verdict_label(data)} · "
            f"{api_ev.get('verdict_gauge_count', 0)} scored gauges)"
        )

    lines.extend(_api_endpoints_section(data))
    lines.extend(_flood_readings_section(claims, data.get("county_flood_payload")))
    lines.extend(_confidence_scoring_section(data))

    api_res = delta.get("api_resolution") or data.get("county_api_mapping") or {}
    if api_res:
        lines.extend(["", "-" * 56, "County API resolution", "-" * 56, ""])
        if api_res.get("county_fips"):
            lines.append(f"FIPS:       {api_res['county_fips']}")
        if api_res.get("usgs_sites"):
            sites = api_res["usgs_sites"]
            lines.append(f"USGS sites: {', '.join(sites[:8])}" + (" ..." if len(sites) > 8 else ""))
        if api_res.get("nws_zone"):
            lines.append(f"NWS zone:   {api_res['nws_zone']}")

    gauge_changes = delta.get("gauge_changes") or []
    if gauge_changes:
        lines.extend(["", "-" * 56, "Hydrology changes since last run", "-" * 56, ""])
        for ch in gauge_changes[:8]:
            site = ch.get("site_no", "?")
            if ch.get("change_type") == "value_changed":
                lines.append(
                    f"• USGS {site}: stage {ch.get('gage_height_from', 'n/a')} → "
                    f"{ch.get('gage_height_to', 'n/a')} ft"
                )
            elif ch.get("change_type") == "new_gauge":
                lines.append(f"• USGS {site}: new gauge reading")

    lines.extend(_delta_section(delta))
    lines.extend(_phase_section(phase, phase_decision))
    lines.extend(monitoring_summary_context(data))
    lines.extend(ontology_belief_section(data))
    lines.extend(_article_sources_section(data))
    lines.extend(_article_evidence_section(data))
    lines.extend(_claims_section(claims))

    snapshots = data.get("source_snapshots") or []
    records_by_id = {
        r.get("source_id"): r for r in (data.get("source_records") or []) if r.get("source_id")
    }

    if snapshots:
        lines.extend(["-" * 56, "Source readings (this cycle)", "-" * 56, ""])
        for idx, snap in enumerate(snapshots, start=1):
            sid = snap.get("source_id", "unknown")
            rec = records_by_id.get(sid, {})
            name = rec.get("source_name") or snap.get("source_name") or sid
            url = rec.get("source_url") or snap.get("snapshot_url") or ""
            fetched_local = snap.get("fetched_at_local") or snap.get("fetched_at")
            fetched_utc = snap.get("fetched_at") or snap.get("fetched_at_utc") or ""
            change = snap.get("change_status")

            api_res = rec.get("api_resolution") or {}
            lines.extend(
                [
                    f"{idx}. {name}",
                    f"   Source ID:  {sid}",
                    f"   URL:        {url}",
                    f"   Status:     {_change_label(change)}",
                    f"   Fetched:    {fetched_local or fetched_utc}",
                ]
            )
            if api_res:
                resolved = api_res.get("resolved_ids") or {}
                if resolved.get("sites"):
                    lines.append(f"   USGS sites: {', '.join(resolved['sites'][:5])}")
                if resolved.get("zone"):
                    lines.append(f"   NWS zone:   {resolved['zone']}")
                lines.append(
                    f"   County match: {'yes' if api_res.get('county_match') else 'no'}"
                )
            if fetched_local and fetched_utc and fetched_local != fetched_utc:
                lines.append(f"   Fetched UTC:{fetched_utc}")
            if snap.get("byte_length") is not None:
                lines.append(f"   Size:       {_format_bytes(snap.get('byte_length'))}")
            lines.append(f"   Preview:    {_excerpt(snap.get('raw_excerpt'))}")
            lines.append("")

    lines.extend(
        [
            "-" * 56,
            "Neo4j persistence",
            "-" * 56,
            f"Snapshots written this cycle: {persist.get('snapshots_created', 0)}",
            f"Snapshots skipped (unchanged/Δ<0.1): {persist.get('snapshots_skipped', 0)}",
            f"Claims persisted:             {claim_persist.get('claims_persisted', 0)}",
            f"Phase updated:                {phase_decision.get('phase_changed', False)}",
            f"Readings in latest query:     {stored.get('count', 0)}",
            f"County timezone:              {persist.get('timezone', 'UTC')}",
            "",
            "Run again to compare fingerprints and detect content changes.",
        ]
    )

    return "\n".join(lines)


def _confidence_label(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


_CONFIDENCE_COMPONENT_LABELS = {
    "source": "src",
    "location": "loc",
    "method": "method",
    "quality": "quality",
    "time": "time",
    "impact": "impact",
}


def _confidence_components_text(components: Dict[str, Any]) -> str:
    """Render only the components actually present on a claim.

    Gauge credibility uses source/location/method/quality, while legacy NWS and
    article claims still carry source/location/time/impact. Printing a fixed
    four-column template made absent values look like scored inputs and hid the
    gauge method/quality terms.
    """
    ordered = ("source", "location", "method", "quality", "time", "impact")
    parts = [
        f"{_CONFIDENCE_COMPONENT_LABELS[key]}={_confidence_label(components[key])}"
        for key in ordered
        if key in components
    ]
    known = set(ordered)
    parts.extend(
        f"{key}={_confidence_label(components[key])}"
        for key in sorted(components)
        if key not in known
    )
    return " ".join(parts) if parts else "components=n/a"


def _source_confidence_lines(data: Dict[str, Any]) -> List[str]:
    """Per-source confidence breakdown for Slack and human reports."""
    records_by_id = {
        r.get("source_id"): r for r in (data.get("source_records") or []) if r.get("source_id")
    }
    assessments = data.get("credibility_assessments") or []
    source_assessments = {
        a.get("target_id"): a for a in assessments if a.get("target_type") == "source"
    }
    claims_by_source: Dict[str, List[Dict[str, Any]]] = {}
    for claim in data.get("evidence_claims") or []:
        sid = claim.get("source_id") or "unknown"
        claims_by_source.setdefault(sid, []).append(claim)

    lines: List[str] = []
    snapshots = data.get("source_snapshots") or []
    if not snapshots:
        lines.append("No source readings this cycle.")
        return lines

    for snap in snapshots:
        sid = snap.get("source_id", "unknown")
        rec = records_by_id.get(sid, {})
        name = rec.get("source_name") or sid
        assess = source_assessments.get(sid)
        change = snap.get("change_status", "unknown")
        lines.append(f"• {name} (`{sid}`) — {change}")
        if assess:
            lines.append(
                f"  Legacy source assessment: src={_confidence_label(assess.get('source_confidence'))} "
                f"loc={_confidence_label(assess.get('location_confidence'))} "
                f"time={_confidence_label(assess.get('time_confidence'))} "
                f"impact={_confidence_label(assess.get('impact_confidence'))}"
            )
        claim_list = claims_by_source.get(sid, [])
        if claim_list:
            for claim in claim_list[:3]:
                comp = claim.get("confidence_components") or {}
                lines.append(
                    f"  Claim `{claim.get('claim_type')}` overall={_confidence_label(claim.get('overall_confidence'))} "
                    f"({_confidence_components_text(comp)})"
                )
        elif not assess:
            lines.append("  Confidence: n/a (no claims extracted)")
    return lines


def format_coordinator_report_with_slack_block(data: Dict[str, Any]) -> str:
    """Human report plus ---SLACK--- block for coordinator agent parsing."""
    human = format_coordinator_report(data)
    slack_msg = data.get("slack_message") or format_slack_report(data)
    return f"{human}\n\n---SLACK---\n{slack_msg}"


def format_slack_report(data: Dict[str, Any]) -> str:
    """Compact Slack markdown for coordinator delivery."""
    if data.get("error"):
        return format_error_report(data)

    prepare_ontology_display(data)

    ms = data.get("mission_spec") or {}
    delta = data.get("report_delta") or {}
    phase_decision = data.get("phase_decision") or {}
    claims = data.get("evidence_claims") or []

    county = data.get("county_under_monitor") or ms.get("county") or "unknown"
    state = data.get("state_under_monitor") or ms.get("state") or ""
    location = ms.get("location") or f"{county} County, {state}".strip(", ")
    phase = ms.get("current_event_phase", "unknown")
    alert = (data.get("belief_state") or {}).get("alert_level", phase_decision.get("alert_level", "none"))
    run_at = data.get("run_at", "now")
    run_kind = "Scheduled heartbeat" if data.get("heartbeat_run") else "On-demand coordinator"

    lines = [
        "*Disaster Sentinel — Flood Monitoring Update*",
        f"*Run at:* {run_at}",
        f"*Run type:* {run_kind}",
        f"*County:* {county} County, {state} (FIPS {ms.get('fips', 'n/a')})",
        f"*Phase:* {_phase_label(phase)} | *Alert:* {alert}",
        f"*Mission:* `{data.get('mission_id', 'unknown')}`",
        "",
        "*Phase evaluation*",
    ]
    if phase_decision.get("phase_changed"):
        lines.append(
            f":warning: *Phase auto-changed:* "
            f"{_phase_label(phase_decision.get('current_phase'))} → "
            f"{_phase_label(phase_decision.get('new_phase'))}"
        )
        if phase_decision.get("matched_rule"):
            rule = phase_decision["matched_rule"]
            lines.append(
                f"Rule matched: `{rule.get('claim_type')}` → `{rule.get('to')}` "
                f"({rule.get('condition', 'n/a')})"
            )
    else:
        lines.append(f"• Phase unchanged — staying in *{_phase_label(phase)}*")

    api_ev = data.get("county_flood_evidence") or {}
    if api_ev.get("overall_confidence") is not None:
        lines.append("")
        lines.append("*Official API display summary*")
        lines.append(
            f"• County flood API: *{api_ev.get('overall_confidence')}* "
            f"({api_ev.get('weight_hint', 'medium')} display label; non-operational)"
        )
        lines.append(
            f"• Verdict: `{_regional_verdict_label(data)}` · "
            f"flooding={_flooding_status_label(data)} · "
            f"{api_ev.get('verdict_gauge_count', 0)} scored gauges"
        )

    lines.append("")
    lines.append("*API endpoints called*")
    for rec in data.get("source_records") or []:
        name = rec.get("source_name") or rec.get("ontology_source_type", "source")
        url = rec.get("source_url") or ""
        lines.append(f"• *{name}*: `{url[:120]}{'...' if len(url) > 120 else ''}`")

    gauge_claims = _iter_displayable_gauge_claims(claims, limit=10)
    lines.append("")
    lines.append("*Flood readings*")
    if gauge_claims:
        for claim in gauge_claims:
            val = claim.get("value") or {}
            if isinstance(val, dict):
                lines.append(f"• {_format_gauge_reading_bullet(val)}")
    else:
        lines.append(f"• {_gauge_readings_empty_message(claims)}")
    nws_alerts = [c for c in claims if c.get("claim_type") in ("warning", "forecast_shift")]
    if nws_alerts:
        for claim in nws_alerts[:3]:
            lines.append(f"• NWS: {_excerpt(claim.get('excerpt'), 60)}")
    else:
        lines.append("• No NWS flood alerts for county zone")

    api_res = delta.get("api_resolution") or data.get("county_api_mapping") or {}
    if api_res.get("usgs_sites") or api_res.get("nws_zone"):
        lines.append("")
        lines.append("*Resolved county IDs*")
        if api_res.get("usgs_sites"):
            lines.append(f"• USGS: {len(api_res['usgs_sites'])} sites")
        if api_res.get("nws_zone"):
            lines.append(f"• NWS zone: `{api_res['nws_zone']}`")

    gauge_changes = delta.get("gauge_changes") or []
    if gauge_changes:
        lines.append("")
        lines.append("*Hydrology changes*")
        for ch in gauge_changes[:10]:
            site = ch.get("site_no", "?")
            if ch.get("change_type") == "value_changed":
                lines.append(
                    f"• USGS `{site}`: {ch.get('gage_height_from', 'n/a')} → "
                    f"{ch.get('gage_height_to', 'n/a')} ft"
                )

    lines.append("")
    lines.append("*Changes since last report*")
    for bullet in delta.get("summary_bullets") or ["First report for this mission"]:
        lines.append(f"• {bullet}")

    lines.append("\n*Source confidence scores*")
    lines.extend(_source_confidence_lines(data))

    lines.append("\n*Top flood claims*")
    if claims:
        for claim in claims[:5]:
            lines.append(
                f"• `{claim.get('claim_type')}` severity={claim.get('severity')} "
                f"overall={_confidence_label(claim.get('overall_confidence'))} "
                f"({_confidence_components_text(claim.get('confidence_components') or {})}) "
                f"— {_excerpt(claim.get('excerpt'), 80)}"
            )
    else:
        lines.append("• No county flood claims extracted this cycle")

    ctx_lines = monitoring_summary_context(data, compact=True)
    if ctx_lines:
        lines.append("")
        lines.extend(ctx_lines)
    belief_lines = ontology_belief_section(data, compact=True)
    if belief_lines:
        lines.append("")
        lines.extend(belief_lines)

    lines.extend(_article_sources_section(data, compact=True))
    lines.extend(_article_evidence_section(data, compact=True))

    lines.append(f"\n_Sentinel · {county} County · phase {_phase_label(phase)} · {run_at}_")
    return "\n".join(lines)



def _strip_leading_title(lines: List[str]) -> List[str]:
    """Remove blank lines and a leading *Title* / underline so Section owns the header."""
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    if out and (out[0].startswith("*") or set(out[0].strip()) <= {"-"}):
        out.pop(0)
        while out and (not out[0].strip() or set(out[0].strip()) <= {"-"}):
            out.pop(0)
    return out


def build_coordinator_output_report(data: Dict[str, Any]):
    """Assemble a typed OutputReport for the coordinator Slack/human renderers (v1)."""
    from watcher.format_ontology import belief_state_section, ontology_context_section, prepare_ontology_display
    from watcher.ontology.output_schema import (
        DEFAULT_SECTION_PRIORITY,
        OutputReport,
        Section,
        SectionType,
    )

    if data.get("error"):
        return OutputReport(
            mission_id=str(data.get("mission_id") or "unknown"),
            report_kind="coordinator",
            generated_at_utc=str(data.get("run_at") or ""),
            sections=[
                Section(
                    section_type=SectionType.MISSION_HEADER,
                    title="Coordinator Error",
                    lines=format_error_report(data).splitlines(),
                    priority=0,
                    atomic=True,
                    splittable=False,
                )
            ],
        )

    prepare_ontology_display(data)

    ms = data.get("mission_spec") or {}
    delta = data.get("report_delta") or {}
    phase_decision = data.get("phase_decision") or {}
    claims = data.get("evidence_claims") or []

    county = data.get("county_under_monitor") or ms.get("county") or "unknown"
    state = data.get("state_under_monitor") or ms.get("state") or ""
    phase = ms.get("current_event_phase", "unknown")
    alert = (data.get("belief_state") or {}).get("alert_level", phase_decision.get("alert_level", "none"))
    run_at = data.get("run_at", "now")
    run_kind = "Scheduled heartbeat" if data.get("heartbeat_run") else "On-demand coordinator"
    mission_id = str(data.get("mission_id") or "unknown")

    report = OutputReport(
        mission_id=mission_id,
        report_kind="coordinator",
        generated_at_utc=str(run_at),
    )

    report.append(
        Section(
            section_type=SectionType.MISSION_HEADER,
            title="Disaster Sentinel — Flood Monitoring Update",
            lines=[
                f"*Run at:* {run_at}",
                f"*Run type:* {run_kind}",
                f"*County:* {county} County, {state} (FIPS {ms.get('fips', 'n/a')})",
                f"*Phase:* {_phase_label(phase)} | *Alert:* {alert}",
                f"*Mission:* `{mission_id}`",
            ],
            priority=DEFAULT_SECTION_PRIORITY[SectionType.MISSION_HEADER],
            atomic=True,
            splittable=False,
        )
    )

    phase_lines: List[str] = []
    if phase_decision.get("phase_changed"):
        phase_lines.append(
            f":warning: *Phase auto-changed:* "
            f"{_phase_label(phase_decision.get('current_phase'))} → "
            f"{_phase_label(phase_decision.get('new_phase'))}"
        )
        if phase_decision.get("matched_rule"):
            rule = phase_decision["matched_rule"]
            phase_lines.append(
                f"Rule matched: `{rule.get('claim_type')}` → `{rule.get('to')}` "
                f"({rule.get('condition', 'n/a')})"
            )
    else:
        phase_lines.append(f"• Phase unchanged — staying in *{_phase_label(phase)}*")
    report.append(
        Section(
            section_type=SectionType.PHASE_STATUS,
            title="Phase evaluation",
            lines=phase_lines,
            priority=DEFAULT_SECTION_PRIORITY[SectionType.PHASE_STATUS],
            atomic=True,
            splittable=False,
        )
    )

    official: List[str] = []
    api_ev = data.get("county_flood_evidence") or {}
    if api_ev.get("overall_confidence") is not None:
        official.append(
            f"• County flood API: *{api_ev.get('overall_confidence')}* "
            f"({api_ev.get('weight_hint', 'medium')} display label; non-operational)"
        )
        official.append(
            f"• Verdict: `{_regional_verdict_label(data)}` · "
            f"flooding={_flooding_status_label(data)} · "
            f"{api_ev.get('verdict_gauge_count', 0)} scored gauges"
        )
    official.append("*API endpoints called*")
    for rec in data.get("source_records") or []:
        name = rec.get("source_name") or rec.get("ontology_source_type", "source")
        url = rec.get("source_url") or ""
        official.append(f"• *{name}*: `{url[:120]}{'...' if len(url) > 120 else ''}`")
    gauge_claims = _iter_displayable_gauge_claims(claims, limit=10)
    official.append("*Flood readings*")
    if gauge_claims:
        for claim in gauge_claims:
            val = claim.get("value") or {}
            if isinstance(val, dict):
                official.append(f"• {_format_gauge_reading_bullet(val)}")
    else:
        official.append(f"• {_gauge_readings_empty_message(claims)}")
    nws_alerts = [c for c in claims if c.get("claim_type") in ("warning", "forecast_shift")]
    if nws_alerts:
        for claim in nws_alerts[:3]:
            official.append(f"• NWS: {_excerpt(claim.get('excerpt'), 60)}")
    else:
        official.append("• No NWS flood alerts for county zone")
    api_res = delta.get("api_resolution") or data.get("county_api_mapping") or {}
    if api_res.get("usgs_sites") or api_res.get("nws_zone"):
        official.append("*Resolved county IDs*")
        if api_res.get("usgs_sites"):
            official.append(f"• USGS: {len(api_res['usgs_sites'])} sites")
        if api_res.get("nws_zone"):
            official.append(f"• NWS zone: `{api_res['nws_zone']}`")
    gauge_changes = delta.get("gauge_changes") or []
    if gauge_changes:
        official.append("*Hydrology changes*")
        for ch in gauge_changes[:10]:
            site = ch.get("site_no", "?")
            if ch.get("change_type") == "value_changed":
                official.append(
                    f"• USGS `{site}`: {ch.get('gage_height_from', 'n/a')} → "
                    f"{ch.get('gage_height_to', 'n/a')} ft"
                )
    official.append("*Source confidence scores*")
    official.extend(_source_confidence_lines(data))
    official.append("*Top flood claims*")
    if claims:
        for claim in claims[:5]:
            official.append(
                f"• `{claim.get('claim_type')}` severity={claim.get('severity')} "
                f"overall={_confidence_label(claim.get('overall_confidence'))} "
                f"({_confidence_components_text(claim.get('confidence_components') or {})}) "
                f"— {_excerpt(claim.get('excerpt'), 80)}"
            )
    else:
        official.append("• No county flood claims extracted this cycle")
    report.append(
        Section(
            section_type=SectionType.OFFICIAL_EVIDENCE,
            title="Official Hydrology",
            lines=official,
            priority=DEFAULT_SECTION_PRIORITY[SectionType.OFFICIAL_EVIDENCE],
        )
    )

    news_lines: List[str] = []
    news_lines.extend(_strip_leading_title(_article_sources_section(data, compact=True)))
    news_lines.extend(_strip_leading_title(_article_evidence_section(data, compact=True)))
    if news_lines:
        report.append(
            Section(
                section_type=SectionType.NEWS_EVIDENCE,
                title="News article evidence",
                lines=news_lines,
                priority=DEFAULT_SECTION_PRIORITY[SectionType.NEWS_EVIDENCE],
            )
        )

    belief = belief_state_section(data, compact=True)
    if belief:
        report.append(belief)
    ontology_ctx = ontology_context_section(data, compact=True)
    if ontology_ctx:
        report.append(ontology_ctx)

    delta_lines = [f"• {b}" for b in (delta.get("summary_bullets") or ["First report for this mission"])]
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
            lines=[f"_Sentinel · {county} County · phase {_phase_label(phase)} · {run_at}_"],
            priority=DEFAULT_SECTION_PRIORITY[SectionType.DELIVERY_FOOTER],
            atomic=True,
            splittable=False,
        )
    )
    return report


def format_slack_report_v1(data: Dict[str, Any]) -> tuple:
    """Return (primary_slack_message, slack_messages_list) via OutputReport pipeline."""
    from watcher.format_slack import build_slack_messages

    report = build_coordinator_output_report(data)
    messages = build_slack_messages(report)
    primary = "\n\n".join(messages) if messages else ""
    return primary, messages
