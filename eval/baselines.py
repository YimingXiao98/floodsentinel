"""Cadence-controlled replay baselines for RQ2.

The primary baseline changes only *when* the full policy acquires evidence.
Phase transitions, confidence gates, and recovery folding remain enabled.  A
``no_role`` run answers a different ablation question and must not stand in for
a fixed scheduler.

For each 1h, 2h, 3h, 6h, and 12h cadence the protocol enumerates every
15-minute phase offset.  The fixed interval with the same nominal number of
acquisitions as the adaptive run is also evaluated at every 15-minute clock
phase. Recovery may end a mission before that budget is spent, so target,
realised counts, and the outcome ranges are reported. If a single matched run
is needed, the nearest realised count is selected with earliest-offset tie
breaking; outcome performance is never part of the selection rule.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.ground_truth import (  # noqa: E402
    apply_threshold_compatibility,
    load as truth_load,
    parse_iso,
)
from eval.metrics import (  # noqa: E402
    DEFAULT_EVALUATION_INTERVAL_SECONDS,
    evaluate,
    evaluate_truth_age_sensitivity,
)
from eval.run_replay import run as replay_run  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

DEFAULT_CADENCE_HOURS = (1.0, 2.0, 3.0, 6.0, 12.0)
DEFAULT_OFFSET_STEP_SECONDS = 900


def fixed_schedule_specs(
    cadence_hours: Sequence[float] = DEFAULT_CADENCE_HOURS,
    *,
    offset_step_seconds: int = DEFAULT_OFFSET_STEP_SECONDS,
) -> List[Dict[str, Any]]:
    """All fixed cadences and predeclared clock-phase offsets."""
    if offset_step_seconds <= 0:
        raise ValueError("offset_step_seconds must be positive")
    specs: List[Dict[str, Any]] = []
    for hours in cadence_hours:
        interval = round(float(hours) * 3600)
        if interval <= 0:
            raise ValueError("cadence hours must be positive")
        offsets = list(range(0, interval, offset_step_seconds)) or [0]
        for offset in offsets:
            specs.append(
                {
                    "name": f"fixed_{hours:g}h_offset_{offset / 3600:g}h",
                    "kind": "fixed_cadence_phase_offset",
                    "fixed_interval_seconds": interval,
                    "schedule_offset_seconds": offset,
                    "phase_reasoning_enabled": True,
                }
            )
    return specs


def matched_budget_schedule_specs(
    *,
    start: str,
    end: str,
    target_acquisitions: int,
    offset_step_seconds: int = DEFAULT_OFFSET_STEP_SECONDS,
) -> List[Dict[str, Any]]:
    """Predeclare every clock phase for the derived equal-budget interval."""
    if offset_step_seconds <= 0:
        raise ValueError("offset_step_seconds must be positive")
    interval = matched_budget_interval_seconds(
        start=start, end=end, target_acquisitions=target_acquisitions
    )
    return [
        {
            "name": f"matched_budget_offset_{offset / 3600:g}h",
            "kind": "nominal_matched_acquisition_budget_phase_offset",
            "target_acquisitions": target_acquisitions,
            "fixed_interval_seconds": interval,
            "schedule_offset_seconds": offset,
            "phase_reasoning_enabled": True,
        }
        for offset in range(0, interval, offset_step_seconds)
    ]


def matched_budget_interval_seconds(
    *, start: str, end: str, target_acquisitions: int
) -> int:
    """Uniform interval with the adaptive run's nominal acquisition budget."""
    duration = max(0.0, (parse_iso(end) - parse_iso(start)).total_seconds())
    if target_acquisitions <= 0:
        return max(60, round(duration + 60))
    # For a half-open [start,end) schedule, D/N yields N nominal acquisitions
    # at 0, I, ..., (N-1)I.  Rounding to a whole second can move the realised
    # count by one; the result artifact always reports that budget gap.
    return max(60, round(duration / target_acquisitions))


