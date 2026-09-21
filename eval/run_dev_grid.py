"""Development tuning grid for the v3 verification-triggered hybrid policy.

Development scaffolding, never confirmatory: it runs only on the four v2
development county-events, whose results diagnose and tune. The selection
criterion is fixed in ``eval/HYBRID_STUDY_PROTOCOL.md`` *before* this grid is
executed; this script implements that criterion mechanically and emits every
row it considered, so the choice is reproducible and auditable.

Output: ``eval/results/study_v3_dev/dev_grid.json``.
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.baselines import (  # noqa: E402
    fixed_schedule_specs,
    matched_budget_schedule_specs,
)
from eval.ground_truth import apply_threshold_compatibility  # noqa: E402
from eval.metrics import evaluate  # noqa: E402
from eval.run_replay import run as replay_run  # noqa: E402
from eval.run_study import FROZEN_REPLAY_ENVIRONMENT  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

DEV_CASES: Tuple[Dict[str, str], ...] = (
    {
        "county": "Harris",
        "state": "TX",
        "fips": "48201",
        "event": "Hurricane Harvey",
        "corpus": "eval/corpus/study/harris_harvey_2017.json",
        "trigger_corpus": "eval/corpus/study_v3/dev/harris_vtec_triggers.json",
        "start": "2017-08-24T05:00:00Z",
        "end": "2017-09-06T05:00:00Z",
    },
    {
        "county": "Duval",
        "state": "FL",
        "fips": "12031",
        "event": "Hurricane Irma",
        "corpus": "eval/corpus/study/duval_irma_2017.json",
        "trigger_corpus": "eval/corpus/study_v3/dev/duval_vtec_triggers.json",
        "start": "2017-09-08T04:00:00Z",
        "end": "2017-09-21T04:00:00Z",
    },
    {
        "county": "Hillsborough",
        "state": "FL",
        "fips": "12057",
        "event": "Hurricane Irma",
        "corpus": "eval/corpus/study/hillsborough_irma_2017.json",
        "trigger_corpus": "eval/corpus/study_v3/dev/hillsborough_vtec_triggers.json",
        "start": "2017-09-08T04:00:00Z",
        "end": "2017-09-21T04:00:00Z",
    },
    {
        "county": "Polk",
        "state": "FL",
        "fips": "12105",
        "event": "Hurricane Irma",
        "corpus": "eval/corpus/study/polk_irma_2017.json",
        "trigger_corpus": "eval/corpus/study_v3/dev/polk_vtec_triggers.json",
        "start": "2017-09-08T04:00:00Z",
        "end": "2017-09-21T04:00:00Z",
    },
)

QUALIFYING_SETS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("warnings", ("FF.W", "FA.W", "FL.W")),
    ("warnings_watches", ("FF.W", "FA.W", "FL.W", "FF.A", "FA.A", "FL.A")),
    (
        "warnings_watches_advisories",
        ("FF.W", "FA.W", "FL.W", "FF.A", "FA.A", "FL.A", "FA.Y", "FL.Y", "FF.Y"),
    ),
)
BURST_INTERVALS_SECONDS: Tuple[int, ...] = (3600, 7200)
BURST_DURATIONS_SECONDS: Tuple[int, ...] = (21600, 43200, 86400)

#: Pooled hourly-clock acquisitions across the four development windows
#: (4 x 312 nominal hourly acquisitions), and the protocol budget guard.
POOLED_HOURLY_BUDGET = 1248
BUDGET_GUARD_FRACTION = 0.60


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def grid_configs() -> List[Dict[str, Any]]:
    configs = []
    for qual_name, qualifying in QUALIFYING_SETS:
        for interval in BURST_INTERVALS_SECONDS:
            for duration in BURST_DURATIONS_SECONDS:
                configs.append(
                    {
                        "config_id": (
                            f"{qual_name}_i{interval // 60}m_d{duration // 3600}h"
                        ),
                        "qualifying_name": qual_name,
                        "qualifying": list(qualifying),
                        "burst_interval_seconds": interval,
                        "burst_duration_seconds": duration,
                    }
                )
    return configs


def _init_worker() -> None:
    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)


_CORPUS_CACHE: Dict[str, Dict[str, Any]] = {}


def _prepared_corpus(path: str) -> Dict[str, Any]:
    if path not in _CORPUS_CACHE:
        corpus, _ = apply_threshold_compatibility(load_corpus(path))
        _CORPUS_CACHE[path] = corpus
    return _CORPUS_CACHE[path]


def _run_task(task: Dict[str, Any]) -> Dict[str, Any]:
    case = task["case"]
    common = dict(
        corpus_path=case["corpus"],
        county=case["county"],
        state=case["state"],
        fips=case["fips"],
        start=case["start"],
        end=case["end"],
        max_staleness_hours=6.0,
    )
    run = replay_run(**common, **task.get("run_kwargs", {}))
    metrics = evaluate(run, corpus=_prepared_corpus(case["corpus"]), events=[])
    episodes = metrics["rq2_event_episodes"]
    timeliness = metrics["rq2_timeliness"]
    trigger = run.get("verification_trigger") or None
    row: Dict[str, Any] = {
        "county": case["county"],
        "fips": case["fips"],
        "policy": task["policy"],
        "config_id": task.get("config_id"),
        "schedule": run.get("schedule"),
        "acquisitions": timeliness["acquisitions"],
        "material_acquisitions": timeliness["material_acquisitions"],
        "observed_flood_episodes": episodes["n_observed_flood_episodes"],
        "episodes_observed_by_schedule": episodes["episodes_observed_by_schedule"],
        "episode_delays_hours": [
            e["schedule_flood_stage_observation_lag_hours"]
            for e in episodes["episodes"]
        ],
        "unobserved_flood_stage_duration_hours": episodes[
            "unobserved_flood_stage_duration_hours"
        ],
        "known_clear_duration_hours": episodes["known_clear_duration_hours"],
        "observed_flood_duration_hours": episodes["observed_flood_duration_hours"],
        "unknown_truth_duration_hours": episodes["unknown_truth_duration_hours"],
        "phase_changes": timeliness["phase_changes"],
        "folded_at": timeliness["folded_at"],
    }
    for key in ("target_acquisitions", "schedule_offset_seconds"):
        if key in task:
            row[key] = task[key]
    if trigger:
        row["trigger"] = {
            "consultations": trigger["trigger_stream_consultations"],
            "immediate_fires": trigger["immediate_fires"],
            "acquisitions_by_reason": trigger["acquisitions_by_reason"],
            "trigger_attributable_acquisitions": trigger[
                "trigger_attributable_acquisitions"
            ],
            "events_first_seen": len(trigger["events_first_seen"]),
            "burst_spans": trigger["burst_spans"],
        }
        # Unnecessary verification burden: trigger-attributable acquisitions
        # at instants whose 1 h hydrologic reference is known clear.
        from eval.ground_truth import hydrologic_truth_at, parse_iso

        clear = 0
        for tick in run.get("ticks") or []:
            if tick.get("acquisition_due_reason") not in (
                "verification_immediate",
                "verification_burst",
            ):
                continue
            hydro = hydrologic_truth_at(
                _prepared_corpus(case["corpus"]),
                parse_iso(tick["t"]),
                max_age_hours=1.0,
            )
            if hydro["is_flooding"] is False:
                clear += 1
        row["trigger"]["verification_acquisitions_during_known_clear"] = clear
    return row


def _median(values: Sequence[float]) -> Optional[float]:
    values = [float(v) for v in values if v is not None]
    return round(statistics.median(values), 4) if values else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="eval/results/study_v3_dev/dev_grid.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    configs = grid_configs()
    tasks: List[Dict[str, Any]] = []
    for case in DEV_CASES:
        tasks.append({"case": case, "policy": "adaptive", "run_kwargs": {}})
        tasks.append(
            {
                "case": case,
                "policy": "hybrid_trigger_disabled",
                "run_kwargs": {"trigger": {"enabled": False}},
            }
        )
        for spec in fixed_schedule_specs():
            tasks.append(
                {
                    "case": case,
                    "policy": spec["name"],
                    "schedule_offset_seconds": spec["schedule_offset_seconds"],
                    "run_kwargs": {
                        "fixed_interval_seconds": spec["fixed_interval_seconds"],
                        "schedule_offset_seconds": spec["schedule_offset_seconds"],
                    },
                }
            )
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
                            "burst_interval_seconds": config[
                                "burst_interval_seconds"
                            ],
                            "burst_duration_seconds": config[
                                "burst_duration_seconds"
                            ],
                        },
                        "trigger_corpus_path": case["trigger_corpus"],
                    },
                }
            )

    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)
    with ProcessPoolExecutor(
        max_workers=args.workers, initializer=_init_worker
    ) as pool:
        rows = list(pool.map(_run_task, tasks, chunksize=4))

    # Matched-budget offset distributions at each hybrid's realized count.
    matched_tasks: List[Dict[str, Any]] = []
    for row in rows:
        if row["policy"] != "hybrid":
            continue
        case = next(c for c in DEV_CASES if c["fips"] == row["fips"])
        for spec in matched_budget_schedule_specs(
            start=case["start"],
            end=case["end"],
            target_acquisitions=row["acquisitions"],
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
    with ProcessPoolExecutor(
        max_workers=args.workers, initializer=_init_worker
    ) as pool:
        matched_rows = list(pool.map(_run_task, matched_tasks, chunksize=4))

    # Selection per the frozen development criterion.
    guard_max = int(POOLED_HOURLY_BUDGET * BUDGET_GUARD_FRACTION)
    config_summary: List[Dict[str, Any]] = []
    for config in configs:
        hybrid_rows = [
            row
            for row in rows
            if row["policy"] == "hybrid" and row["config_id"] == config["config_id"]
        ]
        pooled_acq = sum(row["acquisitions"] for row in hybrid_rows)
        pooled_observed = sum(
            row["episodes_observed_by_schedule"] for row in hybrid_rows
        )
        pooled_unobserved = round(
            sum(
                row["unobserved_flood_stage_duration_hours"] for row in hybrid_rows
            ),
            4,
        )
        margin = 0.0
        per_county = []
        for row in hybrid_rows:
            offsets = [
                m["unobserved_flood_stage_duration_hours"]
                for m in matched_rows
                if m["config_id"] == config["config_id"]
                and m["fips"] == row["fips"]
            ]
            med = _median(offsets)
            margin += row["unobserved_flood_stage_duration_hours"] - (med or 0.0)
            per_county.append(
                {
                    "county": row["county"],
                    "hybrid_unobserved_hours": row[
                        "unobserved_flood_stage_duration_hours"
                    ],
                    "matched_budget_offset_median_unobserved_hours": med,
                    "matched_budget_offset_min_unobserved_hours": (
                        round(min(offsets), 4) if offsets else None
                    ),
                    "matched_budget_offset_max_unobserved_hours": (
                        round(max(offsets), 4) if offsets else None
                    ),
                    "hybrid_acquisitions": row["acquisitions"],
                    "hybrid_episodes": (
                        f"{row['episodes_observed_by_schedule']}/"
                        f"{row['observed_flood_episodes']}"
                    ),
                }
            )
        config_summary.append(
            {
                **config,
                "pooled_acquisitions": pooled_acq,
                "budget_guard_max": guard_max,
                "passes_budget_guard": pooled_acq <= guard_max,
                "pooled_episodes_observed": pooled_observed,
                "pooled_unobserved_hours": pooled_unobserved,
                "pooled_matched_budget_margin_hours": round(margin, 4),
                "per_county": per_county,
            }
        )

    eligible = [c for c in config_summary if c["passes_budget_guard"]]
    selected = None
    if eligible:
        best_observed = max(c["pooled_episodes_observed"] for c in eligible)
        finalists = [
            c for c in eligible if c["pooled_episodes_observed"] == best_observed
        ]
        finalists.sort(
            key=lambda c: (
                c["pooled_matched_budget_margin_hours"],
                c["pooled_unobserved_hours"],
                c["pooled_acquisitions"],
                c["burst_duration_seconds"],
                -c["burst_interval_seconds"],
                len(c["qualifying"]),
            )
        )
        selected = finalists[0]["config_id"]

    out = {
        "schema": "sentinel-v3-dev-grid/1",
        "role": "development_tuning_only_not_confirmatory",
        "protocol": "eval/HYBRID_STUDY_PROTOCOL.md",
        "frozen_replay_environment": dict(FROZEN_REPLAY_ENVIRONMENT),
        "selection_criterion": {
            "budget_guard": (
                f"pooled hybrid acquisitions <= {guard_max} "
                f"({BUDGET_GUARD_FRACTION:.0%} of pooled hourly {POOLED_HOURLY_BUDGET})"
            ),
            "objective": "max pooled episodes observed",
            "tie_breaks": [
                "min pooled matched-budget margin (hybrid minus per-county offset median)",
                "min pooled unobserved hours",
                "min pooled acquisitions",
                "shorter burst duration",
                "longer burst cadence",
                "smaller qualifying set",
            ],
        },
        "inputs": {
            case["fips"]: {
                "corpus": case["corpus"],
                "corpus_sha256": _sha256(_ROOT / case["corpus"]),
                "trigger_corpus": case["trigger_corpus"],
                "trigger_corpus_sha256": _sha256(_ROOT / case["trigger_corpus"]),
            }
            for case in DEV_CASES
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
