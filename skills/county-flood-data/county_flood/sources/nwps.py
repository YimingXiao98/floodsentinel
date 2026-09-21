"""NOAA NWPS flood source — CSV gauge lookup and live API fetch."""

from __future__ import annotations

import csv
import json
import random
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from county_flood.geo import county_match_key, normalize_county_name, normalize_state
from county_flood.models import GaugeReading, SourceResult
from county_flood.sources.selection import gauge_priority_tier, sort_csv_rows
from county_flood.sources.stages import (
    EXCLUDED_CATEGORIES,
    apply_gauge_limit,
    merge_stages,
    resolve_flood_category,
    stages_from_nwps_payload,
)

_SKILL_ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = _SKILL_ROOT / "nwps_all_gauges_report.csv"
NWPS_API_BASE = "https://api.water.noaa.gov/nwps/v1/gauges"
DEFAULT_WORKERS = 6
REQUEST_TIMEOUT = 30

NWPS_FLOOD_CATEGORIES_KNOWN = [
    "no_flooding",
    "action",
    "minor_flooding",
    "moderate_flooding",
    "major_flooding",
    "record_flood",
    "not_defined",
    "fcst_not_current",
    "low_water",
    "data_not_current",
    "out_of_service",
]

_FETCH_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OpenClaw-county-flood-data/1.0",
}

_csv_cache: Optional[List[Dict[str, str]]] = None
_discovered_categories: set[str] = set()


def _clean_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == -999 or number == -9999:
        return None
    return number


def _load_csv_rows() -> List[Dict[str, str]]:
    global _csv_cache
    if _csv_cache is not None:
        return _csv_cache
    if not CSV_PATH.is_file():
        raise FileNotFoundError(f"Gauge catalog not found: {CSV_PATH}")
    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        _csv_cache = list(csv.DictReader(handle))
    return _csv_cache


def _csv_flood_stage_valid(row: Dict[str, str]) -> bool:
    from county_flood.sources.stages import clean_stage_value

    return clean_stage_value(row.get("flood stage")) is not None


def _row_in_service(row: Dict[str, str]) -> bool:
    return str(row.get("in service", "")).strip().lower() == "true"


def resolve_shef_ids_from_csv(
    county: str,
    state: str,
    *,
    gauge_ids: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, str]], int, bool]:
    """Return selected CSV rows, total match count, and whether cap truncated."""
    rows = _load_csv_rows()
    if gauge_ids:
        wanted = {gid.strip().upper() for gid in gauge_ids if gid.strip()}
        matched = [
            row
            for row in rows
            if (row.get("nws shef id") or "").strip().upper() in wanted
        ]
    else:
        county_key = county_match_key(county)
        state_code = normalize_state(state)
        matched = [
            row
            for row in rows
            if normalize_state(row.get("state") or "") == state_code
            and county_match_key(row.get("county") or "") == county_key
            and _row_in_service(row)
        ]
        matched = sort_csv_rows(matched)

    selected, truncated = apply_gauge_limit(matched)
    return selected, len(matched), truncated


def _record_category(category: Optional[str]) -> None:
    if category:
        _discovered_categories.add(str(category).strip().lower())


def _request_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _csv_stage_value(row: Dict[str, str], column: str) -> Optional[float]:
    from county_flood.sources.stages import clean_stage_value

    return clean_stage_value(row.get(column))


def _stages_from_csv_row(row: Optional[Dict[str, str]]) -> Dict[str, Optional[float]]:
    if not row:
        return {
            "action_stage": None,
            "flood_stage": None,
            "moderate_stage": None,
            "major_stage": None,
        }
    return {
        "action_stage": _csv_stage_value(row, "action stage"),
        "flood_stage": _csv_stage_value(row, "flood stage"),
        "moderate_stage": _csv_stage_value(row, "moderate flood stage"),
        "major_stage": _csv_stage_value(row, "major flood stage"),
    }


def _parse_gauge_response(
    payload: Dict[str, Any], csv_row: Optional[Dict[str, str]] = None
) -> GaugeReading:
    observed = payload.get("status", {}).get("observed") or {}
    forecast = payload.get("status", {}).get("forecast") or {}
    obs_cat = observed.get("floodCategory")
    fc_cat = forecast.get("floodCategory")
    _record_category(obs_cat)
    _record_category(fc_cat)

    csv_stages = _stages_from_csv_row(csv_row)
    api_stages = stages_from_nwps_payload(payload)
    merged_stages, threshold_source = merge_stages(csv_stages, api_stages)

    observed_primary = _clean_number(observed.get("primary"))
    nwps_category = obs_cat or fc_cat
    if nwps_category and str(nwps_category).strip().lower() in EXCLUDED_CATEGORIES:
        if obs_cat and str(obs_cat).strip().lower() not in EXCLUDED_CATEGORIES:
            nwps_category = obs_cat
        elif fc_cat and str(fc_cat).strip().lower() not in EXCLUDED_CATEGORIES:
            nwps_category = fc_cat

    result = resolve_flood_category(
        observed_primary=observed_primary,
        nwps_category=nwps_category,
        action_stage=merged_stages["action_stage"],
        flood_stage=merged_stages["flood_stage"],
        moderate_stage=merged_stages["moderate_stage"],
        major_stage=merged_stages["major_stage"],
        threshold_source=threshold_source,
    )

    tier = gauge_priority_tier(csv_row) if csv_row else None

    return GaugeReading(
        source="nwps",
        shef_id=str(payload.get("lid") or (csv_row or {}).get("nws shef id") or ""),
        site_no=(csv_row or {}).get("usgs id") or None,
        name=str(payload.get("name") or (csv_row or {}).get("location name") or ""),
        observed_primary=observed_primary,
        observed_primary_unit=observed.get("primaryUnit") or None,
        observed_flood_category=result.category,
        observed_valid_time=observed.get("validTime") or None,
        forecast_primary=_clean_number(forecast.get("primary")),
        forecast_primary_unit=forecast.get("primaryUnit") or None,
        forecast_flood_category=fc_cat,
        forecast_valid_time=forecast.get("validTime") or None,
        flood_state_score=result.score,
        streamflow_cfs=None,
        action_stage=merged_stages["action_stage"],
        flood_stage=merged_stages["flood_stage"],
        moderate_stage=merged_stages["moderate_stage"],
        major_stage=merged_stages["major_stage"],
        csv_location_name=(csv_row or {}).get("location name") or None,
        classification_method=result.method,
        threshold_source=result.threshold_source,
        unscored_reason=result.unscored_reason,
        priority_tier=tier,
    )


