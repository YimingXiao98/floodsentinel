"""mission-bootstrap — geography resolution only.

Python handles the strict dependency: county + state (+ optional FIPS) →
county_fips, county_name, state, bbox, aoi_id, location string.

All other bootstrap logic (hazard validation, monitoring_summary, Neo4j persist)
lives in SKILL.md and is executed by the agent via watcher.cli shell commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_ROOT = Path(__file__).resolve().parent
_SCOUT_SERVICES = _ROOT.parent / "disaster-scout" / "services"


def _parse_location(location: str) -> Optional[Tuple[str, str]]:
    text = location.strip()
    m = re.match(r"^(.+?)\s+County,\s*([A-Za-z]{2,})$", text, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip().upper()
    m = re.match(r"^(.+?),\s*([A-Za-z]{2,})$", text)
    if m:
        name = m.group(1).strip()
        for suffix in (" County", " Parish", " Borough"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name, m.group(2).strip().upper()
    return None


def _extract_location_from_prompt(prompt: str) -> Optional[str]:
    m = re.search(
        r"(?:watch|monitor)\s+(?:floods?\s+)?(?:in|for)\s+(.+?)(?:\.|$)",
        prompt,
        re.I,
    )
    return m.group(1).strip() if m else None


def resolve_geography(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve county geography. Returns params dict or error envelope."""
    county = str(raw.get("county") or "").strip()
    state = str(raw.get("state") or "").strip()
    fips = str(raw.get("fips") or raw.get("county_fips") or "").strip()
    location = str(raw.get("location") or "").strip()
    prompt = str(raw.get("prompt") or raw.get("command") or "").strip()

    if prompt and not (county and state):
        if not location:
            extracted = _extract_location_from_prompt(prompt)
            if extracted:
                location = extracted
        if location and not (county and state):
            parsed = _parse_location(location)
            if parsed:
                county, state = parsed

    if location and not (county and state):
        parsed = _parse_location(location)
        if parsed:
            county, state = parsed

    if _SCOUT_SERVICES.is_dir() and str(_SCOUT_SERVICES) not in sys.path:
        sys.path.insert(0, str(_SCOUT_SERVICES))

    resolved: Optional[Dict[str, Any]] = None
    try:
        from county_resolver import resolve_county, resolve_from_prompt  # type: ignore

        if fips and len(fips) == 5 and fips.isdigit():
            resolved = resolve_county(county or "", state or "", county_fips=fips)
        elif county and state:
            resolved = resolve_county(county, state)
        elif prompt:
            resolved = resolve_from_prompt(prompt)
    except Exception:
        resolved = None

    if resolved:
        county_fips = resolved["county_fips"]
        county_name = resolved.get("county_name") or county
        state_name = resolved.get("state") or state
        bbox = resolved.get("bbox")
    elif fips and len(fips) == 5 and fips.isdigit():
        county_fips = fips
        county_name = county or f"county_{fips}"
        state_name = state
        bbox = None
    elif county and state:
        return {
            "error": "geocode_error",
            "message": f"Could not resolve FIPS for {county}, {state}; pass fips explicitly",
            "geocode_warning": True,
            "county": county,
            "state": state,
        }
    else:
        return {
            "error": "geocode_error",
            "message": "county and state (or fips, or prompt with location) are required",
        }

    aoi_id = f"aoi_{county_fips}"
    location_str = (
        location
        or f"{county_name} County, {state_name}"
        if state_name
        else county_name
    )

    return {
        "county": county_name,
        "state": state_name,
        "county_fips": county_fips,
        "fips": county_fips,
        "county_name": county_name,
        "aoi_id": aoi_id,
        "location": location_str,
        "bbox": bbox,
    }


try:
    from openclaw.skills import BaseSkill
except ImportError:

    class BaseSkill:
        def __init__(self):
            self.name = "mission-bootstrap"
            self.description = "Resolve county geography for flood mission bootstrap"


class MissionBootstrapSkill(BaseSkill):
    def __init__(self):
        super().__init__()
        self.name = "mission-bootstrap"
        self.description = "Resolve county FIPS and geography for flood mission bootstrap"

    async def perform(self, context: Dict[str, Any] | None = None) -> str:
        return json.dumps(resolve_geography(context or {}), separators=(",", ":"))

    def run(self, *args, **kwargs) -> str:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self.perform(*args, **kwargs))


def _load_file(path: str) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Input file must contain a JSON object")
    return data


def _build_context(argv: Optional[list[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Resolve U.S. county geography (FIPS, bbox, aoi_id) for mission-bootstrap",
    )
    parser.add_argument("--county", default="", help="County name")
    parser.add_argument("--state", default="", help="State name or abbreviation")
    parser.add_argument("--fips", default="", help="5-digit county FIPS")
    parser.add_argument("--location", default="", help="e.g. Harris County, TX")
    parser.add_argument("--file", default="", help="JSON input file")
    parser.add_argument("--prompt", default="", help="Natural-language location request")
    parser.add_argument("--command", default="", help="Slash-style command text")
    args = parser.parse_args(argv)

    ctx = _load_file(args.file) if args.file else {}
    if args.county:
        ctx["county"] = args.county
    if args.state:
        ctx["state"] = args.state
    if args.fips:
        ctx["fips"] = args.fips
    if args.location:
        ctx["location"] = args.location
    prompt = args.prompt or args.command
    if prompt:
        ctx["prompt"] = prompt
    return ctx


skill = MissionBootstrapSkill()


if __name__ == "__main__":
    result = resolve_geography(_build_context())
    print(json.dumps(result, indent=2))
    sys.exit(1 if result.get("error") else 0)
