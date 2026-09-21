"""County → API parameter resolution for NWS alerts and FEMA disasters."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from county_flood.geo import county_match_key, normalize_county_name, normalize_state

NWS_ALERTS_BASE = "https://api.weather.gov/alerts/active"
NWS_ZONE_BASE = "https://api.weather.gov/zones/county"
FEMA_DISASTERS_BASE = (
    "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
)
FLOOD_WARNING_EVENT = "Flood Warning"
REQUEST_TIMEOUT = 30
RECENT_DISASTER_SCORE = 2.0
ACTIVE_FLOOD_WARNING_SCORE = 2.0

_FETCH_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OpenClaw-county-flood-data/1.0",
}

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "disaster-scout"
    / "counties"
    / "county_flood_registry.json"
)

_registry_cache: Optional[Dict[str, Any]] = None


def _load_registry() -> Dict[str, Any]:
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    if not _REGISTRY_PATH.is_file():
        _registry_cache = {}
        return _registry_cache
    try:
        with _REGISTRY_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload.get("counties"), dict):
            _registry_cache = payload["counties"]
        elif isinstance(payload, dict):
            _registry_cache = {
                k: v for k, v in payload.items() if k.isdigit() and len(k) == 5
            }
        else:
            _registry_cache = {}
    except (json.JSONDecodeError, OSError):
        _registry_cache = {}
    return _registry_cache


def _registry_entry(fips: Optional[str]) -> Optional[Dict[str, Any]]:
    if not fips:
        return None
    entry = _load_registry().get(str(fips).zfill(5))
    return entry if isinstance(entry, dict) else None


def derive_nws_zone(fips: str, state: str) -> str:
    """Derive NWS UGC county zone: {ST}C{last 3 FIPS digits}."""
    abbrev = normalize_state(state)
    suffix = str(fips).zfill(5)[-3:]
    return f"{abbrev}C{suffix}"


def resolve_fema_fips_parts(fips: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Split 5-digit FIPS into fips_state_code and fips_county_code."""
    if not fips:
        return None, None
    padded = str(fips).zfill(5)
    if len(padded) != 5 or not padded.isdigit():
        return None, None
    return padded[:2], padded[2:]


def _validate_nws_zone(zone: str) -> bool:
    url = f"{NWS_ZONE_BASE}/{zone}"
    try:
        req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def resolve_nws_zone(
    *,
    fips: Optional[str],
    state: str,
    county: str,
    validate: bool = False,
) -> Tuple[Optional[str], str]:
    """
    Resolve NWS UGC zone for a county.

    Returns (zone_code, resolution_method).
  """
    state_norm = normalize_state(state)
    entry = _registry_entry(fips)
    if entry:
        zone = entry.get("nws_zone")
        if not zone:
            zones = entry.get("nws_zones") or []
            zone = zones[0] if zones else None
        if zone:
            zone = str(zone).upper()
            if validate and not _validate_nws_zone(zone):
                pass
            else:
                return zone, "registry"

    if fips:
        zone = derive_nws_zone(fips, state_norm)
        if validate and not _validate_nws_zone(zone):
            return zone, "derived_unvalidated"
        return zone, "derived"

    # No FIPS — cannot derive zone reliably
    _ = normalize_county_name(county)
    return None, "unavailable"


def build_nws_zone_url(nws_zone: str) -> str:
    """Build NWS county zone metadata URL."""
    return f"{NWS_ZONE_BASE}/{nws_zone}"


def recent_disaster_date_window(
    *,
    reference: Optional[datetime] = None,
) -> Tuple[date, date]:
    """Return inclusive UTC date window for current and previous calendar day."""
    now = reference or datetime.now(timezone.utc)
    today = now.date()
    return today - timedelta(days=1), today


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_recent_disaster_date(
    declaration_date: Optional[str],
    *,
    reference: Optional[datetime] = None,
) -> bool:
    """True when declarationDate falls on today or yesterday (UTC)."""
    parsed = _parse_iso_date(declaration_date)
    if parsed is None:
        return False
    start, end = recent_disaster_date_window(reference=reference)
    return start <= parsed <= end


