"""County and state normalization for CSV matching."""

from __future__ import annotations

import re
from pathlib import Path

STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def normalize_county_name(county: str) -> str:
    c = county.strip()
    if c.lower().endswith(" county"):
        return c[: -len(" county")].strip()
    return c


def normalize_state(state: str) -> str:
    s = state.strip()
    if not s:
        return s
    if len(s) == 2:
        return s.upper()
    return STATE_NAME_TO_CODE.get(s.lower(), s.upper() if len(s) <= 3 else s)


def county_match_key(name: str) -> str:
    """Collapse spaces for De Kalb / DeKalb style matching."""
    return re.sub(r"\s+", "", normalize_county_name(name).lower())


def parse_location(location: str) -> tuple[str, str] | None:
    """Parse 'Harris County, Texas' or 'Harris, TX' into county + state."""
    text = location.strip()
    if not text:
        return None

    m = re.match(r"^(.+?)\s+County,\s*([A-Za-z]{2,})$", text, re.I)
    if m:
        return normalize_county_name(m.group(1)), normalize_state(m.group(2))

    m = re.match(r"^(.+?),\s*([A-Za-z]{2,})$", text)
    if m:
        name = m.group(1).strip()
        for suffix in (" County", " Parish", " Borough"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return normalize_county_name(name), normalize_state(m.group(2))

    return None


def resolve_fips(county: str, state: str) -> str | None:
    """Resolve county FIPS via disaster-scout county_resolver when available."""
    county_norm = normalize_county_name(county)
    state_norm = normalize_state(state)
    if not county_norm or not state_norm:
        return None

    services_dir = (
        Path(__file__).resolve().parents[2] / "disaster-scout" / "services"
    )
    if not services_dir.is_dir():
        return None

    import sys

    services_path = str(services_dir)
    if services_path not in sys.path:
        sys.path.insert(0, services_path)

    try:
        from county_resolver import resolve_county  # type: ignore

        resolved = resolve_county(county_norm, state_norm)
        fips = resolved.get("county_fips") if isinstance(resolved, dict) else None
        return str(fips).zfill(5) if fips else None
    except Exception:
        return None
