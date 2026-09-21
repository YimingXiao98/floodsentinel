"""County gauge selection with scorable-first priority."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from county_flood.sources.stages import clean_stage_value


def _valid_flood_stage(row: Dict[str, str]) -> bool:
    return clean_stage_value(row.get("flood stage")) is not None


def _has_shef(row: Dict[str, str]) -> bool:
    return bool((row.get("nws shef id") or "").strip())


def _has_usgs(row: Dict[str, str]) -> bool:
    return bool((row.get("usgs id") or "").strip())


def _partial_stages_only(row: Dict[str, str]) -> bool:
    cols = ("action stage", "flood stage", "moderate flood stage", "major flood stage")
    any_valid = any(clean_stage_value(row.get(c)) is not None for c in cols)
    return any_valid and not _valid_flood_stage(row)


def gauge_priority_tier(row: Dict[str, str]) -> int:
    """Lower = higher priority (scorable-first)."""
    if _valid_flood_stage(row):
        return 0
    if _has_shef(row):
        return 1
    if _partial_stages_only(row):
        return 2
    if _has_usgs(row):
        return 3
    return 4


def sort_csv_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(
        rows,
        key=lambda r: (gauge_priority_tier(r), (r.get("location name") or "").lower()),
    )


def csv_row_to_site_meta(row: Dict[str, str]) -> Dict[str, Any]:
    return {
        "site_no": (row.get("usgs id") or "").strip() or None,
        "shef_id": (row.get("nws shef id") or "").strip() or None,
        "name": row.get("location name") or "",
        "action_stage": clean_stage_value(row.get("action stage")),
        "flood_stage": clean_stage_value(row.get("flood stage")),
        "moderate_stage": clean_stage_value(row.get("moderate flood stage")),
        "major_stage": clean_stage_value(row.get("major flood stage")),
        "stage_unit": (row.get("flood stage unit") or "ft").strip() or "ft",
        "priority_tier": gauge_priority_tier(row),
        "threshold_source": "csv",
        "csv_row": row,
    }
