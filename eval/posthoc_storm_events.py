#!/usr/bin/env python3
"""Post-hoc comparison of FloodSentinel flood reports with the NOAA NCEI Storm Events Database.

Nothing here was prespecified. The frozen studies grade every policy against a hydrologic reference built from the same USGS/NWPS gauge series and flood-stage thresholds that the policies read, so the reference cannot say whether a reported flood corresponds to flooding that anyone else recorded. This script compares the reference episodes and the replayed flood reports with an external record the policies and the reference never see: the county-level Flood and Flash Flood entries of the NCEI Storm Events Database. Storm Events entries are compiled by NWS forecast offices from spotter, media, emergency-management, and official reports; they have their own timing, duration, and coverage errors and are not ground truth. Agreement and disagreement below describe consistency between two imperfect records, not accuracy.

Inputs, all read only: the frozen per-county result files of Study 3 (eval/results/study_v4_additive) and Study 2 (eval/results/study_v3_hybrid_rev2), their frozen corpus manifests and gauge and trigger corpora (checksums verified), and the Storm Events "details" bulk CSVs in --storm-dir. Outputs are written only under eval/results/posthoc_storm_events/.

(A) Reference episodes against Storm Events. The stored reference episode spans (trigger_accounting.hybrid.episode_trigger_rows) are first recomputed from the corpus with eval.trigger_metrics.reference_episode_intervals and the script fails closed if they differ. An episode [s, e) matches at tolerance T when some Storm Events Flood or Flash Flood entry for the county [b, f] satisfies b - T <= e and s <= f + T; a Storm Events entry matches when some episode satisfies the same condition.

(B) Replayed flood reports against Storm Events. Acquisition instants and per-acquisition verdicts are not stored in the frozen artifacts, so the hybrid and phase adaptation are replayed through eval.run_replay.run under eval.run_study.FROZEN_REPLAY_ENVIRONMENT exactly as figures/make_mechanism_figure.py does, whose helpers are imported, and each regenerated run must reproduce the frozen acquisition count, termination, fold instant, acquisitions by reason, burst spans, immediate fires, and trigger consultations or the script stops. Two per-acquisition flags are used: flood_stage_observed (some gauge in the acquisition classified at minor flood stage or above, the flag the frozen episode grading uses) and is_flooding (the county regional verdict, true at action stage or above, the operational flag).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
FIGURES_DIR = REPO_ROOT / "figures"
if not (FIGURES_DIR / "make_mechanism_figure.py").is_file():
    raise SystemExit("figures/make_mechanism_figure.py is required (the replay helpers are imported from it)")
if str(FIGURES_DIR) not in sys.path:
    sys.path.insert(0, str(FIGURES_DIR))

import make_mechanism_figure as mech  # noqa: E402
from eval.ground_truth import apply_threshold_compatibility, hydrologic_truth_at, parse_iso  # noqa: E402
from eval.run_study import PRIMARY_TRUTH_MAX_AGE_HOURS  # noqa: E402
from eval.trigger_metrics import reference_episode_intervals  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

OUT_DIR = REPO_ROOT / "eval" / "results" / "posthoc_storm_events"
STORM_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
MAIN_TYPES = ("Flood", "Flash Flood")
SIDE_TYPES = ("Coastal Flood", "Heavy Rain")
PRIMARY_TOLERANCE_H = 6
TOLERANCES_H = (0, 6, 24)
GRID_SECONDS = 900
FLAGS = ("flood_stage_observed", "is_flooding")
POLICIES = ("hybrid", "adaptive")
# Zone-filed types that can carry flood impacts from tropical cyclones and surge; reported as context only, never matched.
ZONE_CONTEXT_TYPES = ("Storm Surge/Tide", "Coastal Flood", "Hurricane (Typhoon)", "Tropical Storm", "Tropical Depression", "Heavy Rain", "Flood", "Flash Flood")
# Storm Events files Puerto Rico under STATE_FIPS 99 (not the Census 72), with municipios as county-type (CZ_TYPE C) rows whose CZ_FIPS is the Census municipio code.
STORM_STATE_FIPS_ALIASES = {"72": "99"}

STUDIES = (
    {
        "key": "study3",
        "label": "Study 3 (v4, additive hybrid)",
        "dir": REPO_ROOT / "eval" / "results" / "study_v4_additive",
        "manifest": REPO_ROOT / "eval" / "corpus" / "study_v4" / "manifest.json",
        "anchor_mode": "additive",
    },
    {
        "key": "study2",
        "label": "Study 2 (v3 rev2, re-anchoring hybrid)",
        "dir": REPO_ROOT / "eval" / "results" / "study_v3_hybrid_rev2",
        "manifest": REPO_ROOT / "eval" / "corpus" / "study_v3" / "manifest.json",
        "anchor_mode": "reanchor",
    },
)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# Storm Events
# ---------------------------------------------------------------------------

_TZ_RE = re.compile(r"^([A-Z]+?)([+-]?\d+)$")
_MON = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def tz_offset_hours(cz_timezone: str) -> int:
    """UTC offset from a CZ_TIMEZONE label such as 'CST-6', 'AST-4', or 'GST10'."""
    m = _TZ_RE.match(cz_timezone.strip())
    _require(m is not None, f"unparseable CZ_TIMEZONE {cz_timezone!r}")
    return int(m.group(2))


def parse_storm_time(date_time: str, cz_timezone: str) -> datetime:
    """'22-JUN-16 20:10:00' in the row's stated standard-time zone, returned in UTC."""
    day, mon, yy = date_time[:9].split("-")
    hh, mm, ss = date_time[10:].split(":")
    local = datetime(2000 + int(yy), _MON[mon.upper()], int(day), int(hh), int(mm), int(ss))
    return (local - timedelta(hours=tz_offset_hours(cz_timezone))).replace(tzinfo=timezone.utc)


