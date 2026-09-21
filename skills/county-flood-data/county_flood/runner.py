"""Run county flood aggregation and format output."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from county_flood.aggregator import aggregate_county_flood
from county_flood.geo import normalize_county_name, normalize_state, resolve_fips
from county_flood.sources.stages import score_label


def _format_thresholds(gauge: Dict[str, Any], unit: str) -> Optional[str]:
    labels = (
        ("action_stage", "action"),
        ("flood_stage", "minor"),
        ("moderate_stage", "moderate"),
        ("major_stage", "major"),
    )
    parts = []
    for key, label in labels:
        value = gauge.get(key)
        if value is not None:
            parts.append(f"{label}={value} {unit}")
    if not parts:
        return None
    return "NWS thresholds: " + ", ".join(parts)


def _classification_proof(gauge: Dict[str, Any], unit: str) -> Optional[str]:
    obs = gauge.get("observed_primary")
    if obs is None:
        return None

    source = gauge.get("source") or ""
    category = gauge.get("observed_flood_category")

    if source == "nwps":
        method = gauge.get("classification_method")
        if method == "nwps_api_category" and category:
            return f"Classification: NOAA NWPS floodCategory={category} (from live API)"
        if method == "stage_derived" and category:
            return f"Classification: stage_derived → {category} (NWPS category excluded/unavailable)"
        if category:
            return f"Classification: {category} ({method or 'nwps'})"
        reason = gauge.get("unscored_reason")
        if reason:
            return f"Classification: unscored ({reason})"
        return "Classification: NOAA NWPS returned no scorable category"

    flood_stage = gauge.get("flood_stage")
    if flood_stage is None:
        return "Classification: no NWS flood stages on file for this site (category=n/a)"

    action = gauge.get("action_stage")
    moderate = gauge.get("moderate_stage")
    major = gauge.get("major_stage")
    rules = []
    if action is not None:
        rules.append(f"action >= {action}")
    rules.append(f"minor_flooding >= {flood_stage}")
    if moderate is not None:
        rules.append(f"moderate >= {moderate}")
    if major is not None:
        rules.append(f"major >= {major}")
    rule_text = "; ".join(rules)
    cat = category or "n/a"
    return (
        f"Classification: obs={obs} {unit} vs [{rule_text}] {unit} → {cat}"
    )


def _format_trend_line(gauge: Dict[str, Any], unit: str) -> Optional[str]:
    signal = gauge.get("hydrologic_signal")
    if not signal:
        return None
    gauge_id = gauge.get("shef_id") or gauge.get("site_no") or "n/a"
    source = gauge.get("source") or "n/a"
    obs = gauge.get("observed_primary")
    prev = gauge.get("prior_observed_primary")
    if signal == "cold_start":
        return (
            f"  [{source}] {gauge_id}  {gauge.get('name')}  "
            f"signal=cold_start (first reading stored)"
        )
    if signal == "stable":
        return None
    parts = [f"  [{source}] {gauge_id}  {gauge.get('name')}"]
    if prev is not None and obs is not None:
        delta = gauge.get("delta_primary_ft")
        delta_s = f"{delta:+.1f}" if delta is not None else "n/a"
        parts.append(f"stage {prev} → {obs} {unit} ({delta_s})")
    elif obs is not None:
        parts.append(f"stage {obs} {unit}")
    flow_pct = gauge.get("delta_streamflow_pct")
    if flow_pct is not None:
        parts.append(f"flow {flow_pct:+.0f}%")
    parts.append(f"signal={signal}")
    conf = gauge.get("supplementary_confidence")
    if conf is not None:
        parts.append(f"conf={conf:.2f}")
    return "  ".join(parts)


def format_human_report(result: Dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    source_meta = result.get("sources") or {}
    source_names = ", ".join(source_meta.keys()) or "none"
    verdict_count = summary.get("verdict_gauge_count", summary.get("gauges_scored", 0))
    excluded = summary.get("gauges_excluded", summary.get("gauges_unscored", 0))
    lines = [
        f"County flood data — {result.get('county')}, {result.get('state')} ({result.get('fips') or 'n/a'})",
        f"Sources: {source_names} ({summary.get('gauges_queried', 0)} gauges queried, "
        f"{summary.get('gauges_with_data', 0)} with data, {verdict_count} scored)",
        "",
    ]

    api_resolution = result.get("api_resolution") or {}
    if api_resolution:
        lines.append("API resolution:")
        if api_resolution.get("nws_zone"):
            lines.append(f"  NWS zone: {api_resolution['nws_zone']}")
        if api_resolution.get("nws_zone_url"):
            lines.append(f"  NWS zone URL: {api_resolution['nws_zone_url']}")
        if api_resolution.get("nws_alerts_url"):
            lines.append(f"  NWS alerts URL: {api_resolution['nws_alerts_url']}")
        if api_resolution.get("fema_disasters_url"):
            lines.append(f"  FEMA disasters URL: {api_resolution['fema_disasters_url']}")
        lines.append("")

    regional = summary.get("regional_verdict") or "no_flooding"
    is_flooding = (
        "insufficient_data"
        if regional == "insufficient_data"
        else summary.get("is_flooding", False)
    )
    confidence = summary.get("confidence") or "low"
    eval_mode = summary.get("evaluation_mode") or "scored_gauges"
    max_state = summary.get("max_flood_state")

    lines.append(f"Regional verdict: {regional} (is_flooding={is_flooding}, confidence={confidence})")
    if regional != "insufficient_data" and max_state is not None:
        lines.append(f"Max flood state: {max_state} ({score_label(max_state)})")

    flooding_ids = summary.get("flooding_gauge_ids") or []
    if flooding_ids:
        lines.append(f"Flooding gauge(s): {', '.join(str(i) for i in flooding_ids)}")

    alerts = result.get("alerts") or []
    disasters = result.get("disasters") or []
    nws_meta = source_meta.get("nws_alerts") or {}
    fema_meta = source_meta.get("fema_disasters") or {}

    lines.extend(["", f"Active NWS Flood Warnings ({len(alerts)}):"])
    if nws_meta.get("nws_zone_url"):
        lines.append(f"  NWS zone URL: {nws_meta['nws_zone_url']}")
    elif api_resolution.get("nws_zone_url"):
        lines.append(f"  NWS zone URL: {api_resolution['nws_zone_url']}")
    if nws_meta.get("nws_alerts_url") or nws_meta.get("api_url"):
        lines.append(
            f"  NWS alerts URL: {nws_meta.get('nws_alerts_url') or nws_meta.get('api_url')}"
        )
    if nws_meta.get("nws_zone"):
        lines.append(f"  NWS zone: {nws_meta['nws_zone']}")
    if not alerts:
        lines.append("  (none)")
    else:
        for alert in alerts:
            headline = alert.get("headline") or alert.get("event") or "Flood Warning"
            area = alert.get("area_desc") or "n/a"
            expires = alert.get("expires") or "n/a"
            lines.append(f"  • {headline}")
            lines.append(f"    area={area}  expires={expires}")
            score = alert.get("flood_state_score")
            if score is not None:
                lines.append(f"    flood_state_score={score}")

    lines.extend(["", f"Recent FEMA flood disaster declarations ({len(disasters)}):"])
    window = fema_meta.get("recent_disaster_window") or api_resolution.get(
        "recent_disaster_window"
    )
    if window:
        lines.append(
            f"  Window (UTC): {window.get('start')} to {window.get('end')} (current/previous day)"
        )
    if fema_meta.get("fips_county_code"):
        lines.append(f"  FIPS county code: {fema_meta['fips_county_code']}")
    if not disasters:
        lines.append("  (none)")
    else:
        for disaster in disasters:
            dr = disaster.get("fema_declaration_string") or disaster.get("disaster_number") or "n/a"
            title = disaster.get("declaration_title") or "n/a"
            declared = disaster.get("declaration_date") or "n/a"
            lines.append(f"  • {dr}: {title}")
            lines.append(f"    declared={declared}")
            score = disaster.get("flood_state_score")
            if score is not None:
                lines.append(f"    flood_state_score={score}")

    supplementary_scores = summary.get("supplementary_scores") or {}
    if supplementary_scores:
        parts = [f"{key}={value}" for key, value in supplementary_scores.items()]
        lines.extend(["", f"Supplementary scores: {', '.join(parts)}"])

    if verdict_count:
        lines.append(
            f"Verdict based on {verdict_count} scored gauge(s); "
            f"{excluded} gauge(s) excluded (no NWS flood thresholds)."
        )
    elif eval_mode == "unscored_gauges":
        lines.append(
            f"No scored gauges available; {excluded} gauge(s) have observations only."
        )

    overall = summary.get("overall_confidence")
    if overall is not None:
        weight = summary.get("api_weight_hint") or "n/a"
        lines.append(
            f"Overall API confidence: {overall} (T0 official, {weight} coordinator weight)"
        )

    lines.append("")

    avg_state = summary.get("average_flood_state")
    avg_reading = summary.get("average_primary_reading")
    unit = summary.get("average_primary_unit") or "ft"

    state_line = f"Average flood state: {avg_state if avg_state is not None else 'n/a'}"
    if avg_state is not None:
        state_line += f" ({score_label(avg_state)})"
    lines.append(state_line)

    if avg_reading is not None:
        lines.append(f"Average primary reading: {avg_reading} {unit} (scored gauges only)")
    else:
        lines.append("Average primary reading: n/a")

    lines.extend(
        [
            "",
            "How averages are computed:",
            "  • Regional verdict = worst scored gauge (any score ≥ 1.0 → flooding).",
            f"  • Average flood state = mean of {verdict_count} scored gauge(s) "
            "(0=no_flooding, 1=action, 2=minor, 3=moderate, 4=major, 5=record).",
            f"  • Primary reading = mean gage height across {verdict_count} scored gauge(s).",
            "  • Unscored gauges are excluded from verdict; trends shown separately below.",
        ]
    )

    supplementary = summary.get("supplementary_signals") or []
    trend_gauges = [
        g for g in (result.get("gauges") or [])
        if g.get("hydrologic_signal") in ("rising", "falling", "cold_start")
    ]
    if supplementary or trend_gauges:
        lines.extend(
            [
                "",
                "Supplementary hydrologic trends (low confidence, not used for verdict):",
            ]
        )
        if supplementary:
            for sig in supplementary:
                gauge_id = sig.get("gauge_id") or "n/a"
                parts = [f"  [{sig.get('source', 'n/a')}] {gauge_id}  {sig.get('name', '')}"]
                delta = sig.get("delta_primary_ft")
                if delta is not None:
                    parts.append(f"Δstage={delta:+.1f} ft")
                flow_pct = sig.get("delta_streamflow_pct")
                if flow_pct is not None:
                    parts.append(f"Δflow={flow_pct:+.0f}%")
                parts.append(f"signal={sig.get('hydrologic_signal')}")
                lines.append("  ".join(p for p in parts if p))
        else:
            for gauge in trend_gauges:
                trend_line = _format_trend_line(gauge, unit)
                if trend_line:
                    lines.append(trend_line)

    scored_gauges: List[Dict[str, Any]] = []
    other_gauges: List[Dict[str, Any]] = []
    for gauge in result.get("gauges") or []:
        if gauge.get("flood_state_score") is not None:
            scored_gauges.append(gauge)
        else:
            other_gauges.append(gauge)

    lines.extend(["", "Verdict gauges (scored):"])
    if not scored_gauges:
        lines.append("  (none)")
    for gauge in scored_gauges:
        obs = gauge.get("observed_primary")
        obs_str = f"{obs} {gauge.get('observed_primary_unit') or unit}" if obs is not None else "n/a"
        cat = gauge.get("observed_flood_category") or "n/a"
        gauge_id = gauge.get("shef_id") or gauge.get("site_no") or "n/a"
        source = gauge.get("source") or "n/a"
        flow = gauge.get("streamflow_cfs")
        flow_str = f" flow={flow} cfs" if flow is not None else ""
        score = gauge.get("flood_state_score")
        method = gauge.get("classification_method") or "n/a"
        lines.append(
            f"  [{source}] {gauge_id}  {gauge.get('name')}  obs={obs_str}{flow_str}  "
            f"score={score}  category={cat}  method={method}"
        )
        gauge_unit = gauge.get("observed_primary_unit") or unit
        thresholds = _format_thresholds(gauge, gauge_unit)
        if thresholds:
            lines.append(f"    {thresholds}")
        proof = _classification_proof(gauge, gauge_unit)
        if proof:
            lines.append(f"    {proof}")

    if other_gauges:
        lines.extend(["", "Other gauges (excluded from verdict):"])
        for gauge in other_gauges:
            obs = gauge.get("observed_primary")
            obs_str = f"{obs} {gauge.get('observed_primary_unit') or unit}" if obs is not None else "n/a"
            gauge_id = gauge.get("shef_id") or gauge.get("site_no") or "n/a"
            source = gauge.get("source") or "n/a"
            signal = gauge.get("hydrologic_signal") or "n/a"
            lines.append(
                f"  [{source}] {gauge_id}  {gauge.get('name')}  obs={obs_str}  signal={signal}"
            )

    for source_id, meta in source_meta.items():
        errors = meta.get("fetch_errors") or []
        if not errors:
            continue
        lines.extend(["", f"Fetch errors ({source_id}):"])
        for err in errors:
            label = err.get("shef_id") or err.get("site_no") or err.get("source") or "unknown"
            lines.append(f"  {label}: {err.get('error')}")

    return "\n".join(lines)


def run_county_flood(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
    location: Optional[str] = None,
    gauge_ids: Optional[Sequence[str]] = None,
    site_ids: Optional[Sequence[str]] = None,
    sources: Optional[Sequence[str]] = None,
    prior_gauges: Optional[Sequence[Dict[str, Any]]] = None,
    output_format: str = "human",
    discover_categories: bool = False,
    emit: bool = True,
) -> Dict[str, Any]:
    county_norm = normalize_county_name(county)
    state_norm = normalize_state(state)
    loc = location or f"{county_norm}, {state_norm}"
    resolved_fips = fips or resolve_fips(county_norm, state_norm)

    result = aggregate_county_flood(
        county=county_norm,
        state=state_norm,
        fips=resolved_fips,
        location=loc,
        gauge_ids=gauge_ids,
        site_ids=site_ids,
        sources=sources,
        discover_categories=discover_categories,
        prior_gauges=prior_gauges,
    )
    payload = result.to_dict()

    if emit:
        if output_format == "json":
            print(json.dumps(payload, indent=2))
        else:
            print(format_human_report(payload))

    return payload
