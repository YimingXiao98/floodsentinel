"""Delta computation between coordinator runs."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Noise floor for Slack/report hydrology bullets. Stage deltas below this are
# ignored even if the raw reading changed (e.g. 139.09 → 139.08).
# Override with REPORT_GAUGE_STAGE_THRESHOLD_FT.
_DEFAULT_STAGE_CHANGE_THRESHOLD_FT = 0.1


def stage_change_threshold_ft() -> float:
    raw = os.environ.get("REPORT_GAUGE_STAGE_THRESHOLD_FT", "").strip()
    if not raw:
        return _DEFAULT_STAGE_CHANGE_THRESHOLD_FT
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_STAGE_CHANGE_THRESHOLD_FT


# Noise floor for streamflow. Discharge spans orders of magnitude between
# gauges, so the floor is relative rather than absolute. Without it any reading
# change at all counted as significant, which also defeated the stage floor
# above, since a tick where flow moved reported a change regardless of stage.
# Override with REPORT_GAUGE_FLOW_THRESHOLD_PCT.
_DEFAULT_FLOW_CHANGE_THRESHOLD_PCT = 20.0


def flow_change_threshold_pct() -> float:
    raw = os.environ.get("REPORT_GAUGE_FLOW_THRESHOLD_PCT", "").strip()
    if not raw:
        return _DEFAULT_FLOW_CHANGE_THRESHOLD_PCT
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_FLOW_CHANGE_THRESHOLD_PCT


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _significant_stage_change(prev: Any, curr: Any) -> bool:
    """True when stage appeared/disappeared or moved by >= display threshold."""
    prev_f = _as_float(prev)
    curr_f = _as_float(curr)
    if prev_f is None and curr_f is None:
        return prev != curr and (prev is not None or curr is not None)
    if prev_f is None or curr_f is None:
        return True
    # Round to mm-scale precision so float noise (e.g. 139.10 - 139.00)
    # does not fall just under a 0.1 ft threshold.
    return round(abs(curr_f - prev_f), 3) >= stage_change_threshold_ft()


def extract_gauge_readings(claims: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build per-site hydrology readings from USGS GaugeObservation claims."""
    readings: Dict[str, Dict[str, Any]] = {}
    for claim in claims:
        if claim.get("claim_type") not in ("gauge_rising", "GaugeObservation"):
            continue
        value = claim.get("value")
        if not isinstance(value, dict):
            continue
        site_no = value.get("site_no")
        if not site_no:
            continue
        readings[str(site_no)] = {
            "site_no": str(site_no),
            "gage_height_ft": value.get("gage_height_ft"),
            "streamflow_cfs": value.get("streamflow_cfs"),
            "observation_time": value.get("observation_time"),
            "severity": claim.get("severity"),
            "overall_confidence": claim.get("overall_confidence"),
        }
    return readings


