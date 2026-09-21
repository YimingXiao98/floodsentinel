"""Part B of the v5 ontology protocol: T2 verification on the anticipation gap.

For each stratum county-event: derive the T2 trigger corpus from the frozen
GDELT news corpus through the production detector (parameters carried frozen),
merge with the official VTEC trigger corpus, run the three prespecified arms
under the frozen v4 additive configuration, and evaluate E1/E2/E3 on the
mechanically defined unwarned/warned episode strata. Fail-closed: the
protocol, v4 manifest, news corpora, and trigger corpora are hash-bound in
the output artifact. See ``eval/ONTOLOGY_STUDY_PROTOCOL.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.build_t2_trigger_corpus import build as build_t2  # noqa: E402
from eval.ground_truth import apply_threshold_compatibility  # noqa: E402
from eval.metrics import annotate_run, annotate_run_on_grid, rq2_flood_episodes  # noqa: E402
from eval.run_replay import run as replay_run  # noqa: E402
from eval.run_study import FROZEN_REPLAY_ENVIRONMENT, _sha256, _temporary_environment, _write_json  # noqa: E402
from eval.trigger_metrics import trigger_accounting_metrics  # noqa: E402
from eval.vtec_trigger import load_trigger_corpus, parse_iso, trigger_events_sorted  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

PROTOCOL = _ROOT / "eval" / "ONTOLOGY_STUDY_PROTOCOL.md"
V4_MANIFEST = _ROOT / "eval" / "corpus" / "study_v4" / "manifest.json"
V4_RESULTS = _ROOT / "eval" / "results" / "study_v4_additive"
V5_DIR = _ROOT / "eval" / "corpus" / "study_v5"

#: Stratum county-events and their news corpora (protocol-frozen listing).
STRATUM = (
    ("Marion", "12083", "marion_fl_ian_gdelt.json"),
    ("Sarasota", "12115", "sarasota_fl_idalia_gdelt.json"),
    ("Seminole", "12117", "seminole_fl_nicole_gdelt.json"),
    ("Marshall", "27089", "marshall_mn_2023_gdelt.json"),
    ("Caguas", "72025", "caguas_pr_maria_gdelt.json"),
    ("Cherokee", "48073", "cherokee_tx_2016_gdelt.json"),
)


def _entry_for(manifest: Dict[str, Any], fips: str) -> Dict[str, Any]:
    return next(
        e
        for e in manifest["artifacts"]
        if e["fips"] == fips and e["analysis_role"] == "episode_evaluation"
    )


def _episode_strata(
    entry: Dict[str, Any], policy: Dict[str, Any]
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """(unwarned, warned) reference episodes, protocol stratum definition."""
    excluded = [
        str(r.get("site_no"))
        for r in (entry.get("threshold_exclusions") or [])
        if r.get("site_no")
    ]
    corpus, _ = apply_threshold_compatibility(
        load_corpus(_ROOT / entry["path"]), excluded_site_ids=excluded
    )
    empty = {"window": {"start": entry["analysis_start"], "end": entry["analysis_end"]}, "ticks": []}
    grid = annotate_run_on_grid(empty, corpus=corpus, events=[], interval_seconds=900)
    episodes = rq2_flood_episodes(grid)["episodes"]
    qualifying = {tuple(t.split(".")) for t in policy["qualifying"] if t != "T2.W"}
    duration = timedelta(seconds=policy["burst_duration_seconds"])
    windows = []
    for ev in trigger_events_sorted(load_trigger_corpus(_ROOT / entry["trigger_corpus"])):
        if (ev["phenomena"], ev["significance"]) not in qualifying:
            continue
        avail = parse_iso(ev["available_at"])
        begins = parse_iso(ev["begins_at"]) if ev.get("begins_at") else avail
        start = max(avail, begins)
        windows.append((start, start + duration))
    unwarned, warned = [], []
    for ep in episodes:
        onset = parse_iso(ep["observed_start"])
        anticipated = any(w0 <= onset and w1 > onset for w0, w1 in windows)
        row = {
            "onset": ep["observed_start"],
            "end": ep["last_observed_positive_end_exclusive"],
        }
        (warned if anticipated else unwarned).append(row)
    return unwarned, warned


def _stratum_outcomes(
    run: Dict[str, Any],
    corpus: Dict[str, Any],
    unwarned: Sequence[Dict[str, str]],
) -> Dict[str, Any]:
    """Per-stratum episode observation and unobserved duration for one run."""
    acquisition_ticks = annotate_run(run, corpus=corpus, events=[])
    grid = annotate_run_on_grid(run, corpus=corpus, events=[], interval_seconds=900)
    episodes = rq2_flood_episodes(grid, acquisition_ticks=acquisition_ticks)["episodes"]
    unwarned_onsets = {row["onset"] for row in unwarned}
    out = {"unwarned": {"episodes": 0, "observed": 0, "unobserved_hours": 0.0},
           "warned": {"episodes": 0, "observed": 0, "unobserved_hours": 0.0}}
    for ep in episodes:
        key = "unwarned" if ep["observed_start"] in unwarned_onsets else "warned"
        out[key]["episodes"] += 1
        out[key]["observed"] += int(ep["schedule_observed_episode"])
        out[key]["unobserved_hours"] += ep["unobserved_flood_stage_duration_hours"]
    for key in out:
        out[key]["unobserved_hours"] = round(out[key]["unobserved_hours"], 4)
    return out


def _run_county(task: Dict[str, Any]) -> Dict[str, Any]:
    entry = task["entry"]
    policy = task["policy"]
    excluded = sorted(
        {
            str(r.get("site_no"))
            for r in (entry.get("threshold_exclusions") or [])
            if r.get("site_no")
        }
    )
    corpus, _ = apply_threshold_compatibility(
        load_corpus(_ROOT / entry["path"]), excluded_site_ids=excluded
    )
    common = dict(
        corpus_path=str(_ROOT / entry["path"]),
        county=entry["county"],
        state=entry["state"],
        fips=entry["fips"],
        start=entry["analysis_start"],
        end=entry["analysis_end"],
        max_staleness_hours=6.0,
        threshold_excluded_site_ids=excluded,
        trigger_poll_interval_seconds=policy["poll_interval_seconds"],
    )
    base = {
        "enabled": True,
        "burst_interval_seconds": policy["burst_interval_seconds"],
        "burst_duration_seconds": policy["burst_duration_seconds"],
        "anchor_mode": policy["anchor_mode"],
    }
    arms = {
        "official": ({**base, "qualifying": policy["qualifying"]}, task["official_corpus"]),
        "official_plus_t2": (
            {**base, "qualifying": policy["qualifying"] + ["T2.W"]},
            task["merged_corpus"],
        ),
        "t2_only": ({**base, "qualifying": ["T2.W"]}, task["merged_corpus"]),
    }
    unwarned = task["unwarned"]
    rows = {}
    for name, (trig, corpus_path) in arms.items():
        run = replay_run(**common, trigger=trig, trigger_corpus_path=corpus_path)
        strata = _stratum_outcomes(run, corpus, unwarned)
        accounting = run["verification_trigger"]
        metrics = trigger_accounting_metrics(run, corpus=corpus)
        rows[name] = {
            "acquisitions": len(run["ticks"]),
            "strata": strata,
            "trigger_attributable_acquisitions": accounting[
                "trigger_attributable_acquisitions"
            ],
            "acquisitions_by_reason": accounting["acquisitions_by_reason"],
            "trigger_stream_consultations": accounting["trigger_stream_consultations"],
            "verification_acquisitions_during_known_clear": (
                metrics["verification_acquisitions_during_known_clear"]
                if metrics
                else None
            ),
        }
    return {
        "county": entry["county"],
        "state": entry["state"],
        "fips": entry["fips"],
        "event": entry.get("event"),
        "n_unwarned": len(unwarned),
        "arms": rows,
    }


def _init_worker() -> None:
    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="eval/results/study_v5_ontology/t2_gap_study.json"
    )
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)

    manifest = json.loads(V4_MANIFEST.read_text(encoding="utf-8"))
    policy = dict(manifest["frozen_trigger_policy"])

    tasks = []
    inputs = []
    for county, fips, news_name in STRATUM:
        entry = _entry_for(manifest, fips)
        news_path = V5_DIR / news_name
        if not news_path.is_file():
            raise FileNotFoundError(news_path)
        slug = news_name.replace("_gdelt.json", "")
        t2_path = V5_DIR / f"{slug}_t2_triggers.json"
        if not t2_path.is_file():
            t2 = build_t2(
                news_corpus_path=str(news_path),
                county=entry["county"],
                state=entry["state"],
                fips=entry["fips"],
                start=entry["analysis_start"],
                end=entry["analysis_end"],
                poll_interval_seconds=policy["poll_interval_seconds"],
            )
            t2_path.write_text(json.dumps(t2, indent=1) + "\n", encoding="utf-8")
        merged_path = V5_DIR / f"{slug}_merged_triggers.json"
        official = load_trigger_corpus(_ROOT / entry["trigger_corpus"])
        t2_corpus = load_trigger_corpus(t2_path)
        merged = dict(official)
        merged["events"] = sorted(
            official["events"] + t2_corpus["events"],
            key=lambda e: (e["available_at"], e["event_key"]),
        )
        merged_path.write_text(json.dumps(merged, indent=1) + "\n", encoding="utf-8")
        unwarned, warned = _episode_strata(entry, policy)
        tasks.append(
            {
                "entry": entry,
                "policy": policy,
                "official_corpus": str(_ROOT / entry["trigger_corpus"]),
                "merged_corpus": str(merged_path),
                "unwarned": unwarned,
            }
        )
        inputs.append(
            {
                "county": county,
                "fips": fips,
                "news_corpus": str(news_path.relative_to(_ROOT)),
                "news_corpus_sha256": _sha256(news_path),
                "n_news_articles": len(json.loads(news_path.read_text())["articles"]),
                "t2_corpus": str(t2_path.relative_to(_ROOT)),
                "t2_corpus_sha256": _sha256(t2_path),
                "n_t2_events": len(t2_corpus["events"]),
                "merged_corpus": str(merged_path.relative_to(_ROOT)),
                "merged_corpus_sha256": _sha256(merged_path),
                "official_corpus_sha256": _sha256(_ROOT / entry["trigger_corpus"]),
                "n_unwarned_episodes": len(unwarned),
                "n_warned_episodes": len(warned),
            }
        )

    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker) as pool:
        county_rows = list(pool.map(_run_county, tasks, chunksize=1))

    def _pool(arm: str, stratum: str, field: str) -> float:
        return round(
            sum(r["arms"][arm]["strata"][stratum][field] for r in county_rows), 4
        )

    e1_obs_gain = _pool("official_plus_t2", "unwarned", "observed") - _pool(
        "official", "unwarned", "observed"
    )
    e1_unobs_delta = _pool("official_plus_t2", "unwarned", "unobserved_hours") - _pool(
        "official", "unwarned", "unobserved_hours"
    )
    e1 = e1_obs_gain > 0 or (e1_obs_gain == 0 and e1_unobs_delta < 0)
    e2 = (
        _pool("official_plus_t2", "warned", "observed")
        >= _pool("official", "warned", "observed")
    ) and (
        _pool("official_plus_t2", "unwarned", "unobserved_hours")
        + _pool("official_plus_t2", "warned", "unobserved_hours")
        <= _pool("official", "unwarned", "unobserved_hours")
        + _pool("official", "warned", "unobserved_hours")
    )
    # E3: budget within 60% of pooled fixed-1h offset-0 for these counties,
    # taken from the immutable v4 tables.
    v4_tables = json.loads((V4_RESULTS / "tables.json").read_text(encoding="utf-8"))
    hourly = {
        (r["fips"]): r["acquisitions"]
        for r in v4_tables["schedule_rows"]
        if r["policy"] == "fixed_1h_offset_0h"
    }
    pooled_hourly = sum(hourly[r["fips"]] for r in county_rows)
    pooled_plus = sum(r["arms"]["official_plus_t2"]["acquisitions"] for r in county_rows)
    e3 = pooled_plus <= 0.60 * pooled_hourly

    result = {
        "schema": "sentinel-v5-t2-gap-study/1",
        "protocol": "eval/ONTOLOGY_STUDY_PROTOCOL.md",
        "protocol_sha256": _sha256(PROTOCOL),
        "v4_manifest_sha256": _sha256(V4_MANIFEST),
        "role": "prespecified_extension_with_disclosed_provenance",
        "frozen_policy": policy,
        "inputs": inputs,
        "county_rows": county_rows,
        "pooled": {
            arm: {
                stratum: {
                    "episodes": _pool(arm, stratum, "episodes"),
                    "observed": _pool(arm, stratum, "observed"),
                    "unobserved_hours": _pool(arm, stratum, "unobserved_hours"),
                }
                for stratum in ("unwarned", "warned")
            }
            for arm in ("official", "official_plus_t2", "t2_only")
        },
        "criterion": {
            "E1_gap_value": {
                "pass": bool(e1),
                "unwarned_observed_gain": e1_obs_gain,
                "unwarned_unobserved_delta_hours": round(e1_unobs_delta, 4),
            },
            "E2_no_harm": {"pass": bool(e2)},
            "E3_budget": {
                "pass": bool(e3),
                "pooled_official_plus_t2_acquisitions": pooled_plus,
                "pooled_hourly_offset0_acquisitions": pooled_hourly,
            },
            "criterion_pass": bool(e1 and e2 and e3),
        },
    }
    out = _ROOT / args.out
    _write_json(out, result)
    print(
        json.dumps(
            {
                "out": str(out),
                "sha256": _sha256(out),
                "criterion_pass": result["criterion"]["criterion_pass"],
                "E1": result["criterion"]["E1_gap_value"],
                "E2": e2,
                "E3": e3,
                "pooled": result["pooled"],
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
