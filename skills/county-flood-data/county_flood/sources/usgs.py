"""USGS NWIS IV flood source — county site discovery and gage-height stage scoring."""

from __future__ import annotations

import csv
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from county_flood.geo import county_match_key, normalize_county_name, normalize_state
from county_flood.models import GaugeReading, SourceResult
from county_flood.sources.nwps_enrich import fetch_nwps_payloads
from county_flood.sources.selection import csv_row_to_site_meta, sort_csv_rows
from county_flood.sources.stages import (
    USGS_IV_BATCH_SIZE,
    apply_gauge_limit,
    merge_stages,
    resolve_flood_category,
    stages_from_nwps_payload,
    stages_sufficient_for_classification,
)

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY_PATH = (
    _SKILL_ROOT.parent / "disaster-scout" / "counties" / "county_flood_registry.json"
)
USGS_SITE_URL = "https://waterservices.usgs.gov/nwis/site/"
USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
USGS_DISCHARGE_CODE = "00060"
USGS_GAGE_HEIGHT_CODE = "00065"
REQUEST_TIMEOUT = 30

_FETCH_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OpenClaw-county-flood-data/1.0",
}

_csv_cache: Optional[List[Dict[str, str]]] = None
_registry_cache: Optional[Dict[str, Any]] = None


def _load_csv_rows() -> List[Dict[str, str]]:
    global _csv_cache
    if _csv_cache is not None:
        return _csv_cache
    csv_path = _SKILL_ROOT / "nwps_all_gauges_report.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"Gauge catalog not found: {csv_path}")
    with csv_path.open(newline="", encoding="utf-8") as handle:
        _csv_cache = list(csv.DictReader(handle))
    return _csv_cache


def _load_registry() -> Dict[str, Any]:
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    if not _REGISTRY_PATH.is_file():
        _registry_cache = {}
        return _registry_cache
    try:
        with _REGISTRY_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload.get("counties"), dict):
            _registry_cache = payload["counties"]
        else:
            _registry_cache = {
                key: value
                for key, value in payload.items()
                if isinstance(key, str) and key.isdigit() and len(key) == 5
            }
    except (json.JSONDecodeError, OSError):
        _registry_cache = {}
    return _registry_cache


def _clean_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number in (-999, -9999):
        return None
    return number


def _valid_stage(value: Any) -> Optional[float]:
    from county_flood.sources.stages import clean_stage_value

    return clean_stage_value(value)


def _row_in_service(row: Dict[str, str]) -> bool:
    return str(row.get("in service", "")).strip().lower() == "true"


def _site_no_from_row(row: Dict[str, str]) -> str:
    return (row.get("usgs id") or "").strip()


def _csv_flood_stage_valid(row: Dict[str, str]) -> bool:
    return _valid_stage(row.get("flood stage")) is not None


