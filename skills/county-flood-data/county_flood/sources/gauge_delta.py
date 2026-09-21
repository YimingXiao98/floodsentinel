"""Cold-start and delta-based supplementary signals for unscored gauges."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def stage_rise_threshold_ft() -> float:
    return _env_float("COUNTY_FLOOD_STAGE_RISE_FT", 1.0)


def stage_fall_threshold_ft() -> float:
    return _env_float("COUNTY_FLOOD_STAGE_FALL_FT", 1.0)


def flow_rise_pct_threshold() -> float:
    return _env_float("COUNTY_FLOOD_FLOW_RISE_PCT", 50.0)


def flow_fall_pct_threshold() -> float:
    return _env_float("COUNTY_FLOOD_FLOW_FALL_PCT", 50.0)


def supplementary_confidence_default() -> float:
    return _env_float("COUNTY_FLOOD_SUPPLEMENTARY_CONFIDENCE", 0.45)


def stage_persist_threshold_ft() -> float:
    """Minimum |Δstage| (ft) before Neo4j / prior baseline should advance."""
    return _env_float("COUNTY_FLOOD_STAGE_PERSIST_FT", 0.1)


def gauge_lookup_key(gauge: Dict[str, Any]) -> Optional[str]:
    """Stable key for matching gauges across runs."""
    site = str(gauge.get("site_no") or "").strip()
    shef = str(gauge.get("shef_id") or "").strip().upper()
    if site and shef and site != shef:
        return f"site:{site}"
    if site:
        return f"site:{site}"
    if shef:
        return f"shef:{shef}"
    return None


def index_prior_gauges(prior_gauges: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Build lookup from prior gauge list; last entry wins on duplicate keys."""
    if not prior_gauges:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for gauge in prior_gauges:
        if not isinstance(gauge, dict):
            continue
        key = gauge_lookup_key(gauge)
        if key:
            out[key] = gauge
    return out


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def significant_stage_delta(
    prev: Any,
    curr: Any,
    *,
    threshold_ft: Optional[float] = None,
) -> bool:
    """True when stage appeared or moved by >= persist noise floor."""
    threshold = stage_persist_threshold_ft() if threshold_ft is None else threshold_ft
    prev_f = _as_float(prev)
    curr_f = _as_float(curr)
    if prev_f is None and curr_f is None:
        return False
    if prev_f is None or curr_f is None:
        return True
    return round(abs(curr_f - prev_f), 3) >= threshold


def gauges_require_persist(
    current_gauges: List[Dict[str, Any]],
    prior_gauges: Optional[List[Dict[str, Any]]] = None,
    *,
    threshold_ft: Optional[float] = None,
) -> bool:
    """
    Whether gauge readings should advance the Neo4j baseline.

    Persists on cold start (no prior), new gauges, |Δstage| >= threshold,
    or flood_state_score changes. Sub-threshold stage noise keeps the prior.
    """
    threshold = stage_persist_threshold_ft() if threshold_ft is None else threshold_ft
    current = [
        g
        for g in current_gauges
        if isinstance(g, dict) and not g.get("error") and g.get("observed_primary") is not None
    ]
    if not current:
        return False

    prior_index = index_prior_gauges(prior_gauges)
    if not prior_index:
        return True  # cold start — establish baseline

    for gauge in current:
        key = gauge_lookup_key(gauge)
        prior = prior_index.get(key) if key else None
        if prior is None:
            return True  # newly seen gauge
        if significant_stage_delta(
            prior.get("observed_primary"),
            gauge.get("observed_primary"),
            threshold_ft=threshold,
        ):
            return True
        prev_score = prior.get("flood_state_score")
        curr_score = gauge.get("flood_state_score")
        if prev_score != curr_score and (prev_score is not None or curr_score is not None):
            return True
        prev_cat = prior.get("observed_flood_category")
        curr_cat = gauge.get("observed_flood_category")
        if prev_cat != curr_cat and (prev_cat or curr_cat):
            return True
    return False


def _pct_change(prev: Optional[float], curr: Optional[float]) -> Optional[float]:
    if prev is None or curr is None or prev == 0:
        return None
    return ((curr - prev) / abs(prev)) * 100.0


