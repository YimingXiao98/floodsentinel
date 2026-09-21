"""Independent ground truth for retrospective evaluation.

Two labellings, deliberately kept separate:

**Official truth** — NWS watch/warning intervals from the Iowa Environmental
Mesonet VTEC archive, issued by forecasters and timestamped. These are held out
of system input for gauge-only runs, where the replay corpus carries USGS
readings alone. A run may instead inject them as a T0 alert channel via
``alerts_active_at``, which is how the deployed system sees them; in that case
they are an input and grading falls to hydrologic truth, with the warning
timeline reported only as context.

**Hydrologic truth** — whether a fresh gauge stood above its official NWS flood
stage.  Evaluation uses a prespecified reference-observation age and labels
intervals unknown when a county all-clear cannot be established; the legacy
unbounded last-observation mode remains available for compatibility. The system
sees the same underlying series only up to virtual time t, subject to its own
staleness and dropout policy.

Lives outside `watcher/` because it is evaluation scaffolding, not runtime.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

IEM_VTEC_URL = "https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py"
_HEADERS = {"User-Agent": "OpenClaw-disaster-sentinel-eval/1.0"}

#: VTEC phenomena that indicate flooding. TO (tornado) and others are excluded.
FLOOD_PHENOMENA = {"FF", "FL", "FA"}

#: Ordered severity of a VTEC significance code.
SIGNIFICANCE_RANK = {"Y": 1, "A": 2, "W": 3}  # Advisory < Watch < Warning

TRUTH_LEVELS = ("none", "advisory", "watch", "warning")

_THRESHOLD_KEYS = (
    "action_stage",
    "flood_stage",
    "moderate_stage",
    "major_stage",
)


def parse_iso(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def apply_threshold_compatibility(
    corpus: Dict[str, Any],
    *,
    excluded_site_ids: Sequence[str] = (),
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return an evaluation copy with incompatible thresholds disabled.

    A site is excluded from threshold classification when its metadata contains
    ``threshold_compatible: false`` / ``evaluation_threshold_excluded: true``,
    or when its id is supplied explicitly by a frozen study manifest.  The
    observation remains available as unscored evidence; only the datum-dependent
    stage thresholds are removed.  Replay and oracle truth both call this helper
    so a site cannot be excluded from one side of the comparison only.
    """
    explicit = {str(site_id) for site_id in excluded_site_ids}
    prepared = deepcopy(corpus)
    exclusions: List[Dict[str, Any]] = []
    for site in prepared.get("sites") or []:
        site_no = str(site.get("site_no") or "")
        incompatible = site.get("threshold_compatible") is False
        metadata_excluded = bool(
            site.get("evaluation_threshold_excluded")
            or site.get("evaluation_excluded")
        )
        if site_no not in explicit and not incompatible and not metadata_excluded:
            continue
        reason = (
            site.get("threshold_exclusion_reason")
            or site.get("evaluation_exclusion_reason")
            or ("manifest_site_exclusion" if site_no in explicit else "incompatible_datum")
        )
        original_thresholds = site.get("evaluation_original_thresholds") or {
            key: site.get(key) for key in _THRESHOLD_KEYS
        }
        exclusions.append(
            {
                "site_no": site_no,
                "reason": str(reason),
                "original_thresholds": original_thresholds,
            }
        )
        site["evaluation_original_thresholds"] = original_thresholds
        for key in _THRESHOLD_KEYS:
            site[key] = None
        site["threshold_source"] = "evaluation_excluded_incompatible_datum"
        site["evaluation_threshold_excluded"] = True
    return prepared, sorted(exclusions, key=lambda row: row["site_no"])


# --------------------------------------------------------------------------
# Official truth: NWS VTEC archive
# --------------------------------------------------------------------------


