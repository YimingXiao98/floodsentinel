"""v4 development tuning grid: additive-anchor hybrid on 13 development cases.

Development scaffolding, never confirmatory. The pool is the four v2
development county-events plus the nine v3 held-out county-events, which were
demoted to development after their outcomes were examined
(`eval/V3_HELDOUT_REPORT.md`). The selection criterion is fixed in
`eval/HYBRID_STUDY_PROTOCOL_V4.md` *before* this grid is executed; this script
implements it mechanically and emits every row it considered.

Output: ``eval/results/study_v4_dev/dev_grid.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.baselines import matched_budget_schedule_specs  # noqa: E402
from eval.ground_truth import apply_threshold_compatibility  # noqa: E402
from eval.metrics import evaluate  # noqa: E402
from eval.run_dev_grid import (  # noqa: E402
    DEV_CASES as V2_DEV_CASES,
    BURST_INTERVALS_SECONDS,
    BURST_DURATIONS_SECONDS,
    QUALIFYING_SETS,
)
from eval.run_replay import run as replay_run  # noqa: E402
from eval.run_study import FROZEN_REPLAY_ENVIRONMENT  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

ANCHOR_MODE = "additive"

#: Protocol budget guard: 60% of the pooled nominal hourly budget over the
#: thirteen development county-events (13 x 312 nominal hourly acquisitions).
POOLED_HOURLY_BUDGET = 13 * 312
BUDGET_GUARD_FRACTION = 0.60


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dev_cases() -> List[Dict[str, Any]]:
    cases = [dict(case, threshold_excluded_site_ids=[]) for case in V2_DEV_CASES]
    manifest = json.loads(
        (_ROOT / "eval/corpus/study_v3/manifest.json").read_text(encoding="utf-8")
    )
    for entry in manifest["artifacts"]:
        if entry.get("analysis_role") != "episode_evaluation":
            continue
        cases.append(
            {
                "county": entry["county"],
                "state": entry["state"],
                "fips": entry["fips"],
                "event": entry["event"],
                "corpus": entry["path"],
                "trigger_corpus": entry["trigger_corpus"],
                "start": entry["analysis_start"],
                "end": entry["analysis_end"],
                "threshold_excluded_site_ids": sorted(
                    {
                        str(row.get("site_no"))
                        for row in (entry.get("threshold_exclusions") or [])
                        if row.get("site_no")
                    }
                ),
            }
        )
    return cases


def grid_configs() -> List[Dict[str, Any]]:
    configs = []
    for qual_name, qualifying in QUALIFYING_SETS:
        for interval in BURST_INTERVALS_SECONDS:
            for duration in BURST_DURATIONS_SECONDS:
                configs.append(
                    {
                        "config_id": (
                            f"{qual_name}_i{interval // 60}m_d{duration // 3600}h_additive"
                        ),
                        "qualifying_name": qual_name,
                        "qualifying": list(qualifying),
                        "burst_interval_seconds": interval,
                        "burst_duration_seconds": duration,
                        "anchor_mode": ANCHOR_MODE,
                    }
                )
    return configs


def _init_worker() -> None:
    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)


_CORPUS_CACHE: Dict[str, Any] = {}


def _prepared_corpus(path: str, excluded: Sequence[str]) -> Dict[str, Any]:
    key = f"{path}|{','.join(excluded)}"
    if key not in _CORPUS_CACHE:
        corpus, _ = apply_threshold_compatibility(
            load_corpus(_ROOT / path), excluded_site_ids=excluded
        )
        _CORPUS_CACHE[key] = corpus
    return _CORPUS_CACHE[key]


def _run_task(task: Dict[str, Any]) -> Dict[str, Any]:
    case = task["case"]
    run = replay_run(
        corpus_path=str(_ROOT / case["corpus"]),
        county=case["county"],
        state=case["state"],
        fips=case["fips"],
        start=case["start"],
        end=case["end"],
        max_staleness_hours=6.0,
        threshold_excluded_site_ids=case["threshold_excluded_site_ids"],
        **task.get("run_kwargs", {}),
    )
    metrics = evaluate(
        run,
        corpus=_prepared_corpus(case["corpus"], case["threshold_excluded_site_ids"]),
        events=[],
    )
    episodes = metrics["rq2_event_episodes"]
    trigger = run.get("verification_trigger") or None
    row: Dict[str, Any] = {
        "county": case["county"],
        "fips": case["fips"],
        "event": case["event"],
        "policy": task["policy"],
        "config_id": task.get("config_id"),
        "acquisitions": len(run.get("ticks") or []),
        "observed_flood_episodes": episodes["n_observed_flood_episodes"],
        "episodes_observed_by_schedule": episodes["episodes_observed_by_schedule"],
        "episode_delays_hours": [
            e["schedule_flood_stage_observation_lag_hours"]
            for e in episodes["episodes"]
        ],
        "unobserved_flood_stage_duration_hours": episodes[
            "unobserved_flood_stage_duration_hours"
        ],
        "folded_at": metrics["rq2_timeliness"]["folded_at"],
    }
    for key in ("target_acquisitions", "schedule_offset_seconds"):
        if key in task:
            row[key] = task[key]
    if trigger:
        row["trigger_attributable_acquisitions"] = trigger[
            "trigger_attributable_acquisitions"
        ]
        row["acquisitions_by_reason"] = trigger["acquisitions_by_reason"]
    return row


def _median(values) -> Optional[float]:
    numeric = [float(v) for v in values if v is not None]
    return round(statistics.median(numeric), 4) if numeric else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="eval/results/study_v4_dev/dev_grid.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    cases = dev_cases()
    configs = grid_configs()
    tasks: List[Dict[str, Any]] = []
    for case in cases:
        tasks.append({"case": case, "policy": "adaptive", "run_kwargs": {}})
        for config in configs:
            tasks.append(
                {
                    "case": case,
                    "policy": "hybrid",
                    "config_id": config["config_id"],
                    "run_kwargs": {
                        "trigger": {
                            "enabled": True,
                            "qualifying": config["qualifying"],
                            "burst_interval_seconds": config["burst_interval_seconds"],
                            "burst_duration_seconds": config["burst_duration_seconds"],
                            "anchor_mode": config["anchor_mode"],
                        },
                        "trigger_corpus_path": str(_ROOT / case["trigger_corpus"]),
                    },
                }
            )

    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker) as pool:
        rows = list(pool.map(_run_task, tasks, chunksize=2))

    matched_tasks: List[Dict[str, Any]] = []
    for row in rows:
        if row["policy"] != "hybrid":
            continue
        case = next(c for c in cases if c["fips"] == row["fips"] and c["event"] == row["event"])
        for spec in matched_budget_schedule_specs(
            start=case["start"], end=case["end"], target_acquisitions=row["acquisitions"]
        ):
            matched_tasks.append(
                {
                    "case": case,
                    "policy": f"matched_to_hybrid_{row['config_id']}",
                    "config_id": row["config_id"],
                    "target_acquisitions": row["acquisitions"],
                    "schedule_offset_seconds": spec["schedule_offset_seconds"],
                    "run_kwargs": {
                        "fixed_interval_seconds": spec["fixed_interval_seconds"],
                        "schedule_offset_seconds": spec["schedule_offset_seconds"],
                    },
                }
            )
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker) as pool:
        matched_rows = list(pool.map(_run_task, matched_tasks, chunksize=2))

    guard_max = int(POOLED_HOURLY_BUDGET * BUDGET_GUARD_FRACTION)
    config_summary: List[Dict[str, Any]] = []
    for config in configs:
        hybrid_rows = [
            r for r in rows
            if r["policy"] == "hybrid" and r["config_id"] == config["config_id"]
        ]
        pooled_acq = sum(r["acquisitions"] for r in hybrid_rows)
        pooled_obs = sum(r["episodes_observed_by_schedule"] for r in hybrid_rows)
        pooled_unobs = round(
            sum(r["unobserved_flood_stage_duration_hours"] for r in hybrid_rows), 4
        )
        wins_vs_median = 0
        margin = 0.0
        per_county = []
        for r in hybrid_rows:
            offs = [
                m["unobserved_flood_stage_duration_hours"]
                for m in matched_rows
                if m["config_id"] == config["config_id"]
                and m["fips"] == r["fips"]
                and m["event"] == r["event"]
            ]
            med = _median(offs)
            h = r["unobserved_flood_stage_duration_hours"]
            wins_vs_median += int(med is not None and h < med)
            margin += h - (med or 0.0)
            per_county.append(
                {
                    "county": r["county"],
                    "event": r["event"],
                    "hybrid_unobserved_hours": h,
                    "offset_median_unobserved_hours": med,
                    "offset_min_unobserved_hours": round(min(offs), 4) if offs else None,
                    "hybrid_acquisitions": r["acquisitions"],
                    "hybrid_episodes": (
                        f"{r['episodes_observed_by_schedule']}/{r['observed_flood_episodes']}"
                    ),
                }
            )
        config_summary.append(
            {
                **config,
                "pooled_acquisitions": pooled_acq,
                "budget_guard_max": guard_max,
                "passes_budget_guard": pooled_acq <= guard_max,
                "pooled_episodes_observed": pooled_obs,
                "pooled_unobserved_hours": pooled_unobs,
                "county_events_below_offset_median": wins_vs_median,
                "pooled_margin_vs_offset_median_hours": round(margin, 4),
                "per_county": per_county,
            }
        )

    eligible = [c for c in config_summary if c["passes_budget_guard"]]
    selected = None
    if eligible:
        best = max(c["pooled_episodes_observed"] for c in eligible)
        finalists = [c for c in eligible if c["pooled_episodes_observed"] == best]
        finalists.sort(
            key=lambda c: (
                -c["county_events_below_offset_median"],
                c["pooled_margin_vs_offset_median_hours"],
                c["pooled_unobserved_hours"],
                c["pooled_acquisitions"],
                c["burst_duration_seconds"],
                -c["burst_interval_seconds"],
                len(c["qualifying"]),
            )
        )
        selected = finalists[0]["config_id"]

    out = {
        "schema": "sentinel-v4-dev-grid/1",
        "role": "development_tuning_only_not_confirmatory",
        "protocol": "eval/HYBRID_STUDY_PROTOCOL_V4.md",
        "anchor_mode": ANCHOR_MODE,
        "dev_pool_note": (
            "four v2 development county-events plus the nine demoted v3 "
            "held-out county-events; nothing measured here is confirmatory"
        ),
        "frozen_replay_environment": dict(FROZEN_REPLAY_ENVIRONMENT),
        "selection_criterion": {
            "budget_guard": (
                f"pooled hybrid acquisitions <= {guard_max} "
                f"({BUDGET_GUARD_FRACTION:.0%} of pooled nominal hourly {POOLED_HOURLY_BUDGET})"
            ),
            "objective": "max pooled episodes observed",
            "tie_breaks": [
                "max county-events strictly below offset median",
                "min pooled margin vs offset median",
                "min pooled unobserved hours",
                "min pooled acquisitions",
                "shorter burst duration",
                "longer burst cadence",
                "smaller qualifying set",
            ],
        },
        "inputs": {
            f"{c['fips']}|{c['event']}": {
                "corpus": c["corpus"],
                "corpus_sha256": _sha256(_ROOT / c["corpus"]),
                "trigger_corpus": c["trigger_corpus"],
                "trigger_corpus_sha256": _sha256(_ROOT / c["trigger_corpus"]),
            }
            for c in cases
        },
        "selected_config_id": selected,
        "config_summary": config_summary,
        "rows": rows,
        "matched_budget_rows": matched_rows,
    }
    target = _ROOT / args.out
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "out": str(target),
                "sha256": _sha256(target),
                "n_rows": len(rows),
                "n_matched_rows": len(matched_rows),
                "selected_config_id": selected,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