def compute_hydrologic_signal(
    *,
    observed_primary: Optional[float],
    streamflow_cfs: Optional[float],
    prior: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return supplementary delta fields for one gauge."""
    base: Dict[str, Any] = {
        "hydrologic_signal": None,
        "delta_primary_ft": None,
        "delta_streamflow_pct": None,
        "prior_observed_primary": None,
        "prior_streamflow_cfs": None,
        "prior_fetched_at": None,
        "supplementary_confidence": None,
    }

    if prior is None:
        if observed_primary is not None or streamflow_cfs is not None:
            base["hydrologic_signal"] = "cold_start"
        return base

    prev_primary = prior.get("observed_primary")
    prev_flow = prior.get("streamflow_cfs")
    base["prior_observed_primary"] = prev_primary
    base["prior_streamflow_cfs"] = prev_flow
    base["prior_fetched_at"] = prior.get("_fetched_at") or prior.get("observed_valid_time")

    delta_primary = None
    if observed_primary is not None and prev_primary is not None:
        delta_primary = round(observed_primary - prev_primary, 3)
        base["delta_primary_ft"] = delta_primary

    delta_flow_pct = _pct_change(prev_flow, streamflow_cfs)
    if delta_flow_pct is not None:
        base["delta_streamflow_pct"] = round(delta_flow_pct, 1)

    rising = False
    falling = False
    if delta_primary is not None:
        if delta_primary >= stage_rise_threshold_ft():
            rising = True
        elif delta_primary <= -stage_fall_threshold_ft():
            falling = True

    if delta_flow_pct is not None:
        if delta_flow_pct >= flow_rise_pct_threshold():
            rising = True
        elif delta_flow_pct <= -flow_fall_pct_threshold():
            falling = True

    if rising and falling:
        base["hydrologic_signal"] = "stable"
    elif rising:
        base["hydrologic_signal"] = "rising"
        base["supplementary_confidence"] = supplementary_confidence_default()
    elif falling:
        base["hydrologic_signal"] = "falling"
        base["supplementary_confidence"] = supplementary_confidence_default()
    else:
        base["hydrologic_signal"] = "stable"

    return base


def apply_supplementary_signals(
    gauges: List[Dict[str, Any]],
    prior_gauges: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Attach hydrologic delta fields for every gauge with a prior reading.

    Rising/falling/cold_start supplementary_signals are emitted only for unscored
    gauges (no flood_state_score / no action-minor-major thresholds). Scored gauges
    still get prior/delta fields for reports. Cold-start rise/fall thresholds are
    the observation band when flood categories are unavailable.
    """
    prior_index = index_prior_gauges(prior_gauges)
    updated: List[Dict[str, Any]] = []
    supplementary: List[Dict[str, Any]] = []

    for gauge in gauges:
        row = dict(gauge)
        if row.get("error"):
            updated.append(row)
            continue

        key = gauge_lookup_key(row)
        prior = prior_index.get(key) if key else None
        delta_fields = compute_hydrologic_signal(
            observed_primary=row.get("observed_primary"),
            streamflow_cfs=row.get("streamflow_cfs"),
            prior=prior,
        )
        row.update(delta_fields)

        is_scored = row.get("flood_state_score") is not None
        signal = row.get("hydrologic_signal")
        # Trend / cold-start claims only for unscored gauges (no rated thresholds).
        if not is_scored and signal in ("rising", "falling", "cold_start"):
            entry = {
                "gauge_id": row.get("shef_id") or row.get("site_no"),
                "name": row.get("name"),
                "source": row.get("source"),
                "hydrologic_signal": signal,
                "delta_primary_ft": row.get("delta_primary_ft"),
                "delta_streamflow_pct": row.get("delta_streamflow_pct"),
                "observed_primary": row.get("observed_primary"),
                "streamflow_cfs": row.get("streamflow_cfs"),
                "supplementary_confidence": row.get("supplementary_confidence"),
            }
            if signal == "cold_start":
                entry["supplementary_confidence"] = supplementary_confidence_default()
                row["supplementary_confidence"] = entry["supplementary_confidence"]
            supplementary.append(entry)

        updated.append(row)

    return updated, supplementary
