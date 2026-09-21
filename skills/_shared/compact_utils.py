"""
Shared compact-summary helpers for high-volume skills and cron payload shaping.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple


def env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def truncate_text(value: Any, max_len: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def cap_rows(rows: Any, limit: int) -> List[Any]:
    if not isinstance(rows, list):
        return []
    if len(rows) <= limit:
        return rows
    return rows[:limit]


def approx_json_size_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return 0


def build_nws_compact_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
    issued = cap_rows(data.get("houston_issued_alerts"), 5)
    top_findings = []
    for row in issued:
        if not isinstance(row, dict):
            continue
        top_findings.append(
            {
                "event": row.get("event"),
                "issue_level": row.get("issue_level"),
                "headline": truncate_text(row.get("headline"), 160),
                "expires": row.get("expires"),
            }
        )
    return {
        "skill_id": "em-nws-alerts",
        "status": "ok",
        "generated_at": data.get("generated_at"),
        "headline": f"{int(data.get('houston_severe_weather_alerts_count') or 0)} Guam/CNMI severe alerts",
        "key_metrics": {
            "severe_weather_alerts_count": int(data.get("severe_weather_alerts_count") or 0),
            "metro_severe_weather_alerts_count": int(data.get("houston_severe_weather_alerts_count") or 0),
            "metro_issued_alerts_count": int(data.get("houston_issued_alerts") and len(data.get("houston_issued_alerts")) or 0),
        },
        "top_findings": top_findings,
        "zip_impact": [],
        "domain_flags": {
            "has_warnings": any((r.get("issue_level") == "warning") for r in issued if isinstance(r, dict)),
        },
    }


def build_social_compact_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    incidents = cap_rows(snapshot.get("incidents"), 5)
    top_findings = []
    for row in incidents:
        if not isinstance(row, dict):
            continue
        top_findings.append(
            {
                "incident": truncate_text(row.get("incident"), 160),
                "location": truncate_text(row.get("location"), 80),
                "source": truncate_text(row.get("source"), 60),
                "occurred_at": row.get("occurred_at"),
            }
        )
    return {
        "skill_id": "em-social-scan",
        "status": "ok" if not snapshot.get("error") else "error",
        "generated_at": snapshot.get("scan_timestamp"),
        "headline": f"{int(snapshot.get('total_incidents') or 0)} social incidents (24h)",
        "key_metrics": {
            "total_incidents": int(snapshot.get("total_incidents") or 0),
            "sources_scanned_count": len(snapshot.get("sources_scanned") or []),
        },
        "top_findings": top_findings,
        "zip_impact": [],
        "domain_flags": {
            "has_errors": bool(snapshot.get("error")),
            "has_reddit_feed_errors": bool(snapshot.get("reddit_feed_errors")),
        },
    }


def build_waze_compact_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
    zip_rows = (
        ((data.get("houston_zipcode_impact") or {}).get("zipcodes_affected") or [])
        if isinstance(data.get("houston_zipcode_impact"), dict)
        else []
    )
    zip_rows = cap_rows(zip_rows, 5)
    return {
        "skill_id": "em-waze-snapshot",
        "status": "ok" if not snapshot.get("error") else "error",
        "generated_at": data.get("generated_at"),
        "headline": f"{int(data.get('total_incidents') or 0)} traffic incidents",
        "key_metrics": {
            "total_incidents": int(data.get("total_incidents") or 0),
            "zipcodes_affected": int(((data.get("houston_zipcode_impact") or {}).get("total_zipcodes_affected") or 0)),
        },
        "top_findings": [
            {
                "severity_score": ((data.get("criticality_summary") or {}).get("overall_severity_score")),
                "high_critical": ((data.get("criticality_summary") or {}).get("high_critical")),
                "medium_critical": ((data.get("criticality_summary") or {}).get("medium_critical")),
            }
        ],
        "zip_impact": [
            {
                "zipcode": r.get("zipcode"),
                "incident_count": int(r.get("incident_count") or 0),
                "max_severity": r.get("max_severity"),
            }
            for r in zip_rows
            if isinstance(r, dict)
        ],
        "domain_flags": {
            "has_error": bool(snapshot.get("error")),
        },
    }


def apply_progressive_compaction(artifact: Dict[str, Any], max_bytes: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Shrink artifact deterministically without hard failure.
    """
    out = dict(artifact)
    dropped_domains: List[str] = []
    dropped_items = 0
    level = 0

    def _size() -> int:
        return approx_json_size_bytes(out)

    if _size() <= max_bytes:
        return out, {"compaction_level": level, "dropped_domains": dropped_domains, "dropped_items_count": dropped_items}

    # Level 1: trim lists and long text.
    level = 1
    for key in ("top_findings", "zip_impact_rows"):
        if isinstance(out.get(key), list):
            old = len(out[key])
            out[key] = out[key][:5]
            dropped_items += max(0, old - len(out[key]))
    if isinstance(out.get("domain_summaries"), list):
        for row in out["domain_summaries"]:
            if isinstance(row, dict) and "headline" in row:
                row["headline"] = truncate_text(row.get("headline"), 120)

    if _size() <= max_bytes:
        return out, {"compaction_level": level, "dropped_domains": dropped_domains, "dropped_items_count": dropped_items}

    # Level 2: keep priority domains only.
    level = 2
    priority = {"TOP ZIP IMPACT", "NWS Alerts", "Weather", "Flood/Hydro"}
    domains = out.get("domain_summaries")
    if isinstance(domains, list):
        kept = []
        for d in domains:
            if not isinstance(d, dict):
                continue
            name = str(d.get("domain") or "")
            if name in priority:
                kept.append(d)
            else:
                dropped_domains.append(name)
                dropped_items += 1
        out["domain_summaries"] = kept

    if _size() <= max_bytes:
        return out, {"compaction_level": level, "dropped_domains": dropped_domains, "dropped_items_count": dropped_items}

    # Level 3: executive-only.
    level = 3
    out = {
        "scan_timestamp": out.get("scan_timestamp"),
        "executive_summary": out.get("executive_summary"),
        "top_zip_impact": cap_rows(out.get("zip_impact_rows"), 3),
        "compaction_notice": "Executive-only compact mode applied",
    }
    return out, {"compaction_level": level, "dropped_domains": dropped_domains, "dropped_items_count": dropped_items}
