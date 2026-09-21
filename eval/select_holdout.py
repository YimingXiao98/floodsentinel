"""Mechanical, policy-outcome-blind held-out county-event selection (v3).

Implements the selection rules frozen in ``eval/HYBRID_STUDY_PROTOCOL.md``:
NCEI billion-dollar flood/tropical-cyclone events 2016-2024 processed in
descending CPI-adjusted-cost order, FEMA DR-designated counties ranked by
frozen-registry gauge coverage, v2 corpus building and preflight QC, and a
prespecified stop rule. Every candidate considered and every exclusion reason
is recorded. Nothing in this script observes any policy's behavior.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SKILL_DIR = _ROOT / "skills" / "county-flood-data"
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

NCEI_CSV = _ROOT / "eval" / "corpus" / "study_v3" / "ncei_events_us.csv"
REGISTRY_CSV = _SKILL_DIR / "nwps_all_gauges_report.csv"
CACHE_DIR = _ROOT / "eval" / "corpus" / "study_v3" / "cache"
CORPUS_DIR = _ROOT / "eval" / "corpus" / "study_v3"
SELECTION_OUT = _ROOT / "eval" / "corpus" / "study_v3" / "holdout_selection.json"

FEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
_HEADERS = {"User-Agent": "OpenClaw-disaster-sentinel-eval/1.0"}

DEV_EVENT_NAMES = ("Hurricane Harvey", "Hurricane Irma")
DEV_COUNTY_FIPS = {"48201", "12031", "12057", "12105", "12086"}

MAX_IMPACT_PERIOD_DAYS = 10
WINDOW_BACK_DAYS = 2
WINDOW_FORWARD_DAYS = 10
CANDIDATES_PER_EVENT = 3
STOP_MIN_EPISODES = 10
STOP_MIN_EVENTS = 5
STOP_MAX_COUNTY_EVENTS = 12
MAX_COVERAGE_ONLY = 2
MIN_ACK_COVERAGE = 0.4
DATUM_SCREEN_FLOOD_FRACTION = 0.98

#: Registry vertical datums that mark tidal/coastal thresholds. Stages against
#: a tide datum measure coastal inundation, which every post-storm high tide
#: can cross; the v2 contract this study extends is river-gauge monitoring, so
#: these sites are threshold-excluded symmetrically and omitted from ranking.
TIDAL_DATUM_NAMES = {"MHHW*", "MLLW*", "MHW", "MSL", "HAT"}

FEMA_INCIDENT_TYPES = {
    "hurricane",
    "flood",
    "severe storm",
    "severe storm(s)",
    "tropical storm",
    "coastal storm",
    "dam/levee break",
    "typhoon",
}

_FIPS_TO_USPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}

_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

_STORM_NAME_RE = re.compile(
    r"(?:Hurricane|Tropical Storm|Typhoon)\s+([A-Z][a-z]+)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", "".join(
        ch.lower() if ch.isalnum() else "_" for ch in value
    )).strip("_")


def _parse_ncei_date(raw: str) -> date:
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))


def load_event_frame() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Frame events in descending cost order, plus the excluded rows."""
    lines = NCEI_CSV.read_text(encoding="utf-8").splitlines()
    reader = csv.DictReader(lines[2:])
    frame: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for row in reader:
        name = (row.get("Name") or "").strip()
        disaster = (row.get("Disaster") or "").strip()
        begin_raw = (row.get("Begin Date") or "").strip()
        end_raw = (row.get("End Date") or "").strip()
        if disaster not in ("Flooding", "Tropical Cyclone") or len(begin_raw) < 8:
            continue
        begin = _parse_ncei_date(begin_raw)
        end = _parse_ncei_date(end_raw)
        if not (2016 <= begin.year <= 2024):
            continue
        record = {
            "name": name,
            "disaster": disaster,
            "begin": begin.isoformat(),
            "end": end.isoformat(),
            "impact_period_days": (end - begin).days,
            "cpi_adjusted_cost_musd": float(row.get("CPI-Adjusted Cost") or 0.0),
        }
        if any(dev in name for dev in DEV_EVENT_NAMES):
            excluded.append({**record, "exclusion": "development_event"})
            continue
        if (end - begin).days > MAX_IMPACT_PERIOD_DAYS:
            excluded.append(
                {**record, "exclusion": "impact_period_exceeds_window_convention"}
            )
            continue
        storm_match = _STORM_NAME_RE.search(name)
        record["storm_token"] = (
            storm_match.group(1).upper() if storm_match else None
        )
        record["named_states"] = sorted(
            {
                usps
                for state_name, usps in _STATE_NAMES.items()
                if re.search(rf"\b{state_name}\b", name.lower())
            }
        )
        frame.append(record)
    frame.sort(key=lambda e: (-e["cpi_adjusted_cost_musd"], e["begin"], e["name"]))
    return frame, excluded