def _fetch_one(shef_id: str, csv_row: Optional[Dict[str, str]]) -> GaugeReading:
    url = f"{NWPS_API_BASE}/{shef_id}"
    try:
        payload = _request_json(url)
        return _parse_gauge_response(payload, csv_row)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return GaugeReading(
                source="nwps",
                shef_id=shef_id,
                name=(csv_row or {}).get("location name") or shef_id,
                csv_location_name=(csv_row or {}).get("location name") or None,
                error="HTTP 404",
                priority_tier=gauge_priority_tier(csv_row) if csv_row else None,
            )
        return GaugeReading(
            source="nwps",
            shef_id=shef_id,
            name=(csv_row or {}).get("location name") or shef_id,
            csv_location_name=(csv_row or {}).get("location name") or None,
            error=f"HTTP {exc.code}",
            priority_tier=gauge_priority_tier(csv_row) if csv_row else None,
        )
    except Exception as exc:
        return GaugeReading(
            source="nwps",
            shef_id=shef_id,
            name=(csv_row or {}).get("location name") or shef_id,
            csv_location_name=(csv_row or {}).get("location name") or None,
            error=str(exc),
            priority_tier=gauge_priority_tier(csv_row) if csv_row else None,
        )


def discover_flood_categories(*, sample_size: int = 50) -> List[str]:
    rows = _load_csv_rows()
    in_service = [r for r in rows if _row_in_service(r) and (r.get("nws shef id") or "").strip()]
    if not in_service:
        return sorted(NWPS_FLOOD_CATEGORIES_KNOWN)

    step = max(1, len(in_service) // sample_size)
    sample_rows = [in_service[i] for i in range(0, len(in_service), step)][:sample_size]
    random.shuffle(sample_rows)

    for row in sample_rows:
        shef_id = (row.get("nws shef id") or "").strip()
        if not shef_id:
            continue
        try:
            payload = _request_json(f"{NWPS_API_BASE}/{shef_id}")
            observed = payload.get("status", {}).get("observed") or {}
            forecast = payload.get("status", {}).get("forecast") or {}
            _record_category(observed.get("floodCategory"))
            _record_category(forecast.get("floodCategory"))
        except Exception:
            continue

    all_cats = set(NWPS_FLOOD_CATEGORIES_KNOWN) | _discovered_categories
    return sorted(all_cats)


def fetch_nwps_county_gauges(
    *,
    county: str,
    state: str,
    gauge_ids: Optional[Sequence[str]] = None,
    discover_categories: bool = False,
) -> SourceResult:
    county_norm = normalize_county_name(county)
    state_norm = normalize_state(state)

    if discover_categories and not _discovered_categories:
        discover_flood_categories()

    selected_rows, match_count, truncated = resolve_shef_ids_from_csv(
        county_norm, state_norm, gauge_ids=gauge_ids
    )
    shef_to_row = {
        (row.get("nws shef id") or "").strip().upper(): row for row in selected_rows
    }
    shef_ids = [(row.get("nws shef id") or "").strip() for row in selected_rows]
    shef_ids = [sid for sid in shef_ids if sid]

    gauges: List[GaugeReading] = []
    fetch_errors: List[Dict[str, str]] = []

    if shef_ids:
        workers = min(DEFAULT_WORKERS, max(1, len(shef_ids)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_fetch_one, sid, shef_to_row.get(sid.upper())): sid
                for sid in shef_ids
            }
            for future in as_completed(futures):
                sid = futures[future]
                reading = future.result()
                if reading.error:
                    fetch_errors.append({"shef_id": sid, "error": reading.error})
                gauges.append(reading)

        gauges.sort(key=lambda g: (g.priority_tier or 99, g.shef_id))

    categories = sorted(set(NWPS_FLOOD_CATEGORIES_KNOWN) | _discovered_categories)

    return SourceResult(
        source_id="nwps",
        gauges=gauges,
        fetch_errors=fetch_errors,
        discovered_flood_categories=categories,
        metadata={
            "api_base": NWPS_API_BASE,
            "csv_match_count": match_count,
            "csv_truncated": truncated or match_count > len(shef_ids),
            "csv_path": str(CSV_PATH),
        },
    )


class NwpsFloodSource:
    source_id = "nwps"

    def fetch(
        self,
        *,
        county: str,
        state: str,
        fips: Optional[str] = None,
        gauge_ids: Optional[Sequence[str]] = None,
        site_ids: Optional[Sequence[str]] = None,
        discover_categories: bool = False,
    ) -> SourceResult:
        return fetch_nwps_county_gauges(
            county=county,
            state=state,
            gauge_ids=gauge_ids,
            discover_categories=discover_categories,
        )
