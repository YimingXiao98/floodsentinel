"""FEMA OpenFEMA flood disaster declarations source."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from county_flood.api_resolution import (
    ACTIVE_FLOOD_WARNING_SCORE,
    RECENT_DISASTER_SCORE,
    build_fema_disasters_url,
    county_matches_fema_designated_area,
    is_recent_disaster_date,
    recent_disaster_date_window,
    resolve_fema_fips_parts,
)
from county_flood.geo import normalize_county_name, normalize_state
from county_flood.models import DisasterDeclaration, SourceResult

REQUEST_TIMEOUT = 30
DEFAULT_TOP = 5

_FETCH_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OpenClaw-county-flood-data/1.0",
}


def _request_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _parse_disaster_record(record: Dict[str, Any]) -> DisasterDeclaration:
    declaration_date = record.get("declarationDate")
    recent = is_recent_disaster_date(declaration_date)
    return DisasterDeclaration(
        disaster_number=record.get("disasterNumber"),
        fema_declaration_string=str(record.get("femaDeclarationString") or ""),
        declaration_title=str(record.get("declarationTitle") or ""),
        declaration_date=declaration_date,
        incident_begin_date=record.get("incidentBeginDate"),
        incident_end_date=record.get("incidentEndDate"),
        designated_area=str(record.get("designatedArea") or ""),
        fips_state_code=record.get("fipsStateCode"),
        fips_county_code=record.get("fipsCountyCode"),
        ia_program_declared=record.get("iaProgramDeclared"),
        pa_program_declared=record.get("paProgramDeclared"),
        hm_program_declared=record.get("hmProgramDeclared"),
        flood_state_score=RECENT_DISASTER_SCORE if recent else None,
    )


def _filter_recent_disasters(
    disasters: List[DisasterDeclaration],
) -> List[DisasterDeclaration]:
    return [
        disaster
        for disaster in disasters
        if is_recent_disaster_date(disaster.declaration_date)
    ]


def _filter_disasters_for_county(
    disasters: List[DisasterDeclaration],
    *,
    county: str,
    fips_county_code: Optional[str],
) -> List[DisasterDeclaration]:
    if fips_county_code:
        return disasters
    return [
        disaster
        for disaster in disasters
        if county_matches_fema_designated_area(disaster.designated_area, county)
    ]


def fetch_fema_disasters(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
    top: int = DEFAULT_TOP,
) -> SourceResult:
    county_norm = normalize_county_name(county)
    state_norm = normalize_state(state)
    _, fips_county_code = resolve_fema_fips_parts(fips)
    window_start, window_end = recent_disaster_date_window()
    api_url, url_resolution = build_fema_disasters_url(
        state=state_norm,
        fips_county_code=fips_county_code,
        top=top,
        declaration_start=window_start,
        declaration_end=window_end,
    )

    disasters: List[DisasterDeclaration] = []
    fetch_errors: List[Dict[str, str]] = []

    try:
        payload = _request_json(api_url)
        records = payload.get("DisasterDeclarationsSummaries") or []
        disasters = [_parse_disaster_record(record) for record in records]
        disasters = _filter_recent_disasters(disasters)
        if url_resolution == "state" or url_resolution == "state_recent":
            disasters = _filter_disasters_for_county(
                disasters,
                county=county_norm,
                fips_county_code=fips_county_code,
            )
    except urllib.error.HTTPError as exc:
        fetch_errors.append({"source": "fema_disasters", "error": f"HTTP {exc.code}"})
    except Exception as exc:
        fetch_errors.append({"source": "fema_disasters", "error": str(exc)})

    return SourceResult(
        source_id="fema_disasters",
        disasters=disasters,
        fetch_errors=fetch_errors,
        metadata={
            "api_url": api_url,
            "fips_county_code": fips_county_code,
            "url_resolution": url_resolution,
            "disaster_count": len(disasters),
            "recent_disaster_window": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
            "scored_disaster_count": sum(
                1 for disaster in disasters if disaster.flood_state_score is not None
            ),
        },
    )


class FemaDisastersFloodSource:
    source_id = "fema_disasters"

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
        return fetch_fema_disasters(county=county, state=state, fips=fips)