def _fema_query(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cached national OpenFEMA query for one event's DR county designations."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"fema_{_slug(event['name'])}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    begin = date.fromisoformat(event["begin"]) - timedelta(days=5)
    end = date.fromisoformat(event["end"]) + timedelta(days=5)
    rows: List[Dict[str, Any]] = []
    skip = 0
    while True:
        params = urllib.parse.urlencode(
            {
                "$filter": (
                    f"declarationType eq 'DR' and "
                    f"incidentBeginDate ge '{begin.isoformat()}T00:00:00.000z' and "
                    f"incidentBeginDate le '{end.isoformat()}T23:59:59.000z'"
                ),
                "$select": (
                    "disasterNumber,declarationType,incidentType,designatedArea,"
                    "fipsStateCode,fipsCountyCode,incidentBeginDate,declarationTitle,state"
                ),
                "$top": "1000",
                "$skip": str(skip),
            }
        )
        req = urllib.request.Request(f"{FEMA_URL}?{params}", headers=_HEADERS)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    payload = json.load(resp)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
        batch = payload.get("DisasterDeclarationsSummaries") or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000
    cache.write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n", "utf-8")
    return rows


def candidate_counties(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Deduplicated candidate counties for one event, per the frozen filters."""
    rows = _fema_query(event)
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if str(row.get("declarationType")) != "DR":
            continue
        if str(row.get("incidentType") or "").strip().lower() not in FEMA_INCIDENT_TYPES:
            continue
        county_code = str(row.get("fipsCountyCode") or "")
        state_code = str(row.get("fipsStateCode") or "")
        if county_code in ("", "000") or state_code not in _FIPS_TO_USPS:
            continue
        title = str(row.get("declarationTitle") or "").upper()
        if event["storm_token"] and event["storm_token"] not in title:
            continue
        usps = _FIPS_TO_USPS[state_code]
        if event["named_states"] and usps not in event["named_states"]:
            continue
        fips = f"{state_code}{county_code}"
        if fips in DEV_COUNTY_FIPS:
            continue
        area = str(row.get("designatedArea") or "")
        county_name = re.sub(r"\s*\((County|Parish|Borough|Municipio)\)\s*$", "", area)
        if "(" in county_name:  # reservations, "(in (P)MSA...)" style areas
            continue
        record = out.setdefault(
            fips,
            {
                "fips": fips,
                "state": usps,
                "county": county_name,
                "disaster_numbers": [],
            },
        )
        record["disaster_numbers"] = sorted(
            set(record["disaster_numbers"]) | {int(row.get("disasterNumber") or 0)}
        )
    return list(out.values())


_REGISTRY_CACHE: Optional[List[Dict[str, str]]] = None


def _registry_rows() -> List[Dict[str, str]]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        with REGISTRY_CSV.open(encoding="utf-8-sig", newline="") as handle:
            _REGISTRY_CACHE = list(csv.DictReader(handle))
    return _REGISTRY_CACHE


def _is_tidal_datum(row: Dict[str, str]) -> bool:
    return (row.get("nrldb vertical datum name") or "").strip() in TIDAL_DATUM_NAMES


def registry_gauge_counts(county: str, state: str) -> Tuple[int, int]:
    """(in-service non-tidal gauges with parseable flood stage, total in-service)."""
    from county_flood.geo import county_match_key, normalize_state

    state_key = normalize_state(state)
    county_key = county_match_key(county)
    with_stage = total = 0
    for row in _registry_rows():
        if normalize_state(row.get("state") or "") != state_key:
            continue
        if county_match_key(row.get("county") or "") != county_key:
            continue
        if str(row.get("in service") or "").strip().lower() != "true":
            continue
        if not str(row.get("usgs id") or "").strip():
            continue
        total += 1
        if _is_tidal_datum(row):
            continue
        try:
            float(str(row.get("flood stage") or "").strip())
            with_stage += 1
        except ValueError:
            pass
    return with_stage, total


def tidal_datum_exclusions(corpus: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Symmetric threshold exclusions for tidal-datum reference sites."""
    tidal_ids = {
        str(row.get("usgs id") or "").strip()
        for row in _registry_rows()
        if _is_tidal_datum(row)
    }
    out = []
    for site in corpus.get("sites") or []:
        site_no = str(site.get("site_no") or "")
        if site_no in tidal_ids and site.get("flood_stage") is not None:
            out.append(
                {
                    "site_no": site_no,
                    "reason": "tidal_datum_threshold_not_riverine_flood_stage",
                }
            )
    return out


# --------------------------------------------------------------------------
# Corpus building and QC for one candidate
# --------------------------------------------------------------------------


def _analysis_window_utc(state: str, start: date, end: date) -> Tuple[str, str]:
    """Half-open UTC window from local calendar dates, v2 convention.

    v2 used the county's local standard-time offset (Harris CST -6 -> 05:00Z,
    Florida EST -5 -> 04:00Z) applied to local midnight of the start date and
    of the day after the end date.
    """
    from watcher.timezone_util import state_to_timezone
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone as _tz

    zone = ZoneInfo(state_to_timezone(state) or "UTC")
    # Standard (non-DST) offset for the zone, taken in January.
    january = datetime(start.year, 1, 15, tzinfo=zone)
    offset = january.utcoffset()
    start_utc = datetime(
        start.year, start.month, start.day, tzinfo=_tz.utc
    ) - offset
    end_next = end + timedelta(days=1)
    end_utc = datetime(
        end_next.year, end_next.month, end_next.day, tzinfo=_tz.utc
    ) - offset
    fmt = lambda dt: dt.astimezone(_tz.utc).isoformat().replace("+00:00", "Z")
    return fmt(start_utc), fmt(end_utc)


def datum_screen_exclusions(corpus: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Sites whose in-window stages are near-continuously at/above flood stage."""
    out = []
    for site in corpus.get("sites") or []:
        flood_stage = site.get("flood_stage")
        if flood_stage is None:
            continue
        stages = [
            obs.get("stage")
            for obs in site.get("observations") or []
            if obs.get("stage") is not None
        ]
        if len(stages) < 20:
            continue
        frac = sum(s >= flood_stage for s in stages) / len(stages)
        if frac >= DATUM_SCREEN_FLOOD_FRACTION:
            out.append(
                {
                    "site_no": str(site.get("site_no")),
                    "reason": "datum_screen_near_continuous_flood",
                    "fraction_at_or_above_flood_stage": round(frac, 4),
                }
            )
    return out


def evaluate_candidate(
    event: Dict[str, Any], candidate: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the corpus and run the v2 preflight QC for one candidate."""
    from watcher.replay import build_corpus, save_corpus
    from eval.run_study import _preflight

    begin = date.fromisoformat(event["begin"])
    start = begin - timedelta(days=WINDOW_BACK_DAYS)
    end = begin + timedelta(days=WINDOW_FORWARD_DAYS)
    slug = f"{_slug(candidate['county'])}_{candidate['fips']}_{_slug(event['name'])[:40]}"
    corpus_path = CORPUS_DIR / f"{slug}.json"
    if corpus_path.is_file():
        corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    else:
        corpus = build_corpus(
            county=candidate["county"],
            state=candidate["state"],
            fips=candidate["fips"],
            start=start.isoformat(),
            end=end.isoformat(),
        )
        save_corpus(corpus, corpus_path)

    analysis_start, analysis_end = _analysis_window_utc(
        candidate["state"], start, end
    )
    exclusions = tidal_datum_exclusions(corpus)
    tidal_excluded_ids = {row["site_no"] for row in exclusions}
    exclusions.extend(
        row
        for row in datum_screen_exclusions(corpus)
        if row["site_no"] not in tidal_excluded_ids
    )
    entry: Dict[str, Any] = {
        "path": str(corpus_path.relative_to(_ROOT)),
        "county": candidate["county"],
        "state": candidate["state"],
        "fips": candidate["fips"],
        "event": event["name"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "analysis_start": analysis_start,
        "analysis_end": analysis_end,
        "sha256": _sha256(corpus_path),
        "threshold_exclusions": exclusions,
    }
    preflight = _preflight(entry, interval_seconds=900)
    primary = next(
        row
        for row in preflight["truth_coverage"]
        if row["truth_max_age_hours"] == 1.0
    )
    coverage = float(primary["known_duration_proportion"] or 0.0)
    episodes = int(primary["n_observed_flood_episodes"] or 0)
    result = {
        "event": event["name"],
        "candidate": candidate,
        "corpus_path": entry["path"],
        "corpus_sha256": entry["sha256"],
        "window": {"start": entry["start"], "end": entry["end"]},
        "analysis_start": analysis_start,
        "analysis_end": analysis_end,
        "datum_screen_exclusions": exclusions,
        "reference_sites_after_exclusion": preflight[
            "reference_sites_after_exclusion"
        ],
        "primary_truth_coverage": coverage,
        "hydrologic_episodes_1h": episodes,
        "preflight_warnings": preflight["warnings"],
        "preflight_blocked": preflight["blocked"],
    }

    if preflight["blocked"]:
        codes = {w["code"] for w in preflight["warnings"] if w["blocking"]}
        if codes == {"limited_primary_truth_coverage_unreviewed"} and (
            coverage >= MIN_ACK_COVERAGE
        ):
            entry["limited_truth_coverage_reviewed"] = True
            entry["limited_truth_coverage_rationale"] = (
                f"At the prespecified 1 h maximum truth age, archived gauge "
                f"gaps leave {(1 - coverage):.0%} of the window unknown. Per "
                f"the frozen v3 selection rule (coverage >= {MIN_ACK_COVERAGE}"
                f"), this expected censoring is retained without imputation "
                f"and reported in interval-censored episode metrics."
            )
            preflight = _preflight(entry, interval_seconds=900)
            result["preflight_blocked"] = preflight["blocked"]
            result["limited_truth_coverage_acknowledged"] = True
        else:
            result["decision"] = "excluded"
            result["exclusion_reason"] = f"preflight_blocked:{sorted(codes)}"
            return result

    if result["preflight_blocked"]:
        result["decision"] = "excluded"
        result["exclusion_reason"] = "preflight_blocked_after_acknowledgement"
        return result
    if episodes >= 1:
        result["decision"] = "episode_evaluation"
        result["manifest_entry"] = entry
    else:
        result["decision"] = "coverage_only_candidate"
        result["manifest_entry"] = entry
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(SELECTION_OUT))
    args = parser.parse_args(argv)

    frame, frame_excluded = load_event_frame()
    considered: List[Dict[str, Any]] = []
    included: List[Dict[str, Any]] = []
    coverage_only: List[Dict[str, Any]] = []
    included_events: set = set()
    tried_by_event: Dict[str, List[str]] = {}

    def stop_reached() -> bool:
        episodes = sum(r["hydrologic_episodes_1h"] for r in included)
        return (
            episodes >= STOP_MIN_EPISODES and len(included_events) >= STOP_MIN_EVENTS
        ) or len(included) >= STOP_MAX_COUNTY_EVENTS

    for pass_index in (1, 2):
        for event in frame:
            if stop_reached():
                break
            # Pass 1 admits at most one county per event; pass 2 (reached only
            # if the stop rule is unmet) admits the next passing candidate per
            # event in the same frozen order, previously tried counties
            # excluded via the persistent tried list.
            candidates = candidate_counties(event)
            for candidate in candidates:
                with_stage, total = registry_gauge_counts(
                    candidate["county"], candidate["state"]
                )
                candidate["registry_gauges_with_flood_stage"] = with_stage
                candidate["registry_gauges_in_service"] = total
            ranked = sorted(
                candidates,
                key=lambda c: (
                    -c["registry_gauges_with_flood_stage"],
                    -c["registry_gauges_in_service"],
                    c["state"],
                    c["county"],
                ),
            )
            tried = tried_by_event.setdefault(event["name"], [])
            attempts = 0
            for candidate in ranked:
                if attempts >= CANDIDATES_PER_EVENT:
                    break
                if candidate["registry_gauges_with_flood_stage"] < 1:
                    break  # ranked order: nothing below has coverage either
                if candidate["fips"] in tried:
                    continue
                tried.append(candidate["fips"])
                attempts += 1
                print(
                    f"[pass {pass_index}] {event['name']} -> trying "
                    f"{candidate['county']}, {candidate['state']} "
                    f"({candidate['fips']}; "
                    f"{candidate['registry_gauges_with_flood_stage']} gauges)",
                    file=sys.stderr,
                )
                try:
                    result = evaluate_candidate(event, candidate)
                except Exception as exc:  # record and continue
                    result = {
                        "event": event["name"],
                        "candidate": candidate,
                        "decision": "excluded",
                        "exclusion_reason": f"build_or_qc_error: {exc}",
                    }
                result["selection_pass"] = pass_index
                considered.append(result)
                if result["decision"] == "episode_evaluation":
                    included.append(result)
                    included_events.add(event["name"])
                    break
                if (
                    result["decision"] == "coverage_only_candidate"
                    and len(coverage_only) < MAX_COVERAGE_ONLY
                ):
                    coverage_only.append(result)
            if stop_reached():
                break
        if stop_reached():
            break

    selection = {
        "schema": "sentinel-v3-holdout-selection/1",
        "protocol": "eval/HYBRID_STUDY_PROTOCOL.md",
        "policy_outcome_blind": True,
        "ncei_events_csv": str(NCEI_CSV.relative_to(_ROOT)),
        "ncei_events_csv_sha256": _sha256(NCEI_CSV),
        "registry_csv": str(REGISTRY_CSV.relative_to(_ROOT)),
        "registry_csv_sha256": _sha256(REGISTRY_CSV),
        "rules": {
            "event_order": "descending CPI-adjusted cost",
            "max_impact_period_days": MAX_IMPACT_PERIOD_DAYS,
            "window_days": [WINDOW_BACK_DAYS, WINDOW_FORWARD_DAYS],
            "candidates_per_event": CANDIDATES_PER_EVENT,
            "stop_min_episodes": STOP_MIN_EPISODES,
            "stop_min_events": STOP_MIN_EVENTS,
            "stop_max_county_events": STOP_MAX_COUNTY_EVENTS,
            "max_coverage_only": MAX_COVERAGE_ONLY,
            "min_acknowledged_truth_coverage": MIN_ACK_COVERAGE,
            "datum_screen_flood_fraction": DATUM_SCREEN_FLOOD_FRACTION,
            "development_counties_excluded": sorted(DEV_COUNTY_FIPS),
        },
        "event_frame": frame,
        "event_frame_excluded": frame_excluded,
        "included_episode_evaluation": included,
        "included_coverage_only": coverage_only,
        "all_candidates_considered": considered,
        "pooled_hydrologic_episodes_1h": sum(
            r["hydrologic_episodes_1h"] for r in included
        ),
        "n_events_included": len(included_events),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(selection, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "out": str(out_path),
                "included": [
                    f"{r['candidate']['county']}, {r['candidate']['state']} "
                    f"({r['event']}): {r['hydrologic_episodes_1h']} episodes"
                    for r in included
                ],
                "coverage_only": [
                    f"{r['candidate']['county']}, {r['candidate']['state']}"
                    for r in coverage_only
                ],
                "pooled_episodes": selection["pooled_hydrologic_episodes_1h"],
                "n_events": len(included_events),
                "n_considered": len(considered),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
