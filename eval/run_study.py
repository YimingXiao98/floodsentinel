"""Generate deterministic per-county, aggregate, and tables-ready artifacts.

The command is intentionally fail-closed.  It verifies corpus checksums and
analysis windows, constructs truth-coverage diagnostics before any replay, and
stops on unresolved threshold/datum warnings unless explicitly overridden.
Primary runs never inject the optional NWS timing reference into the system.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.baselines import (  # noqa: E402
    DEFAULT_CADENCE_HOURS,
    compare_schedules,
    matched_budget_schedule_specs,
)
from eval.ground_truth import (  # noqa: E402
    apply_threshold_compatibility,
    load as truth_load,
    parse_iso,
)
from eval.metrics import (  # noqa: E402
    DEFAULT_EVALUATION_INTERVAL_SECONDS,
    annotate_run_on_grid,
    evaluate,
    rq2_flood_episodes,
)
from eval.run_replay import run as replay_run  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

PRIMARY_TRUTH_MAX_AGE_HOURS = 1.0
TRUTH_AGE_HORIZONS = (0.5, 1.0, 2.0, 6.0)
FOLD_HORIZONS_HOURS = (24.0, 48.0, 72.0)
CLEAR_GAUGE_REQUIREMENTS = (1, 3, 5, 8)
POLICY_STALENESS_HOURS = (3.0, 6.0, 12.0)
PRIMARY_CLEAR_GAUGE_REQUIREMENT = 5
PRIMARY_POLICY_STALENESS_HOURS = 6.0

# Canonical publication-generation environment.  These are explicit strings
# rather than reliance on absent variables so a hostile shell cannot silently
# change a replay. The frozen sensitivity design varies only the declared
# minimum-gauge and synchronized policy-reading-age knobs; nested overrides
# restore these primary values.
FROZEN_REPLAY_ENVIRONMENT: Dict[str, str] = {
    "SENTINEL_ABLATION": "full",
    "SENTINEL_TIER_WEIGHT": "1.0",
    "SENTINEL_READING_MAX_AGE_HOURS": "6.0",
    "SENTINEL_ABLATED_CONFIDENCE": "0.75",
    "SENTINEL_WATERSHED_NEIGHBOURS": "off",
    "SENTINEL_WATERSHED_LOCATION_PRECISION": "0.65",
    "COUNTY_FLOOD_MIN_GAUGES": str(PRIMARY_CLEAR_GAUGE_REQUIREMENT),
    "COUNTY_FLOOD_MAX_GAUGES": "10",
    "COUNTY_FLOOD_STAGE_RISE_FT": "1.0",
    "COUNTY_FLOOD_STAGE_FALL_FT": "1.0",
    "COUNTY_FLOOD_STAGE_PERSIST_FT": "0.1",
    "COUNTY_FLOOD_FLOW_RISE_PCT": "50.0",
    "COUNTY_FLOOD_FLOW_FALL_PCT": "50.0",
    "COUNTY_FLOOD_SUPPLEMENTARY_CONFIDENCE": "0.45",
    "REPORT_GAUGE_STAGE_THRESHOLD_FT": "0.1",
    "REPORT_GAUGE_FLOW_THRESHOLD_PCT": "20.0",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def _resolve_repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else _ROOT / path


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(_ROOT))
    except ValueError:
        return str(resolved)


@contextmanager
def _temporary_environment(values: Dict[str, str]):
    """Temporarily freeze runtime knobs used by an in-process replay."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _implementation_files() -> List[Path]:
    """All code/config that can affect deterministic study outputs."""
    candidates = set((_ROOT / "eval").glob("*.py"))
    candidates.update((_ROOT / "watcher").rglob("*.py"))
    candidates.update((_ROOT / "watcher" / "ontology").glob("stub_*.json"))
    candidates.update(
        (_ROOT / "skills" / "county-flood-data" / "county_flood").rglob("*.py")
    )
    excluded_parts = {"__pycache__", "tests", "results"}
    return sorted(
        (
            path
            for path in candidates
            if path.is_file() and not excluded_parts.intersection(path.parts)
        ),
        key=_portable_path,
    )


def _truth_path_for(entry: Dict[str, Any]) -> Optional[Path]:
    explicit = entry.get("truth_path")
    if explicit:
        return _resolve_repo_path(str(explicit))
    return None


