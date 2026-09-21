"""Bridge county-flood-data skill output into watcher SourceRecord/SourceSnapshot shape."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from watcher.fetch import apply_change_detection
from watcher.neo4j_client import get_prior_county_flood_gauges
from watcher.sources.county_api_resolver import resolve_county_flood_sources

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "county-flood-data"

SOURCE_MODES = {
    "nws": ["nwps"],
    "usgs": ["usgs"],
    "both": ["nwps", "usgs", "nws_alerts"],
}

SOURCE_LABELS = {
    "nwps": ("NWPSSource", "NOAA NWPS river gauges"),
    "usgs": ("USGSGaugeSource", "USGS NWIS IV stream gauges"),
    "nws_alerts": ("NWSSource", "NWS Active Alerts API (County Zone)"),
}

GAUGE_SOURCE_KEYS = frozenset({"nwps", "usgs"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_skill_path() -> None:
    skill_dir = str(_SKILL_DIR)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)


def _snapshot_slug(ts: str) -> str:
    return ts.replace(":", "").replace("-", "").replace("+", "").replace(".", "")


def _apply_gauge_stage_persist_gate(
    snapshots: List[Dict[str, Any]],
    prior_gauges: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Downgrade gauge snapshots to change_status=unchanged when |Δstage| < 0.1 ft.

    Cold start (no prior gauges) and real stage/category moves stay persistable.
    NWS alert snapshots are left to fingerprint-based change detection only.
    """
    _ensure_skill_path()
    from county_flood.sources.gauge_delta import gauges_require_persist

    out: List[Dict[str, Any]] = []
    for snap in snapshots:
        sid = str(snap.get("source_id") or "")
        if "nws_alerts" in sid or not sid.startswith("src_county_flood_"):
            out.append(snap)
            continue
        if snap.get("change_status") == "unchanged":
            out.append(snap)
            continue

        gauges: List[Dict[str, Any]] = []
        try:
            payload = json.loads(snap.get("raw_text") or "{}")
            gauges = [g for g in (payload.get("gauges") or []) if isinstance(g, dict)]
        except (TypeError, json.JSONDecodeError):
            gauges = []

        # Filter prior to same source when possible (usgs vs nwps).
        source_key = None
        for key in GAUGE_SOURCE_KEYS:
            if f"_{key}_" in sid:
                source_key = key
                break
        prior_for_source = prior_gauges
        if source_key:
            prior_for_source = [
                g for g in prior_gauges if (g.get("source") or "") == source_key
            ] or prior_gauges

        if gauges_require_persist(gauges, prior_for_source):
            out.append(snap)
            continue

        gated = dict(snap)
        gated["change_status"] = "unchanged"
        gated["skip_reason"] = "below_stage_delta"
        out.append(gated)
    return out


