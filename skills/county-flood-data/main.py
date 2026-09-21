#!/usr/bin/env python3
"""CLI entry for county-flood-data skill (NWPS + USGS NWIS IV)."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from county_flood.geo import parse_location
from county_flood.runner import run_county_flood

SOURCE_MODES = {
    "nws": ["nwps"],
    "usgs": ["usgs"],
    "both": ["nwps", "usgs"],
    "all": ["nwps", "usgs", "nws_alerts", "fema_disasters"],
}

DEFAULT_SOURCE_LIST = SOURCE_MODES["all"]


def resolve_sources(args: argparse.Namespace) -> List[str]:
    """Resolve active flood APIs from --source or advanced --sources override."""
    raw = str(getattr(args, "sources", "") or "").strip()
    if raw:
        return [source.strip().lower() for source in raw.split(",") if source.strip()]

    mode = str(getattr(args, "source", "both") or "both").strip().lower()
    return list(SOURCE_MODES.get(mode, DEFAULT_SOURCE_LIST))


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        choices=["nws", "usgs", "both", "all"],
        default="all",
        help="Flood API to query: nws (NOAA NWPS), usgs (USGS NWIS IV), both (gauges only), or all (default, includes NWS alerts + FEMA disasters)",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="Advanced: comma-separated source ids (nwps, usgs, usgs_ogc, nws_alerts, fema_disasters). Overrides --source.",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="County flood data via NOAA NWPS and USGS NWIS IV"
    )
    parser.add_argument("--county", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--fips", default="")
    parser.add_argument(
        "--location",
        default="",
        help='Natural language location, e.g. "Harris County, Texas"',
    )
    parser.add_argument("--gauge-ids", default="", help="Comma-separated NWPS SHEF IDs")
    parser.add_argument(
        "--site-ids",
        default="",
        help="Comma-separated USGS NWIS site numbers",
    )
    add_source_args(parser)
    parser.add_argument("--format", choices=["json", "human"], default="human")
    parser.add_argument(
        "--discover-categories",
        action="store_true",
        help="Sample national gauges to discover NWPS floodCategory values",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    county = args.county.strip()
    state = args.state.strip()
    location = args.location.strip()

    if location and not (county and state):
        parsed = parse_location(location)
        if not parsed:
            print(
                f'ERROR: could not parse location "{location}" (expected "County, ST")',
                file=sys.stderr,
            )
            return 1
        county, state = parsed

    if not county or not state:
        print("ERROR: provide --location or both --county and --state", file=sys.stderr)
        return 1

    gauge_ids = [g.strip() for g in args.gauge_ids.split(",") if g.strip()] or None
    site_ids = [s.strip() for s in args.site_ids.split(",") if s.strip()] or None
    sources = resolve_sources(args)
    run_county_flood(
        county=county,
        state=state,
        fips=args.fips or None,
        location=location or None,
        gauge_ids=gauge_ids,
        site_ids=site_ids,
        sources=sources,
        output_format=args.format,
        discover_categories=args.discover_categories,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