def _validate_declared_artifact(
    *,
    county: str,
    label: str,
    path_value: Optional[str],
    sha256_value: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not path_value:
        if sha256_value:
            raise ValueError(f"{county}: {label} hash declared without a path")
        return None
    if not sha256_value:
        raise ValueError(f"{county}: {label} path requires a SHA-256 checksum")
    path = _resolve_repo_path(str(path_value))
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = _sha256(path)
    if actual != sha256_value:
        raise ValueError(
            f"checksum mismatch for {label} {path}: "
            f"manifest={sha256_value} actual={actual}"
        )
    return {"path": path, "sha256": actual}


def _excluded_site_ids(entry: Dict[str, Any]) -> List[str]:
    values = entry.get("threshold_exclusions") or []
    out = []
    for value in values:
        site_no = value.get("site_no") if isinstance(value, dict) else value
        if site_no:
            out.append(str(site_no))
    return sorted(set(out))


def _validate_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    analysis_role = entry.get("analysis_role")
    if analysis_role not in (None, "episode_evaluation", "coverage_only"):
        raise ValueError(
            f"invalid analysis_role for {entry.get('county')}: {analysis_role}"
        )
    required = (
        "path",
        "county",
        "state",
        "fips",
        "analysis_start",
        "analysis_end",
        "sha256",
    )
    missing = [key for key in required if not entry.get(key)]
    if missing:
        raise ValueError(f"{entry.get('county', entry.get('path'))}: missing {missing}")
    path = _resolve_repo_path(str(entry["path"]))
    if not path.is_file():
        raise FileNotFoundError(path)
    actual_hash = _sha256(path)
    if actual_hash != entry["sha256"]:
        raise ValueError(
            f"checksum mismatch for {path}: manifest={entry['sha256']} actual={actual_hash}"
        )
    truth = _validate_declared_artifact(
        county=entry["county"],
        label="truth reference",
        path_value=entry.get("truth_path"),
        sha256_value=entry.get("truth_sha256"),
    )
    datum_qc = entry.get("datum_qc") or {}
    calibration = _validate_declared_artifact(
        county=entry["county"],
        label="datum calibration",
        path_value=datum_qc.get("calibration_path"),
        sha256_value=datum_qc.get("calibration_sha256"),
    )
    start, end = parse_iso(entry["analysis_start"]), parse_iso(entry["analysis_end"])
    elapsed = (end - start).total_seconds() / 3600.0
    if elapsed <= 0:
        raise ValueError(f"invalid analysis interval for {entry['county']}")
    return {
        "path": path,
        "sha256": actual_hash,
        "elapsed_hours": elapsed,
        "truth": truth,
        "calibration": calibration,
    }


def _leading_trailing_unknown_hours(ticks: Sequence[Dict[str, Any]]) -> tuple[float, float]:
    scored = [tick for tick in ticks if not tick.get("evaluation_boundary")]
    leading = 0.0
    for tick in scored:
        if tick.get("truth_hydrologic_flooding") is not None:
            break
        leading += float(tick.get("evaluation_interval_hours") or 0.0)
    trailing = 0.0
    for tick in reversed(scored):
        if tick.get("truth_hydrologic_flooding") is not None:
            break
        trailing += float(tick.get("evaluation_interval_hours") or 0.0)
    return round(leading, 4), round(trailing, 4)


def _truth_coverage_row(
    entry: Dict[str, Any],
    corpus: Dict[str, Any],
    *,
    horizon: float,
    excluded_ids: Sequence[str],
    interval_seconds: int,
) -> Dict[str, Any]:
    run = {
        "window": {
            "start": entry["analysis_start"],
            "end": entry["analysis_end"],
        },
        "ticks": [],
        "threshold_exclusions": [
            {"site_no": site_no, "reason": "manifest_site_exclusion"}
            for site_no in excluded_ids
        ],
    }
    ticks = annotate_run_on_grid(
        run,
        corpus=corpus,
        events=[],
        interval_seconds=interval_seconds,
        truth_max_age_hours=horizon,
    )
    episodes = rq2_flood_episodes(ticks)
    scored = [tick for tick in ticks if not tick.get("evaluation_boundary")]
    known_points = sum(
        tick.get("truth_hydrologic_flooding") is not None for tick in scored
    )
    unknown_points = len(scored) - known_points
    leading, trailing = _leading_trailing_unknown_hours(ticks)
    known_hours = episodes["known_truth_duration_hours"]
    unknown_hours = episodes["unknown_truth_duration_hours"]
    return {
        "county": entry["county"],
        "state": entry["state"],
        "fips": entry["fips"],
        "analysis_role": entry.get("analysis_role") or "unspecified",
        "truth_max_age_hours": horizon,
        "evaluation_points": len(scored),
        "known_points": known_points,
        "unknown_points": unknown_points,
        "known_point_proportion": (
            round(known_points / len(scored), 4) if scored else None
        ),
        "known_duration_hours": known_hours,
        "unknown_duration_hours": unknown_hours,
        "known_duration_proportion": episodes["truth_duration_coverage"],
        "leading_unknown_hours": leading,
        "trailing_unknown_hours": trailing,
        "observed_flood_duration_hours": episodes[
            "observed_flood_duration_hours"
        ],
        "n_observed_flood_episodes": episodes["n_observed_flood_episodes"],
    }


def _preflight(
    entry: Dict[str, Any],
    *,
    interval_seconds: int,
) -> Dict[str, Any]:
    info = _validate_entry(entry)
    raw_corpus = load_corpus(info["path"])
    excluded_ids = _excluded_site_ids(entry)
    corpus, exclusions = apply_threshold_compatibility(
        raw_corpus, excluded_site_ids=excluded_ids
    )
    reference_sites = sum(
        1
        for site in corpus.get("sites") or []
        if site.get("flood_stage") is not None and site.get("observations")
    )
    coverage = [
        _truth_coverage_row(
            entry,
            corpus,
            horizon=horizon,
            excluded_ids=excluded_ids,
            interval_seconds=interval_seconds,
        )
        for horizon in TRUTH_AGE_HORIZONS
    ]
    primary = next(
        row
        for row in coverage
        if row["truth_max_age_hours"] == PRIMARY_TRUTH_MAX_AGE_HOURS
    )
    warnings: List[Dict[str, Any]] = []
    analysis_role = entry.get("analysis_role") or (
        "coverage_only" if reference_sites == 0 else "episode_evaluation"
    )
    for row in coverage:
        row["analysis_role"] = analysis_role
    if analysis_role == "episode_evaluation" and reference_sites == 0:
        warnings.append(
            {
                "code": "episode_evaluation_without_reference_site",
                "blocking": True,
            }
        )
    if entry.get("threshold_qc_status") == "blocked":
        warnings.append(
            {"code": "threshold_qc_blocked", "blocking": True}
        )
    primary_coverage = float(primary["known_duration_proportion"] or 0.0)
    limited_coverage_reviewed = entry.get("limited_truth_coverage_reviewed") is True
    limited_coverage_rationale = str(
        entry.get("limited_truth_coverage_rationale") or ""
    ).strip()
    limited_coverage_acknowledged = bool(
        limited_coverage_reviewed and limited_coverage_rationale
    )
    if reference_sites > 0 and primary_coverage < 0.8:
        warnings.append(
            {
                "code": (
                    "limited_primary_truth_coverage_reviewed"
                    if limited_coverage_acknowledged
                    else "limited_primary_truth_coverage_unreviewed"
                ),
                "blocking": not limited_coverage_acknowledged,
                "known_duration_proportion": primary_coverage,
                "review_threshold": 0.8,
                "limited_truth_coverage_reviewed": limited_coverage_reviewed,
                "limited_truth_coverage_rationale": (
                    limited_coverage_rationale or None
                ),
                "acknowledgement_valid": limited_coverage_acknowledged,
            }
        )
    known_hours = float(primary["known_duration_hours"] or 0.0)
    flood_hours = float(primary["observed_flood_duration_hours"] or 0.0)
    continuous_reviewed = bool(entry.get("continuous_flood_reference_reviewed"))
    if (
        reference_sites > 0
        and known_hours > 0
        and flood_hours / known_hours >= 0.98
        and not continuous_reviewed
    ):
        warnings.append(
            {
                "code": "near_continuous_flood_reference_requires_datum_review",
                "blocking": True,
                "flood_fraction_of_known_duration": round(
                    flood_hours / known_hours, 4
                ),
            }
        )
    return {
        "county": entry["county"],
        "state": entry["state"],
        "fips": entry["fips"],
        "event": entry.get("event"),
        "analysis_role": analysis_role,
        "analysis_role_rationale": entry.get("analysis_role_rationale"),
        "corpus_path": str(entry["path"]),
        "corpus_sha256": info["sha256"],
        "truth_reference": (
            {
                "path": _portable_path(info["truth"]["path"]),
                "sha256": info["truth"]["sha256"],
            }
            if info["truth"]
            else None
        ),
        "datum_calibration": (
            {
                "path": _portable_path(info["calibration"]["path"]),
                "sha256": info["calibration"]["sha256"],
            }
            if info["calibration"]
            else None
        ),
        "analysis_start": entry["analysis_start"],
        "analysis_end": entry["analysis_end"],
        "analysis_elapsed_hours": info["elapsed_hours"],
        "analysis_interval_convention": "[start,end)",
        "reference_sites_after_exclusion": reference_sites,
        "limited_truth_coverage_reviewed": limited_coverage_reviewed,
        "limited_truth_coverage_rationale": limited_coverage_rationale or None,
        "threshold_exclusions": exclusions,
        "truth_coverage": coverage,
        "warnings": warnings,
        "blocked": any(item["blocking"] for item in warnings),
    }


def _range(values: Iterable[Optional[float]]) -> Dict[str, Optional[float]]:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return {"min": None, "median": None, "max": None}
    return {
        "min": round(min(numeric), 4),
        "median": round(statistics.median(numeric), 4),
        "max": round(max(numeric), 4),
    }


def _schedule_row(
    entry: Dict[str, Any], name: str, summary: Dict[str, Any]
) -> Dict[str, Any]:
    metrics = summary["metrics"]
    schedule = summary.get("schedule") or {}
    timeliness = metrics["rq2_timeliness"]
    episodes = metrics["rq2_event_episodes"]
    agreement = metrics["rq1_policy_state_agreement"]["pooled"]
    episode_rows = episodes.get("episodes") or []
    return {
        "county": entry["county"],
        "state": entry["state"],
        "fips": entry["fips"],
        "event": entry.get("event"),
        "analysis_role": entry.get("analysis_role") or "unspecified",
        "policy": name,
        "schedule_kind": schedule.get("kind"),
        "fixed_interval_hours": (
            round(schedule["fixed_interval_seconds"] / 3600.0, 4)
            if schedule.get("fixed_interval_seconds") is not None
            else None
        ),
        "schedule_offset_hours": round(
            float(schedule.get("offset_seconds") or 0) / 3600.0, 4
        ),
        "acquisitions": timeliness["acquisitions"],
        "current_runtime_logical_report_delivery_opportunities": timeliness[
            "current_runtime_logical_report_delivery_opportunities"
        ],
        "material_acquisitions": timeliness["material_acquisitions"],
        "hypothetical_logical_report_delivery_opportunities_if_materiality_gated": timeliness[
            "hypothetical_logical_report_delivery_opportunities_if_materiality_gated"
        ],
        "hypothetical_materiality_gate_suppression_rate": timeliness[
            "hypothetical_materiality_gate_suppression_rate"
        ],
        "truth_known_points": timeliness["truth_known_points"],
        "truth_unknown_points": timeliness["truth_unknown_points"],
        "truth_known_duration_proportion": episodes["truth_duration_coverage"],
        "known_clear_duration_hours": episodes["known_clear_duration_hours"],
        "observed_flood_duration_hours": episodes["observed_flood_duration_hours"],
        "unknown_truth_duration_hours": episodes["unknown_truth_duration_hours"],
        "observed_flood_episodes": episodes["n_observed_flood_episodes"],
        "episodes_observed_by_schedule": episodes[
            "episodes_observed_by_schedule"
        ],
        "episodes_not_observed_by_schedule": episodes[
            "episodes_not_observed_by_schedule"
        ],
        "schedule_flood_stage_observation_lag_hours": [
            row["schedule_flood_stage_observation_lag_hours"]
            for row in episode_rows
        ],
        "unobserved_flood_stage_duration_hours": episodes[
            "unobserved_flood_stage_duration_hours"
        ],
        "unobserved_flood_stage_duration_rate": episodes[
            "unobserved_flood_stage_duration_rate"
        ],
        "phase_changes": timeliness["phase_changes"],
        "phase_duration_hours": timeliness["phase_duration_hours"],
        "folded_at": timeliness["folded_at"],
        "policy_truth_agreement_tp": agreement["tp"],
        "policy_truth_agreement_fp": agreement["fp"],
        "policy_truth_agreement_tn": agreement["tn"],
        "policy_truth_agreement_fn": agreement["fn"],
        "policy_truth_agreement_unknown": agreement["unknown"],
        "policy_truth_agreement_note": (
            "action-stage policy state vs flood-stage reference; not detector accuracy"
        ),
        "source_availability": metrics["rq4_source_availability"],
        "target_acquisitions": summary.get("target_acquisitions"),
        "realised_acquisitions": summary.get("realised_acquisitions"),
        "budget_gap": summary.get("budget_gap"),
        "selected_nearest_realised_acquisition_count": summary.get(
            "selected_nearest_realised_acquisition_count"
        ),
    }


def _matched_budget_fields(
    *,
    target_acquisitions: int,
    realised_acquisitions: int,
    selected: bool = True,
) -> Dict[str, Any]:
    """Expose the count-only matched-budget selection on every summary row."""
    return {
        "target_acquisitions": target_acquisitions,
        "realised_acquisitions": realised_acquisitions,
        "budget_gap": realised_acquisitions - target_acquisitions,
        "selected_nearest_realised_acquisition_count": selected,
    }


def _matched_budget_offset_rows(
    entry: Dict[str, Any],
    *,
    candidates: Sequence[tuple[Dict[str, Any], Dict[str, Any], int]],
    target_acquisitions: int,
    selected_offset_seconds: int,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    evaluation_interval_seconds: int,
) -> List[Dict[str, Any]]:
    """Retain outcome summaries for every prespecified matched-budget offset."""
    rows = []
    for spec, run, realised in candidates:
        summary = {
            "schedule": run.get("schedule"),
            "metrics": evaluate(
                run,
                corpus=corpus,
                events=events,
                evaluation_interval_seconds=evaluation_interval_seconds,
            ),
            **_matched_budget_fields(
                target_acquisitions=target_acquisitions,
                realised_acquisitions=realised,
                selected=(
                    spec["schedule_offset_seconds"] == selected_offset_seconds
                ),
            ),
        }
        rows.append(_schedule_row(entry, spec["name"], summary))
    return rows


def _matched_budget_offset_ranges(
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compact, prespecified outcome ranges across matched-budget offsets."""
    return {
        "n_phase_offsets": len(rows),
        "fixed_interval_hours": rows[0]["fixed_interval_hours"] if rows else None,
        "target_acquisitions": rows[0]["target_acquisitions"] if rows else None,
        "realised_acquisitions": _range(
            row["realised_acquisitions"] for row in rows
        ),
        "episodes_observed_by_schedule": _range(
            row["episodes_observed_by_schedule"] for row in rows
        ),
        "episodes_not_observed_by_schedule": _range(
            row["episodes_not_observed_by_schedule"] for row in rows
        ),
        "flood_stage_observation_lag_hours": _range(
            value
            for row in rows
            for value in row["schedule_flood_stage_observation_lag_hours"]
        ),
        "unobserved_flood_stage_duration_hours": _range(
            row["unobserved_flood_stage_duration_hours"] for row in rows
        ),
        "unobserved_flood_stage_duration_rate": _range(
            row["unobserved_flood_stage_duration_rate"] for row in rows
        ),
        "phase_changes": _range(row["phase_changes"] for row in rows),
        "n_folded_runs": sum(row["folded_at"] is not None for row in rows),
        "selection_rule": (
            "all prespecified 15-minute clock offsets retained; selected run uses "
            "nearest realised acquisition count and earliest-offset tie-break; "
            "outcomes never affect selection"
        ),
    }


def _fixed_ranges(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, float], List[Dict[str, Any]]] = {}
    for row in rows:
        cadence = row.get("fixed_interval_hours")
        if not row.get("policy", "").startswith("fixed_") or cadence is None:
            continue
        grouped.setdefault((row["fips"], cadence), []).append(row)
    out = []
    for (fips, cadence), members in sorted(grouped.items()):
        out.append(
            {
                "county": members[0]["county"],
                "state": members[0]["state"],
                "fips": fips,
                "fixed_interval_hours": cadence,
                "n_phase_offsets": len(members),
                "acquisitions": _range(row["acquisitions"] for row in members),
                "hypothetical_logical_report_delivery_opportunities_if_materiality_gated": _range(
                    row[
                        "hypothetical_logical_report_delivery_opportunities_if_materiality_gated"
                    ]
                    for row in members
                ),
                "episodes_observed_by_schedule": _range(
                    row["episodes_observed_by_schedule"] for row in members
                ),
                "episodes_not_observed_by_schedule": _range(
                    row["episodes_not_observed_by_schedule"] for row in members
                ),
                "unobserved_flood_stage_duration_hours": _range(
                    row["unobserved_flood_stage_duration_hours"] for row in members
                ),
                "flood_stage_observation_lag_hours": _range(
                    value
                    for row in members
                    for value in row[
                        "schedule_flood_stage_observation_lag_hours"
                    ]
                ),
            }
        )
    return out


def _matched_ranges(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not row.get("policy", "").startswith("matched_budget_offset_"):
            continue
        grouped.setdefault(row["fips"], []).append(row)
    out = []
    for fips, members in sorted(grouped.items()):
        out.append(
            {
                "county": members[0]["county"],
                "state": members[0]["state"],
                "fips": fips,
                "fixed_interval_hours": members[0]["fixed_interval_hours"],
                "target_acquisitions": members[0]["target_acquisitions"],
                "n_phase_offsets": len(members),
                "realised_acquisitions": _range(
                    row["realised_acquisitions"] for row in members
                ),
                "episodes_observed_by_schedule": _range(
                    row["episodes_observed_by_schedule"] for row in members
                ),
                "episodes_not_observed_by_schedule": _range(
                    row["episodes_not_observed_by_schedule"] for row in members
                ),
                "unobserved_flood_stage_duration_hours": _range(
                    row["unobserved_flood_stage_duration_hours"]
                    for row in members
                ),
                "flood_stage_observation_lag_hours": _range(
                    value
                    for row in members
                    for value in row[
                        "schedule_flood_stage_observation_lag_hours"
                    ]
                ),
                "selection_rule": (
                    "nearest realised acquisition count, earliest offset tie-break; "
                    "outcome performance not used"
                ),
            }
        )
    return out


def _fold_sensitivity(
    entry: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    truth_path: Optional[Path],
    excluded_ids: Sequence[str],
    evaluation_interval_seconds: int,
    offset_step_seconds: int,
) -> List[Dict[str, Any]]:
    common = {
        "corpus_path": str(_resolve_repo_path(entry["path"])),
        "truth_path": str(truth_path) if truth_path else None,
        "county": entry["county"],
        "state": entry["state"],
        "fips": entry["fips"],
        "start": entry["analysis_start"],
        "end": entry["analysis_end"],
        "max_staleness_hours": PRIMARY_POLICY_STALENESS_HOURS,
        "threshold_excluded_site_ids": excluded_ids,
    }
    rows = []
    for horizon in FOLD_HORIZONS_HOURS:
        adaptive = replay_run(**common, recovery_min_clear_hours=horizon)
        target = len(adaptive.get("ticks") or [])
        matched_runs = []
        for spec in matched_budget_schedule_specs(
            start=entry["analysis_start"],
            end=entry["analysis_end"],
            target_acquisitions=target,
            offset_step_seconds=offset_step_seconds,
        ):
            matched = replay_run(
                **common,
                recovery_min_clear_hours=horizon,
                fixed_interval_seconds=spec["fixed_interval_seconds"],
                schedule_offset_seconds=spec["schedule_offset_seconds"],
            )
            realised = len(matched.get("ticks") or [])
            matched_runs.append((spec, matched, realised))
        selected_spec, selected_matched, selected_realised = min(
            matched_runs,
            key=lambda item: (
                abs(item[2] - target), item[0]["schedule_offset_seconds"]
            ),
        )
        matched_offset_rows = _matched_budget_offset_rows(
            entry,
            candidates=matched_runs,
            target_acquisitions=target,
            selected_offset_seconds=selected_spec["schedule_offset_seconds"],
            corpus=corpus,
            events=events,
            evaluation_interval_seconds=evaluation_interval_seconds,
        )
        for policy, run in (
            ("adaptive", adaptive),
            ("matched_budget_selected", selected_matched),
        ):
            summary = {
                "schedule": run.get("schedule"),
                "metrics": evaluate(
                    run,
                    corpus=corpus,
                    events=events,
                    evaluation_interval_seconds=evaluation_interval_seconds,
                ),
            }
            if policy == "matched_budget_selected":
                summary.update(
                    _matched_budget_fields(
                        target_acquisitions=target,
                        realised_acquisitions=selected_realised,
                    )
                )
            rows.append(
                {
                    "recovery_min_clear_hours": horizon,
                    "policy": policy,
                    "default_48h_status": (
                        "post-audit engineering correction motivated by Hillsborough "
                        "recurrence; not optimized or validated"
                    ),
                    "matched_budget_selection": (
                        {
                            "target_acquisitions": target,
                            "derived_interval_seconds": selected_spec[
                                "fixed_interval_seconds"
                            ],
                            "n_phase_offsets": len(matched_runs),
                            "selected_offset_seconds": selected_spec[
                                "schedule_offset_seconds"
                            ],
                            "selected_realised_acquisitions": selected_realised,
                            "realised_acquisitions_min": min(
                                item[2] for item in matched_runs
                            ),
                            "realised_acquisitions_max": max(
                                item[2] for item in matched_runs
                            ),
                            "selection_rule": (
                                "minimum absolute realised-minus-adaptive acquisition "
                                "count; earliest offset breaks ties"
                            ),
                        }
                        if policy == "matched_budget_selected"
                        else None
                    ),
                    "matched_budget_offset_outcome_ranges": (
                        _matched_budget_offset_ranges(matched_offset_rows)
                        if policy == "matched_budget_selected"
                        else None
                    ),
                    "matched_budget_offset_rows": (
                        matched_offset_rows
                        if policy == "matched_budget_selected"
                        else None
                    ),
                    **_schedule_row(entry, policy, summary),
                }
            )
    return rows


def _selected_matched_budget_replay(
    *,
    common: Dict[str, Any],
    target_acquisitions: int,
    start: str,
    end: str,
    offset_step_seconds: int,
) -> tuple[
    Dict[str, Any],
    Dict[str, Any],
    List[tuple[Dict[str, Any], Dict[str, Any], int]],
]:
    """Select a matched-budget run by count only, never by its outcomes."""
    candidates = []
    for spec in matched_budget_schedule_specs(
        start=start,
        end=end,
        target_acquisitions=target_acquisitions,
        offset_step_seconds=offset_step_seconds,
    ):
        run = replay_run(
            **common,
            fixed_interval_seconds=spec["fixed_interval_seconds"],
            schedule_offset_seconds=spec["schedule_offset_seconds"],
        )
        candidates.append((spec, run, len(run.get("ticks") or [])))
    spec, selected, realised = min(
        candidates,
        key=lambda item: (
            abs(item[2] - target_acquisitions),
            item[0]["schedule_offset_seconds"],
        ),
    )
    return selected, {
        "target_acquisitions": target_acquisitions,
        "derived_interval_seconds": spec["fixed_interval_seconds"],
        "n_phase_offsets": len(candidates),
        "selected_offset_seconds": spec["schedule_offset_seconds"],
        "selected_realised_acquisitions": realised,
        "realised_acquisitions_min": min(item[2] for item in candidates),
        "realised_acquisitions_max": max(item[2] for item in candidates),
        "selection_rule": (
            "minimum absolute realised-minus-adaptive acquisition count; "
            "earliest offset breaks ties; outcomes are not used"
        ),
    }, candidates


def _policy_parameter_sensitivity(
    entry: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    truth_path: Optional[Path],
    excluded_ids: Sequence[str],
    evaluation_interval_seconds: int,
    offset_step_seconds: int,
) -> Dict[str, List[Dict[str, Any]]]:
    """Bounded safety sensitivities for two chosen runtime constants."""
    base = {
        "corpus_path": str(_resolve_repo_path(entry["path"])),
        "truth_path": str(truth_path) if truth_path else None,
        "county": entry["county"],
        "state": entry["state"],
        "fips": entry["fips"],
        "start": entry["analysis_start"],
        "end": entry["analysis_end"],
        "threshold_excluded_site_ids": excluded_ids,
    }
    outputs: Dict[str, List[Dict[str, Any]]] = {
        "clear_gauge_requirement": [],
        "policy_staleness_hours": [],
    }
    settings = [
        (
            "clear_gauge_requirement",
            float(value),
            int(value),
            PRIMARY_POLICY_STALENESS_HOURS,
            value == PRIMARY_CLEAR_GAUGE_REQUIREMENT,
        )
        for value in CLEAR_GAUGE_REQUIREMENTS
    ] + [
        (
            "policy_staleness_hours",
            float(value),
            PRIMARY_CLEAR_GAUGE_REQUIREMENT,
            float(value),
            value == PRIMARY_POLICY_STALENESS_HOURS,
        )
        for value in POLICY_STALENESS_HOURS
    ]
    for parameter, value, min_gauges, staleness, primary in settings:
        common = {**base, "max_staleness_hours": staleness}
        # Payload eligibility and claim admission have separate freshness
        # controls in production. Move both together in this sensitivity so a
        # nominal 12-hour arm does not retain the primary 6-hour claim window.
        with _temporary_environment(
            {
                "COUNTY_FLOOD_MIN_GAUGES": str(min_gauges),
                "SENTINEL_READING_MAX_AGE_HOURS": str(staleness),
            }
        ):
            adaptive = replay_run(**common)
            target = len(adaptive.get("ticks") or [])
            matched, selection, matched_candidates = _selected_matched_budget_replay(
                common=common,
                target_acquisitions=target,
                start=entry["analysis_start"],
                end=entry["analysis_end"],
                offset_step_seconds=offset_step_seconds,
            )
        matched_offset_rows = _matched_budget_offset_rows(
            entry,
            candidates=matched_candidates,
            target_acquisitions=target,
            selected_offset_seconds=selection["selected_offset_seconds"],
            corpus=corpus,
            events=events,
            evaluation_interval_seconds=evaluation_interval_seconds,
        )
        for policy, run in (
            ("adaptive", adaptive),
            ("matched_budget_selected", matched),
        ):
            summary = {
                "schedule": run.get("schedule"),
                "metrics": evaluate(
                    run,
                    corpus=corpus,
                    events=events,
                    evaluation_interval_seconds=evaluation_interval_seconds,
                ),
            }
            if policy == "matched_budget_selected":
                summary.update(
                    _matched_budget_fields(
                        target_acquisitions=target,
                        realised_acquisitions=selection[
                            "selected_realised_acquisitions"
                        ],
                    )
                )
            outputs[parameter].append(
                {
                    "sensitivity_parameter": parameter,
                    "sensitivity_value": (
                        int(value)
                        if parameter == "clear_gauge_requirement"
                        else value
                    ),
                    "primary_setting": primary,
                    "engineering_safety_sensitivity_not_optimized": True,
                    "effective_policy_parameters": {
                        "clear_gauge_requirement": min_gauges,
                        "policy_max_staleness_hours": staleness,
                        "claim_reading_max_age_hours": staleness,
                        "truth_max_age_hours": PRIMARY_TRUTH_MAX_AGE_HOURS,
                    },
                    "policy": policy,
                    "matched_budget_selection": (
                        selection if policy == "matched_budget_selected" else None
                    ),
                    "matched_budget_offset_outcome_ranges": (
                        _matched_budget_offset_ranges(matched_offset_rows)
                        if policy == "matched_budget_selected"
                        else None
                    ),
                    "matched_budget_offset_rows": (
                        matched_offset_rows
                        if policy == "matched_budget_selected"
                        else None
                    ),
                    **_schedule_row(entry, policy, summary),
                }
            )
    return outputs


def generate(
    *,
    manifest_path: Path,
    out_dir: Path,
    cadence_hours: Sequence[float] = DEFAULT_CADENCE_HOURS,
    offset_step_seconds: int = 900,
    evaluation_interval_seconds: int = DEFAULT_EVALUATION_INTERVAL_SECONDS,
    allow_qc_warnings: bool = False,
    write_raw_runs: bool = False,
) -> Dict[str, Any]:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(
            f"study output directory must be absent or empty: {out_dir}; "
            "move the prior artifact tree before regeneration"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = list(manifest.get("artifacts") or [])
    if not entries:
        raise ValueError("study manifest has no artifacts")
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
                "sensitivity_hours": list(TRUTH_AGE_HORIZONS),
                "negative_requires_all_reporting_thresholded_gauges_fresh": True,
            },
            "counties": preflight,
        },
    )
    blocked = [row for row in preflight if row["blocked"]]
    if blocked and not allow_qc_warnings:
        names = ", ".join(row["county"] for row in blocked)
        raise RuntimeError(
            f"preflight blocked study generation for: {names}; inspect preflight_qc.json"
        )

    county_results = []
    schedule_rows: List[Dict[str, Any]] = []
    matched_selected_rows: List[Dict[str, Any]] = []
    episode_rows: List[Dict[str, Any]] = []
    fold_rows: List[Dict[str, Any]] = []
    clear_gauge_sensitivity_rows: List[Dict[str, Any]] = []
    policy_staleness_sensitivity_rows: List[Dict[str, Any]] = []
    coverage_rows = [
        coverage
        for county in preflight
        for coverage in county["truth_coverage"]
    ]

    for entry in entries:
        slug = f"{_slug(entry['county'])}_{entry['fips']}"
        corpus_path = _resolve_repo_path(entry["path"])
        raw_corpus = load_corpus(corpus_path)
        truth_path = _truth_path_for(entry)
        events = (
            (truth_load(truth_path) or {}).get("events") or []
            if truth_path
            else []
        )
        excluded_ids = _excluded_site_ids(entry)
        corpus, _ = apply_threshold_compatibility(
            raw_corpus, excluded_site_ids=excluded_ids
        )
        raw_dir = out_dir / "counties" / slug / "runs" if write_raw_runs else None
        with _temporary_environment(FROZEN_REPLAY_ENVIRONMENT):
            comparison = compare_schedules(
                corpus_path=str(corpus_path),
                truth_path=str(truth_path) if truth_path else None,
                county=entry["county"],
                state=entry["state"],
                fips=entry["fips"],
                start=entry["analysis_start"],
                end=entry["analysis_end"],
                max_staleness_hours=PRIMARY_POLICY_STALENESS_HOURS,
                cadence_hours=cadence_hours,
                offset_step_seconds=offset_step_seconds,
                evaluation_interval_seconds=evaluation_interval_seconds,
                runs_dir=str(raw_dir) if raw_dir else None,
                threshold_excluded_site_ids=excluded_ids,
            )
            county_fold_rows = _fold_sensitivity(
                entry,
                corpus=corpus,
                events=events,
                truth_path=truth_path,
                excluded_ids=excluded_ids,
                evaluation_interval_seconds=evaluation_interval_seconds,
                offset_step_seconds=offset_step_seconds,
            )
            policy_sensitivity = _policy_parameter_sensitivity(
                entry,
                corpus=corpus,
                events=events,
                truth_path=truth_path,
                excluded_ids=excluded_ids,
                evaluation_interval_seconds=evaluation_interval_seconds,
                offset_step_seconds=offset_step_seconds,
            )
        result = {
            "schema": "sentinel-county-study-result/3",
            "corpus_manifest_entry": entry,
            "truth_reference": _portable_path(truth_path) if truth_path else None,
            "nws_system_input": None,
            "comparison": comparison,
            "fold_sensitivity": county_fold_rows,
            "clear_gauge_requirement_sensitivity": policy_sensitivity[
                "clear_gauge_requirement"
            ],
            "policy_staleness_sensitivity": policy_sensitivity[
                "policy_staleness_hours"
            ],
        }
        result_path = out_dir / "counties" / slug / "result.json"
        _write_json(result_path, result)
        county_results.append(
            {
                "county": entry["county"],
                "state": entry["state"],
                "fips": entry["fips"],
                "analysis_role": entry.get("analysis_role") or "unspecified",
                "result_path": str(result_path.relative_to(out_dir)),
            }
        )

        rows = [_schedule_row(entry, "adaptive", comparison["adaptive"])]
        rows.extend(
            _schedule_row(entry, row["name"], row)
            for row in comparison["fixed_phase_offsets"]
        )
        rows.extend(
            _schedule_row(entry, row["name"], row)
            for row in comparison["matched_budget_phase_offsets"]
        )
        matched_selected_rows.append(
            {
                **_schedule_row(
                    entry,
                    "matched_budget_selected",
                    comparison["matched_budget"],
                ),
                "selection_rule": comparison["matched_budget"]["selection_rule"],
            }
        )
        schedule_rows.extend(rows)
        adaptive_episodes = comparison["adaptive"]["metrics"][
            "rq2_event_episodes"
        ]["episodes"]
        episode_rows.extend(
            {
                "county": entry["county"],
                "state": entry["state"],
                "fips": entry["fips"],
                "event": entry.get("event"),
                "analysis_role": entry.get("analysis_role") or "unspecified",
                **episode,
            }
            for episode in adaptive_episodes
        )
        fold_rows.extend(county_fold_rows)
        clear_gauge_sensitivity_rows.extend(
            policy_sensitivity["clear_gauge_requirement"]
        )
        policy_staleness_sensitivity_rows.extend(
            policy_sensitivity["policy_staleness_hours"]
        )

    adaptive_rows = [row for row in schedule_rows if row["policy"] == "adaptive"]
    evaluable_adaptive_rows = [
        row
        for row in adaptive_rows
        if row["analysis_role"] == "episode_evaluation"
    ]
    total_episodes = sum(
        row["observed_flood_episodes"] for row in evaluable_adaptive_rows
    )
    observed_episodes = sum(
        row["episodes_observed_by_schedule"] for row in evaluable_adaptive_rows
    )
    primary_coverage = [
        row
        for row in coverage_rows
        if row["truth_max_age_hours"] == PRIMARY_TRUTH_MAX_AGE_HOURS
    ]
    known_hours = sum(float(row["known_duration_hours"] or 0) for row in primary_coverage)
    unknown_hours = sum(
        float(row["unknown_duration_hours"] or 0) for row in primary_coverage
    )
    evaluable_coverage = [
        row
        for row in primary_coverage
        if row["analysis_role"] == "episode_evaluation"
    ]
    evaluable_known_hours = sum(
        float(row["known_duration_hours"] or 0) for row in evaluable_coverage
    )
    evaluable_unknown_hours = sum(
        float(row["unknown_duration_hours"] or 0) for row in evaluable_coverage
    )
    coverage_only = [
        row for row in primary_coverage if row["analysis_role"] == "coverage_only"
    ]
    aggregate = {
        "schema": "sentinel-study-aggregate/3",
        "analysis_interval_convention": "[analysis_start,analysis_end)",
        "evaluation_interval_seconds": evaluation_interval_seconds,
        "n_counties": len(entries),
        "n_episode_evaluation_counties": len(evaluable_coverage),
        "n_coverage_only_counties": len(coverage_only),
        "adaptive_acquisitions": sum(row["acquisitions"] for row in adaptive_rows),
        "adaptive_current_runtime_logical_report_delivery_opportunities": sum(
            row["current_runtime_logical_report_delivery_opportunities"]
            for row in adaptive_rows
        ),
        "adaptive_material_acquisitions": sum(
            row["material_acquisitions"] for row in adaptive_rows
        ),
        "adaptive_hypothetical_logical_report_delivery_opportunities_if_materiality_gated": sum(
            row[
                "hypothetical_logical_report_delivery_opportunities_if_materiality_gated"
            ]
            for row in adaptive_rows
        ),
        "observed_flood_episodes": total_episodes,
        "episodes_observed_by_adaptive_schedule": observed_episodes,
        "episode_observation_rate": (
            round(observed_episodes / total_episodes, 4)
            if total_episodes
            else None
        ),
        "all_county_primary_truth_known_duration_hours": round(known_hours, 4),
        "all_county_primary_truth_unknown_duration_hours": round(unknown_hours, 4),
        "all_county_primary_truth_known_duration_proportion": (
            round(known_hours / (known_hours + unknown_hours), 4)
            if known_hours + unknown_hours
            else None
        ),
        "episode_evaluation_primary_truth_known_duration_hours": round(
            evaluable_known_hours, 4
        ),
        "episode_evaluation_primary_truth_unknown_duration_hours": round(
            evaluable_unknown_hours, 4
        ),
        "episode_evaluation_primary_truth_known_duration_proportion": (
            round(
                evaluable_known_hours
                / (evaluable_known_hours + evaluable_unknown_hours),
                4,
            )
            if evaluable_known_hours + evaluable_unknown_hours
            else None
        ),
        "coverage_pooling_note": (
            "all-county coverage includes coverage-only counties; episode and "
            "latency denominators use only analysis_role=episode_evaluation"
        ),
        "pooling_note": (
            "descriptive county-episode sums only; common-grid points are not "
            "treated as independent inferential samples"
        ),
    }
    tables = {
        "schema": "sentinel-study-tables/3",
        "county_primary": adaptive_rows,
        "schedule_by_county_and_offset": schedule_rows,
        "fixed_cadence_offset_ranges": _fixed_ranges(schedule_rows),
        "matched_budget_offset_ranges": _matched_ranges(schedule_rows),
        "matched_budget_selected": matched_selected_rows,
        "adaptive_episode_rows": episode_rows,
        "truth_coverage_by_county_and_horizon": coverage_rows,
        "recovery_fold_sensitivity": fold_rows,
        "clear_gauge_requirement_sensitivity": clear_gauge_sensitivity_rows,
        "policy_staleness_sensitivity": policy_staleness_sensitivity_rows,
    }
    _write_json(out_dir / "aggregate.json", aggregate)
    _write_json(out_dir / "tables.json", tables)

    implementation_files = _implementation_files()
    input_artifacts: List[Dict[str, Any]] = []
    for county in preflight:
        input_artifacts.append(
            {
                "county": county["county"],
                "kind": "gauge_corpus",
                "path": county["corpus_path"],
                "sha256": county["corpus_sha256"],
            }
        )
        if county["truth_reference"]:
            input_artifacts.append(
                {
                    "county": county["county"],
                    "kind": "nws_timing_reference",
                    **county["truth_reference"],
                }
            )
        if county["datum_calibration"]:
            input_artifacts.append(
                {
                    "county": county["county"],
                    "kind": "datum_calibration",
                    **county["datum_calibration"],
                }
            )
    generated_files = sorted(
        path
        for path in out_dir.rglob("*.json")
        if path.name != "artifact_manifest.json"
    )
    artifact_manifest = {
        "schema": "sentinel-study-artifact-manifest/3",
        "source_manifest": _portable_path(manifest_path),
        "source_manifest_sha256": _sha256(manifest_path),
        "config": {
            "analysis_interval_convention": "[analysis_start,analysis_end)",
            "evaluation_interval_seconds": evaluation_interval_seconds,
            "cadence_hours": list(cadence_hours),
            "phase_offset_step_seconds": offset_step_seconds,
            "truth_max_age_hours": PRIMARY_TRUTH_MAX_AGE_HOURS,
            "truth_age_sensitivity_hours": list(TRUTH_AGE_HORIZONS),
            "recovery_fold_sensitivity_hours": list(FOLD_HORIZONS_HOURS),
            "primary_clear_gauge_requirement": PRIMARY_CLEAR_GAUGE_REQUIREMENT,
            "clear_gauge_requirement_sensitivity": list(
                CLEAR_GAUGE_REQUIREMENTS
            ),
            "primary_policy_staleness_hours": PRIMARY_POLICY_STALENESS_HOURS,
            "policy_staleness_sensitivity_hours": list(POLICY_STALENESS_HOURS),
            "frozen_replay_environment": dict(FROZEN_REPLAY_ENVIRONMENT),
            "environment_sensitivity_overrides": {
                "COUNTY_FLOOD_MIN_GAUGES": list(CLEAR_GAUGE_REQUIREMENTS),
                "SENTINEL_READING_MAX_AGE_HOURS": list(
                    POLICY_STALENESS_HOURS
                ),
            },
            "replay_argument_sensitivity_overrides": {
                "max_staleness_hours": list(POLICY_STALENESS_HOURS),
            },
            "caller_environment_restored_after_generation": True,
            "nws_system_input": None,
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
        "implementation_sha256": {
            _portable_path(path): _sha256(path) for path in implementation_files
        },
        "input_artifacts": input_artifacts,
        "county_results": county_results,
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
    parser.add_argument(
        "--manifest", default="eval/corpus/study/manifest.json"
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--cadence-hours",
        type=float,
        nargs="+",
        default=list(DEFAULT_CADENCE_HOURS),
    )
    parser.add_argument("--offset-step-minutes", type=float, default=15.0)
    parser.add_argument("--evaluation-grid-minutes", type=float, default=15.0)
    parser.add_argument("--allow-qc-warnings", action="store_true")
    parser.add_argument("--write-raw-runs", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = generate(
            manifest_path=_resolve_repo_path(args.manifest),
            out_dir=Path(args.out_dir),
            cadence_hours=args.cadence_hours,
            offset_step_seconds=max(60, round(args.offset_step_minutes * 60)),
            evaluation_interval_seconds=max(
                60, round(args.evaluation_grid_minutes * 60)
            ),
            allow_qc_warnings=args.allow_qc_warnings,
            write_raw_runs=args.write_raw_runs,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"study generation failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "artifact_manifest": str(
                    Path(args.out_dir) / "artifact_manifest.json"
                ),
                "n_artifacts": len(result["artifacts"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