def _cross_check_fields(row: Mapping[str, str], which: str) -> bool:
    """BEGIN/END_DATE_TIME local value must agree with *_YEARMONTH, *_DAY, *_TIME."""
    dt = row[f"{which}_DATE_TIME"]
    day, mon, yy = dt[:9].split("-")
    hh, mm, _ = dt[10:].split(":")
    ym = f"{2000 + int(yy):04d}{_MON[mon.upper()]:02d}"
    return ym == row[f"{which}_YEARMONTH"] and int(day) == int(row[f"{which}_DAY"]) and int(hh) * 100 + int(mm) == int(row[f"{which}_TIME"])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _storm_file_for_year(storm_dir: Path, year: int) -> Path:
    files = sorted(storm_dir.glob(f"StormEvents_details-ftp_v1.0_d{year}_c*.csv.gz"))
    _require(bool(files), f"no Storm Events details file for {year} in {storm_dir}; download it from {STORM_URL}")
    return files[-1]  # latest c-date


def load_storm_events(storm_dir: Path, years: Sequence[int], wanted: Sequence[Tuple[str, str]]) -> Tuple[Dict[Tuple[str, str], List[Dict[str, Any]]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """County-type (CZ_TYPE C) rows of all four event types for the wanted (state_fips, cz_fips) keys in Storm Events coding."""
    wanted_set = set(wanted)
    wanted_states = {k[0] for k in wanted}
    zone_rows: List[Dict[str, Any]] = []
    by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {k: [] for k in wanted}
    files = []
    parse_checks = {"rows_checked": 0, "rows_failed": 0, "examples": []}
    for year in sorted(set(years)):
        path = _storm_file_for_year(storm_dir, year)
        files.append({"year": year, "file": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size})
        with gzip.open(path, "rt", encoding="latin-1", newline="") as fh:
            for row in csv.DictReader(fh):
                if row["CZ_TYPE"] == "Z" and row["EVENT_TYPE"] in ZONE_CONTEXT_TYPES and row["STATE_FIPS"].strip() in wanted_states:
                    zb = parse_storm_time(row["BEGIN_DATE_TIME"], row["CZ_TIMEZONE"])
                    ze = parse_storm_time(row["END_DATE_TIME"], row["CZ_TIMEZONE"])
                    zone_rows.append({"event_id": row["EVENT_ID"], "event_type": row["EVENT_TYPE"], "state_fips": row["STATE_FIPS"].strip(), "cz_fips": row["CZ_FIPS"], "cz_name": row["CZ_NAME"], "begin_utc": _iso(zb), "end_utc": _iso(ze), "_begin": zb, "_end": ze})
                    continue
                if row["EVENT_TYPE"] not in MAIN_TYPES + SIDE_TYPES:
                    continue
                # Zone rows (CZ_TYPE Z) carry forecast-zone numbers, not county codes, so a zone with the same number as the county is unrelated; only county rows are kept.
                if row["CZ_TYPE"] != "C":
                    continue
                key = (row["STATE_FIPS"].strip(), row["CZ_FIPS"].strip().lstrip("0") or "0")
                if key not in wanted_set:
                    continue
                for which in ("BEGIN", "END"):
                    parse_checks["rows_checked"] += 1
                    if not _cross_check_fields(row, which):
                        parse_checks["rows_failed"] += 1
                begin = parse_storm_time(row["BEGIN_DATE_TIME"], row["CZ_TIMEZONE"])
                end = parse_storm_time(row["END_DATE_TIME"], row["CZ_TIMEZONE"])
                _require(end >= begin, f"Storm Events {row['EVENT_ID']} ends before it begins")
                rec = {
                    "event_id": row["EVENT_ID"],
                    "episode_id": row["EPISODE_ID"],
                    "event_type": row["EVENT_TYPE"],
                    "cz_type": row["CZ_TYPE"],
                    "state": row["STATE"],
                    "state_fips": row["STATE_FIPS"],
                    "cz_fips": row["CZ_FIPS"],
                    "cz_name": row["CZ_NAME"],
                    "wfo": row["WFO"],
                    "cz_timezone": row["CZ_TIMEZONE"],
                    "begin_local": row["BEGIN_DATE_TIME"],
                    "end_local": row["END_DATE_TIME"],
                    "begin_utc": _iso(begin),
                    "end_utc": _iso(end),
                    "flood_cause": row.get("FLOOD_CAUSE") or None,
                    "source": row.get("SOURCE") or None,
                    "begin_location": row.get("BEGIN_LOCATION") or None,
                    "_begin": begin,
                    "_end": end,
                }
                by_key[key].append(rec)
                if len(parse_checks["examples"]) < 5 and row["EVENT_TYPE"] in MAIN_TYPES:
                    parse_checks["examples"].append({k: rec[k] for k in ("event_id", "cz_timezone", "begin_local", "begin_utc", "end_local", "end_utc")} | {"BEGIN_YEARMONTH": row["BEGIN_YEARMONTH"], "BEGIN_DAY": row["BEGIN_DAY"], "BEGIN_TIME": row["BEGIN_TIME"]})
    _require(parse_checks["rows_failed"] == 0, f"{parse_checks['rows_failed']} Storm Events date fields disagree with YEARMONTH/DAY/TIME")
    return by_key, zone_rows, files, parse_checks


def storm_key(fips: str) -> Tuple[str, str]:
    state, county = fips[:2], fips[2:]
    state = STORM_STATE_FIPS_ALIASES.get(state, state.lstrip("0"))
    return state, county.lstrip("0") or "0"


# ---------------------------------------------------------------------------
# County-event loading and replay
# ---------------------------------------------------------------------------


def county_events(study: Mapping[str, Any]) -> List[Dict[str, Any]]:
    manifest = json.loads(Path(study["manifest"]).read_text())
    policy = manifest["frozen_trigger_policy"]
    out = []
    for path in sorted((Path(study["dir"]) / "counties").glob("*/result.json")):
        result = json.loads(path.read_text())
        entry = result["corpus_manifest_entry"]
        if entry.get("analysis_role") != "episode_evaluation":
            continue
        out.append({"study": study["key"], "anchor_mode": study["anchor_mode"], "result_path": str(path), "policy": policy})
    return out


def _replay_task(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Replay hybrid and adaptation for one county-event and verify both against the frozen summary (mechanism-script checks)."""
    result_path = Path(task["result_path"])
    result = json.loads(result_path.read_text())
    policy = task["policy"]
    entry = result["corpus_manifest_entry"]
    corpus_path = mech._resolve(entry["path"])
    trigger_path = mech._resolve(entry["trigger_corpus"])
    _require(mech._sha256(corpus_path) == entry["sha256"], f"gauge corpus checksum mismatch: {corpus_path}")
    _require(mech._sha256(trigger_path) == entry["trigger_corpus_sha256"], f"trigger corpus checksum mismatch: {trigger_path}")
    excluded = sorted({str(r["site_no"]) for r in (entry.get("threshold_exclusions") or []) if r.get("site_no")})
    windows = mech._warning_windows(trigger_path, policy["qualifying"], int(policy["burst_duration_seconds"]))
    mech._check_windows_against_result(windows, result)
    stored_mode = ((result["policies"]["hybrid"].get("verification_trigger") or {}).get("policy") or {}).get("anchor_mode")
    if stored_mode is not None:
        _require(stored_mode == task["anchor_mode"], f"{result_path}: stored anchor mode {stored_mode} differs from {task['anchor_mode']}")
    hybrid_kwargs = {
        "trigger": {
            "enabled": True,
            "qualifying": list(policy["qualifying"]),
            "burst_interval_seconds": int(policy["burst_interval_seconds"]),
            "burst_duration_seconds": int(policy["burst_duration_seconds"]),
            "anchor_mode": task["anchor_mode"],
        },
        "trigger_corpus_path": str(trigger_path),
        "trigger_poll_interval_seconds": int(policy["poll_interval_seconds"]),
    }
    runs = {
        "hybrid": mech._run_policy(entry=entry, excluded_ids=excluded, run_kwargs=hybrid_kwargs),
        "adaptive": mech._run_policy(entry=entry, excluded_ids=excluded, run_kwargs={}),
    }
    checks = {}
    for name in POLICIES:
        mech._verify_run(name, runs[name], mech._stored_row(result, name), result["policies"][name])
        checks[name] = "reproduced"
    slim = {
        name: [
            {"t": t["t"], "flood_stage_observed": bool(t.get("flood_stage_observed")), "is_flooding": bool(t.get("is_flooding")), "alert_level": t.get("alert_level"), "reason": t.get("acquisition_due_reason"), "folded": bool(t.get("folded"))}
            for t in runs[name]["ticks"]
        ]
        for name in POLICIES
    }
    return {"result_path": task["result_path"], "ticks": slim, "checks": checks, "termination": {n: runs[n].get("termination") for n in POLICIES}}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _near(a0: datetime, a1: datetime, b0: datetime, b1: datetime, tol_h: float) -> bool:
    """Closed intervals [a0, a1] and [b0, b1] overlap after widening b by tol on each side."""
    tol = timedelta(hours=tol_h)
    return b0 - tol <= a1 and a0 <= b1 + tol


def _reference_status(corpus: Dict[str, Any], start: datetime, end: datetime, lo: datetime, hi: datetime) -> Dict[str, Any]:
    """Reference labels on the 15-minute study grid inside [lo, hi] clipped to the analysis window."""
    counts = {"positive": 0, "negative": 0, "unknown": 0}
    t = start
    step = timedelta(seconds=GRID_SECONDS)
    while t < end:
        if lo <= t <= hi:
            v = hydrologic_truth_at(corpus, t, max_age_hours=PRIMARY_TRUTH_MAX_AGE_HOURS)["is_flooding"]
            counts["positive" if v is True else "negative" if v is False else "unknown"] += 1
        t += step
    if counts["positive"]:
        label = "positive"
    elif counts["negative"] and not counts["unknown"]:
        label = "negative"
    elif counts["unknown"] and not counts["negative"]:
        label = "unknown"
    elif counts["negative"] or counts["unknown"]:
        label = "negative_and_unknown"
    else:
        label = "outside_window"
    return {"label": label, "grid_points": counts}


def analyse_county_event(result_path: str, storm_rows: List[Dict[str, Any]], zone_rows: Sequence[Mapping[str, Any]], replay: Mapping[str, Any]) -> Dict[str, Any]:
    result = json.loads(Path(result_path).read_text())
    entry = result["corpus_manifest_entry"]
    start, end = parse_iso(entry["analysis_start"]), parse_iso(entry["analysis_end"])
    excluded = sorted({str(r["site_no"]) for r in (entry.get("threshold_exclusions") or []) if r.get("site_no")})
    corpus, _ = apply_threshold_compatibility(load_corpus(mech._resolve(entry["path"])), excluded_site_ids=excluded)

    stored_eps = [{"start": r["start"], "end": r["end"]} for r in result["trigger_accounting"]["hybrid"]["episode_trigger_rows"]]
    recomputed = reference_episode_intervals(corpus, start=entry["analysis_start"], end=entry["analysis_end"], interval_seconds=GRID_SECONDS, truth_max_age_hours=PRIMARY_TRUTH_MAX_AGE_HOURS)
    _require(recomputed == stored_eps, f"{result_path}: recomputed reference episodes {recomputed} differ from stored {stored_eps}")
    episodes = [(parse_iso(e["start"]), parse_iso(e["end"])) for e in stored_eps]

    # Storm Events rows in the analysis window. Window is [start, end); a row overlaps when begin < end and row end >= start.
    in_window = [r for r in storm_rows if r["_begin"] < end and r["_end"] >= start]
    name_ok = all(r["cz_name"].upper().replace(".", "").replace(" ", "") .startswith(entry["county"].upper().replace(".", "").replace(" ", "")[:5]) for r in in_window if r["cz_type"] == "C")
    main = sorted([r for r in in_window if r["cz_type"] == "C" and r["event_type"] in MAIN_TYPES], key=lambda r: (r["_begin"], r["event_id"]))
    side_counts = {}
    for et in SIDE_TYPES + MAIN_TYPES:
        n = sum(1 for r in in_window if r["event_type"] == et)
        if n:
            side_counts[et] = n

    # Context only: zone rows in the same state whose CZ_NAME contains the county name as a word (e.g. COASTAL SARASOTA), overlapping the window. Not used in any match.
    name_re = re.compile(r"\b" + re.escape(entry["county"].upper()) + r"\b")
    skey = storm_key(entry["fips"])
    zone_ctx = [
        {k: v for k, v in z.items() if not k.startswith("_")}
        for z in zone_rows
        if z["state_fips"] == skey[0] and name_re.search(z["cz_name"].upper()) and z["_begin"] < end and z["_end"] >= start
    ]

    ticks = replay["ticks"]
    folded_at = {p: (parse_iso(ticks[p][-1]["t"]) if ticks[p] and ticks[p][-1]["folded"] else None) for p in POLICIES}

    storm_out = []
    for r in main:
        ref = _reference_status(corpus, start, end, r["_begin"] - timedelta(hours=PRIMARY_TOLERANCE_H), r["_end"] + timedelta(hours=PRIMARY_TOLERANCE_H))
        row = {k: v for k, v in r.items() if not k.startswith("_")}
        row["reference_status_pm6h"] = ref
        row["matches_reference_episode"] = {str(tol): any(_near(r["_begin"], r["_end"], s, e, tol) for s, e in episodes) for tol in TOLERANCES_H}
        row["reported"] = {}
        for p in POLICIES:
            row["reported"][p] = {}
            for flag in FLAGS:
                row["reported"][p][flag] = {str(tol): any(tk[flag] and _near(r["_begin"], r["_end"], parse_iso(tk["t"]), parse_iso(tk["t"]), tol) for tk in ticks[p]) for tol in TOLERANCES_H}
            row["reported"][p]["begins_after_fold"] = bool(folded_at[p] and r["_begin"] > folded_at[p])
            row["reported"][p]["any_acquisition_pm6h"] = any(_near(r["_begin"], r["_end"], parse_iso(tk["t"]), parse_iso(tk["t"]), PRIMARY_TOLERANCE_H) for tk in ticks[p])
        storm_out.append(row)

    ep_out = []
    for (s, e), stored in zip(episodes, result["trigger_accounting"]["hybrid"]["episode_trigger_rows"]):
        ep_out.append({
            "start": _iso(s), "end": _iso(e),
            "trigger_covered": stored.get("trigger_covered"),
            "matches_storm_event": {str(tol): [r["event_id"] for r in main if _near(s, e, r["_begin"], r["_end"], tol)] for tol in TOLERANCES_H},
        })

    acq_out = {}
    for p in POLICIES:
        acq_out[p] = {"acquisitions": len(ticks[p]), "folded_at": _iso(folded_at[p]) if folded_at[p] else None}
        for flag in FLAGS:
            pos = [tk for tk in ticks[p] if tk[flag]]
            d = {"positive_reports": len(pos)}
            for tol in TOLERANCES_H:
                inside = [tk["t"] for tk in pos if any(_near(r["_begin"], r["_end"], parse_iso(tk["t"]), parse_iso(tk["t"]), tol) for r in main)]
                d[f"within_storm_event_pm{tol}h"] = len(inside)
            d["outside_storm_events_pm6h"] = [tk["t"] for tk in pos if not any(_near(r["_begin"], r["_end"], parse_iso(tk["t"]), parse_iso(tk["t"]), PRIMARY_TOLERANCE_H) for r in main)]
            def _dist_h(t: datetime) -> float:
                return min(max((r["_begin"] - t).total_seconds(), (t - r["_end"]).total_seconds(), 0.0) for r in main) / 3600.0
            d["outside_storm_events_pm6h_hours_to_nearest_entry"] = [round(_dist_h(parse_iso(t)), 2) for t in d["outside_storm_events_pm6h"]] if main else []
            acq_out[p][flag] = d

    return {
        "county": entry["county"], "state": entry["state"], "fips": entry["fips"], "event": entry.get("event"), "event_ncei_name": entry.get("event_ncei_name"),
        "analysis_start": entry["analysis_start"], "analysis_end": entry["analysis_end"], "result": _rel(Path(result_path)),
        "storm_key": list(storm_key(entry["fips"])), "storm_cz_names": sorted({r["cz_name"] for r in in_window if r["cz_type"] == "C"}), "storm_cz_name_check": name_ok,
        "storm_event_type_counts_in_window": side_counts,
        "zone_context_rows_by_name": sorted(zone_ctx, key=lambda z: (z["begin_utc"], z["event_id"])),
        "replay_checks": replay["checks"],
        "reference_episodes": ep_out,
        "storm_events": storm_out,
        "acquisitions": acq_out,
    }


def pooled(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"county_events": len(rows)}
    out["county_events_with_zero_storm_flood_entries"] = [f"{r['county']} {r['state']} {r['analysis_start'][:10]}" for r in rows if not r["storm_events"]]
    out["county_events_with_zero_reference_episodes"] = [f"{r['county']} {r['state']} {r['analysis_start'][:10]}" for r in rows if not r["reference_episodes"]]
    n_ep = sum(len(r["reference_episodes"]) for r in rows)
    n_se = sum(len(r["storm_events"]) for r in rows)
    out["A"] = {}
    for tol in TOLERANCES_H:
        k = str(tol)
        ep_m = sum(1 for r in rows for e in r["reference_episodes"] if e["matches_storm_event"][k])
        se_m = sum(1 for r in rows for s in r["storm_events"] if s["matches_reference_episode"][k])
        out["A"][f"pm{tol}h"] = {
            "reference_episodes_matched": ep_m, "reference_episodes": n_ep, "rate_episodes": round(ep_m / n_ep, 4) if n_ep else None,
            "storm_events_matched": se_m, "storm_events": n_se, "rate_storm_events": round(se_m / n_se, 4) if n_se else None,
        }
    out["B"] = {}
    for p in POLICIES:
        out["B"][p] = {}
        for flag in FLAGS:
            d = {}
            for tol in TOLERANCES_H:
                k = str(tol)
                pos = sum(r["acquisitions"][p][flag]["positive_reports"] for r in rows)
                inside = sum(r["acquisitions"][p][flag][f"within_storm_event_pm{tol}h"] for r in rows)
                rep = sum(1 for r in rows for s in r["storm_events"] if s["reported"][p][flag][k])
                pos_with = sum(r["acquisitions"][p][flag]["positive_reports"] for r in rows if r["storm_events"])
                d[f"pm{tol}h"] = {
                    "positive_reports_in_county_events_with_storm_entries": pos_with,
                    "positive_reports_in_county_events_without_storm_entries": pos - pos_with,
                    "rate_reports_among_county_events_with_storm_entries": round(inside / pos_with, 4) if pos_with else None,
                    "positive_reports": pos, "positive_reports_within_storm_event": inside, "rate_reports": round(inside / pos, 4) if pos else None,
                    "storm_events": n_se, "storm_events_with_positive_report": rep, "rate_storm_events": round(rep / n_se, 4) if n_se else None,
                }
            out["B"][p][flag] = d
        out["B"][p]["acquisitions"] = sum(r["acquisitions"][p]["acquisitions"] for r in rows)
        out["B"][p]["storm_events_beginning_after_fold"] = sum(1 for r in rows for s in r["storm_events"] if s["reported"][p]["begins_after_fold"])
    # Reference status of Storm Events rows, and of the rows the hybrid never reported.
    status: Dict[str, int] = {}
    for r in rows:
        for s in r["storm_events"]:
            lab = s["reference_status_pm6h"]["label"]
            status[lab] = status.get(lab, 0) + 1
    out["storm_event_reference_status_pm6h"] = status
    for flag in FLAGS:
        miss: Dict[str, int] = {}
        for r in rows:
            for s in r["storm_events"]:
                if not s["reported"]["hybrid"][flag][str(PRIMARY_TOLERANCE_H)]:
                    lab = s["reference_status_pm6h"]["label"]
                    miss[lab] = miss.get(lab, 0) + 1
        out[f"hybrid_unreported_storm_events_by_reference_status_{flag}_pm6h"] = miss
    return out


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _frac(a: int, b: int) -> str:
    return f"{a} of {b} ({100 * a / b:.1f}%)" if b else f"{a} of 0 (n/a)"


def _ce(r: Mapping[str, Any]) -> str:
    return f"{r['county']}, {r['state']} ({r['analysis_start'][:10]})"


def render_markdown(summary: Mapping[str, Any]) -> str:
    L: List[str] = []
    L.append("# Post hoc: FloodSentinel flood status against the NCEI Storm Events Database")
    L.append("")
    L.append("Post hoc, not prespecified. Generated by `eval/posthoc_storm_events.py` from frozen artifacts and the Storm Events bulk CSVs listed below; the frozen directories were only read.")
    L.append("")
    L.append("## What this is and is not")
    L.append("")
    L.append("The frozen studies grade policies against a hydrologic reference built from the same USGS/NWPS gauge series and flood-stage thresholds the policies read, so that reference cannot confirm that a reported flood corresponds to flooding recorded by anyone else. This comparison uses an external record neither the policies nor the reference see: county-level Flood and Flash Flood entries in the NOAA NCEI Storm Events Database. Storm Events is a report-based record compiled by NWS forecast offices from spotter, media, emergency-management, and official reports. Its begin and end times are often approximate or rounded, its coverage depends on who reported what, it records impacts (roads closed, water in structures) rather than stage above a threshold, and a single physical flood may appear as several entries or none. Agreement and disagreement below therefore describe consistency between two imperfect records; this is a comparison with an independent record, not a validation against ground truth.")
    L.append("")
    L.append("## Data and handling")
    L.append("")
    L.append(f"Storm Events details files (from {STORM_URL}, latest c-date per year at download on {summary['generated_on']}):")
    L.append("")
    L.append("| Year | File | SHA-256 |")
    L.append("|---|---|---|")
    for f in summary["storm_files"]:
        L.append(f"| {f['year']} | `{f['file']}` | `{f['sha256']}` |")
    L.append("")
    L.append("Main comparison: EVENT_TYPE in {Flood, Flash Flood}, CZ_TYPE C, and STATE_FIPS plus CZ_FIPS equal to the county FIPS. Coastal Flood and Heavy Rain county-type entries are counted per county-event but not used. Coastal Flood is normally filed against forecast zones (CZ_TYPE Z), whose numbers are not county codes and cannot be matched to a county without a zone-county correlation table, so zone rows are ignored and the Coastal Flood counts here are close to zero by construction; they do not measure coastal flooding in these counties. A Storm Events row is kept when it overlaps the analysis window [analysis_start, analysis_end).")
    L.append("")
    L.append("Puerto Rico: Storm Events files Puerto Rico under STATE_FIPS 99, not the Census code 72, and records municipios as county-type rows (CZ_TYPE C) whose CZ_FIPS is the Census municipio code (Caguas 25, Ponce 113). The script therefore maps state 72 to 99 and matches CZ_FIPS as usual; the matched CZ_NAME values are listed per county-event and were checked against the county name.")
    L.append("")
    pc = summary["storm_parse_checks"]
    L.append(f"Time zones: BEGIN_DATE_TIME and END_DATE_TIME are local times in the zone named by CZ_TIMEZONE; the signed number in the label (CST-6, EST-5, AST-4) is the UTC offset in hours, so UTC = local minus offset. Every flood-related row in all nine files, including July and August rows, carries a standard-time label (no EDT or CDT labels appear), consistent with the Storm Data convention of local standard time, and the offset was applied as stated with no daylight-saving adjustment. The local date-time strings were cross-checked against BEGIN/END_YEARMONTH, _DAY, and _TIME on every matched row: {pc['rows_checked']} fields checked, {pc['rows_failed']} disagreements. Examples of the conversion are in summary.json (`storm_parse_checks.examples`).")
    L.append("")
    L.append(f"Matching: a reference episode [s, e) and a Storm Events entry [b, f] match at tolerance T when b minus T is at or before e and s is at or before f plus T. An acquisition at instant t falls within an entry when b minus T is at or before t and t is at or before f plus T. The primary tolerance is {PRIMARY_TOLERANCE_H} h; 0 h and 24 h are reported as sensitivity. Reference status during a Storm Events entry is the reference label (1 h maximum reference age, three-valued) on the 15-minute study grid within the entry plus or minus 6 h, clipped to the analysis window: positive if any grid point is positive, negative if all are known negative, unknown if all are unknown, negative_and_unknown otherwise.")
    L.append("")
    L.append("Replays: acquisition instants and per-acquisition verdicts are not stored in the frozen artifacts, so the hybrid and phase adaptation were replayed for every county-event through `eval.run_replay.run` under the frozen replay environment with the helpers of `figures/make_mechanism_figure.py`. Every replay had to reproduce the frozen acquisition count, termination, fold instant, acquisitions by reason, burst spans, immediate fires, and trigger consultations, and the stored reference episodes had to equal the episodes recomputed from the corpus, or the script stops. Two per-acquisition flags are reported: `flood_stage_observed` (some gauge in the acquisition at minor flood stage or above; the flag the frozen episode grading uses) and `is_flooding` (the county regional verdict, true at action stage or above; the operational flag behind the reported status).")
    L.append("")
    L.append("Limitations of this comparison: (1) Storm Events files are indexed by begin year and every analysis window lies within one calendar year, so an entry that began in an earlier year and ran into a window would be missed; no window starts before 6 March, so none is expected. (2) Only county-type rows are matched; zone-filed flood impacts (tropical cyclone, surge, coastal) are listed as context for county-events without county entries but not matched. (3) A mission that folds stops acquiring, so Storm Events entries after the fold cannot be reported by that policy; they are counted and flagged. (4) The plus or minus 6 h tolerance is a choice; 0 h and 24 h are shown and the conclusions should be read across all three. (5) Storm Events entries for one storm are often many near-duplicate rows (for example several Flash Flood rows for different locations in the same county within an hour), so entry counts are not independent floods.")
    L.append("")
    for st in summary["studies"]:
        L.extend(_render_study(st))
    return "\n".join(L) + "\n"


def _render_study(st: Mapping[str, Any]) -> List[str]:
    L: List[str] = []
    P = st["pooled"]
    rows = st["county_events"]
    L.append(f"## {st['label']}: {P['county_events']} episode-evaluation county-events")
    L.append("")
    allrep = all(v == "reproduced" for r in rows for v in r["replay_checks"].values())
    L.append(f"Replay reproduction: {'every' if allrep else 'NOT every'} hybrid and adaptation replay reproduced its frozen summary ({2 * len(rows)} replays).")
    L.append("")
    z = P["county_events_with_zero_storm_flood_entries"]
    L.append(f"Coverage: {len(z)} of {P['county_events']} county-events have no Storm Events Flood or Flash Flood entry for the county in the window{': ' + '; '.join(z) if z else ''}. {len(P['county_events_with_zero_reference_episodes'])} have no reference episode{': ' + '; '.join(P['county_events_with_zero_reference_episodes']) if P['county_events_with_zero_reference_episodes'] else ''}.")
    L.append("")
    zc = [r for r in rows if not r["storm_events"]]
    if zc:
        L.append("Zone-filed context for the county-events with no county Flood or Flash Flood entry (zone rows of types " + ", ".join(ZONE_CONTEXT_TYPES) + " in the same state whose CZ_NAME contains the county name, overlapping the window; context only, never matched, because tropical-cyclone impacts are often filed against forecast zones as Hurricane, Tropical Storm, or Storm Surge/Tide entries whose narrative, not type, records any flooding):")
        L.append("")
        for r in zc:
            z = r["zone_context_rows_by_name"]
            L.append(f"- {_ce(r)}: " + ("; ".join(f"{x['event_type']} {x['event_id']} {x['cz_name']} {x['begin_utc']} to {x['end_utc']}" for x in z) if z else "no zone row found by name") + ".")
        L.append("")
    L.append("### (A) Reference episodes against Storm Events")
    L.append("")
    L.append("| Tolerance | Reference episodes with a Storm Events entry | Storm Events entries with a reference episode |")
    L.append("|---|---|---|")
    for tol in TOLERANCES_H:
        a = P["A"][f"pm{tol}h"]
        L.append(f"| plus or minus {tol} h | {_frac(a['reference_episodes_matched'], a['reference_episodes'])} | {_frac(a['storm_events_matched'], a['storm_events'])} |")
    L.append("")
    L.append(f"Reference status (plus or minus 6 h) of all Storm Events entries: {', '.join(f'{k} {v}' for k, v in sorted(P['storm_event_reference_status_pm6h'].items()))}.")
    L.append("")
    L.append("Reference episodes without a Storm Events entry at plus or minus 6 h:")
    L.append("")
    any_ = False
    for r in rows:
        for e in r["reference_episodes"]:
            if not e["matches_storm_event"]["6"]:
                any_ = True
                alt = e["matches_storm_event"]["24"]
                L.append(f"- {_ce(r)}: episode {e['start']} to {e['end']}; {'matched at 24 h by ' + ', '.join(alt) if alt else 'no entry even at 24 h'}.")
    if not any_:
        L.append("- none")
    L.append("")
    L.append("Storm Events entries without a reference episode at plus or minus 6 h:")
    L.append("")
    any_ = False
    for r in rows:
        for s in r["storm_events"]:
            if not s["matches_reference_episode"]["6"]:
                any_ = True
                g = s["reference_status_pm6h"]
                L.append(f"- {_ce(r)}: {s['event_type']} {s['event_id']} {s['begin_utc']} to {s['end_utc']} ({s['cz_name']}); reference {g['label']} (grid points positive {g['grid_points']['positive']}, negative {g['grid_points']['negative']}, unknown {g['grid_points']['unknown']}); {'matched at 24 h' if s['matches_reference_episode']['24'] else 'not matched at 24 h'}.")
    if not any_:
        L.append("- none")
    L.append("")
    L.append("### (B) Replayed flood reports against Storm Events")
    L.append("")
    L.append("| Policy | Flag | Tolerance | Flood-positive reports within a Storm Events entry | Storm Events entries with a flood-positive report |")
    L.append("|---|---|---|---|---|")
    for p in POLICIES:
        for flag in FLAGS:
            for tol in TOLERANCES_H:
                b = P["B"][p][flag][f"pm{tol}h"]
                L.append(f"| {p} | {flag} | plus or minus {tol} h | {_frac(b['positive_reports_within_storm_event'], b['positive_reports'])} | {_frac(b['storm_events_with_positive_report'], b['storm_events'])} |")
    L.append("")
    for p in POLICIES:
        for flag in FLAGS:
            b = P["B"][p][flag]["pm6h"]
            dists = sorted(x for r in rows for x in r["acquisitions"][p][flag]["outside_storm_events_pm6h_hours_to_nearest_entry"])
            after = sum(1 for r in rows for t in r["acquisitions"][p][flag]["outside_storm_events_pm6h"] if r["storm_events"] and parse_iso(t) > max(parse_iso(x["end_utc"]) for x in r["storm_events"]))
            dtxt = f"the other {len(dists)} lie a median {dists[len(dists) // 2]:.1f} h (range {dists[0]:.1f} to {dists[-1]:.1f} h) from the nearest entry, {after} of them after the county's last entry ended" if dists else "none lie outside"
            L.append(f"{p} `{flag}` at plus or minus 6 h: of {b['positive_reports']} flood-positive reports, {b['positive_reports_in_county_events_without_storm_entries']} come from county-events with no Storm Events flood entry at all; among the {b['positive_reports_in_county_events_with_storm_entries']} in county-events that have entries, {_frac(b['positive_reports_within_storm_event'], b['positive_reports_in_county_events_with_storm_entries'])} fall within an entry and {dtxt}.")
            L.append("")
    for p in POLICIES:
        L.append(f"{p}: {P['B'][p]['acquisitions']} acquisitions in total; {P['B'][p]['storm_events_beginning_after_fold']} Storm Events entries begin after the {p} mission folded, when it no longer acquired.")
        L.append("")
    for flag in FLAGS:
        m = P[f"hybrid_unreported_storm_events_by_reference_status_{flag}_pm6h"]
        L.append(f"Storm Events entries with no hybrid `{flag}` report within plus or minus 6 h, by reference status: {', '.join(f'{k} {v}' for k, v in sorted(m.items())) or 'none'}. A Storm Events flood during which the gauge reference was negative or unknown is a gauge-coverage limit (no scorable gauge at flood stage), not a scheduling miss; only entries with a positive reference could have been reported by a better schedule.")
        L.append("")
    L.append("Storm Events entries the hybrid never reported (no `flood_stage_observed` report within plus or minus 6 h):")
    L.append("")
    any_ = False
    for r in rows:
        for s in r["storm_events"]:
            rep = s["reported"]["hybrid"]
            if not rep["flood_stage_observed"]["6"]:
                any_ = True
                g = s["reference_status_pm6h"]
                notes = []
                notes.append(f"is_flooding report {'yes' if rep['is_flooding']['6'] else 'no'}")
                notes.append(f"any hybrid acquisition in span {'yes' if rep['any_acquisition_pm6h'] else 'no'}")
                if rep["begins_after_fold"]:
                    notes.append("begins after hybrid fold")
                notes.append(f"adaptation flood_stage_observed report {'yes' if s['reported']['adaptive']['flood_stage_observed']['6'] else 'no'}")
                L.append(f"- {_ce(r)}: {s['event_type']} {s['event_id']} {s['begin_utc']} to {s['end_utc']}; reference {g['label']} (positive {g['grid_points']['positive']}, negative {g['grid_points']['negative']}, unknown {g['grid_points']['unknown']} grid points); {'; '.join(notes)}.")
    if not any_:
        L.append("- none")
    L.append("")
    L.append("Hybrid flood-positive reports (`flood_stage_observed`) outside every Storm Events entry plus or minus 6 h:")
    L.append("")
    any_ = False
    for r in rows:
        out = r["acquisitions"]["hybrid"]["flood_stage_observed"]["outside_storm_events_pm6h"]
        if out:
            any_ = True
            L.append(f"- {_ce(r)}: {len(out)} of {r['acquisitions']['hybrid']['flood_stage_observed']['positive_reports']} ({out[0]} to {out[-1]}){' (county has no Storm Events flood entry in the window)' if not r['storm_events'] else ''}.")
    if not any_:
        L.append("- none")
    L.append("")
    L.append("### Per county-event rows (primary tolerance, plus or minus 6 h)")
    L.append("")
    L.append("Columns: reference episodes (matched to Storm Events); Storm Events Flood/Flash Flood entries (matched to reference episodes); hybrid flood_stage_observed reports (within Storm Events entries); Storm Events entries with a hybrid flood_stage_observed report; the same two for adaptation; hybrid is_flooding reports (within); other types in window.")
    L.append("")
    L.append("| County-event | Storm Events CZ_NAME | Ref episodes | SE entries | Hybrid FS reports in SE | SE with hybrid FS report | Adapt FS reports in SE | SE with adapt FS report | Hybrid is_flooding in SE | Other types |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        ne = len(r["reference_episodes"])
        nem = sum(1 for e in r["reference_episodes"] if e["matches_storm_event"]["6"])
        ns = len(r["storm_events"])
        nsm = sum(1 for s in r["storm_events"] if s["matches_reference_episode"]["6"])
        h = r["acquisitions"]["hybrid"]
        a = r["acquisitions"]["adaptive"]
        hs = sum(1 for s in r["storm_events"] if s["reported"]["hybrid"]["flood_stage_observed"]["6"])
        as_ = sum(1 for s in r["storm_events"] if s["reported"]["adaptive"]["flood_stage_observed"]["6"])
        other = ", ".join(f"{k} {v}" for k, v in r["storm_event_type_counts_in_window"].items() if k not in MAIN_TYPES) or "none"
        L.append(f"| {_ce(r)} | {', '.join(r['storm_cz_names']) or 'none'} | {nem}/{ne} | {nsm}/{ns} | {h['flood_stage_observed']['within_storm_event_pm6h']}/{h['flood_stage_observed']['positive_reports']} | {hs}/{ns} | {a['flood_stage_observed']['within_storm_event_pm6h']}/{a['flood_stage_observed']['positive_reports']} | {as_}/{ns} | {h['is_flooding']['within_storm_event_pm6h']}/{h['is_flooding']['positive_reports']} | {other} |")
    L.append("")
    return L


# ---------------------------------------------------------------------------


def _strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--storm-dir", type=Path, required=True, help="directory holding StormEvents_details-ftp_v1.0_dYYYY_c*.csv.gz")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--studies", default="study3,study2", help="comma-separated subset of study3,study2")
    args = ap.parse_args(argv)
    wanted_studies = [s for s in STUDIES if s["key"] in args.studies.split(",")]

    tasks = [t for s in wanted_studies for t in county_events(s)]
    entries = {t["result_path"]: json.loads(Path(t["result_path"]).read_text())["corpus_manifest_entry"] for t in tasks}
    years = sorted({parse_iso(e["analysis_start"]).year for e in entries.values()} | {(parse_iso(e["analysis_end"]) - timedelta(seconds=1)).year for e in entries.values()})
    _require(all(parse_iso(e["analysis_start"]).year == (parse_iso(e["analysis_end"]) - timedelta(seconds=1)).year for e in entries.values()), "an analysis window crosses a calendar year")
    # Files are indexed by begin year; every window lies inside one calendar year (checked), so an entry overlapping it begins in that year or, if it began in an earlier year and ran into the window, would be missed. No window starts before 13 March, so that case is not expected; it is recorded as a limitation.
    keys = sorted({storm_key(e["fips"]) for e in entries.values()})
    storm_by_key, zone_rows, storm_files, parse_checks = load_storm_events(args.storm_dir, years, keys)

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        replays = {r["result_path"]: r for r in pool.map(_replay_task, tasks, chunksize=1)}

    studies_out = []
    for s in wanted_studies:
        rows = [analyse_county_event(t["result_path"], storm_by_key[storm_key(entries[t["result_path"]]["fips"])], zone_rows, replays[t["result_path"]]) for t in tasks if t["study"] == s["key"]]
        bad_names = [f"{r['county']} {r['storm_cz_names']}" for r in rows if not r["storm_cz_name_check"]]
        _require(not bad_names, f"Storm Events CZ_NAME does not match the county: {bad_names}")
        studies_out.append({"key": s["key"], "label": s["label"], "study_dir": _rel(Path(s["dir"])), "anchor_mode": s["anchor_mode"], "pooled": pooled(rows), "county_events": rows})

    summary = {
        "schema": "sentinel-posthoc-storm-events/1",
        "post_hoc": True,
        "generated_on": datetime.now(timezone.utc).date().isoformat(),
        "generator": "eval/posthoc_storm_events.py",
        "caveat": "Storm Events is a report-based record compiled by NWS offices from spotter, media, emergency-management, and official reports, with its own timing and coverage errors; this is a comparison with an independent record, not a validation against ground truth.",
        "storm_source": STORM_URL,
        "storm_files": storm_files,
        "storm_parse_checks": parse_checks,
        "storm_filters": {"main_event_types": list(MAIN_TYPES), "side_event_types_counted_only": list(SIDE_TYPES), "cz_type": "C", "key": "STATE_FIPS + CZ_FIPS == county FIPS", "state_fips_aliases_census_to_storm_events": STORM_STATE_FIPS_ALIASES, "window_rule": "entry begin < analysis_end and entry end >= analysis_start"},
        "timezone_rule": "UTC = local BEGIN/END_DATE_TIME minus the signed hour offset in CZ_TIMEZONE (e.g. CST-6 adds 6 h); all matched labels are standard-time labels, applied without daylight-saving adjustment",
        "tolerances_hours": list(TOLERANCES_H),
        "primary_tolerance_hours": PRIMARY_TOLERANCE_H,
        "reference_truth_max_age_hours": PRIMARY_TRUTH_MAX_AGE_HOURS,
        "flags": {"flood_stage_observed": "some gauge in the acquisition at minor flood stage or above (score >= 2)", "is_flooding": "county regional verdict at action stage or above (score >= 1); operational flag"},
        "studies": studies_out,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(_strip(summary), indent=2, sort_keys=False) + "\n")
    (OUT_DIR / "summary.md").write_text(render_markdown(summary))
    for st in studies_out:
        P = st["pooled"]
        a = P["A"]["pm6h"]
        print(f"{st['label']}: episodes matched {a['reference_episodes_matched']}/{a['reference_episodes']}, storm events matched {a['storm_events_matched']}/{a['storm_events']}")
        for p in POLICIES:
            for flag in FLAGS:
                b = P["B"][p][flag]["pm6h"]
                print(f"  {p} {flag}: reports in SE {b['positive_reports_within_storm_event']}/{b['positive_reports']}; SE reported {b['storm_events_with_positive_report']}/{b['storm_events']}")
    print(f"wrote {_rel(OUT_DIR / 'summary.json')} and {_rel(OUT_DIR / 'summary.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