def fetch_vtec_events(ugc: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """All VTEC events for a UGC county code over a date range."""
    params = urllib.parse.urlencode(
        {"ugc": ugc, "sdate": start_date, "edate": end_date}
    )
    req = urllib.request.Request(f"{IEM_VTEC_URL}?{params}", headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.load(resp)
    return payload.get("events") or []


def flood_events(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep flood-related events, dropping tornado and unrelated phenomena."""
    out = []
    for event in events:
        if event.get("phenomena") not in FLOOD_PHENOMENA:
            continue
        if not event.get("issue") or not event.get("expire"):
            continue
        out.append(
            {
                "phenomena": event.get("phenomena"),
                "significance": event.get("significance"),
                "name": event.get("name"),
                "issue": event.get("issue"),
                "expire": event.get("expire"),
                "eventid": event.get("eventid"),
                "wfo": event.get("wfo"),
                "ugc": event.get("ugc"),
            }
        )
    return sorted(out, key=lambda e: e["issue"])


def truth_level_at(events: Sequence[Dict[str, Any]], when: datetime) -> str:
    """Most severe flood significance in effect at `when`."""
    rank = 0
    for event in events:
        if parse_iso(event["issue"]) <= when < parse_iso(event["expire"]):
            rank = max(rank, SIGNIFICANCE_RANK.get(event.get("significance"), 0))
    return TRUTH_LEVELS[rank]


def flash_flood_active(events: Sequence[Dict[str, Any]], when: datetime) -> bool:
    """A Flash Flood Warning specifically (the acute, life-safety signal)."""
    for event in events:
        if event.get("phenomena") != "FF" or event.get("significance") != "W":
            continue
        if parse_iso(event["issue"]) <= when < parse_iso(event["expire"]):
            return True
    return False


#: Score the county-flood NWS source assigns to an active flood warning.
ACTIVE_FLOOD_WARNING_SCORE = 2.0

#: VTEC significance to the NWS severity vocabulary the claim extractor reads.
_SIGNIFICANCE_TO_SEVERITY = {"W": "Severe", "A": "Moderate", "Y": "Minor"}


def alerts_active_at(
    events: Sequence[Dict[str, Any]], when: datetime, *, county: str, state: str
) -> List[Dict[str, Any]]:
    """VTEC events in effect at `when`, shaped like live NWS alert dicts.

    Lets a replay feed the same T0 alert channel the deployed system consumes,
    instead of evaluating a gauge-only variant of the system.
    """
    out = []
    for event in events:
        if not (parse_iso(event["issue"]) <= when < parse_iso(event["expire"])):
            continue
        name = event.get("name") or "Flood Warning"
        out.append(
            {
                "id": f"vtec_{event.get('wfo')}_{event.get('eventid')}_{event.get('phenomena')}",
                "event": name,
                "headline": f"{name} in effect for {county} County, {state}",
                "severity": _SIGNIFICANCE_TO_SEVERITY.get(
                    event.get("significance"), "Unknown"
                ),
                "urgency": "Immediate" if event.get("significance") == "W" else "Expected",
                "area_desc": f"{county}, {state}",
                "effective": event["issue"],
                "expires": event["expire"],
                "status": "Actual",
                "nws_zone": event.get("ugc"),
                "flood_state_score": ACTIVE_FLOOD_WARNING_SCORE
                if event.get("significance") == "W"
                else None,
            }
        )
    return out


def first_flood_warning_time(events: Sequence[Dict[str, Any]]) -> Optional[datetime]:
    """Issue time of the earliest flood *warning*, the onset reference for lead time."""
    warnings = [e for e in events if e.get("significance") == "W"]
    if not warnings:
        return None
    return min(parse_iso(e["issue"]) for e in warnings)


# --------------------------------------------------------------------------
# Hydrologic truth: gauge above official flood stage, with full hindsight
# --------------------------------------------------------------------------


def hydrologic_truth_at(
    corpus: Dict[str, Any],
    when: datetime,
    *,
    max_age_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Whether any fresh reference gauge stood above flood stage at ``when``.

    ``max_age_hours=None`` preserves the original hindsight/last-observation
    behaviour for callers that need it.  Evaluation should pass a prespecified
    finite horizon.  With a finite horizon the county label is three-valued:

    * ``True`` when at least one fresh gauge is above flood stage;
    * ``False`` when every reporting, thresholded reference gauge is fresh and
      none is above flood stage; and
    * ``None`` when no fresh positive exists and one or more reference gauges
      are stale or missing, because an all-clear is then not observable.

    Sites that contain no observations anywhere in the frozen corpus are not
    reference gauges; otherwise a never-reporting site would make the whole
    event unknowable.  Their absence remains a corpus-coverage limitation.
    """
    if max_age_hours is not None and max_age_hours < 0:
        raise ValueError("max_age_hours must be non-negative or None")
    above_flood: List[str] = []
    above_action: List[str] = []
    observed = 0
    fresh_reference = 0
    reference_sites = [
        site
        for site in (corpus.get("sites") or [])
        if site.get("flood_stage") is not None and (site.get("observations") or [])
    ]
    for site in corpus.get("sites") or []:
        observations = site.get("observations") or []
        if not observations:
            continue
        cutoff = _utc_iso(when)
        latest = None
        for obs in observations:
            if obs["t"] <= cutoff:
                latest = obs
            else:
                break
        if latest is None or latest.get("stage") is None:
            continue
        if max_age_hours is not None:
            age_hours = (when - parse_iso(latest["t"])).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                continue
        observed += 1
        stage = latest["stage"]
        flood_stage = site.get("flood_stage")
        action_stage = site.get("action_stage")
        if flood_stage is not None and (site.get("observations") or []):
            fresh_reference += 1
        if flood_stage is not None and stage >= flood_stage:
            above_flood.append(site["site_no"])
        elif action_stage is not None and stage >= action_stage:
            above_action.append(site["site_no"])
    reference_count = len(reference_sites)
    if max_age_hours is None:
        known = fresh_reference > 0
        is_flooding: Optional[bool] = bool(above_flood)
    else:
        known = bool(above_flood) or (
            reference_count > 0 and fresh_reference == reference_count
        )
        is_flooding = bool(above_flood) if known else None
    return {
        "gauges_observed": observed,
        "reference_gauges": reference_count,
        "reference_gauges_fresh": fresh_reference,
        "reference_gauges_stale_or_missing": max(reference_count - fresh_reference, 0),
        "gauges_above_flood_stage": above_flood,
        "gauges_above_action_stage": above_action,
        "truth_known": known,
        "is_flooding": is_flooding,
    }


# --------------------------------------------------------------------------
# Timeline assembly
# --------------------------------------------------------------------------


def build_truth_timeline(
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    ticks: Sequence[datetime],
    max_age_hours: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Per-tick labels combining official and hydrologic truth."""
    timeline = []
    for tick in ticks:
        hydro = hydrologic_truth_at(corpus, tick, max_age_hours=max_age_hours)
        timeline.append(
            {
                "t": _utc_iso(tick),
                "official_level": truth_level_at(events, tick),
                "flash_flood_warning": flash_flood_active(events, tick),
                "hydrologic_is_flooding": hydro["is_flooding"],
                "hydrologic_truth_known": hydro["truth_known"],
                "gauges_above_flood_stage": len(hydro["gauges_above_flood_stage"]),
                "gauges_above_action_stage": len(hydro["gauges_above_action_stage"]),
                "gauges_observed": hydro["gauges_observed"],
                "reference_gauges": hydro["reference_gauges"],
                "reference_gauges_fresh": hydro["reference_gauges_fresh"],
            }
        )
    return timeline


def save(obj: Any, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return target


def load(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