def fetch_active_flood_alert_dicts(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Light active-alert fetch for the verification-trigger consultation.

    Returns the same alert dict shape the coordinator payload carries, without
    touching gauges, snapshots, claims, or Neo4j. A failure returns an empty
    list: the trigger stream is best-effort and its absence must never block
    the scheduled cadence.
    """
    _ensure_skill_path()
    try:
        from county_flood.sources.nws_alerts import fetch_nws_alerts

        result = fetch_nws_alerts(county=county, state=state, fips=fips)
        alerts = getattr(result, "alerts", None) or []
        return [alert.to_dict() for alert in alerts]
    except Exception:
        return []


def fetch_county_flood_sources(
    *,
    county: str,
    state: str,
    fips: str,
    location: str,
    mission_id: str,
    source_mode: str = "both",
    prior_by_source: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run county-flood-data and normalize to watcher fetch result envelope."""
    _ensure_skill_path()
    from county_flood.runner import run_county_flood

    active_sources = SOURCE_MODES.get(source_mode, SOURCE_MODES["both"])
    prior_gauges: List[Dict[str, Any]] = []
    try:
        prior_gauges = get_prior_county_flood_gauges(mission_id, fips)
    except Exception:
        prior_gauges = []

    try:
        payload = run_county_flood(
            county=county,
            state=state,
            fips=fips or None,
            location=location,
            sources=active_sources,
            prior_gauges=prior_gauges or None,
            output_format="json",
            emit=False,
        )
    except Exception as exc:
        return {
            "source_records": [],
            "source_snapshots": [],
            "failed_fetches": [
                {
                    "source_id": f"src_county_flood_{fips}",
                    "source_url": "",
                    "failure_reason": str(exc),
                }
            ],
            "county_flood_payload": None,
            "county_api_mapping": None,
            "discovery_mode": "county_flood_data_skill",
            "sources_attempted": len(active_sources),
            "sources_succeeded": 0,
            "fetched_at": _utc_now(),
        }

    generated_at = payload.get("generated_at") or _utc_now()
    mapping = resolve_county_flood_sources(fips, county, state)

    source_records: List[Dict[str, Any]] = []
    snapshots: List[Dict[str, Any]] = []

    for src_key in active_sources:
        if src_key not in GAUGE_SOURCE_KEYS:
            continue
        ontology_type, label = SOURCE_LABELS[src_key]
        record_id = f"src_county_flood_{src_key}_{fips}"
        meta = (payload.get("sources") or {}).get(src_key) or {}
        api_base = meta.get("api_base") or ""
        gauges = [g for g in (payload.get("gauges") or []) if g.get("source") == src_key]

        source_summary = {
            "average_flood_state": (payload.get("summary") or {}).get("average_flood_state"),
            "average_primary_reading": (payload.get("summary") or {}).get("average_primary_reading"),
            "gauges_with_data": len(gauges),
        }

        source_payload = {
            "schema": "county-flood-data/1",
            "generated_at": generated_at,
            "county": payload.get("county"),
            "state": payload.get("state"),
            "fips": fips,
            "source": src_key,
            "summary": source_summary,
            "source_metadata": meta,
            "gauges": gauges,
        }
        canonical = json.dumps(source_payload, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        source_records.append(
            {
                "source_id": record_id,
                "source_name": label,
                "source_url": api_base,
                "source_type": "official",
                "ontology_source_type": ontology_type,
                "source_type_iri": f"ds:{ontology_type}",
                "trust_tier": {"level": 0, "name": "T0"},
                "health_status": "verified",
                "resolution_strategy": "county_flood_data_skill",
                "discovery_hint": "county_flood_data_skill",
                "api_resolution": {
                    "county_fips": fips,
                    "county_match": True,
                    "source_mode": source_mode,
                    "resolved_ids": {
                        "sites": [g.get("site_no") for g in gauges if g.get("site_no")],
                        "shef_ids": [g.get("shef_id") for g in gauges if g.get("shef_id")],
                    },
                    "gauges_queried": len(gauges),
                },
            }
        )

        snapshots.append(
            {
                "snapshot_id": f"snap_{record_id}_{_snapshot_slug(generated_at)}",
                "source_id": record_id,
                "fetched_at": generated_at,
                "fetched_at_utc": generated_at,
                "published_at": generated_at,
                "content_fingerprint": fingerprint,
                "raw_excerpt": canonical[:2000],
                "raw_text": canonical,
                "snapshot_url": api_base,
                "change_status": "new",
                "content_type": "application/json",
                "byte_length": len(canonical.encode("utf-8")),
            }
        )

    if "nws_alerts" in active_sources:
        nws_meta = (payload.get("sources") or {}).get("nws_alerts") or {}
        alerts = payload.get("alerts") or []
        api_url = nws_meta.get("api_url") or nws_meta.get("nws_alerts_url") or ""
        nws_zone = nws_meta.get("nws_zone") or ""
        zone_info = mapping.nws_zone or {}
        county_match = bool(zone_info.get("validated"))
        record_id = f"src_county_flood_nws_alerts_{fips}"
        nws_payload = {
            "schema": "county-flood-nws-alerts/1",
            "generated_at": generated_at,
            "county": payload.get("county"),
            "state": payload.get("state"),
            "fips": fips,
            "api_url": api_url,
            "nws_zone": nws_zone,
            "alerts": alerts,
        }
        canonical = json.dumps(nws_payload, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        source_records.append(
            {
                "source_id": record_id,
                "source_name": SOURCE_LABELS["nws_alerts"][1],
                "source_url": api_url,
                "source_type": "official",
                "ontology_source_type": "NWSSource",
                "source_type_iri": "ds:NWSSource",
                "trust_tier": {"level": 0, "name": "T0"},
                "health_status": "verified",
                "resolution_strategy": "county_flood_data_skill",
                "discovery_hint": "nws_alerts_by_county_zone",
                "api_resolution": {
                    "county_fips": fips,
                    "county_match": county_match,
                    "source_mode": source_mode,
                    "resolved_ids": {
                        "zone": zone_info.get("zone") or nws_zone,
                        "zone_label": zone_info.get("label"),
                    },
                    "validation_method": zone_info.get("validation_method"),
                    "alerts_queried": len(alerts),
                },
            }
        )
        snapshots.append(
            {
                "snapshot_id": f"snap_{record_id}_{_snapshot_slug(generated_at)}",
                "source_id": record_id,
                "fetched_at": generated_at,
                "fetched_at_utc": generated_at,
                "published_at": generated_at,
                "content_fingerprint": fingerprint,
                "raw_excerpt": canonical[:2000],
                "raw_text": canonical,
                "snapshot_url": api_url,
                "change_status": "new",
                "content_type": "application/json",
                "byte_length": len(canonical.encode("utf-8")),
            }
        )

    if prior_by_source:
        snapshots = apply_change_detection(snapshots, prior_by_source)
    snapshots = _apply_gauge_stage_persist_gate(snapshots, prior_gauges)

    return {
        "source_records": source_records,
        "source_snapshots": snapshots,
        "failed_fetches": [],
        "county_flood_payload": payload,
        "county_api_mapping": mapping.to_dict(),
        "discovery_mode": "county_flood_data_skill",
        "credibility_assessments": [],
        "dropped_sources": [],
        "degraded_sources": [],
        "sources_attempted": len(source_records),
        "sources_succeeded": len(snapshots),
        "fetched_at": generated_at,
    }
