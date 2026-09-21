"""Held-out v3 hybrid study generator — deterministic and fail-closed.

Runs the frozen verification-triggered hybrid policy and every prespecified
comparator on the held-out county-events, under the frozen v2 grading
contract, and evaluates the prespecified primary criterion (P1/P2/P3 in
``eval/HYBRID_STUDY_PROTOCOL.md``) mechanically.

Fail-closed rules: the corpus manifest, every gauge corpus, every trigger
corpus, the frozen protocol file, the selection artifact, and the development
grid artifact must all match their declared SHA-256 values, or generation
aborts before any replay. The output directory must be absent or empty. The
hybrid-with-trigger-disabled arm must reproduce the adaptive schedule
tick-for-tick, or generation aborts (live/replay parity invariant).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.baselines import (  # noqa: E402
    DEFAULT_CADENCE_HOURS,
    fixed_schedule_specs,
    matched_budget_schedule_specs,
)
from eval.ground_truth import apply_threshold_compatibility  # noqa: E402
from eval.metrics import evaluate, evaluate_truth_age_sensitivity  # noqa: E402
from eval.run_replay import run as replay_run  # noqa: E402
from eval.run_study import (  # noqa: E402
    FROZEN_REPLAY_ENVIRONMENT,
    PRIMARY_POLICY_STALENESS_HOURS,
    PRIMARY_TRUTH_MAX_AGE_HOURS,
    _implementation_files,
    _portable_path,
    _preflight,
    _resolve_repo_path,
    _schedule_row,
    _sha256,
    _slug,
    _temporary_environment,
    _validate_declared_artifact,
    _write_json,
)
from eval.trigger_metrics import trigger_accounting_metrics  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

MANIFEST_SCHEMA = "sentinel-study-corpus-manifest-v3/1"
RESULT_SCHEMA = "sentinel-county-study-v3-result/1"
AGGREGATE_SCHEMA = "sentinel-study-v3-aggregate/1"
TABLES_SCHEMA = "sentinel-study-v3-tables/1"
ARTIFACT_MANIFEST_SCHEMA = "sentinel-study-v3-artifact-manifest/1"

HYBRID_POLL_OFFSET_SENSITIVITY_SECONDS = (300, 600)
BUDGET_GUARD_FRACTION = 0.60

AUDIT_TICK_KEYS = (
    "acquisition_due_reason",
    "verification_burst_active",
    "verification_burst_until",
    "trigger_new_event_keys",
)


def _validate_manifest(manifest_path: Path) -> Dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"unexpected manifest schema: {manifest.get('schema')!r}")
    for label in ("protocol", "selection", "dev_grid"):
        info = _validate_declared_artifact(
            county="(study)",
            label=label,
            path_value=manifest.get(label),
            sha256_value=manifest.get(f"{label}_sha256"),
        )
        if info is None:
            raise ValueError(f"manifest must declare {label} with a checksum")
    policy = manifest.get("frozen_trigger_policy") or {}
    for key in (
        "qualifying",
        "burst_interval_seconds",
        "burst_duration_seconds",
        "poll_interval_seconds",
    ):
        if not policy.get(key):
            raise ValueError(f"frozen_trigger_policy missing {key}")
    entries = list(manifest.get("artifacts") or [])
    if not entries:
        raise ValueError("study manifest has no artifacts")
    for entry in entries:
        required = (
            "path",
            "county",
            "state",
            "fips",
            "analysis_role",
            "analysis_start",
            "analysis_end",
            "sha256",
            "trigger_corpus",
            "trigger_corpus_sha256",
        )
        missing = [key for key in required if not entry.get(key)]
        if missing:
            raise ValueError(f"{entry.get('county')}: manifest entry missing {missing}")
        for label, path_key, hash_key in (
            ("gauge corpus", "path", "sha256"),
            ("trigger corpus", "trigger_corpus", "trigger_corpus_sha256"),
        ):
            _validate_declared_artifact(
                county=str(entry["county"]),
                label=label,
                path_value=entry.get(path_key),
                sha256_value=entry.get(hash_key),
            )
    return manifest


def _strip_audit(tick: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in tick.items() if k not in AUDIT_TICK_KEYS}
    out["claims"] = [
        {k: v for k, v in claim.items() if k != "claim_id"}
        for claim in tick.get("claims") or []
    ]
    out["all_claims"] = tick.get("all_claims")
    return out


def _run_one(task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    entry = task["entry"]
    run = replay_run(
        corpus_path=str(_resolve_repo_path(entry["path"])),
        county=entry["county"],
        state=entry["state"],
        fips=entry["fips"],
        start=entry["analysis_start"],
        end=entry["analysis_end"],
        max_staleness_hours=PRIMARY_POLICY_STALENESS_HOURS,
        threshold_excluded_site_ids=task["excluded_ids"],
        **task.get("run_kwargs", {}),
    )
    return task["name"], run


def _init_worker() -> None:
    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)


def _median(values: Sequence[Optional[float]]) -> Optional[float]:
    numeric = [float(v) for v in values if v is not None]
    return round(statistics.median(numeric), 4) if numeric else None


def _summary_for(
    run: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    evaluation_interval_seconds: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary = {
        "schedule": run.get("schedule"),
        "metrics": evaluate(
            run,
            corpus=corpus,
            events=[],
            evaluation_interval_seconds=evaluation_interval_seconds,
        ),
    }
    if extra:
        summary.update(extra)
    return summary


def generate(
    *,
    manifest_path: Path,
    out_dir: Path,
    workers: int = 8,
    evaluation_interval_seconds: int = 900,
    offset_step_seconds: int = 900,
    allow_qc_warnings: bool = False,
) -> Dict[str, Any]:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(
            f"study output directory must be absent or empty: {out_dir}"
        )
    manifest = _validate_manifest(manifest_path)
    # Hash the implementation before any replay runs, so the manifest attests
    # the code that actually produced the results rather than whatever the
    # tree holds once a long generation finishes.
    implementation_sha256 = {
        _portable_path(path): _sha256(path) for path in _implementation_files()
    }
    entries = list(manifest["artifacts"])
    policy = dict(manifest["frozen_trigger_policy"])
    trigger_spec = {
        "enabled": True,
        "qualifying": list(policy["qualifying"]),
        "burst_interval_seconds": int(policy["burst_interval_seconds"]),
        "burst_duration_seconds": int(policy["burst_duration_seconds"]),
    }
    poll_seconds = int(policy["poll_interval_seconds"])

    preflight = [
        _preflight(entry, interval_seconds=evaluation_interval_seconds)
        for entry in entries
    ]
    _write_json(
        out_dir / "preflight_qc.json",
        {
            "schema": "sentinel-study-preflight/2",
            "truth_policy": {
                "primary_max_age_hours": PRIMARY_TRUTH_MAX_AGE_HOURS,
                "negative_requires_all_reporting_thresholded_gauges_fresh": True,
            },
            "counties": preflight,
        },
    )
    blocked = [row for row in preflight if row["blocked"]]
    if blocked and not allow_qc_warnings:
        names = ", ".join(row["county"] for row in blocked)
        raise RuntimeError(f"preflight blocked v3 generation for: {names}")

    county_results = []
    schedule_rows: List[Dict[str, Any]] = []
    trigger_rows: List[Dict[str, Any]] = []
    criterion_inputs: List[Dict[str, Any]] = []

    for entry in entries:
        # The window start disambiguates a county appearing in two events
        # (e.g. Horry, SC under Florence and again under Ian).
        slug = f"{_slug(entry['county'])}_{entry['fips']}_{entry['analysis_start'][:10]}"
        excluded_ids = sorted(
            {
                str(row.get("site_no"))
                for row in (entry.get("threshold_exclusions") or [])
                if row.get("site_no")
            }
        )
        corpus, _ = apply_threshold_compatibility(
            load_corpus(_resolve_repo_path(entry["path"])),
            excluded_site_ids=excluded_ids,
        )
        trigger_corpus_path = str(_resolve_repo_path(entry["trigger_corpus"]))

        base = {"entry": entry, "excluded_ids": excluded_ids}
        hybrid_kwargs = {
            "trigger": trigger_spec,
            "trigger_corpus_path": trigger_corpus_path,
            "trigger_poll_interval_seconds": poll_seconds,
        }
        tasks: List[Dict[str, Any]] = [
            {**base, "name": "adaptive", "run_kwargs": {}},
            {**base, "name": "hybrid", "run_kwargs": dict(hybrid_kwargs)},
            {
                **base,
                "name": "hybrid_trigger_disabled",
                "run_kwargs": {
                    "trigger": {"enabled": False},
                    "trigger_poll_interval_seconds": poll_seconds,
                },
            },
            {
                **base,
                "name": "hybrid_immediate_only",
                "run_kwargs": {
                    **hybrid_kwargs,
                    "trigger": {**trigger_spec, "burst_cap_enabled": False},
                },
            },
            {
                **base,
                "name": "hybrid_burst_only",
                "run_kwargs": {
                    **hybrid_kwargs,
                    "trigger": {**trigger_spec, "immediate_fire_enabled": False},
                },
            },
        ]
        for offset in HYBRID_POLL_OFFSET_SENSITIVITY_SECONDS:
            tasks.append(
                {
                    **base,
                    "name": f"hybrid_poll_offset_{offset}s",
                    "run_kwargs": {
                        **hybrid_kwargs,
                        "schedule_offset_seconds": offset,
                    },
                }
            )
        for spec in fixed_schedule_specs(
            DEFAULT_CADENCE_HOURS, offset_step_seconds=offset_step_seconds
        ):
            tasks.append(
                {
                    **base,
                    "name": spec["name"],
                    "run_kwargs": {
                        "fixed_interval_seconds": spec["fixed_interval_seconds"],
                        "schedule_offset_seconds": spec["schedule_offset_seconds"],
                    },
                }
            )

        # Only the context manager sets the frozen values, so the caller's
        # environment is genuinely restored afterwards; workers receive the
        # same values through their initializer.
        with _temporary_environment(FROZEN_REPLAY_ENVIRONMENT):
            with ProcessPoolExecutor(
                max_workers=workers, initializer=_init_worker
            ) as pool:
                runs = dict(pool.map(_run_one, tasks, chunksize=1))

            # Parity invariant: the poll-grid driver with the trigger disabled
            # must reproduce the adaptive schedule exactly.
            adaptive_ticks = [
                _strip_audit(t) for t in runs["adaptive"]["ticks"]
            ]
            disabled_ticks = [
                _strip_audit(t) for t in runs["hybrid_trigger_disabled"]["ticks"]
            ]
            if adaptive_ticks != disabled_ticks:
                raise RuntimeError(
                    f"{entry['county']}: hybrid_trigger_disabled does not "
                    "reproduce the adaptive schedule; parity invariant violated"
                )

            matched_tasks: List[Dict[str, Any]] = []
            for target_name in ("hybrid", "adaptive"):
                target = len(runs[target_name]["ticks"])
                for spec in matched_budget_schedule_specs(
                    start=entry["analysis_start"],
                    end=entry["analysis_end"],
                    target_acquisitions=target,
                    offset_step_seconds=offset_step_seconds,
                ):
                    matched_tasks.append(
                        {
                            **base,
                            "name": f"matched_to_{target_name}_{spec['name']}",
                            "target_name": target_name,
                            "target_acquisitions": target,
                            "run_kwargs": {
                                "fixed_interval_seconds": spec[
                                    "fixed_interval_seconds"
                                ],
                                "schedule_offset_seconds": spec[
                                    "schedule_offset_seconds"
                                ],
                            },
                        }
                    )
            with ProcessPoolExecutor(
                max_workers=workers, initializer=_init_worker
            ) as pool:
                matched_runs = list(
                    zip(matched_tasks, (r for _, r in pool.map(_run_one, matched_tasks, chunksize=1)))
                )

        rows_this_county: List[Dict[str, Any]] = []
        trigger_this_county: Dict[str, Any] = {}
        for name, run in runs.items():
            extra = None
            if name in ("hybrid", "adaptive"):
                extra = {
                    "truth_age_sensitivity": evaluate_truth_age_sensitivity(
                        run,
                        corpus=corpus,
                        events=[],
                        evaluation_interval_seconds=evaluation_interval_seconds,
                    )
                }
            summary = _summary_for(
                run,
                corpus=corpus,
                evaluation_interval_seconds=evaluation_interval_seconds,
                extra=extra,
            )
            row = _schedule_row(entry, name, summary)
            accounting = run.get("verification_trigger")
            if accounting:
                row["verification_trigger_policy"] = accounting["policy"]
                row["trigger_stream_consultations"] = accounting[
                    "trigger_stream_consultations"
                ]
                row["trigger_attributable_acquisitions"] = accounting[
                    "trigger_attributable_acquisitions"
                ]
                row["acquisitions_by_reason"] = accounting["acquisitions_by_reason"]
                metrics = trigger_accounting_metrics(
                    run,
                    corpus=corpus,
                    truth_max_age_hours=PRIMARY_TRUTH_MAX_AGE_HOURS,
                    interval_seconds=evaluation_interval_seconds,
                )
                trigger_this_county[name] = metrics
                trigger_rows.append(
                    {
                        "county": entry["county"],
                        "fips": entry["fips"],
                        "event": entry.get("event"),
                        "analysis_role": entry.get("analysis_role"),
                        "policy": name,
                        **{
                            k: v
                            for k, v in (metrics or {}).items()
                            if k
                            not in ("episode_trigger_rows", "burst_window_rows")
                        },
                    }
                )
            rows_this_county.append(row)

        matched_rows: List[Dict[str, Any]] = []
        for task, run in matched_runs:
            summary = _summary_for(
                run,
                corpus=corpus,
                evaluation_interval_seconds=evaluation_interval_seconds,
                extra={
                    "target_acquisitions": task["target_acquisitions"],
                    "realised_acquisitions": len(run["ticks"]),
                    "budget_gap": len(run["ticks"]) - task["target_acquisitions"],
                },
            )
            row = _schedule_row(entry, task["name"], summary)
            row["matched_target_policy"] = task["target_name"]
            matched_rows.append(row)

        schedule_rows.extend(rows_this_county)
        schedule_rows.extend(matched_rows)

        hybrid_row = next(r for r in rows_this_county if r["policy"] == "hybrid")
        adaptive_row = next(
            r for r in rows_this_county if r["policy"] == "adaptive"
        )
        fixed_1h_offset0 = next(
            r
            for r in rows_this_county + matched_rows
            if r["policy"] == "fixed_1h_offset_0h"
        )
        matched_hybrid = [
            r for r in matched_rows if r["matched_target_policy"] == "hybrid"
        ]
        if entry.get("analysis_role") == "episode_evaluation":
            criterion_inputs.append(
                {
                    "county": entry["county"],
                    "fips": entry["fips"],
                    "event": entry.get("event"),
                    "episodes_total": hybrid_row["observed_flood_episodes"],
                    "hybrid_episodes_observed": hybrid_row[
                        "episodes_observed_by_schedule"
                    ],
                    "adaptive_episodes_observed": adaptive_row[
                        "episodes_observed_by_schedule"
                    ],
                    "hybrid_unobserved_hours": hybrid_row[
                        "unobserved_flood_stage_duration_hours"
                    ],
                    "adaptive_unobserved_hours": adaptive_row[
                        "unobserved_flood_stage_duration_hours"
                    ],
                    "hybrid_acquisitions": hybrid_row["acquisitions"],
                    "adaptive_acquisitions": adaptive_row["acquisitions"],
                    "hourly_offset0_acquisitions": fixed_1h_offset0["acquisitions"],
                    "matched_hybrid_offsets_episodes_observed_max": max(
                        r["episodes_observed_by_schedule"] for r in matched_hybrid
                    ),
                    "matched_hybrid_offsets_unobserved_min": min(
                        r["unobserved_flood_stage_duration_hours"]
                        for r in matched_hybrid
                    ),
                    "matched_hybrid_offsets_unobserved_median": _median(
                        r["unobserved_flood_stage_duration_hours"]
                        for r in matched_hybrid
                    ),
                    "matched_hybrid_offsets_unobserved_max": max(
                        r["unobserved_flood_stage_duration_hours"]
                        for r in matched_hybrid
                    ),
                    "n_matched_hybrid_offsets": len(matched_hybrid),
                }
            )

        result = {
            "schema": RESULT_SCHEMA,
            "corpus_manifest_entry": entry,
            "nws_system_input": "vtec_trigger_stream_scheduling_only",
            "policies": {
                name: {
                    "schedule": runs[name].get("schedule"),
                    "termination": runs[name].get("termination"),
                    "verification_trigger": runs[name].get("verification_trigger"),
                }
                for name in runs
            },
            "schedule_rows": rows_this_county,
            "matched_budget_rows": matched_rows,
            "trigger_accounting": trigger_this_county,
        }
        result_path = out_dir / "counties" / slug / "result.json"
        _write_json(result_path, result)
        county_results.append(
            {
                "county": entry["county"],
                "state": entry["state"],
                "fips": entry["fips"],
                "analysis_role": entry.get("analysis_role"),
                "result_path": str(result_path.relative_to(out_dir)),
            }
        )

    # ------------------------------------------------------------------
    # Prespecified primary criterion (protocol P1/P2/P3), mechanical.
    # ------------------------------------------------------------------
    pooled_hybrid_eps = sum(c["hybrid_episodes_observed"] for c in criterion_inputs)
    pooled_adaptive_eps = sum(
        c["adaptive_episodes_observed"] for c in criterion_inputs
    )
    pooled_best_offset_eps = sum(
        c["matched_hybrid_offsets_episodes_observed_max"] for c in criterion_inputs
    )
    p1 = (
        pooled_hybrid_eps >= pooled_best_offset_eps
        and pooled_hybrid_eps >= pooled_adaptive_eps
    )
    wins = sum(
        c["hybrid_unobserved_hours"] < c["matched_hybrid_offsets_unobserved_min"]
        for c in criterion_inputs
    )
    losses = sum(
        c["hybrid_unobserved_hours"] > c["matched_hybrid_offsets_unobserved_min"]
        for c in criterion_inputs
    )
    pooled_hybrid_unobs = round(
        sum(c["hybrid_unobserved_hours"] for c in criterion_inputs), 4
    )
    pooled_median_unobs = round(
        sum(
            c["matched_hybrid_offsets_unobserved_median"] or 0.0
            for c in criterion_inputs
        ),
        4,
    )
    p2 = (wins > losses) and (pooled_hybrid_unobs <= pooled_median_unobs)
    pooled_hybrid_acq = sum(c["hybrid_acquisitions"] for c in criterion_inputs)
    pooled_hourly_acq = sum(
        c["hourly_offset0_acquisitions"] for c in criterion_inputs
    )
    p3 = pooled_hybrid_acq <= BUDGET_GUARD_FRACTION * pooled_hourly_acq
    aggregate = {
        "schema": AGGREGATE_SCHEMA,
        "protocol": manifest["protocol"],
        "protocol_sha256": manifest["protocol_sha256"],
        "analysis_interval_convention": "[analysis_start,analysis_end)",
        "evaluation_interval_seconds": evaluation_interval_seconds,
        "n_counties": len(entries),
        "n_episode_evaluation_counties": len(criterion_inputs),
        "frozen_trigger_policy": policy,
        "criterion_inputs": criterion_inputs,
        "primary_criterion": {
            "P1_completeness": {
                "pass": p1,
                "pooled_hybrid_episodes_observed": pooled_hybrid_eps,
                "pooled_adaptive_episodes_observed": pooled_adaptive_eps,
                "pooled_best_matched_offset_episodes_observed": (
                    pooled_best_offset_eps
                ),
                "rule": (
                    "hybrid pooled episodes >= per-county best matched-budget "
                    "offsets summed, and >= adaptive pooled episodes"
                ),
            },
            "P2_delay": {
                "pass": p2,
                "county_events_hybrid_below_offset_minimum": wins,
                "county_events_hybrid_above_offset_minimum": losses,
                "pooled_hybrid_unobserved_hours": pooled_hybrid_unobs,
                "pooled_matched_offset_median_unobserved_hours": (
                    pooled_median_unobs
                ),
                "rule": (
                    "strictly more county-events below than above the "
                    "matched-offset minimum, and pooled unobserved <= pooled "
                    "matched-offset median"
                ),
            },
            "P3_budget": {
                "pass": p3,
                "pooled_hybrid_acquisitions": pooled_hybrid_acq,
                "pooled_hourly_offset0_acquisitions": pooled_hourly_acq,
                "budget_guard_fraction": BUDGET_GUARD_FRACTION,
            },
            "criterion_pass": bool(p1 and p2 and p3),
        },
        "pooling_note": (
            "descriptive county-event sums only; common-grid points are not "
            "treated as independent inferential samples"
        ),
    }
    tables = {
        "schema": TABLES_SCHEMA,
        "schedule_rows": schedule_rows,
        "trigger_accounting_rows": trigger_rows,
    }
    _write_json(out_dir / "aggregate.json", aggregate)
    _write_json(out_dir / "tables.json", tables)

    input_artifacts = []
    for entry in entries:
        input_artifacts.append(
            {
                "county": entry["county"],
                "kind": "gauge_corpus",
                "path": entry["path"],
                "sha256": entry["sha256"],
            }
        )
        input_artifacts.append(
            {
                "county": entry["county"],
                "kind": "vtec_trigger_stream",
                "path": entry["trigger_corpus"],
                "sha256": entry["trigger_corpus_sha256"],
            }
        )
    generated_files = sorted(
        path
        for path in out_dir.rglob("*.json")
        if path.name != "artifact_manifest.json"
    )
    artifact_manifest = {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "source_manifest": _portable_path(manifest_path),
        "source_manifest_sha256": _sha256(manifest_path),
        "protocol": manifest["protocol"],
        "protocol_sha256": manifest["protocol_sha256"],
        "selection": manifest["selection"],
        "selection_sha256": manifest["selection_sha256"],
        "dev_grid": manifest["dev_grid"],
        "dev_grid_sha256": manifest["dev_grid_sha256"],
        "config": {
            "analysis_interval_convention": "[analysis_start,analysis_end)",
            "evaluation_interval_seconds": evaluation_interval_seconds,
            "cadence_hours": list(DEFAULT_CADENCE_HOURS),
            "phase_offset_step_seconds": offset_step_seconds,
            "truth_max_age_hours": PRIMARY_TRUTH_MAX_AGE_HOURS,
            "primary_policy_staleness_hours": PRIMARY_POLICY_STALENESS_HOURS,
            "frozen_replay_environment": dict(FROZEN_REPLAY_ENVIRONMENT),
            "frozen_trigger_policy": policy,
            "hybrid_poll_offset_sensitivity_seconds": list(
                HYBRID_POLL_OFFSET_SENSITIVITY_SECONDS
            ),
            "budget_guard_fraction": BUDGET_GUARD_FRACTION,
            "nws_system_input": "vtec_trigger_stream_scheduling_only",
            "nws_timing_reference": None,
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "version_detail": sys.version,
        },
        "implementation_manifest_rule": (
            "sorted eval/*.py, watcher/**/*.py, watcher/ontology/stub_*.json, "
            "and skills/county-flood-data/county_flood/**/*.py; caches, tests, "
            "and results excluded"
        ),
        "implementation_sha256": implementation_sha256,
        "implementation_hashed_before_generation": True,
        "input_artifacts": input_artifacts,
        "county_results": county_results,
        "reproduction_commands": [
            (
                "python3 eval/run_study_v3.py --manifest "
                + _portable_path(manifest_path)
                + " --out-dir <empty-dir>"
            )
        ],
        "artifacts": [
            {
                "path": str(path.relative_to(out_dir)),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in generated_files
        ],
    }
    _write_json(out_dir / "artifact_manifest.json", artifact_manifest)
    return artifact_manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="eval/corpus/study_v3/manifest.json")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--allow-qc-warnings", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = generate(
            manifest_path=_resolve_repo_path(args.manifest),
            out_dir=Path(args.out_dir),
            workers=args.workers,
            allow_qc_warnings=args.allow_qc_warnings,
        )
    except (FileNotFoundError, RuntimeError, ValueError, FileExistsError) as exc:
        print(f"v3 study generation failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "artifact_manifest": str(Path(args.out_dir) / "artifact_manifest.json"),
                "n_artifacts": len(result["artifacts"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
