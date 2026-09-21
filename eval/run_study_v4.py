"""Held-out v4 additive-hybrid study generator — deterministic and fail-closed.

Runs the frozen additive verification-triggered policy and every prespecified
comparator on the v4 held-out county-events, under the unchanged v2 grading
contract, and evaluates the prespecified Q1/Q2/Q3 criterion in
``eval/HYBRID_STUDY_PROTOCOL_V4.md`` mechanically, including the exact
one-sided binomial sign test and the underpower rule.

Fail-closed rules follow the v3 generator: manifest, corpora, trigger
corpora, protocol, selection, and development-grid hashes must all match or
generation aborts before any replay; the output directory must be empty; the
implementation is hashed before any replay runs; and the trigger-disabled arm
must reproduce the adaptive schedule tick-for-tick.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.baselines import (  # noqa: E402
    DEFAULT_CADENCE_HOURS,
    fixed_schedule_specs,
    matched_budget_schedule_specs,
)
from eval.ground_truth import apply_threshold_compatibility  # noqa: E402
from eval.metrics import evaluate_truth_age_sensitivity  # noqa: E402
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
from eval.run_study_v3 import (  # noqa: E402
    AUDIT_TICK_KEYS,
    HYBRID_POLL_OFFSET_SENSITIVITY_SECONDS,
    _init_worker,
    _median,
    _run_one,
    _strip_audit,
    _summary_for,
)
from eval.trigger_metrics import trigger_accounting_metrics  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

MANIFEST_SCHEMA = "sentinel-study-corpus-manifest-v4/1"
RESULT_SCHEMA = "sentinel-county-study-v4-result/1"
AGGREGATE_SCHEMA = "sentinel-study-v4-aggregate/1"
TABLES_SCHEMA = "sentinel-study-v4-tables/1"
ARTIFACT_MANIFEST_SCHEMA = "sentinel-study-v4-artifact-manifest/1"

BUDGET_GUARD_FRACTION = 0.60
SIGN_TEST_ALPHA = 0.05
MIN_POWERED_COUNTY_EVENTS = 12


def _binomial_sign_test_p(wins: int, losses: int) -> Optional[float]:
    """Exact one-sided binomial tail P(X >= wins | n = wins+losses, p = 1/2)."""
    n = wins + losses
    if n == 0:
        return None
    tail = sum(math.comb(n, k) for k in range(wins, n + 1))
    return tail / (2**n)


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
        "anchor_mode",
    ):
        if not policy.get(key):
            raise ValueError(f"frozen_trigger_policy missing {key}")
    if policy["anchor_mode"] != "additive":
        raise ValueError("v4 frozen policy must use the additive anchor mode")
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
        "anchor_mode": "additive",
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
        raise RuntimeError(f"preflight blocked v4 generation for: {names}")

    county_results = []
    schedule_rows: List[Dict[str, Any]] = []
    trigger_rows: List[Dict[str, Any]] = []
    criterion_inputs: List[Dict[str, Any]] = []

    for entry in entries:
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
                    "trigger": {"enabled": False, "anchor_mode": "additive"},
                    "trigger_poll_interval_seconds": poll_seconds,
                },
            },
            {
                **base,
                "name": "hybrid_reanchor_diagnostic",
                "run_kwargs": {
                    **hybrid_kwargs,
                    "trigger": {**trigger_spec, "anchor_mode": "reanchor"},
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

        with _temporary_environment(FROZEN_REPLAY_ENVIRONMENT):
            with ProcessPoolExecutor(
                max_workers=workers, initializer=_init_worker
            ) as pool:
                runs = dict(pool.map(_run_one, tasks, chunksize=1))

            adaptive_ticks = [_strip_audit(t) for t in runs["adaptive"]["ticks"]]
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
                    zip(
                        matched_tasks,
                        (r for _, r in pool.map(_run_one, matched_tasks, chunksize=1)),
                    )
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
                            if k not in ("episode_trigger_rows", "burst_window_rows")
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
        adaptive_row = next(r for r in rows_this_county if r["policy"] == "adaptive")
        reanchor_row = next(
            r for r in rows_this_county if r["policy"] == "hybrid_reanchor_diagnostic"
        )
        fixed_1h_offset0 = next(
            r for r in rows_this_county if r["policy"] == "fixed_1h_offset_0h"
        )
        matched_hybrid = [
            r for r in matched_rows if r["matched_target_policy"] == "hybrid"
        ]
        if entry.get("analysis_role") == "episode_evaluation":
            offset_unobs = sorted(
                r["unobserved_flood_stage_duration_hours"] for r in matched_hybrid
            )
            hybrid_unobs = hybrid_row["unobserved_flood_stage_duration_hours"]
            rank = sum(1 for v in offset_unobs if v < hybrid_unobs)
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
                    "reanchor_episodes_observed": reanchor_row[
                        "episodes_observed_by_schedule"
                    ],
                    "hybrid_unobserved_hours": hybrid_unobs,
                    "adaptive_unobserved_hours": adaptive_row[
                        "unobserved_flood_stage_duration_hours"
                    ],
                    "reanchor_unobserved_hours": reanchor_row[
                        "unobserved_flood_stage_duration_hours"
                    ],
                    "hybrid_acquisitions": hybrid_row["acquisitions"],
                    "adaptive_acquisitions": adaptive_row["acquisitions"],
                    "hourly_offset0_acquisitions": fixed_1h_offset0["acquisitions"],
                    "matched_hybrid_offsets_episodes_observed_median": _median(
                        r["episodes_observed_by_schedule"] for r in matched_hybrid
                    ),
                    "matched_hybrid_offsets_episodes_observed_max": max(
                        r["episodes_observed_by_schedule"] for r in matched_hybrid
                    ),
                    "matched_hybrid_offsets_unobserved_min": min(offset_unobs),
                    "matched_hybrid_offsets_unobserved_median": _median(offset_unobs),
                    "matched_hybrid_offsets_unobserved_max": max(offset_unobs),
                    "n_matched_hybrid_offsets": len(matched_hybrid),
                    "hybrid_rank_in_offset_distribution": rank,
                    "hybrid_rank_semantics": (
                        "number of matched-budget offsets with strictly lower "
                        "unobserved duration than the hybrid; 0 means the "
                        "hybrid is at least as good as every offset"
                    ),
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
    # Prespecified criterion (protocol Q1/Q2/Q3), mechanical.
    # ------------------------------------------------------------------
    n = len(criterion_inputs)
    powered = n >= MIN_POWERED_COUNTY_EVENTS
    pooled_hybrid_eps = sum(c["hybrid_episodes_observed"] for c in criterion_inputs)
    pooled_adaptive_eps = sum(
        c["adaptive_episodes_observed"] for c in criterion_inputs
    )
    pooled_median_offset_eps = sum(
        c["matched_hybrid_offsets_episodes_observed_median"] or 0
        for c in criterion_inputs
    )
    q1 = (
        pooled_hybrid_eps >= pooled_median_offset_eps
        and pooled_hybrid_eps >= pooled_adaptive_eps
    )
    wins = sum(
        c["hybrid_unobserved_hours"] < c["matched_hybrid_offsets_unobserved_median"]
        for c in criterion_inputs
    )
    losses = sum(
        c["hybrid_unobserved_hours"] > c["matched_hybrid_offsets_unobserved_median"]
        for c in criterion_inputs
    )
    sign_p = _binomial_sign_test_p(wins, losses)
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
    q2 = (
        sign_p is not None
        and sign_p < SIGN_TEST_ALPHA
        and pooled_hybrid_unobs <= pooled_median_unobs
    )
    pooled_hybrid_acq = sum(c["hybrid_acquisitions"] for c in criterion_inputs)
    pooled_hourly_acq = sum(
        c["hourly_offset0_acquisitions"] for c in criterion_inputs
    )
    q3 = pooled_hybrid_acq <= BUDGET_GUARD_FRACTION * pooled_hourly_acq

    # Secondary: the v3 oracle-minimum test, reported but never gating.
    oracle_wins = sum(
        c["hybrid_unobserved_hours"] < c["matched_hybrid_offsets_unobserved_min"]
        for c in criterion_inputs
    )
    oracle_losses = sum(
        c["hybrid_unobserved_hours"] > c["matched_hybrid_offsets_unobserved_min"]
        for c in criterion_inputs
    )

    aggregate = {
        "schema": AGGREGATE_SCHEMA,
        "protocol": manifest["protocol"],
        "protocol_sha256": manifest["protocol_sha256"],
        "analysis_interval_convention": "[analysis_start,analysis_end)",
        "evaluation_interval_seconds": evaluation_interval_seconds,
        "n_counties": len(entries),
        "n_episode_evaluation_county_events": n,
        "powered": powered,
        "min_powered_county_events": MIN_POWERED_COUNTY_EVENTS,
        "frozen_trigger_policy": policy,
        "criterion_inputs": criterion_inputs,
        "primary_criterion": {
            "Q1_completeness": {
                "pass": q1,
                "pooled_hybrid_episodes_observed": pooled_hybrid_eps,
                "pooled_adaptive_episodes_observed": pooled_adaptive_eps,
                "pooled_median_offset_episodes_observed": pooled_median_offset_eps,
            },
            "Q2_delay": {
                "pass": q2,
                "county_events_below_offset_median": wins,
                "county_events_above_offset_median": losses,
                "sign_test_one_sided_p": sign_p,
                "alpha": SIGN_TEST_ALPHA,
                "pooled_hybrid_unobserved_hours": pooled_hybrid_unobs,
                "pooled_offset_median_unobserved_hours": pooled_median_unobs,
            },
            "Q3_budget": {
                "pass": q3,
                "pooled_hybrid_acquisitions": pooled_hybrid_acq,
                "pooled_hourly_offset0_acquisitions": pooled_hourly_acq,
                "budget_guard_fraction": BUDGET_GUARD_FRACTION,
            },
            "criterion_pass": bool(powered and q1 and q2 and q3),
            "criterion_verdict": (
                ("pass" if (q1 and q2 and q3) else "fail")
                if powered
                else "underpowered_no_verdict"
            ),
        },
        "secondary_oracle_minimum_comparison": {
            "county_events_below_offset_minimum": oracle_wins,
            "county_events_above_offset_minimum": oracle_losses,
            "note": "the v3 prespecified test, reported for continuity, never gating",
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
            "sign_test_alpha": SIGN_TEST_ALPHA,
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
                "python3 eval/run_study_v4.py --manifest "
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
    parser.add_argument("--manifest", default="eval/corpus/study_v4/manifest.json")
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
        print(f"v4 study generation failed: {exc}", file=sys.stderr)
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