def resolve_api_urls(
    *,
    county: str,
    state: str,
    fips: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve county-scoped API URLs for NWS alerts/zone and FEMA disasters."""
    county_norm = normalize_county_name(county)
    state_norm = normalize_state(state)
    nws_zone, zone_resolution = resolve_nws_zone(
        fips=fips, state=state_norm, county=county_norm
    )
    nws_alerts_url, alerts_resolution = build_nws_alerts_url(
        state=state_norm, nws_zone=nws_zone
    )
    _, fips_county_code = resolve_fema_fips_parts(fips)
    start, end = recent_disaster_date_window()
    fema_disasters_url, fema_resolution = build_fema_disasters_url(
        state=state_norm,
        fips_county_code=fips_county_code,
        declaration_start=start,
        declaration_end=end,
    )
    return {
        "nws_zone": nws_zone,
        "nws_zone_url": build_nws_zone_url(nws_zone) if nws_zone else None,
        "nws_alerts_url": nws_alerts_url,
        "nws_alerts_base": NWS_ALERTS_BASE,
        "nws_zone_base": NWS_ZONE_BASE,
        "fema_disasters_url": fema_disasters_url,
        "zone_resolution": zone_resolution,
        "alerts_url_resolution": alerts_resolution,
        "fema_url_resolution": fema_resolution,
        "fips_county_code": fips_county_code,
        "recent_disaster_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    }


def build_nws_alerts_url(
    *,
    state: str,
    nws_zone: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Build NWS active alerts URL for Flood Warning events.

    Returns (url, resolution_method).
    """
    params: Dict[str, str] = {"event": FLOOD_WARNING_EVENT}
    if nws_zone:
        params["zone"] = nws_zone
        query = urllib.parse.urlencode(params)
        return f"{NWS_ALERTS_BASE}?{query}", "zone"

    params["area"] = normalize_state(state)
    query = urllib.parse.urlencode(params)
    return f"{NWS_ALERTS_BASE}?{query}", "state_fallback"


def build_fema_disasters_url(
    *,
    state: str,
    fips_county_code: Optional[str] = None,
    top: int = 5,
    declaration_start: Optional[date] = None,
    declaration_end: Optional[date] = None,
) -> Tuple[str, str]:
    """
    Build FEMA OpenFEMA disaster declarations URL for flood incidents.

    Returns (url, resolution_method).
    """
    state_norm = normalize_state(state)
    filters = [
        f"state eq '{state_norm}'",
        "incidentType eq 'Flood'",
    ]
    method = "state"
    if fips_county_code:
        filters.append(f"fipsCountyCode eq '{fips_county_code}'")
        method = "county"

    if declaration_start is not None:
        filters.append(f"declarationDate ge '{declaration_start.isoformat()}'")
        method = f"{method}_recent"
    if declaration_end is not None:
        end_exclusive = declaration_end + timedelta(days=1)
        filters.append(f"declarationDate lt '{end_exclusive.isoformat()}'")

    filter_expr = " and ".join(filters)
    params = {
        "$filter": filter_expr,
        "$orderby": "declarationDate desc",
        "$top": str(top),
        "$format": "json",
    }
    query = urllib.parse.urlencode(params)
    return f"{FEMA_DISASTERS_BASE}?{query}", method


def county_matches_area_desc(area_desc: str, county: str) -> bool:
    """Check if NWS areaDesc mentions the target county."""
    county_key = county_match_key(county)
    if not county_key:
        return False
    for part in area_desc.split(";"):
        part_norm = county_match_key(part.split(",")[0].strip())
        if part_norm == county_key:
            return True
    return False


def county_matches_fema_designated_area(designated_area: str, county: str) -> bool:
    """Check if FEMA designatedArea matches the target county."""
    county_norm = normalize_county_name(county)
    if not county_norm:
        return False
    area = designated_area.strip()
    expected = f"{county_norm} (County)"
    return area.lower() == expected.lower() or county_norm.lower() in area.lower()