def _summary(
    run: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    evaluation_interval_seconds: int = DEFAULT_EVALUATION_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    return {
        "schedule": run.get("schedule"),
        "recovery_fold_policy": run.get("recovery_fold_policy"),
        "termination": run.get("termination"),
        "n_acquisitions": len(run.get("ticks") or []),
        "metrics": evaluate(
            run,
            corpus=corpus,
            events=events,
            evaluation_interval_seconds=evaluation_interval_seconds,
        ),
        "truth_age_sensitivity": evaluate_truth_age_sensitivity(
            run,
            corpus=corpus,
            events=events,
            evaluation_interval_seconds=evaluation_interval_seconds,
        ),
    }


def compare_schedules(
    *,
    corpus_path: str,
    truth_path: Optional[str],
    county: str,
    state: str,
    fips: str,
    start: str,
    end: str,
    news_corpus_path: Optional[str] = None,
    nws_input_path: Optional[str] = None,
    max_staleness_hours: Optional[float] = 6.0,
    cadence_hours: Sequence[float] = DEFAULT_CADENCE_HOURS,
    offset_step_seconds: int = DEFAULT_OFFSET_STEP_SECONDS,
    evaluation_interval_seconds: int = DEFAULT_EVALUATION_INTERVAL_SECONDS,
    runs_dir: Optional[str] = None,
    threshold_excluded_site_ids: Sequence[str] = (),
    recovery_min_clear_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Run adaptive, fixed/offset, and nominal matched-budget schedules."""
    from watcher.config import role_layer_enabled

    if not role_layer_enabled():
        raise ValueError(
            "cadence baselines require role-layer phase reasoning; use full or no_belief"
        )

    raw_corpus = load_corpus(corpus_path)
    corpus, _ = apply_threshold_compatibility(
        raw_corpus, excluded_site_ids=threshold_excluded_site_ids
    )
    events = (
        (truth_load(truth_path) or {}).get("events") or []
        if truth_path
        else []
    )
    common = {
        "corpus_path": corpus_path,
        "truth_path": truth_path,
        "nws_input_path": nws_input_path,
        "news_corpus_path": news_corpus_path,
        "county": county,
        "state": state,
        "fips": fips,
        "start": start,
        "end": end,
        "max_staleness_hours": max_staleness_hours,
        "threshold_excluded_site_ids": threshold_excluded_site_ids,
        "recovery_min_clear_hours": recovery_min_clear_hours,
    }

    adaptive = replay_run(**common)
    raw_runs: List[tuple[str, Dict[str, Any]]] = [("adaptive", adaptive)]
    rows: List[Dict[str, Any]] = []
    for spec in fixed_schedule_specs(
        cadence_hours, offset_step_seconds=offset_step_seconds
    ):
        baseline = replay_run(
            **common,
            fixed_interval_seconds=spec["fixed_interval_seconds"],
            schedule_offset_seconds=spec["schedule_offset_seconds"],
        )
        raw_runs.append((spec["name"], baseline))
        rows.append(
            {
                "name": spec["name"],
                **_summary(
                    baseline,
                    corpus=corpus,
                    events=events,
                    evaluation_interval_seconds=evaluation_interval_seconds,
                ),
            }
        )

    target = len(adaptive.get("ticks") or [])
    matched_rows: List[Dict[str, Any]] = []
    for spec in matched_budget_schedule_specs(
        start=start,
        end=end,
        target_acquisitions=target,
        offset_step_seconds=offset_step_seconds,
    ):
        matched = replay_run(
            **common,
            fixed_interval_seconds=spec["fixed_interval_seconds"],
            schedule_offset_seconds=spec["schedule_offset_seconds"],
        )
        realised = len(matched.get("ticks") or [])
        raw_runs.append((spec["name"], matched))
        matched_rows.append(
            {
                "name": spec["name"],
                "kind": spec["kind"],
                "target_acquisitions": target,
                "realised_acquisitions": realised,
                "budget_gap": realised - target,
                "absolute_budget_gap": abs(realised - target),
                **_summary(
                    matched,
                    corpus=corpus,
                    events=events,
                    evaluation_interval_seconds=evaluation_interval_seconds,
                ),
            }
        )
    selected_index = min(
        range(len(matched_rows)),
        key=lambda index: (
            matched_rows[index]["absolute_budget_gap"],
            int(
                (matched_rows[index].get("schedule") or {}).get("offset_seconds")
                or 0
            ),
        ),
    )
    for index, row in enumerate(matched_rows):
        row["selected_nearest_realised_acquisition_count"] = index == selected_index
    matched_row = {
        **matched_rows[selected_index],
        "selection_rule": (
            "minimum absolute realised-minus-adaptive acquisition count; "
            "earliest prespecified clock offset breaks ties"
        ),
    }

    if runs_dir:
        target_dir = Path(runs_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, raw in raw_runs:
            (target_dir / f"{name}.json").write_text(
                json.dumps(raw, indent=2), encoding="utf-8"
            )

    return {
        "schema": "sentinel-cadence-baselines/1",
        "ablation": adaptive.get("ablation"),
        "county": county,
        "state": state,
        "window": {"start": start, "end": end},
        "analysis_interval_convention": "[start,end)",
        "principle": "schedule_override_with_phase_reasoning_preserved",
        "evaluation_interval_seconds": evaluation_interval_seconds,
        "adaptive": _summary(
            adaptive,
            corpus=corpus,
            events=events,
            evaluation_interval_seconds=evaluation_interval_seconds,
        ),
        "fixed_phase_offsets": rows,
        "matched_budget_phase_offsets": matched_rows,
        "matched_budget": matched_row,
        "matched_budget_offset_summary": {
            "derived_interval_seconds": (
                (matched_row.get("schedule") or {}).get("fixed_interval_seconds")
            ),
            "n_phase_offsets": len(matched_rows),
            "target_acquisitions": target,
            "realised_acquisitions_min": min(
                row["realised_acquisitions"] for row in matched_rows
            ),
            "realised_acquisitions_max": max(
                row["realised_acquisitions"] for row in matched_rows
            ),
            "selected_name": matched_row["name"],
            "selected_by_outcome_performance": False,
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare adaptive and fixed schedules")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--truth", default="")
    parser.add_argument("--nws-input", default="")
    parser.add_argument("--news-corpus", default="")
    parser.add_argument("--county", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--fips", default="")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--ablation", choices=("full", "no_belief"), default="full")
    parser.add_argument("--max-staleness-hours", type=float, default=6.0)
    parser.add_argument(
        "--exclude-threshold-site", action="append", default=[]
    )
    parser.add_argument("--recovery-min-clear-hours", type=float, default=None)
    parser.add_argument("--cadence-hours", type=float, nargs="+", default=list(DEFAULT_CADENCE_HOURS))
    parser.add_argument("--offset-step-hours", type=float, default=0.25)
    parser.add_argument(
        "--evaluation-interval-minutes", type=float, default=15.0
    )
    parser.add_argument("--runs-dir", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    os.environ["SENTINEL_ABLATION"] = args.ablation
    result = compare_schedules(
        corpus_path=args.corpus,
        truth_path=args.truth or None,
        nws_input_path=args.nws_input or None,
        news_corpus_path=args.news_corpus or None,
        county=args.county,
        state=args.state,
        fips=args.fips,
        start=args.start,
        end=args.end,
        max_staleness_hours=args.max_staleness_hours,
        cadence_hours=args.cadence_hours,
        offset_step_seconds=max(60, round(args.offset_step_hours * 3600)),
        evaluation_interval_seconds=max(
            60, round(args.evaluation_interval_minutes * 60)
        ),
        runs_dir=args.runs_dir or None,
        threshold_excluded_site_ids=args.exclude_threshold_site,
        recovery_min_clear_hours=args.recovery_min_clear_hours,
    )
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"wrote {target} ({len(result['fixed_phase_offsets'])} fixed/offset baselines)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
