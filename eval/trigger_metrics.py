"""Trigger-stream accounting metrics for hybrid verification-triggered runs.

These metrics report what the trigger *cost* and what it *pointed at*. They
never enter episode grading, which remains the unchanged v2 hydrologic
contract in :mod:`eval.metrics`. Burst windows are defined entirely by NWS
issuance, so the yield denominator is independently defensible; hydrologic
episodes are policy-independent, so the missed-trigger rate is too.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from eval.ground_truth import hydrologic_truth_at, parse_iso
from eval.metrics import annotate_run_on_grid, rq2_flood_episodes

_TRIGGER_REASONS = ("verification_immediate", "verification_burst")


def reference_episode_intervals(
    corpus: Dict[str, Any],
    *,
    start: str,
    end: str,
    interval_seconds: int = 900,
    truth_max_age_hours: float = 1.0,
) -> List[Dict[str, str]]:
    """Policy-independent hydrologic episode spans on the common grid.

    Each span runs from the episode's first positive grid instant through the
    exclusive end of its last positive instant (censored spans keep those
    bounds; unknown padding is not included).
    """
    empty_run = {"window": {"start": start, "end": end}, "ticks": []}
    grid = annotate_run_on_grid(
        empty_run,
        corpus=corpus,
        events=[],
        interval_seconds=interval_seconds,
        truth_max_age_hours=truth_max_age_hours,
    )
    episodes = rq2_flood_episodes(grid)
    return [
        {
            "start": episode["observed_start"],
            "end": episode["last_observed_positive_end_exclusive"],
        }
        for episode in episodes["episodes"]
    ]


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


def trigger_accounting_metrics(
    run: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    truth_max_age_hours: float = 1.0,
    interval_seconds: int = 900,
) -> Optional[Dict[str, Any]]:
    """Cost and yield of the verification trigger for one hybrid run."""
    accounting = run.get("verification_trigger")
    if not accounting:
        return None
    window = run.get("window") or {}
    episodes = reference_episode_intervals(
        corpus,
        start=window["start"],
        end=window["end"],
        interval_seconds=interval_seconds,
        truth_max_age_hours=truth_max_age_hours,
    )
    # A mission that folds stops acquiring and stops consulting the stream, so
    # windows and episodes after that instant are outside what this run could
    # observe. Report the boundary rather than letting a truncated denominator
    # read as full-window coverage.
    termination = run.get("termination") or {}
    terminated_at = (
        str(termination.get("t"))
        if termination.get("reason") == "recovery_continuous_clear"
        and termination.get("t")
        else None
    )
    episodes_after_termination = (
        sum(1 for ep in episodes if terminated_at and ep["start"] >= terminated_at)
        if terminated_at
        else 0
    )

    # Unnecessary burden: trigger-attributable acquisitions taken while the
    # hydrologic reference was known clear at that instant.
    clear = flooding = unknown = 0
    for tick in run.get("ticks") or []:
        if tick.get("acquisition_due_reason") not in _TRIGGER_REASONS:
            continue
        hydro = hydrologic_truth_at(
            corpus, parse_iso(tick["t"]), max_age_hours=truth_max_age_hours
        )
        if hydro["is_flooding"] is True:
            flooding += 1
        elif hydro["is_flooding"] is False:
            clear += 1
        else:
            unknown += 1

    # Yield: burst windows (NWS-issuance-defined) that overlap an episode.
    windows = [
        {
            "event_key": row["event_key"],
            "start": row["burst_window_start"],
            "end": row["burst_window_end"],
            "stale_at_first_sight": bool(row.get("stale_at_first_sight")),
        }
        for row in accounting.get("events_first_seen") or []
    ]
    for row in windows:
        row["overlaps_hydrologic_episode"] = any(
            _overlaps(row["start"], row["end"], ep["start"], ep["end"])
            for ep in episodes
        )
    active_windows = [row for row in windows if not row["stale_at_first_sight"]]
    overlapping = sum(row["overlaps_hydrologic_episode"] for row in active_windows)

    # Coverage: episodes preceded or covered by a qualifying window.
    episode_rows = []
    for ep in episodes:
        covering = [
            row
            for row in windows
            if _overlaps(row["start"], row["end"], ep["start"], ep["end"])
        ]
        anticipating = [row for row in covering if row["start"] <= ep["start"]]
        episode_rows.append(
            {
                **ep,
                "trigger_covered": bool(covering),
                "trigger_anticipated_onset": bool(anticipating),
            }
        )

    return {
        "verification_acquisitions_total": accounting[
            "trigger_attributable_acquisitions"
        ],
        "verification_acquisitions_during_known_clear": clear,
        "verification_acquisitions_during_flooding": flooding,
        "verification_acquisitions_during_unknown": unknown,
        "trigger_stream_consultations": accounting["trigger_stream_consultations"],
        "immediate_fires": accounting["immediate_fires"],
        "burst_windows_total": len(windows),
        "burst_windows_active_at_first_sight": len(active_windows),
        "burst_windows_overlapping_episode": overlapping,
        "burst_window_episode_yield": (
            round(overlapping / len(active_windows), 4) if active_windows else None
        ),
        "yield_denominator_semantics": (
            "burst windows are defined by NWS issuance, independent of any "
            "policy outcome; a run that folds stops consulting the stream, so "
            "windows and episodes after run_terminated_at are outside this "
            "run's observable span"
        ),
        "run_terminated_at": terminated_at,
        "reference_episodes_after_termination": episodes_after_termination,
        "episodes_with_trigger_coverage": sum(
            row["trigger_covered"] for row in episode_rows
        ),
        "episodes_with_onset_anticipating_trigger": sum(
            row["trigger_anticipated_onset"] for row in episode_rows
        ),
        "episodes_total": len(episode_rows),
        "episode_trigger_rows": episode_rows,
        "burst_window_rows": windows,
    }