def resolve_usgs_sites_from_csv(
    county: str,
    state: str,
    *,
    site_ids: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """Return selected site metadata, total match count, truncated flag."""
    rows = _load_csv_rows()

    if site_ids:
        wanted = {site.strip() for site in site_ids if site.strip()}
        matched = [row for row in rows if _site_no_from_row(row) in wanted]
    else:
        county_key = county_match_key(county)
        state_code = normalize_state(state)
        matched = [
            row
            for row in rows
            if normalize_state(row.get("state") or "") == state_code
            and county_match_key(row.get("county") or "") == county_key
            and _row_in_service(row)
            and _site_no_from_row(row)
        ]
        matched = sort_csv_rows(matched)

    selected_rows, truncated = apply_gauge_limit(matched)
    sites = [csv_row_to_site_meta(row) for row in selected_rows]
    return sites, len(matched), truncated


def _registry_sites(fips: Optional[str]) -> List[Dict[str, Any]]:
    if not fips:
        return []
    entry = _load_registry().get(str(fips).zfill(5))
    if not isinstance(entry, dict):
        return []
    sites: List[Dict[str, Any]] = []
    for item in entry.get("usgs_sites") or []:
        if isinstance(item, dict):
            site_no = str(item.get("site_no") or item.get("id") or "").strip()
            if site_no:
                sites.append(
                    {
                        "site_no": site_no,
                        "name": item.get("name") or site_no,
                        "action_stage": _valid_stage(item.get("action_stage")),
                        "flood_stage": _valid_stage(item.get("flood_stage")),
                        "moderate_stage": _valid_stage(item.get("moderate_stage")),
                        "major_stage": _valid_stage(item.get("major_stage")),
                        "stage_unit": item.get("stage_unit") or "ft",
                        "shef_id": item.get("shef_id"),
                        "priority_tier": 1 if item.get("flood_stage") else 3,
                    }
                )
        elif item:
            site_no = str(item).strip()
            if site_no:
                sites.append(
                    {
                        "site_no": site_no,
                        "name": site_no,
                        "action_stage": None,
                        "flood_stage": None,
                        "moderate_stage": None,
                        "major_stage": None,
                        "stage_unit": "ft",
                        "shef_id": None,
                        "priority_tier": 4,
                    }
                )
    return sites


def _request_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _request_text(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def _parse_usgs_site_json(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    sites: List[Dict[str, str]] = []
    for entry in (payload.get("value") or {}).get("site") or []:
        site_codes = entry.get("siteCode") or []
        site_no = None
        for code in site_codes:
            if code.get("network") == "NWIS" or code.get("agencyCode") == "USGS":
                site_no = code.get("value")
                break
        if not site_no and site_codes:
            site_no = site_codes[0].get("value")
        if not site_no:
            continue
        sites.append(
            {
                "site_no": str(site_no),
                "name": entry.get("siteName") or str(site_no),
            }
        )
    return sites


def _parse_usgs_site_rdb(text: str) -> List[Dict[str, str]]:
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if len(lines) < 3:
        return []
    headers = lines[0].split("\t")
    sites: List[Dict[str, str]] = []
    for row in lines[2:]:
        cols = row.split("\t")
        if len(cols) < len(headers):
            continue
        record = dict(zip(headers, cols))
        site_no = (record.get("site_no") or "").strip()
        if not site_no:
            continue
        sites.append(
            {
                "site_no": site_no,
                "name": (record.get("station_nm") or site_no).strip(),
                # Which watershed the gauge drains, used to find hydrologically
                # related gauges when a county has too few of its own.
                "huc_cd": (record.get("huc_cd") or "").strip(),
                "lat": _clean_number(record.get("dec_lat_va")),
                "lon": _clean_number(record.get("dec_long_va")),
            }
        )
    return sites


def discover_usgs_sites_by_fips(fips: str) -> List[Dict[str, Any]]:
    county_fips = str(fips).zfill(5)
    params = urllib.parse.urlencode(
        {
            "format": "rdb",
            "countyCd": county_fips,
            "siteType": "ST",
            "siteStatus": "active",
        }
    )
    text = _request_text(f"{USGS_SITE_URL}?{params}")
    discovered: List[Dict[str, str]] = []
    if text:
        discovered = _parse_usgs_site_rdb(text)
    if not discovered:
        json_params = urllib.parse.urlencode(
            {
                "format": "json",
                "countyCd": county_fips,
                "siteType": "ST",
                "siteStatus": "active",
            }
        )
        payload = _request_json(f"{USGS_SITE_URL}?{json_params}")
        discovered = _parse_usgs_site_json(payload)

    return [
        {
            "site_no": site["site_no"],
            "name": site["name"],
            "action_stage": None,
            "flood_stage": None,
            "moderate_stage": None,
            "major_stage": None,
            "stage_unit": "ft",
            "shef_id": None,
            "priority_tier": 4,
            "huc_cd": site.get("huc_cd") or "",
            "lat": site.get("lat"),
            "lon": site.get("lon"),
        }
        for site in discovered
    ]


#: Subbasin level. USGS returns codes at several resolutions in the same column;
#: eight digits is the standard subbasin and the level a flood signal travels
#: across, so longer codes are truncated to it and shorter ones ignored.
_HUC_SUBBASIN_DIGITS = 8

#: Below this many scorable gauges a county is not decidable on its own, and the
#: watershed is searched for substitutes.
MIN_SCORABLE_SITES = 3


def subbasin_codes(sites: Sequence[Dict[str, Any]]) -> List[str]:
    """The distinct subbasins a set of gauges drains."""
    out: List[str] = []
    for site in sites:
        code = str(site.get("huc_cd") or "").strip()
        if len(code) < _HUC_SUBBASIN_DIGITS:
            continue
        code = code[:_HUC_SUBBASIN_DIGITS]
        if code not in out:
            out.append(code)
    return out


#: How far a substitute gauge may sit from the county's nearest own gauge, in km.
#:
#: Shared drainage alone is too weak a test. A subbasin in flat coastal terrain
#: can span a hundred kilometres and more, so a gauge can share a county's
#: hydrologic unit while describing a river that county has never seen. Distance
#: is what keeps the substitute about the same water.
MAX_NEIGHBOUR_DISTANCE_KM = 60.0


def _haversine_km(a_lat, a_lon, b_lat, b_lon) -> Optional[float]:
    if None in (a_lat, a_lon, b_lat, b_lon):
        return None
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def nearest_own_gauge_km(
    site: Dict[str, Any], own_sites: Sequence[Dict[str, Any]]
) -> Optional[float]:
    """Distance from a candidate to the closest gauge the county already has."""
    distances = [
        d
        for own in own_sites
        if (
            d := _haversine_km(
                site.get("lat"), site.get("lon"), own.get("lat"), own.get("lon")
            )
        )
        is not None
    ]
    return min(distances) if distances else None


def discover_watershed_neighbour_sites(
    *,
    own_sites: Sequence[Dict[str, Any]],
    max_subbasins: int = 4,
    max_distance_km: float = MAX_NEIGHBOUR_DISTANCE_KM,
) -> List[Dict[str, Any]]:
    """Active gauges near the county that drain the same subbasin.

    Used only where the county's own gauges are too few to decide on. Sharing a
    subbasin establishes that the water is connected; distance establishes that
    it is nearby, and both are required. The returned sites are marked so that
    everything downstream can tell they describe connected water rather than
    water in the monitored county.
    """
    own_ids = {str(s.get("site_no")) for s in own_sites if s.get("site_no")}
    neighbours: Dict[str, Dict[str, Any]] = {}
    for code in subbasin_codes(own_sites)[:max_subbasins]:
        params = urllib.parse.urlencode(
            {
                "format": "rdb",
                "huc": code,
                "siteType": "ST",
                "siteStatus": "active",
            }
        )
        text = _request_text(f"{USGS_SITE_URL}?{params}")
        for site in _parse_usgs_site_rdb(text) if text else []:
            site_no = site["site_no"]
            if site_no in own_ids or site_no in neighbours:
                continue
            distance = nearest_own_gauge_km(site, own_sites)
            if distance is None or distance > max_distance_km:
                continue
            neighbours[site_no] = {
                "site_no": site_no,
                "name": site["name"],
                "lat": site.get("lat"),
                "lon": site.get("lon"),
                "action_stage": None,
                "flood_stage": None,
                "moderate_stage": None,
                "major_stage": None,
                "stage_unit": "ft",
                "shef_id": None,
                "priority_tier": 5,
                "huc_cd": site.get("huc_cd") or code,
                "location_relation": "shared_watershed",
                "location_basis": "shared_watershed",
                "distance_km": round(distance, 1),
            }
    return list(neighbours.values())


def _merge_site_lists(*lists: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for sites in lists:
        for site in sites:
            site_no = str(site.get("site_no") or "").strip()
            if not site_no:
                continue
            if site_no not in merged:
                merged[site_no] = dict(site)
            else:
                existing = merged[site_no]
                for key, value in site.items():
                    if value not in (None, "") and existing.get(key) in (None, ""):
                        existing[key] = value
                if site.get("priority_tier", 99) < existing.get("priority_tier", 99):
                    existing["priority_tier"] = site["priority_tier"]
    out = list(merged.values())
    out.sort(key=lambda s: (s.get("priority_tier", 99), (s.get("name") or "").lower()))
    selected, truncated = apply_gauge_limit(out)
    return selected


# Re-export for backward compatibility
from county_flood.sources.stages import gage_height_flood_category  # noqa: E402


#: Ranked weakest first: a reading marked both provisional and estimated is an
#: estimate. Mirrors watcher.claims.confidence, kept local because skills do not
#: import watcher.
_QUALIFIER_RANK = ("e", "P", "R", "A")


def _worst_qualifier(qualifiers: Any) -> Optional[str]:
    """The weakest code attached to one observation, or None."""
    if not qualifiers:
        return None
    codes = [str(q) for q in qualifiers] if not isinstance(qualifiers, str) else [qualifiers]
    for code in _QUALIFIER_RANK:
        if code in codes:
            return code
    return codes[0] if codes else None


def _parse_nwis_iv(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    stations: Dict[str, Dict[str, Any]] = {}
    for item in (payload.get("value") or {}).get("timeSeries") or []:
        site_codes = item.get("sourceInfo", {}).get("siteCode") or []
        if not site_codes:
            continue
        site_no = str(site_codes[0].get("value") or "")
        if not site_no:
            continue

        if site_no not in stations:
            stations[site_no] = {
                "site_no": site_no,
                "station_name": item.get("sourceInfo", {}).get("siteName") or site_no,
                "observation_time": None,
                "streamflow_cfs": None,
                "gage_height_ft": None,
                "data_qualifier": None,
            }

        variable_codes = item.get("variable", {}).get("variableCode") or []
        parameter_code = variable_codes[0].get("value") if variable_codes else None
        values = item.get("values") or []
        if not values:
            continue
        observations = values[0].get("value") or []
        if not observations:
            continue

        latest = observations[-1]
        observed_value = _clean_number(latest.get("value"))
        observed_time = latest.get("dateTime")
        if observed_time:
            stations[site_no]["observation_time"] = observed_time

        # What the agency says about this reading. Stage is the measurement the
        # flood state is decided from, so its qualifier is the one that counts;
        # discharge only fills in when stage carries none.
        qualifier = _worst_qualifier(latest.get("qualifiers"))
        if qualifier and (
            parameter_code == USGS_GAGE_HEIGHT_CODE
            or not stations[site_no]["data_qualifier"]
        ):
            stations[site_no]["data_qualifier"] = qualifier

        if parameter_code == USGS_DISCHARGE_CODE:
            stations[site_no]["streamflow_cfs"] = observed_value
        elif parameter_code == USGS_GAGE_HEIGHT_CODE:
            stations[site_no]["gage_height_ft"] = observed_value

    return stations


def fetch_usgs_iv(site_nos: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not site_nos:
        return {}
    merged: Dict[str, Dict[str, Any]] = {}
    ids = list(site_nos)
    for start in range(0, len(ids), USGS_IV_BATCH_SIZE):
        chunk = ids[start : start + USGS_IV_BATCH_SIZE]
        params = urllib.parse.urlencode(
            {
                "format": "json",
                "sites": ",".join(chunk),
                "parameterCd": f"{USGS_DISCHARGE_CODE},{USGS_GAGE_HEIGHT_CODE}",
                "siteStatus": "active",
            }
        )
        payload = _request_json(f"{USGS_IV_URL}?{params}")
        merged.update(_parse_nwis_iv(payload))
    return merged


#: Fields the gauge table supplies that discovery cannot.
_CSV_THRESHOLD_FIELDS = (
    "shef_id",
    "action_stage",
    "flood_stage",
    "moderate_stage",
    "major_stage",
    "stage_unit",
    "priority_tier",
)


def _apply_csv_thresholds(sites: List[Dict[str, Any]]) -> None:
    """Fill thresholds for sites looked up by number rather than by county."""
    wanted = [str(s.get("site_no")) for s in sites if s.get("site_no")]
    if not wanted:
        return
    known, _, _ = resolve_usgs_sites_from_csv("", "", site_ids=wanted)
    by_site = {str(k.get("site_no")): k for k in known if k.get("site_no")}
    for site in sites:
        match = by_site.get(str(site.get("site_no")))
        if not match:
            continue
        for field in _CSV_THRESHOLD_FIELDS:
            if match.get(field) is not None:
                site[field] = match[field]


def _enrich_sites_from_nwps(sites: List[Dict[str, Any]]) -> None:
    need_enrich = [
        s
        for s in sites
        if not stages_sufficient_for_classification(s)
        and (s.get("shef_id") or "").strip()
    ]
    shef_ids = [(s.get("shef_id") or "").strip() for s in need_enrich]
    payloads = fetch_nwps_payloads(shef_ids)
    for site in need_enrich:
        shef = (site.get("shef_id") or "").strip().upper()
        payload = payloads.get(shef)
        if not payload:
            continue
        site_stages = {
            "action_stage": site.get("action_stage"),
            "flood_stage": site.get("flood_stage"),
            "moderate_stage": site.get("moderate_stage"),
            "major_stage": site.get("major_stage"),
        }
        api_stages = stages_from_nwps_payload(payload)
        merged, source = merge_stages(site_stages, api_stages)
        site.update(merged)
        if site.get("threshold_source") in (None, "none", "csv"):
            site["threshold_source"] = source
        observed = (payload.get("status") or {}).get("observed") or {}
        site["_nwps_category"] = observed.get("floodCategory")


def _site_to_reading(
    site: Dict[str, Any],
    observation: Optional[Dict[str, Any]],
    *,
    error: Optional[str] = None,
) -> GaugeReading:
    site_no = str(site.get("site_no") or "")
    name = str(site.get("name") or site_no)
    gage_height = None if not observation else observation.get("gage_height_ft")
    obs_time = None if not observation else observation.get("observation_time")

    threshold_source = site.get("threshold_source") or "csv"
    nwps_category = site.get("_nwps_category")

    result = resolve_flood_category(
        observed_primary=gage_height,
        nwps_category=nwps_category,
        action_stage=site.get("action_stage"),
        flood_stage=site.get("flood_stage"),
        moderate_stage=site.get("moderate_stage"),
        major_stage=site.get("major_stage"),
        threshold_source=threshold_source,
    )

    return GaugeReading(
        source="usgs",
        shef_id=site.get("shef_id") or site_no,
        site_no=site_no,
        name=name if not observation else str(observation.get("station_name") or name),
        observed_primary=gage_height,
        observed_primary_unit=site.get("stage_unit") or "ft",
        observed_flood_category=result.category,
        observed_valid_time=obs_time,
        flood_state_score=result.score,
        streamflow_cfs=None if not observation else observation.get("streamflow_cfs"),
        action_stage=site.get("action_stage"),
        flood_stage=site.get("flood_stage"),
        moderate_stage=site.get("moderate_stage"),
        major_stage=site.get("major_stage"),
        csv_location_name=site.get("name"),
        error=error,
        classification_method=result.method,
        threshold_source=result.threshold_source,
        data_qualifier=None if not observation else observation.get("data_qualifier"),
        location_relation=site.get("location_relation"),
        location_basis=site.get("location_basis"),
        unscored_reason=result.unscored_reason,
        priority_tier=site.get("priority_tier"),
    )


def select_county_usgs_sites(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
    site_ids: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    county_norm = normalize_county_name(county)
    state_norm = normalize_state(state)

    csv_sites, csv_match_count, csv_truncated = resolve_usgs_sites_from_csv(
        county_norm,
        state_norm,
        site_ids=site_ids,
    )
    registry_sites = _registry_sites(fips)
    discovered_sites = discover_usgs_sites_by_fips(fips) if fips and not site_ids else []

    if site_ids:
        selected_sites = csv_sites
        truncated = csv_truncated
    else:
        selected_sites = _merge_site_lists(csv_sites, registry_sites, discovered_sites)
        truncated = csv_truncated or len(discovered_sites) > 0

    # Record how each site was tied to the county. A site returned by a query
    # keyed on the county's FIPS code is one the agency places in the county;
    # one matched only on the county's name is usually the same site, but name
    # matching is fallible in a way FIPS is not.
    by_fips = {
        str(s.get("site_no"))
        for s in list(registry_sites) + list(discovered_sites)
        if s.get("site_no")
    }
    for site in selected_sites:
        site["location_basis"] = (
            "fips_verified" if str(site.get("site_no")) in by_fips else "name_matched"
        )

    metadata = {
        "csv_match_count": csv_match_count,
        "csv_truncated": truncated,
        "registry_sites": len(registry_sites),
        "discovered_sites": len(discovered_sites),
        "sites_selected": len(selected_sites),
        "fips": fips,
    }
    return selected_sites, metadata


def fetch_usgs_county_gauges(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
    site_ids: Optional[Sequence[str]] = None,
    include_watershed_neighbours: bool = False,
) -> SourceResult:
    selected_sites, site_meta = select_county_usgs_sites(
        county=county,
        state=state,
        fips=fips,
        site_ids=site_ids,
    )

    for site in selected_sites:
        if site.get("threshold_source") is None:
            site["threshold_source"] = "csv"

    _enrich_sites_from_nwps(selected_sites)

    # Whether a county can be decided on is a question of how many of its gauges
    # carry a flood threshold, not how many exist: a county can be dense with
    # sensors and still have nothing to compare a reading against. The count is
    # taken after enrichment, because that is where thresholds arrive.
    neighbour_sites: List[Dict[str, Any]] = []
    if include_watershed_neighbours and not site_ids:
        scorable = [s for s in selected_sites if s.get("flood_stage") is not None]
        if len(scorable) < MIN_SCORABLE_SITES:
            neighbour_sites = discover_watershed_neighbour_sites(
                own_sites=selected_sites
            )
            # Site discovery returns bare identifiers. Thresholds live in the
            # NWS gauge table, which is normally read by county; a neighbour is
            # by definition in another county, so it is looked up by its own
            # site number instead.
            _apply_csv_thresholds(neighbour_sites)
            for site in neighbour_sites:
                site.setdefault("threshold_source", "csv")
            _enrich_sites_from_nwps(neighbour_sites)
            # Only a neighbour that can be scored is worth carrying; an
            # unscorable one adds a fetch and tells the county nothing.
            site_meta["watershed_neighbours_found"] = len(neighbour_sites)
            neighbour_sites = [
                s for s in neighbour_sites if s.get("flood_stage") is not None
            ]
            selected_sites = _merge_site_lists(selected_sites, neighbour_sites)
    site_meta.setdefault("watershed_neighbours_found", 0)
    site_meta["watershed_neighbour_sites"] = len(neighbour_sites)

    site_nos = [site["site_no"] for site in selected_sites if site.get("site_no")]
    fetch_errors: List[Dict[str, str]] = []
    gauges: List[GaugeReading] = []

    observations: Dict[str, Dict[str, Any]] = {}
    if site_nos:
        try:
            observations = fetch_usgs_iv(site_nos)
        except Exception as exc:
            fetch_errors.append({"source": "usgs_iv", "error": str(exc)})
            for site in selected_sites:
                gauges.append(_site_to_reading(site, None, error=str(exc)))
            return SourceResult(
                source_id="usgs",
                gauges=gauges,
                fetch_errors=fetch_errors,
                metadata={
                    "api_base": USGS_IV_URL,
                    "site_service": USGS_SITE_URL,
                    **site_meta,
                },
            )

    for site in selected_sites:
        site_no = site["site_no"]
        observation = observations.get(site_no)
        if not observation:
            gauges.append(_site_to_reading(site, None, error="no_iv_data"))
            fetch_errors.append({"site_no": site_no, "error": "no_iv_data"})
            continue
        gauges.append(_site_to_reading(site, observation))

    gauges.sort(key=lambda gauge: (gauge.priority_tier or 99, gauge.site_no or ""))

    return SourceResult(
        source_id="usgs",
        gauges=gauges,
        fetch_errors=fetch_errors,
        metadata={
            "api_base": USGS_IV_URL,
            "site_service": USGS_SITE_URL,
            **site_meta,
        },
    )


class UsgsFloodSource:
    source_id = "usgs"

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
        selected_site_ids = site_ids or gauge_ids
        return fetch_usgs_county_gauges(
            county=county,
            state=state,
            fips=fips,
            site_ids=selected_site_ids,
        )