def extract_api_resolution_summary(
    source_records: List[Dict[str, Any]],
    county_api_mapping: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Summarize county API IDs used for this run."""
    summary: Dict[str, Any] = {
        "usgs_sites": [],
        "nws_zone": None,
        "county_fips": None,
    }
    if county_api_mapping:
        summary["county_fips"] = county_api_mapping.get("county_fips")
        summary["usgs_sites"] = [
            s.get("site_no") for s in (county_api_mapping.get("usgs_sites") or []) if s.get("site_no")
        ]
        zone = county_api_mapping.get("nws_zone") or {}
        summary["nws_zone"] = zone.get("zone")

    for rec in source_records:
        api_res = rec.get("api_resolution") or {}
        resolved = api_res.get("resolved_ids") or {}
        if resolved.get("sites"):
            summary["usgs_sites"] = resolved["sites"]
        if resolved.get("zone"):
            summary["nws_zone"] = resolved["zone"]
        if api_res.get("county_fips"):
            summary["county_fips"] = api_res["county_fips"]
    return summary


def _compare_gauge_readings(
    prior: Dict[str, Dict[str, Any]],
    current: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return list of gauge value changes between runs."""
    changes: List[Dict[str, Any]] = []
    all_sites = set(prior) | set(current)
    for site_no in sorted(all_sites):
        prev = prior.get(site_no) or {}
        curr = current.get(site_no) or {}
        prev_stage = prev.get("gage_height_ft")
        curr_stage = curr.get("gage_height_ft")
        prev_flow = prev.get("streamflow_cfs")
        curr_flow = curr.get("streamflow_cfs")

        stage_changed = _significant_stage_change(prev_stage, curr_stage)
        flow_changed = _significant_flow_change(prev_flow, curr_flow)
        if site_no not in prior:
            changes.append(
                {
                    "site_no": site_no,
                    "change_type": "new_gauge",
                    "gage_height_ft": curr_stage,
                    "streamflow_cfs": curr_flow,
                }
            )
        elif site_no not in current:
            changes.append({"site_no": site_no, "change_type": "removed_gauge"})
        elif stage_changed or flow_changed:
            entry: Dict[str, Any] = {
                "site_no": site_no,
                "change_type": "value_changed",
            }
            # Only surface stage in the bullet when the delta clears the noise floor.
            if stage_changed:
                entry["gage_height_from"] = prev_stage
                entry["gage_height_to"] = curr_stage
            if flow_changed:
                entry["streamflow_from"] = prev_flow
                entry["streamflow_to"] = curr_flow
            changes.append(entry)
    return changes


def _gauge_change_bullets(changes: List[Dict[str, Any]]) -> List[str]:
    bullets: List[str] = []
    for ch in changes[:8]:
        site = ch.get("site_no", "?")
        if ch.get("change_type") == "new_gauge":
            parts = [f"USGS {site}: new reading"]
            if ch.get("gage_height_ft") is not None:
                parts.append(f"stage {ch['gage_height_ft']:.2f} ft")
            if ch.get("streamflow_cfs") is not None:
                parts.append(f"flow {ch['streamflow_cfs']:.0f} cfs")
            bullets.append(" ".join(parts))
        elif ch.get("change_type") == "value_changed":
            parts = [f"USGS {site}:"]
            if ch.get("gage_height_from") is not None or ch.get("gage_height_to") is not None:
                parts.append(
                    f"stage {ch.get('gage_height_from', 'n/a')} → {ch.get('gage_height_to', 'n/a')} ft"
                )
            if ch.get("streamflow_from") is not None or ch.get("streamflow_to") is not None:
                parts.append(
                    f"flow {ch.get('streamflow_from', 'n/a')} → {ch.get('streamflow_to', 'n/a')} cfs"
                )
            bullets.append(" ".join(parts))
    return bullets


def _significant_flow_change(prev: Any, curr: Any) -> bool:
    """True when streamflow appeared/disappeared or moved by >= the threshold."""
    prev_f = _as_float(prev)
    curr_f = _as_float(curr)
    if prev_f is None and curr_f is None:
        return prev != curr and (prev is not None or curr is not None)
    if prev_f is None or curr_f is None:
        return True
    if prev_f == 0:
        return curr_f != 0
    change_pct = abs(curr_f - prev_f) / abs(prev_f) * 100.0
    return change_pct >= flow_change_threshold_pct()


def build_report_snapshot(
    *,
    mission_id: str,
    phase: str,
    alert_level: str,
    evidence_claims: List[Dict[str, Any]],
    source_snapshots: List[Dict[str, Any]],
    phase_decision: Dict[str, Any],
    source_records: Optional[List[Dict[str, Any]]] = None,
    county_api_mapping: Optional[Dict[str, Any]] = None,
    county_flood_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    claim_summaries = [
        {
            "claim_id": c.get("claim_id"),
            "claim_type": c.get("claim_type"),
            "severity": c.get("severity"),
            "overall_confidence": c.get("overall_confidence"),
        }
        for c in evidence_claims
    ]
    source_status = {
        s.get("source_id", "unknown"): s.get("change_status", "unknown")
        for s in source_snapshots
    }
    flood_signals = [c for c in evidence_claims if c.get("claim_type") in ("warning", "gauge_rising")]
    gauge_readings = extract_gauge_readings(evidence_claims)
    api_resolution = extract_api_resolution_summary(source_records or [], county_api_mapping)
    return {
        "mission_id": mission_id,
        "run_at": _utc_now(),
        "phase": phase,
        "alert_level": alert_level,
        "claim_ids": [c.get("claim_id") for c in evidence_claims if c.get("claim_id")],
        "claims": claim_summaries,
        "source_change_status": source_status,
        "flood_signal_count": len(flood_signals),
        "gauge_readings": gauge_readings,
        "api_resolution": api_resolution,
        "county_flood_evidence": county_flood_evidence or {},
        "phase_decision": phase_decision.get("phase_decision"),
        "phase_changed": phase_decision.get("phase_changed", False),
    }


def compute_delta(
    current: Dict[str, Any],
    prior: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare current report snapshot to prior coordinator report."""
    if not prior:
        gauge_readings = current.get("gauge_readings") or {}
        bullets = ["First coordinator report for this mission."]
        if gauge_readings:
            bullets.append(f"USGS gauges monitored: {len(gauge_readings)}")
        api_res = current.get("api_resolution") or {}
        if api_res.get("nws_zone"):
            bullets.append(f"NWS zone: {api_res['nws_zone']}")
        return {
            "first_run": True,
            "phase_changed": False,
            "phase_from": None,
            "phase_to": current.get("phase"),
            "alert_from": None,
            "alert_to": current.get("alert_level"),
            "new_claims": len(current.get("claim_ids", [])),
            "removed_claims": 0,
            "claims_delta": current.get("claims", []),
            "source_changes": current.get("source_change_status", {}),
            "flood_signal_delta": current.get("flood_signal_count", 0),
            "gauge_changes": [],
            "gauge_readings": gauge_readings,
            "api_resolution": api_res,
            "summary_bullets": bullets,
        }

    prior_ids = set(prior.get("claim_ids") or [])
    current_ids = set(current.get("claim_ids") or [])
    new_ids = current_ids - prior_ids
    removed_ids = prior_ids - current_ids

    phase_from = prior.get("phase")
    phase_to = current.get("phase")
    phase_changed = phase_from != phase_to

    source_changes: Dict[str, Dict[str, str]] = {}
    prior_sources = prior.get("source_change_status") or {}
    current_sources = current.get("source_change_status") or {}
    for sid, status in current_sources.items():
        prev = prior_sources.get(sid)
        if prev != status:
            source_changes[sid] = {"from": prev or "unknown", "to": status}

    flood_delta = current.get("flood_signal_count", 0) - prior.get("flood_signal_count", 0)
    gauge_changes = _compare_gauge_readings(
        prior.get("gauge_readings") or {},
        current.get("gauge_readings") or {},
    )

    bullets: List[str] = []
    if phase_changed:
        bullets.append(f"Phase: {phase_from} → {phase_to}")
    else:
        bullets.append(f"Phase unchanged: {phase_to}")
    if prior.get("alert_level") != current.get("alert_level"):
        bullets.append(f"Alert level: {prior.get('alert_level')} → {current.get('alert_level')}")
    if new_ids:
        bullets.append(f"New claims: {len(new_ids)}")
    if removed_ids:
        bullets.append(f"Removed claims: {len(removed_ids)}")
    for sid, ch in source_changes.items():
        bullets.append(f"Source {sid}: {ch['from']} → {ch['to']}")
    if flood_delta:
        bullets.append(f"Flood signals delta: {flood_delta:+d}")
    bullets.extend(_gauge_change_bullets(gauge_changes))

    prior_api = prior.get("api_resolution") or {}
    current_api = current.get("api_resolution") or {}
    prior_sites = set(prior_api.get("usgs_sites") or [])
    current_sites = set(current_api.get("usgs_sites") or [])
    if prior_sites != current_sites:
        bullets.append(f"USGS site list changed: {len(prior_sites)} → {len(current_sites)} gauges")
    if prior_api.get("nws_zone") != current_api.get("nws_zone") and current_api.get("nws_zone"):
        bullets.append(
            f"NWS zone: {prior_api.get('nws_zone', 'none')} → {current_api.get('nws_zone')}"
        )

    if not bullets:
        bullets.append("No material changes since last report.")

    return {
        "first_run": False,
        "phase_changed": phase_changed,
        "phase_from": phase_from,
        "phase_to": phase_to,
        "alert_from": prior.get("alert_level"),
        "alert_to": current.get("alert_level"),
        "new_claims": len(new_ids),
        "removed_claims": len(removed_ids),
        "claims_delta": [c for c in current.get("claims", []) if c.get("claim_id") in new_ids],
        "source_changes": source_changes,
        "flood_signal_delta": flood_delta,
        "gauge_changes": gauge_changes,
        "gauge_readings": current.get("gauge_readings") or {},
        "api_resolution": current_api,
        "summary_bullets": bullets,
    }
