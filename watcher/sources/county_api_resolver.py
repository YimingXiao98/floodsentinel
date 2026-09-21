"""Resolve county FIPS to USGS NWIS site IDs and NWS alert zone codes."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from watcher.geo_util import normalize_county_name, normalize_state

_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_COUNTY_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "disaster-scout"
    / "counties"
)
_COUNTY_OVERRIDES_DIR = _COUNTY_DIR
_COUNTY_REGISTRY_PATH = _COUNTY_DIR / "county_flood_registry.json"

_registry_cache: Optional[Dict[str, Any]] = None

USGS_SITE_URL = "https://waterservices.usgs.gov/nwis/site/"
USGS_IV_URL_TEMPLATE = (
    "https://waterservices.usgs.gov/nwis/iv/"
    "?format=json&sites={sites}&parameterCd=00060,00065&siteStatus=active"
)
NWS_ALERTS_URL_TEMPLATE = "https://api.weather.gov/alerts/active?zone={zone}"
NWS_ZONE_URL_TEMPLATE = "https://api.weather.gov/zones/county/{zone}"

MAX_USGS_SITES = 10
CACHE_TTL_SECONDS = 86400  # 24h

_FETCH_HEADERS = {
    "User-Agent": "DisasterSentinel/1.0 (flood monitoring)",
    "Accept": "application/json",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _http_get_json(url: str, *, timeout: int = 30) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def _cache_path(county_fips: str) -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / f"gauge_cache_{county_fips}.json"


def _load_cache(county_fips: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(county_fips)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(county_fips: str, data: Dict[str, Any]) -> None:
    path = _cache_path(county_fips)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _cache_fresh(cache: Dict[str, Any]) -> bool:
    resolved_at = cache.get("resolved_at")
    if not resolved_at:
        return False
    try:
        ts = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < CACHE_TTL_SECONDS
    except ValueError:
        return False


def _load_registry() -> Dict[str, Any]:
    """Load nationwide county flood registry (FIPS-keyed)."""
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    if not _COUNTY_REGISTRY_PATH.exists():
        _registry_cache = {}
        return _registry_cache
    try:
        with _COUNTY_REGISTRY_PATH.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload.get("counties"), dict):
            _registry_cache = payload["counties"]
        elif isinstance(payload, dict):
            _registry_cache = {k: v for k, v in payload.items() if k.isdigit() and len(k) == 5}
        else:
            _registry_cache = {}
    except (json.JSONDecodeError, OSError):
        _registry_cache = {}
    return _registry_cache


def _load_registry_entry(county_fips: str) -> Optional[Dict[str, Any]]:
    """Lookup curated mapping for a 5-digit county FIPS."""
    fips = str(county_fips).zfill(5)
    entry = _load_registry().get(fips)
    return entry if isinstance(entry, dict) else None


def _sites_from_registry_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    sites: List[Dict[str, Any]] = []
    for item in entry.get("usgs_sites") or []:
        if isinstance(item, dict):
            site_no = str(item.get("site_no") or item.get("id") or "")
            if site_no:
                sites.append(
                    {
                        "site_no": site_no,
                        "name": item.get("name") or site_no,
                        "lat": item.get("lat"),
                        "lon": item.get("lon"),
                    }
                )
        elif item:
            sites.append({"site_no": str(item), "name": str(item), "lat": None, "lon": None})
    return sites[:MAX_USGS_SITES]


def _nws_from_registry_entry(entry: Dict[str, Any], county_fips: str, state: str) -> Optional[Dict[str, Any]]:
    zone = entry.get("nws_zone")
    if not zone:
        zones = entry.get("nws_zones") or []
        zone = zones[0] if zones else None
    if not zone:
        return None
    zone = str(zone).upper()
    validated = _validate_nws_zone_direct(zone)
    return validated or {
        "zone": zone,
        "zone_url": NWS_ZONE_URL_TEMPLATE.format(zone=zone),
        "label": entry.get("county_name") or zone,
        "validated": False,
        "validation_method": "registry",
    }


def _load_yaml_override(county_fips: str) -> Optional[Dict[str, Any]]:
    """Load optional per-county YAML override if present."""
    for name in (f"{county_fips}.yaml", f"{county_fips}.yml"):
        path = _COUNTY_OVERRIDES_DIR / name
        if not path.exists():
            continue
        try:
            import yaml  # type: ignore

            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _state_abbrev(state: str) -> str:
    return normalize_state(state)


def _derive_nws_zone(county_fips: str, state: str) -> str:
    """Derive NWS UGC county zone: {ST}C{last 3 FIPS digits}."""
    abbrev = _state_abbrev(state)
    suffix = county_fips[-3:] if len(county_fips) >= 3 else county_fips
    return f"{abbrev}C{suffix}"


def _county_name_matches(label: str, county_name: str) -> bool:
    label_l = label.lower()
    county_l = normalize_county_name(county_name).lower()
    if not county_l:
        return False
    return county_l in label_l or f"{county_l} county" in label_l


def _parse_usgs_site_json(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    sites: List[Dict[str, Any]] = []
    value = payload.get("value") or {}
    for entry in value.get("site") or []:
        site_codes = entry.get("siteCode") or []
        site_no = None
        for code in site_codes:
            if code.get("network") == "NWIS" or code.get("agencyCode") == "USGS":
                site_no = code.get("value")
                break
        if not site_no:
            site_no = site_codes[0].get("value") if site_codes else None
        if not site_no:
            continue

        geo = entry.get("geoLocation", {}).get("geogLocation", {})
        lat = geo.get("latitude")
        lon = geo.get("longitude")
        sites.append(
            {
                "site_no": str(site_no),
                "name": entry.get("siteName") or str(site_no),
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
            }
        )
    return sites


def _parse_usgs_site_rdb(text: str) -> List[Dict[str, Any]]:
    """Parse USGS NWIS site RDB (tab-separated) response."""
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if len(lines) < 3:
        return []

    headers = lines[0].split("\t")
    data_lines = lines[2:]
    sites: List[Dict[str, Any]] = []
    for row in data_lines:
        cols = row.split("\t")
        if len(cols) < len(headers):
            continue
        record = dict(zip(headers, cols))
        site_no = (record.get("site_no") or "").strip()
        if not site_no:
            continue
        lat = record.get("dec_lat_va")
        lon = record.get("dec_long_va")
        sites.append(
            {
                "site_no": site_no,
                "name": (record.get("station_nm") or site_no).strip(),
                "lat": float(lat) if lat and lat.strip() else None,
                "lon": float(lon) if lon and lon.strip() else None,
            }
        )
    return sites


def _http_get_text(url: str, *, timeout: int = 30) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def discover_usgs_sites(county_fips: str) -> List[Dict[str, Any]]:
    """Discover active stream gauges for a county FIPS via NWIS site service."""
    params = urllib.parse.urlencode(
        {
            "format": "rdb",
            "countyCd": county_fips,
            "siteType": "ST",
            "siteStatus": "active",
        }
    )
    text = _http_get_text(f"{USGS_SITE_URL}?{params}")
    if text:
        sites = _parse_usgs_site_rdb(text)
        if sites:
            return sites[:MAX_USGS_SITES]

    json_params = urllib.parse.urlencode(
        {
            "format": "json",
            "countyCd": county_fips,
            "siteType": "ST",
            "siteStatus": "active",
        }
    )
    payload = _http_get_json(f"{USGS_SITE_URL}?{json_params}")
    if payload:
        return _parse_usgs_site_json(payload)[:MAX_USGS_SITES]
    return []


def _validate_nws_zone_direct(zone: str) -> Optional[Dict[str, Any]]:
    payload = _http_get_json(NWS_ZONE_URL_TEMPLATE.format(zone=zone))
    if not payload:
        return None
    props = payload.get("properties") or {}
    return {
        "zone": zone,
        "zone_url": NWS_ZONE_URL_TEMPLATE.format(zone=zone),
        "label": props.get("name") or zone,
        "validated": True,
        "validation_method": "direct_zone_lookup",
    }


def _discover_nws_zone_fallback(
    county_fips: str,
    county_name: str,
    state: str,
) -> Optional[Dict[str, Any]]:
    abbrev = _state_abbrev(state)
    payload = _http_get_json(
        f"https://api.weather.gov/zones?type=county&area={abbrev}&limit=500"
    )
    if not payload:
        return None

    fips_suffix = county_fips[-3:] if len(county_fips) >= 3 else county_fips
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        zone_id = (props.get("id") or props.get("@id") or "").split("/")[-1]
        zone_name = props.get("name") or ""
        same = str(props.get("SAME") or "")
        fips_code = str(props.get("FIPS") or props.get("COUNTYFIPS") or "")

        if fips_code and fips_code.zfill(5) == county_fips.zfill(5):
            return {
                "zone": zone_id,
                "zone_url": NWS_ZONE_URL_TEMPLATE.format(zone=zone_id),
                "label": zone_name,
                "validated": True,
                "validation_method": "zones_list_fips_match",
            }
        if same and same.endswith(fips_suffix) and _county_name_matches(zone_name, county_name):
            return {
                "zone": zone_id,
                "zone_url": NWS_ZONE_URL_TEMPLATE.format(zone=zone_id),
                "label": zone_name,
                "validated": True,
                "validation_method": "zones_list_name_match",
            }
        if _county_name_matches(zone_name, county_name):
            return {
                "zone": zone_id,
                "zone_url": NWS_ZONE_URL_TEMPLATE.format(zone=zone_id),
                "label": zone_name,
                "validated": True,
                "validation_method": "zones_list_name_match",
            }
    return None


def discover_nws_zone(
    county_fips: str,
    county_name: str,
    state: str,
) -> Optional[Dict[str, Any]]:
    """Derive and validate NWS county zone for alert filtering."""
    candidate = _derive_nws_zone(county_fips, state)
    direct = _validate_nws_zone_direct(candidate)
    if direct:
        if _county_name_matches(direct.get("label", ""), county_name) or not county_name:
            return direct
    return _discover_nws_zone_fallback(county_fips, county_name, state)


@dataclass
class CountyFloodSourceMapping:
    county_fips: str
    county_name: str
    state: str
    usgs_sites: List[Dict[str, Any]] = field(default_factory=list)
    nws_zone: Optional[Dict[str, Any]] = None
    resolved_at: str = ""
    cache_hit: bool = False
    override_applied: bool = False
    resolution_source: str = "auto"

    def usgs_site_ids(self) -> List[str]:
        return [s["site_no"] for s in self.usgs_sites if s.get("site_no")]

    def usgs_fetch_url(self) -> Optional[str]:
        sites = self.usgs_site_ids()
        if not sites:
            return None
        return USGS_IV_URL_TEMPLATE.format(sites=",".join(sites))

    def nws_fetch_url(self) -> Optional[str]:
        if not self.nws_zone or not self.nws_zone.get("zone"):
            return None
        return NWS_ALERTS_URL_TEMPLATE.format(zone=self.nws_zone["zone"])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "county_fips": self.county_fips,
            "county_name": self.county_name,
            "state": self.state,
            "resolved_at": self.resolved_at,
            "cache_hit": self.cache_hit,
            "override_applied": self.override_applied,
            "resolution_source": self.resolution_source,
            "usgs_sites": self.usgs_sites,
            "nws_zone": self.nws_zone,
        }


def resolve_county_flood_sources(
    county_fips: str,
    county_name: str,
    state: str,
    *,
    force_refresh: bool = False,
) -> CountyFloodSourceMapping:
    """Resolve USGS site IDs and NWS zone for a county."""
    fips = str(county_fips).zfill(5)
    county = normalize_county_name(county_name)
    state_norm = normalize_state(state)

    if not force_refresh:
        cached = _load_cache(fips)
        if cached and _cache_fresh(cached):
            return CountyFloodSourceMapping(
                county_fips=fips,
                county_name=cached.get("county_name") or county,
                state=cached.get("state") or state_norm,
                usgs_sites=cached.get("usgs_sites") or [],
                nws_zone=cached.get("nws_zone"),
                resolved_at=cached.get("resolved_at") or "",
                cache_hit=True,
                override_applied=bool(cached.get("override_applied")),
                resolution_source=cached.get("resolution_source") or "cache",
            )

    registry = _load_registry_entry(fips)
    yaml_override = _load_yaml_override(fips)
    usgs_sites: List[Dict[str, Any]] = []
    nws_zone: Optional[Dict[str, Any]] = None
    override_applied = False
    resolution_source = "auto"

    if registry:
        override_applied = True
        resolution_source = "registry"
        county = registry.get("county_name") or county
        state_norm = normalize_state(registry.get("state") or state_norm)
        usgs_sites = _sites_from_registry_entry(registry)
        nws_zone = _nws_from_registry_entry(registry, fips, state_norm)
    elif yaml_override:
        override_applied = True
        resolution_source = "yaml"
        for site_no in yaml_override.get("usgs_sites") or []:
            usgs_sites.append({"site_no": str(site_no), "name": str(site_no), "lat": None, "lon": None})
        zones = yaml_override.get("nws_zones") or []
        if zones:
            zone = str(zones[0]).upper()
            nws_zone = _validate_nws_zone_direct(zone) or {
                "zone": zone,
                "zone_url": NWS_ZONE_URL_TEMPLATE.format(zone=zone),
                "label": zone,
                "validated": False,
                "validation_method": "yaml_override",
            }

    # Always fill up to MAX_USGS_SITES from live discovery when registry/YAML is short.
    if len(usgs_sites) < MAX_USGS_SITES:
        discovered = discover_usgs_sites(fips)
        known = {str(s.get("site_no")) for s in usgs_sites if s.get("site_no")}
        for site in discovered:
            site_no = str(site.get("site_no") or "")
            if not site_no or site_no in known:
                continue
            usgs_sites.append(site)
            known.add(site_no)
            if len(usgs_sites) >= MAX_USGS_SITES:
                break
        if discovered and resolution_source == "registry":
            resolution_source = "registry+usgs_api"
        elif discovered and resolution_source == "yaml":
            resolution_source = "yaml+usgs_api"
        elif usgs_sites and resolution_source == "auto":
            resolution_source = "usgs_api"
    usgs_sites = usgs_sites[:MAX_USGS_SITES]

    if not nws_zone:
        nws_zone = discover_nws_zone(fips, county, state_norm)
        if nws_zone and resolution_source == "auto":
            resolution_source = "nws_api"

    resolved_at = _utc_now()
    mapping = CountyFloodSourceMapping(
        county_fips=fips,
        county_name=county,
        state=state_norm,
        usgs_sites=usgs_sites,
        nws_zone=nws_zone,
        resolved_at=resolved_at,
        cache_hit=False,
        override_applied=override_applied,
        resolution_source=resolution_source,
    )

    _save_cache(fips, mapping.to_dict())
    return mapping


def build_source_url(
    source_type: str,
    template: str,
    mapping: CountyFloodSourceMapping,
) -> tuple[Optional[str], Dict[str, Any]]:
    """Build county-specific fetch URL and api_resolution metadata."""
    strategy = ""
    resolved_ids: Dict[str, Any] = {}
    county_match = False

    if source_type == "USGSGaugeSource":
        strategy = "usgs_nwis_sites_by_county_fips"
        sites = mapping.usgs_site_ids()
        resolved_ids = {"sites": sites}
        county_match = bool(sites)
        if sites:
            url = template.format(sites=",".join(sites))
            return url, {
                "strategy": strategy,
                "county_fips": mapping.county_fips,
                "resolved_ids": resolved_ids,
                "county_match": county_match,
                "cache_hit": mapping.cache_hit,
                "site_count": len(sites),
            }
        return None, {
            "strategy": strategy,
            "county_fips": mapping.county_fips,
            "resolved_ids": resolved_ids,
            "county_match": False,
            "cache_hit": mapping.cache_hit,
            "resolution_error": "no_usgs_sites_found",
        }

    if source_type == "NWSSource":
        strategy = "nws_alerts_by_county_zone"
        zone_info = mapping.nws_zone or {}
        zone = zone_info.get("zone")
        resolved_ids = {"zone": zone, "zone_label": zone_info.get("label")}
        county_match = bool(zone_info.get("validated"))
        if zone:
            url = template.format(zone=zone)
            return url, {
                "strategy": strategy,
                "county_fips": mapping.county_fips,
                "resolved_ids": resolved_ids,
                "county_match": county_match,
                "cache_hit": mapping.cache_hit,
                "validation_method": zone_info.get("validation_method"),
            }
        return None, {
            "strategy": strategy,
            "county_fips": mapping.county_fips,
            "resolved_ids": resolved_ids,
            "county_match": False,
            "cache_hit": mapping.cache_hit,
            "resolution_error": "no_nws_zone_found",
        }

    return None, {"strategy": "unknown", "county_match": False}
