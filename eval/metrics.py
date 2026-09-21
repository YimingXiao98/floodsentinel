"""Metrics for the four research questions.

Decision runs acquire evidence on different schedules.  Detection, timeliness,
and abstention are therefore scored on one exogenous grid, carrying the latest
policy state forward without looking ahead.  Acquisition-level quantities
(claim quality, calibration, phase changes, and message materiality) remain on
the native run ticks so a slow schedule neither earns a smaller denominator nor
duplicates the same claim at every evaluation point.

RQ1  detection quality of the county flood assessment, pooled and on the
     conflict subset where a low-tier source contradicts the gauges
RQ2  timeliness of escalation and reporting burden
RQ3  calibration of the confidence scores
RQ4  honest abstention under sparse or missing data
     and acquisition-level separation of absent versus stale stage sources
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from eval.ground_truth import (
    apply_threshold_compatibility,
    first_flood_warning_time,
    hydrologic_truth_at,
    parse_iso,
    truth_level_at,
)

#: Phases at or above which the system is treating the event as active.
ESCALATED_PHASES = ("elevated_watch", "imminent", "active_incident")

#: Claim severities that assert flooding.
_POSITIVE_SEVERITIES = ("minor", "moderate", "severe", "extreme")

#: USGS IV observations in the frozen study corpora are normally sampled every
#: 15 minutes, so the common grid uses that source resolution.  It remains
#: exogenous to every acquisition policy.
DEFAULT_EVALUATION_INTERVAL_SECONDS = 900

#: Primary oracle freshness rule and prespecified sensitivity values.  USGS IV
#: series in these corpora are normally sampled every 15 minutes.
DEFAULT_TRUTH_MAX_AGE_HOURS = 1.0
TRUTH_MAX_AGE_SENSITIVITY_HOURS = (0.5, 2.0, 6.0)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _f1(precision: Optional[float], recall: Optional[float]) -> Optional[float]:
    # Undefined only when a component is missing. A precision or recall of
    # exactly 0.0 is a real score, and returning None there would drop the
    # worst configuration out of the comparison as though it had no data.
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 4)


# --------------------------------------------------------------------------
# Truth alignment
# --------------------------------------------------------------------------


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evaluation_grid(
    start: str,
    end: str,
    *,
    interval_seconds: int = DEFAULT_EVALUATION_INTERVAL_SECONDS,
    offset_seconds: int = 0,
) -> List[datetime]:
    """Return an exogenous, inclusive evaluation grid.

    The event end is always included, even when the interval does not divide
    the window exactly.  ``offset_seconds`` is an evaluation protocol choice,
    not the acquisition schedule offset used by :mod:`eval.run_replay`.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if offset_seconds < 0 or offset_seconds >= interval_seconds:
        raise ValueError("offset_seconds must be in [0, interval_seconds)")
    first = parse_iso(start) + timedelta(seconds=offset_seconds)
    stop = parse_iso(end)
    if first > stop:
        return [stop]
    out: List[datetime] = []
    current = first
    step = timedelta(seconds=interval_seconds)
    while current <= stop:
        out.append(current)
        current += step
    if not out or out[-1] < stop:
        out.append(stop)
    return out


def _initial_policy_state(run: Dict[str, Any]) -> Dict[str, Any]:
    """Policy state before the first acquisition in an offset schedule."""
    supplied = run.get("initial_policy_state")
    if isinstance(supplied, dict):
        return deepcopy(supplied)
    return {
        "phase_before": "early_watch",
        "phase_after": "early_watch",
        "phase_changed": False,
        "alert_level": "none",
        "regional_verdict": "insufficient_data",
        "is_flooding": False,
        "flood_stage_observed": False,
        "flood_stage_observed_sites": [],
        "max_flood_state": None,
        "gauges_with_data": 0,
        "gauges_scored": 0,
        "api_confidence": None,
        "claims": [],
        "all_claims": [],
        "n_claims": 0,
        "n_rejected_claims": 0,
        "material_acquisition": False,
        "material_change": False,
        "folded": False,
    }


def annotate_run(
    run: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    truth_max_age_hours: Optional[float] = DEFAULT_TRUTH_MAX_AGE_HOURS,
) -> List[Dict[str, Any]]:
    """Attach ground truth to each tick of a run."""
    annotated = []
    for tick in run.get("ticks") or []:
        when = parse_iso(tick["t"])
        hydro = hydrologic_truth_at(
            corpus, when, max_age_hours=truth_max_age_hours
        )
        annotated.append(
            {
                **tick,
                "truth_hydrologic_flooding": hydro["is_flooding"],
                "truth_known": hydro["truth_known"],
                "truth_gauges_above_flood": len(hydro["gauges_above_flood_stage"]),
                "truth_flooding_sites": hydro["gauges_above_flood_stage"],
                "truth_reference_gauges": hydro["reference_gauges"],
                "truth_reference_gauges_fresh": hydro["reference_gauges_fresh"],
                "truth_official_level": truth_level_at(events, when),
            }
        )
    return annotated


def annotate_run_on_grid(
    run: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    interval_seconds: int = DEFAULT_EVALUATION_INTERVAL_SECONDS,
    offset_seconds: int = 0,
    truth_max_age_hours: Optional[float] = DEFAULT_TRUTH_MAX_AGE_HOURS,
) -> List[Dict[str, Any]]:
    """Score a run on a common grid using last-observation carry-forward.

    At time ``g`` this function can use only a decision timestamped at or before
    ``g``.  If an acquisition schedule starts after the event window begins,
    the explicit initial policy state is carried until the first decision.  A
    terminal folded state is likewise carried through the rest of the window,
    making post-fold flood recurrence visible rather than dropping it from the
    denominator.
    """
    window = run.get("window") or {}
    start, end = window.get("start"), window.get("end")
    if not start or not end:
        raise ValueError("run.window.start and run.window.end are required")

    source_ticks = sorted(
        (deepcopy(t) for t in (run.get("ticks") or [])),
        key=lambda t: parse_iso(t["t"]),
    )
    source_times = [parse_iso(t["t"]) for t in source_ticks]
    points = evaluation_grid(
        start,
        end,
        interval_seconds=interval_seconds,
        offset_seconds=offset_seconds,
    )
    initial = _initial_policy_state(run)
    latest: Optional[Dict[str, Any]] = None
    source_index = 0
    aligned: List[Dict[str, Any]] = []

    for point_index, when in enumerate(points):
        while source_index < len(source_ticks) and source_times[source_index] <= when:
            latest = source_ticks[source_index]
            source_index += 1
        state = deepcopy(latest if latest is not None else initial)
        observed_at = latest.get("t") if latest is not None else None
        hydro = hydrologic_truth_at(
            corpus, when, max_age_hours=truth_max_age_hours
        )
        next_when = points[point_index + 1] if point_index + 1 < len(points) else when
        aligned.append(
            {
                **state,
                "t": _utc_iso(when),
                "policy_observed_at": observed_at,
                "policy_initialized": latest is not None,
                "policy_state_age_hours": (
                    round((when - parse_iso(observed_at)).total_seconds() / 3600.0, 4)
                    if observed_at
                    else None
                ),
                "evaluation_interval_hours": round(
                    (next_when - when).total_seconds() / 3600.0, 6
                ),
                "evaluation_boundary": when == parse_iso(end),
                "truth_hydrologic_flooding": hydro["is_flooding"],
                "truth_known": hydro["truth_known"],
                "truth_gauges_above_flood": len(hydro["gauges_above_flood_stage"]),
                "truth_flooding_sites": hydro["gauges_above_flood_stage"],
                "truth_reference_gauges": hydro["reference_gauges"],
                "truth_reference_gauges_fresh": hydro["reference_gauges_fresh"],
                "truth_official_level": truth_level_at(events, when),
            }
        )
    return aligned


# --------------------------------------------------------------------------
# RQ1 — detection quality
# --------------------------------------------------------------------------


def _confusion(ticks: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    tp = fp = tn = fn = 0
    unknown = 0
    for tick in ticks:
        if tick.get("evaluation_boundary"):
            continue
        if (
            "truth_hydrologic_flooding" in tick
            and tick.get("truth_hydrologic_flooding") is None
        ):
            unknown += 1
            continue
        predicted = bool(tick.get("is_flooding"))
        actual = bool(tick.get("truth_hydrologic_flooding"))
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "unknown": unknown}


def _scores(confusion: Dict[str, int]) -> Dict[str, Any]:
    tp, fp, fn, tn = confusion["tp"], confusion["fp"], confusion["fn"], confusion["tn"]
    precision = _rate(tp, tp + fp)
    recall = _rate(tp, tp + fn)
    return {
        **confusion,
        "n": tp + fp + tn + fn,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "accuracy": _rate(tp + tn, tp + fp + tn + fn),
        "unknown": int(confusion.get("unknown") or 0),
        "coverage": _rate(tp + fp + tn + fn, tp + fp + tn + fn + int(confusion.get("unknown") or 0)),
    }


def is_conflict_tick(tick: Dict[str, Any]) -> bool:
    """A low-tier source asserts flooding while no official source does.

    This is the only regime where reliability tiering can change an outcome: if
    every source agrees, weighting them differently cannot alter the verdict.

    Conflict is a property of the *sources*, so it is read off the claims
    rather than off the resulting verdict. Keying it to the verdict would
    restrict the subset to intervals the system already called clear, which can
    contain no true positives by construction.
    """
    claims = tick.get("all_claims") or tick.get("claims") or []
    low_tier_asserts = any(
        str(c.get("tier") or "T0") not in ("T0", "T1")
        and str(c.get("severity") or "").lower() in _POSITIVE_SEVERITIES
        for c in claims
    )
    official_asserts = any(
        str(c.get("tier") or "T0") in ("T0", "T1")
        and str(c.get("severity") or "").lower() in _POSITIVE_SEVERITIES
        for c in claims
    )
    return bool(low_tier_asserts) and not official_asserts


def rq1_policy_state_agreement(ticks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    conflict = [
        t
        for t in ticks
        if not t.get("evaluation_boundary") and is_conflict_tick(t)
    ]
    return {
        "metric_semantics": {
            "policy_positive": "system is_flooding (action stage or higher)",
            "reference_positive": "fresh gauge at published flood stage or higher",
            "interpretation": "policy-state agreement, not independent detector accuracy",
        },
        "pooled": _scores(_confusion(ticks)),
        "conflict_subset": {
            **_scores(_confusion(conflict)),
            "n_conflict_ticks": len(conflict),
        },
    }


def rq1_detection(ticks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Backward-compatible name for :func:`rq1_policy_state_agreement`."""
    return rq1_policy_state_agreement(ticks)


def rq1_evidence_quality(
    ticks: Sequence[Dict[str, Any]], corpus: Dict[str, Any]
) -> Dict[str, Any]:
    """Precision of the evidence the system admits.

    This is where the belief layer actually acts. With it enabled, structurally
    valid claims must also clear the phase confidence floor after tier
    adjustment. The no-belief ablation bypasses that epistemic gate, admitting
    every structurally valid claim. The question is whether the extra evidence
    is *wrong*.

    A gauge claim is checked against the state of its own gauge at that instant;
    a news claim against whether the county was flooding at all.
    """
    site_flood_stage = {
        s["site_no"]: s.get("flood_stage")
        for s in corpus.get("sites") or []
        if s.get("site_no")
    }

    tp = fp = tn = fn = 0
    by_tier: Dict[str, Dict[str, int]] = {}
    for tick in ticks:
        if (
            "truth_hydrologic_flooding" in tick
            and tick.get("truth_hydrologic_flooding") is None
        ):
            continue
        if "truth_flooding_sites" in tick:
            flooding_sites = set(tick.get("truth_flooding_sites") or [])
            county_flooding = bool(tick.get("truth_hydrologic_flooding"))
        else:
            when = parse_iso(tick["t"])
            hydro = hydrologic_truth_at(corpus, when)
            flooding_sites = set(hydro["gauges_above_flood_stage"])
            county_flooding = bool(hydro["is_flooding"])

        for claim in tick.get("claims") or []:
            # Unscored observations assert no flood state, so they are
            # abstentions rather than denials and would otherwise bank
            # guaranteed true negatives for whichever configuration admits more
            # of them.
            if claim.get("unscored"):
                continue
            asserts_flood = (
                str(claim.get("severity") or "").lower() in _POSITIVE_SEVERITIES
            )
            site = claim.get("site_no")
            if site and site_flood_stage.get(site) is not None:
                actual = site in flooding_sites
            else:
                actual = county_flooding

            tier = claim.get("tier") or "T0"
            bucket = by_tier.setdefault(tier, {"tp": 0, "fp": 0, "tn": 0, "fn": 0})
            if asserts_flood and actual:
                tp += 1
                bucket["tp"] += 1
            elif asserts_flood and not actual:
                fp += 1
                bucket["fp"] += 1
            elif not asserts_flood and actual:
                fn += 1
                bucket["fn"] += 1
            else:
                tn += 1
                bucket["tn"] += 1

    return {
        "admitted": _scores({"tp": tp, "fp": fp, "tn": tn, "fn": fn}),
        "by_tier": {tier: _scores(counts) for tier, counts in sorted(by_tier.items())},
        "claims_admitted": sum(int(t.get("n_claims") or 0) for t in ticks),
        "claims_rejected": sum(int(t.get("n_rejected_claims") or 0) for t in ticks),
    }


# --------------------------------------------------------------------------
# RQ2 — timeliness and burden
# --------------------------------------------------------------------------


def _first_true_time(ticks: Sequence[Dict[str, Any]], key) -> Optional[str]:
    for tick in ticks:
        if key(tick):
            return tick["t"]
    return None


def _interval_hours(tick: Dict[str, Any]) -> float:
    try:
        return max(0.0, float(tick.get("evaluation_interval_hours") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _lag_hours(later: Optional[str], earlier: Optional[Any]) -> Optional[float]:
    if not later or earlier is None:
        return None
    later_dt = parse_iso(later)
    earlier_dt = earlier if hasattr(earlier, "tzinfo") else parse_iso(earlier)
    return round((later_dt - earlier_dt).total_seconds() / 3600.0, 4)


def _episode_flood_stage_acquisition(
    tick: Dict[str, Any], *, episode_onset: datetime
) -> Optional[str]:
    """Return qualifying acquisition provenance for the current episode.

    A carried ``flood_stage_observed=True`` state from before the episode is not
    a new observation of this episode.  The acquisition must be timestamped at
    or after onset and no later than the grid point carrying it.
    """
    if not bool(tick.get("flood_stage_observed")):
        return None
    observed_at = tick.get("policy_observed_at")
    if not observed_at:
        return None
    try:
        observed_dt = parse_iso(str(observed_at))
        grid_dt = parse_iso(str(tick["t"]))
    except (KeyError, TypeError, ValueError):
        return None
    if episode_onset <= observed_dt <= grid_dt:
        return _utc_iso(observed_dt)
    return None


def rq2_flood_episodes(
    ticks: Sequence[Dict[str, Any]],
    *,
    acquisition_ticks: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Event-level onset and missed-duration metrics on the common grid.

    Unknown truth intervals are never treated as dry. Positive segments are
    merged unless at least one known-false interval separates them; internal or
    adjacent unknown time censors the episode and is excluded from positive and
    missed-duration denominators. Thus sensor dropout cannot manufacture a new
    episode or a recovery.
    """
    episodes: List[Dict[str, Any]] = []
    index = 0
    while index < len(ticks):
        if (
            ticks[index].get("evaluation_boundary")
            or ticks[index].get("truth_hydrologic_flooding") is not True
        ):
            index += 1
            continue
        start_index = index
        last_positive_index = index
        cursor = index + 1
        while cursor < len(ticks):
            candidate = ticks[cursor]
            truth = candidate.get("truth_hydrologic_flooding")
            if candidate.get("evaluation_boundary"):
                break
            if truth is False:
                break
            if truth is True:
                last_positive_index = cursor
            cursor += 1

        members = list(ticks[start_index : last_positive_index + 1])
        # The episode's censored span continues through internal/trailing
        # unknown time until a known-clear tick or the zero-duration boundary.
        # Acquisitions in that span can observe the episode even if a later
        # acquisition clears the carried state before another positive grid
        # point appears.
        episode_span_members = list(ticks[start_index:cursor])
        positive_members = [
            tick
            for tick in members
            if tick.get("truth_hydrologic_flooding") is True
        ]
        internal_unknown = [
            tick
            for tick in members
            if tick.get("truth_hydrologic_flooding") is None
        ]
        trailing_unknown = [
            tick
            for tick in ticks[last_positive_index + 1 : cursor]
            if tick.get("truth_hydrologic_flooding") is None
        ]
        # Cadence effectiveness is about when an acquisition first contains a
        # flood-stage-or-higher gauge reading.  ``is_flooding`` is deliberately
        # not used here: the live policy turns that flag on at action stage and
        # can therefore precede the reference episode by design.
        first_flood_stage_observation = None
        first_flood_stage_observation_grid_time = None
        episode_onset = parse_iso(members[0]["t"])
        right_boundary = ticks[cursor] if cursor < len(ticks) else None
        episode_span_end = (
            parse_iso(right_boundary["t"])
            if right_boundary is not None
            else None
        )
        if acquisition_ticks is not None:
            candidates = []
            for acquisition_tick in acquisition_ticks:
                if not bool(acquisition_tick.get("flood_stage_observed")):
                    continue
                acquisition_dt = parse_iso(str(acquisition_tick["t"]))
                if acquisition_dt < episode_onset:
                    continue
                if episode_span_end is not None and acquisition_dt >= episode_span_end:
                    continue
                candidates.append(acquisition_dt)
            if candidates:
                first_flood_stage_observation = _utc_iso(min(candidates))
                first_flood_stage_observation_grid_time = next(
                    (
                        member["t"]
                        for member in episode_span_members
                        if _episode_flood_stage_acquisition(
                            member, episode_onset=episode_onset
                        )
                        == first_flood_stage_observation
                    ),
                    None,
                )
        else:
            # Backward-compatible fallback for callers that supply only the
            # common grid. Production study evaluation passes native
            # acquisition ticks so brief off-grid observations cannot vanish.
            for member in episode_span_members:
                acquisition = _episode_flood_stage_acquisition(
                    member, episode_onset=episode_onset
                )
                if acquisition is not None and first_flood_stage_observation is None:
                    first_flood_stage_observation = acquisition
                    first_flood_stage_observation_grid_time = member["t"]
        qualifying_acquisitions = [
            _episode_flood_stage_acquisition(
                member, episode_onset=episode_onset
            )
            for member in positive_members
        ]
        first_escalation = _first_true_time(
            positive_members, lambda t: t.get("phase_after") in ESCALATED_PHASES
        )
        duration = sum(_interval_hours(t) for t in positive_members)
        unobserved_flood_stage = sum(
            _interval_hours(t)
            for t, acquisition in zip(positive_members, qualifying_acquisitions)
            if acquisition is None
        )
        missed_escalation = sum(
            _interval_hours(t)
            for t in positive_members
            if t.get("phase_after") not in ESCALATED_PHASES
        )
        end_dt = parse_iso(positive_members[-1]["t"]) + timedelta(
            hours=_interval_hours(positive_members[-1])
        )
        left_truth = (
            ticks[start_index - 1].get("truth_hydrologic_flooding")
            if start_index > 0
            else None
        )
        right_truth = (
            right_boundary.get("truth_hydrologic_flooding")
            if right_boundary is not None
            else None
        )
        episodes.append(
            {
                "episode": len(episodes) + 1,
                "observed_start": members[0]["t"],
                "last_observed_positive_end_exclusive": _utc_iso(end_dt),
                "duration_hours": round(duration, 4),
                "left_censored": start_index == 0 or left_truth is None,
                "right_censored": bool(trailing_unknown) or right_truth is not False,
                "interval_censored": bool(internal_unknown or trailing_unknown),
                "internal_unknown_duration_hours": round(
                    sum(_interval_hours(t) for t in internal_unknown), 4
                ),
                "trailing_unknown_duration_hours": round(
                    sum(_interval_hours(t) for t in trailing_unknown), 4
                ),
                "schedule_observed_episode": (
                    first_flood_stage_observation is not None
                ),
                "first_schedule_flood_stage_observation": (
                    first_flood_stage_observation
                ),
                "first_schedule_flood_stage_observation_grid_time": (
                    first_flood_stage_observation_grid_time
                ),
                "observation_requires_acquisition_at_or_after_episode_onset": True,
                "schedule_flood_stage_observation_lag_hours": _lag_hours(
                    first_flood_stage_observation, members[0]["t"]
                ),
                "positive_duration_before_first_schedule_observation_hours": round(
                    sum(
                        (
                            _interval_hours(tick)
                            if first_flood_stage_observation is None
                            else max(
                                0.0,
                                min(
                                    _interval_hours(tick),
                                    (
                                        parse_iso(first_flood_stage_observation)
                                        - parse_iso(tick["t"])
                                    ).total_seconds()
                                    / 3600.0,
                                ),
                            )
                        )
                        for tick in positive_members
                    ),
                    4,
                ),
                "escalated": first_escalation is not None,
                "first_escalation": first_escalation,
                "escalation_lag_hours": _lag_hours(first_escalation, members[0]["t"]),
                "unobserved_flood_stage_duration_hours": round(
                    unobserved_flood_stage, 4
                ),
                "missed_escalation_duration_hours": round(missed_escalation, 4),
            }
        )
        index = cursor + 1 if right_truth is False else max(cursor, index + 1)

    total_flood = sum(e["duration_hours"] for e in episodes)
    unobserved_flood_stage = sum(
        e["unobserved_flood_stage_duration_hours"] for e in episodes
    )
    missed_escalation = sum(e["missed_escalation_duration_hours"] for e in episodes)
    unknown_duration = sum(
        _interval_hours(t)
        for t in ticks
        if t.get("truth_hydrologic_flooding") is None
    )
    known_duration = sum(
        _interval_hours(t)
        for t in ticks
        if t.get("truth_hydrologic_flooding") is not None
    )
    detected = sum(1 for e in episodes if e["schedule_observed_episode"])
    escalated = sum(1 for e in episodes if e["escalated"])
    return {
        "schedule_observation_semantics": (
            "first acquisition at or after the current episode onset containing "
            "a gauge at flood/minor stage or higher; action-stage policy state and "
            "flood-stage state carried from a prior episode do not count. The "
            "native acquisition log is searched through internal/trailing unknown "
            "censoring until (but not including) known clear or window end"
        ),
        "n_observed_flood_episodes": len(episodes),
        "n_fully_observed_episodes": sum(
            1
            for e in episodes
            if not e["left_censored"]
            and not e["right_censored"]
            and not e["interval_censored"]
        ),
        "episodes_observed_by_schedule": detected,
        "episodes_not_observed_by_schedule": len(episodes) - detected,
        "episode_observation_rate": _rate(detected, len(episodes)),
        "episodes_escalated": escalated,
        "episode_escalation_recall": _rate(escalated, len(episodes)),
        "known_truth_duration_hours": round(known_duration, 4),
        "known_clear_duration_hours": round(known_duration - total_flood, 4),
        "known_clear_share_of_known_duration": (
            round((known_duration - total_flood) / known_duration, 4)
            if known_duration > 0
            else None
        ),
        "unknown_truth_duration_hours": round(unknown_duration, 4),
        "truth_duration_coverage": (
            round(known_duration / (known_duration + unknown_duration), 4)
            if known_duration + unknown_duration > 0
            else None
        ),
        "observed_flood_duration_hours": round(total_flood, 4),
        "unobserved_flood_stage_duration_hours": round(
            unobserved_flood_stage, 4
        ),
        "unobserved_duration_semantics": (
            "truth-positive duration on the common evaluation grid whose carried "
            "current policy acquisition lacks a flood-stage-or-higher gauge; "
            "brief off-grid observations still count for episode observation"
        ),
        "unobserved_flood_stage_duration_rate": (
            round(unobserved_flood_stage / total_flood, 4)
            if total_flood > 0
            else None
        ),
        "missed_escalation_duration_hours": round(missed_escalation, 4),
        "missed_escalation_duration_rate": (
            round(missed_escalation / total_flood, 4) if total_flood > 0 else None
        ),
        "episodes": episodes,
    }


def rq2_timeliness(
    ticks: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    *,
    acquisition_ticks: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    scored_ticks = [t for t in ticks if not t.get("evaluation_boundary")]
    first_truth = _first_true_time(
        scored_ticks, lambda t: t.get("truth_hydrologic_flooding") is True
    )
    first_grid_escalation = _first_true_time(
        scored_ticks, lambda t: t.get("phase_after") in ESCALATED_PHASES
    )
    first_grid_policy_positive = _first_true_time(
        scored_ticks, lambda t: t.get("is_flooding")
    )
    first_grid_flood_stage_observation = _first_true_time(
        scored_ticks, lambda t: t.get("flood_stage_observed")
    )
    acquisitions = list(acquisition_ticks if acquisition_ticks is not None else ticks)
    first_escalation = _first_true_time(
        acquisitions, lambda t: t.get("phase_after") in ESCALATED_PHASES
    )
    first_policy_positive = _first_true_time(
        acquisitions, lambda t: t.get("is_flooding")
    )
    first_flood_stage_observation = _first_true_time(
        acquisitions, lambda t: t.get("flood_stage_observed")
    )
    official = first_flood_warning_time(events)

    # Common-grid points where known truth says flooding but the system had not
    # escalated. Duration-based forms live in rq2_flood_episodes.
    missed = sum(
        1
        for t in scored_ticks
        if t.get("truth_hydrologic_flooding") is True
        and t.get("phase_after") not in ESCALATED_PHASES
    )
    truth_positive = sum(
        1 for t in scored_ticks if t.get("truth_hydrologic_flooding") is True
    )

    material = sum(
        1
        for acquisition in acquisitions
        if acquisition.get(
            "material_acquisition", acquisition.get("material_change", False)
        )
    )
    phase_duration_hours: Dict[str, float] = {}
    for tick in scored_ticks:
        phase = str(tick.get("phase_after") or "unknown")
        phase_duration_hours[phase] = phase_duration_hours.get(phase, 0.0) + _interval_hours(tick)
    return {
        "policy_positive_semantics": (
            "system is_flooding begins at action stage; lead/lag against flood-stage "
            "onset may be negative by design and is not detector accuracy"
        ),
        "window_first_timing_semantics": (
            "system timestamps are actual acquisition times; reference onset is on "
            "the common grid. Window-first lags pair the first qualifying acquisition "
            "with the first reference-positive time and can span a missed first "
            "episode; use rq2_event_episodes for episode-level timing"
        ),
        "evaluation_grid_points": len(scored_ticks),
        "evaluation_boundary_points": len(ticks) - len(scored_ticks),
        "truth_known_points": sum(
            1
            for t in scored_ticks
            if t.get("truth_hydrologic_flooding") is not None
        ),
        "truth_unknown_points": sum(
            1
            for t in scored_ticks
            if t.get("truth_hydrologic_flooding") is None
        ),
        "acquisitions": len(acquisitions),
        "current_runtime_logical_report_delivery_opportunities": len(acquisitions),
        "report_delivery_opportunity_semantics": (
            "one logical coordinator report is generated per due acquisition; "
            "Slack rendering may split one report into multiple transport messages, "
            "and replay measures neither attempted nor successful delivery"
        ),
        "material_acquisitions": material,
        "hypothetical_logical_report_delivery_opportunities_if_materiality_gated": (
            material
        ),
        "hypothetical_materiality_gate_suppression_rate": _rate(
            len(acquisitions) - material, len(acquisitions)
        ),
        "nonmaterial_acquisitions": len(acquisitions) - material,
        # Names used by superseded artifacts are retained as a schema map only.
        # They are not emitted as numeric fields because neither replay nor a
        # coordinator cycle measures Slack transport-message count or delivery.
        "legacy_metric_aliases": {
            "current_runtime_messages": (
                "current_runtime_logical_report_delivery_opportunities"
            ),
            "messages_if_report_every_acquisition": (
                "current_runtime_logical_report_delivery_opportunities"
            ),
            "hypothetical_messages_if_materiality_gated": (
                "hypothetical_logical_report_delivery_opportunities_if_materiality_gated"
            ),
            "messages_if_materiality_gated": (
                "hypothetical_logical_report_delivery_opportunities_if_materiality_gated"
            ),
            "materiality_gate_suppression_rate": (
                "hypothetical_materiality_gate_suppression_rate"
            ),
        },
        "first_hydrologic_flooding": first_truth,
        "first_policy_positive": first_policy_positive,
        "first_schedule_flood_stage_observation": first_flood_stage_observation,
        "first_system_escalation": first_escalation,
        "first_grid_state_with_policy_positive": first_grid_policy_positive,
        "first_grid_state_with_flood_stage_observation": (
            first_grid_flood_stage_observation
        ),
        "first_grid_state_in_escalated_phase": first_grid_escalation,
        "first_official_warning": official.isoformat().replace("+00:00", "Z")
        if official
        else None,
        "policy_positive_lead_lag_hours_vs_flood_stage": _lag_hours(
            first_policy_positive, first_truth
        ),
        "schedule_flood_stage_observation_lag_hours_vs_reference": _lag_hours(
            first_flood_stage_observation, first_truth
        ),
        "escalation_lag_hours_vs_hydrologic": _lag_hours(first_escalation, first_truth),
        "escalation_lag_hours_vs_official": _lag_hours(first_escalation, official),
        "grid_state_policy_positive_lag_hours_vs_flood_stage": _lag_hours(
            first_grid_policy_positive, first_truth
        ),
        "grid_state_flood_stage_observation_lag_hours_vs_reference": _lag_hours(
            first_grid_flood_stage_observation, first_truth
        ),
        "grid_state_escalation_lag_hours_vs_hydrologic": _lag_hours(
            first_grid_escalation, first_truth
        ),
        "phase_changes": sum(1 for t in acquisitions if t.get("phase_changed")),
        "phase_duration_hours": {
            phase: round(hours, 4)
            for phase, hours in sorted(phase_duration_hours.items())
        },
        "folded_at": _first_true_time(acquisitions, lambda t: bool(t.get("folded"))),
        "missed_escalation_ticks": missed,
        "missed_escalation_rate": _rate(missed, truth_positive),
    }


# --------------------------------------------------------------------------
# RQ3 — calibration
# --------------------------------------------------------------------------


def _claim_outcomes(
    ticks: Sequence[Dict[str, Any]], corpus: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """(confidence, correct) for every claim that makes an assertion.

    A gauge claim is graded against the state of its own gauge, not the county,
    so a gauge correctly reading below flood stage while a different gauge
    floods is not counted wrong.

    Unscored observation claims are abstentions: they report a reading without
    asserting a flood state, so they are excluded rather than counted as
    denials.
    """
    site_ids = (
        {s["site_no"] for s in (corpus.get("sites") or []) if s.get("site_no")}
        if corpus
        else set()
    )
    outcomes = []
    for tick in ticks:
        if tick.get("truth_hydrologic_flooding") is None:
            continue
        county_flooding = bool(tick.get("truth_hydrologic_flooding"))
        flooding_sites = set(tick.get("truth_flooding_sites") or [])
        # Calibration is a property of the scores themselves, so it is measured
        # over every scored claim. Restricting to admitted claims would grade the
        # scores against a threshold derived from those same scores.
        for claim in tick.get("all_claims") or tick.get("claims") or []:
            confidence = claim.get("confidence")
            if confidence is None or claim.get("unscored"):
                continue
            asserts_flood = str(claim.get("severity") or "").lower() in _POSITIVE_SEVERITIES
            site = claim.get("site_no")
            actual = (
                site in flooding_sites
                if site and site in site_ids
                else county_flooding
            )
            outcomes.append(
                {
                    "confidence": float(confidence),
                    "correct": 1 if asserts_flood == actual else 0,
                    "tier": claim.get("tier") or "T0",
                    "claim_type": claim.get("claim_type"),
                }
            )
    return outcomes


def rq3_calibration(
    ticks: Sequence[Dict[str, Any]],
    *,
    n_bins: int = 10,
    corpus: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    outcomes = _claim_outcomes(ticks, corpus)
    if not outcomes:
        return {"n_claims": 0, "brier": None, "ece": None, "bins": []}

    brier = sum((o["confidence"] - o["correct"]) ** 2 for o in outcomes) / len(outcomes)

    bins: List[Dict[str, Any]] = []
    ece = 0.0
    for index in range(n_bins):
        low = index / n_bins
        high = (index + 1) / n_bins
        members = [
            o
            for o in outcomes
            if (low <= o["confidence"] < high)
            or (index == n_bins - 1 and o["confidence"] == 1.0)
        ]
        if not members:
            continue
        mean_conf = sum(o["confidence"] for o in members) / len(members)
        accuracy = sum(o["correct"] for o in members) / len(members)
        ece += (len(members) / len(outcomes)) * abs(accuracy - mean_conf)
        bins.append(
            {
                "range": [round(low, 2), round(high, 2)],
                "n": len(members),
                "mean_confidence": round(mean_conf, 4),
                "accuracy": round(accuracy, 4),
                "gap": round(accuracy - mean_conf, 4),
            }
        )

    # Discrimination: does higher confidence actually mean more often correct?
    # With a single confidence value the split into halves is arbitrary, so the
    # comparison is undefined rather than equal. Reporting a number there would
    # suggest the ablated system discriminates when it cannot.
    distinct = {round(o["confidence"], 4) for o in outcomes}
    ordered = sorted(outcomes, key=lambda o: o["confidence"])
    half = len(ordered) // 2
    if len(distinct) < 2 or not half or len(ordered) - half == 0:
        lower_acc = upper_acc = None
    else:
        lower_acc = sum(o["correct"] for o in ordered[:half]) / half
        upper_acc = sum(o["correct"] for o in ordered[half:]) / (len(ordered) - half)

    return {
        "n_claims": len(outcomes),
        "brier": round(brier, 4),
        "ece": round(ece, 4),
        "distinct_confidences": len({round(o["confidence"], 4) for o in outcomes}),
        "accuracy_low_confidence_half": round(lower_acc, 4) if lower_acc is not None else None,
        "accuracy_high_confidence_half": round(upper_acc, 4) if upper_acc is not None else None,
        "bins": bins,
    }


# --------------------------------------------------------------------------
# RQ4 — honest abstention
# --------------------------------------------------------------------------


def rq4_abstention(ticks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How the system behaves when it cannot see enough to judge.

    A tick counts as low-evidence when no gauge could be scored. The question is
    whether the system abstains (insufficient_data) or issues a confident
    all-clear (no_flooding) it has not earned.
    """
    scored_ticks = [t for t in ticks if not t.get("evaluation_boundary")]
    low_evidence = [
        t for t in scored_ticks if int(t.get("gauges_scored") or 0) == 0
    ]
    abstained = [t for t in low_evidence if t.get("regional_verdict") == "insufficient_data"]
    false_all_clear = [
        t
        for t in low_evidence
        if t.get("regional_verdict") == "no_flooding"
    ]
    # The dangerous case: claimed all-clear while the river was actually flooding.
    unsafe = [
        t for t in false_all_clear if t.get("truth_hydrologic_flooding") is True
    ]
    ungradable_all_clear = [
        t for t in false_all_clear if t.get("truth_hydrologic_flooding") is None
    ]

    return {
        "n_ticks": len(scored_ticks),
        "low_evidence_ticks": len(low_evidence),
        "abstained": len(abstained),
        "abstention_rate": _rate(len(abstained), len(low_evidence)),
        "all_clear_without_scored_gauges": len(false_all_clear),
        "unsafe_all_clear_ticks": len(unsafe),
        "ungradable_all_clear_ticks": len(ungradable_all_clear),
        "truth_unknown_ticks": sum(
            1 for t in scored_ticks if t.get("truth_hydrologic_flooding") is None
        ),
        "mean_gauges_scored": round(
            sum(int(t.get("gauges_scored") or 0) for t in scored_ticks)
            / len(scored_ticks),
            2,
        )
        if scored_ticks
        else None,
        "mean_gauges_with_data": round(
            sum(int(t.get("gauges_with_data") or 0) for t in scored_ticks)
            / len(scored_ticks),
            2,
        )
        if scored_ticks
        else None,
    }


def rq4_source_availability(
    acquisition_ticks: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate missing and stale source states at actual acquisitions.

    Counts use acquisition--source opportunities, not elapsed-time intervals;
    slower policies therefore have fewer opportunities by design. Both raw
    counts and within-opportunity rates are retained.
    """

    groups = ("all_selected_sites", "threshold_compatible_sites")
    categories = (
        "no_prior_observation",
        "latest_observation_missing_stage",
        "stale_stage",
        "fresh_stage",
    )
    result: Dict[str, Any] = {
        "unit": "acquisition_source_opportunity",
        "n_acquisitions": len(acquisition_ticks),
        "interpretation": (
            "missing and stale counts are evaluated only when a policy acquires; "
            "they are not continuous elapsed-time exposure"
        ),
    }
    for group in groups:
        totals = {category: 0 for category in categories}
        source_opportunities = 0
        acquisitions_with_any = {category: 0 for category in categories}
        for tick in acquisition_ticks:
            values = ((tick.get("source_availability") or {}).get(group) or {})
            source_opportunities += int(values.get("sources_total") or 0)
            for category in categories:
                count = int(values.get(category) or 0)
                totals[category] += count
                acquisitions_with_any[category] += int(count > 0)
        result[group] = {
            "source_opportunities": source_opportunities,
            **totals,
            "missing_stage_total": (
                totals["no_prior_observation"]
                + totals["latest_observation_missing_stage"]
            ),
            "fresh_stage_rate": (
                round(totals["fresh_stage"] / source_opportunities, 4)
                if source_opportunities
                else None
            ),
            "stale_stage_rate": (
                round(totals["stale_stage"] / source_opportunities, 4)
                if source_opportunities
                else None
            ),
            "missing_stage_rate": (
                round(
                    (
                        totals["no_prior_observation"]
                        + totals["latest_observation_missing_stage"]
                    )
                    / source_opportunities,
                    4,
                )
                if source_opportunities
                else None
            ),
            "acquisitions_with_any": acquisitions_with_any,
        }
    return result


# --------------------------------------------------------------------------


def evaluate(
    run: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    evaluation_interval_seconds: int = DEFAULT_EVALUATION_INTERVAL_SECONDS,
    evaluation_offset_seconds: int = 0,
    truth_max_age_hours: Optional[float] = DEFAULT_TRUTH_MAX_AGE_HOURS,
) -> Dict[str, Any]:
    """All four research questions for one run under a fixed protocol."""
    excluded_ids = [
        row.get("site_no")
        for row in (run.get("threshold_exclusions") or [])
        if row.get("site_no")
    ]
    corpus, threshold_exclusions = apply_threshold_compatibility(
        corpus, excluded_site_ids=excluded_ids
    )
    acquisition_ticks = annotate_run(
        run,
        corpus=corpus,
        events=events,
        truth_max_age_hours=truth_max_age_hours,
    )
    grid_ticks = annotate_run_on_grid(
        run,
        corpus=corpus,
        events=events,
        interval_seconds=evaluation_interval_seconds,
        offset_seconds=evaluation_offset_seconds,
        truth_max_age_hours=truth_max_age_hours,
    )
    episodes = rq2_flood_episodes(
        grid_ticks, acquisition_ticks=acquisition_ticks
    )
    truth_reference = run.get("truth_reference")
    nws_input = run.get("nws_system_input")
    return {
        "schema": "sentinel-evaluation/4",
        "ablation": run.get("ablation"),
        "county": run.get("county"),
        "state": run.get("state"),
        "window": run.get("window"),
        "protocol": {
            "analysis_interval_convention": "[start,end)",
            "common_grid_boundary": "end retained only as zero-duration state boundary",
            "evaluation_interval_seconds": evaluation_interval_seconds,
            "evaluation_offset_seconds": evaluation_offset_seconds,
            "policy_alignment": "last_acquisition_carried_forward",
            "truth_max_age_hours": truth_max_age_hours,
            "truth_max_age_sensitivity_hours": list(
                TRUTH_MAX_AGE_SENSITIVITY_HOURS
            ),
            "unknown_truth_policy": "excluded_and_censored",
            "threshold_exclusions": threshold_exclusions,
            "nws_truth_reference": truth_reference,
            "nws_system_input": nws_input,
            "nws_reference_independent_of_system_input": not bool(
                run.get("nws_reference_is_system_input")
            ),
        },
        "n_acquisitions": len(acquisition_ticks),
        "n_evaluation_points": sum(
            1 for tick in grid_ticks if not tick.get("evaluation_boundary")
        ),
        "n_evaluation_boundary_points": sum(
            1 for tick in grid_ticks if tick.get("evaluation_boundary")
        ),
        "rq1_policy_state_agreement": rq1_policy_state_agreement(grid_ticks),
        "rq1_evidence_quality": rq1_evidence_quality(acquisition_ticks, corpus),
        "rq2_timeliness": rq2_timeliness(
            grid_ticks, events, acquisition_ticks=acquisition_ticks
        ),
        "rq2_event_episodes": episodes,
        "rq3_calibration": rq3_calibration(acquisition_ticks, corpus=corpus),
        "rq4_abstention": rq4_abstention(grid_ticks),
        "rq4_source_availability": rq4_source_availability(acquisition_ticks),
    }


def evaluate_truth_age_sensitivity(
    run: Dict[str, Any],
    *,
    corpus: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    horizons_hours: Sequence[float] = TRUTH_MAX_AGE_SENSITIVITY_HOURS,
    evaluation_interval_seconds: int = DEFAULT_EVALUATION_INTERVAL_SECONDS,
    evaluation_offset_seconds: int = 0,
) -> List[Dict[str, Any]]:
    """Re-score outcome metrics under prespecified oracle freshness horizons."""
    excluded_ids = [
        row.get("site_no")
        for row in (run.get("threshold_exclusions") or [])
        if row.get("site_no")
    ]
    corpus, _ = apply_threshold_compatibility(
        corpus, excluded_site_ids=excluded_ids
    )
    rows: List[Dict[str, Any]] = []
    for horizon in horizons_hours:
        acquisition_ticks = annotate_run(
            run,
            corpus=corpus,
            events=events,
            truth_max_age_hours=float(horizon),
        )
        ticks = annotate_run_on_grid(
            run,
            corpus=corpus,
            events=events,
            interval_seconds=evaluation_interval_seconds,
            offset_seconds=evaluation_offset_seconds,
            truth_max_age_hours=float(horizon),
        )
        rows.append(
            {
                "truth_max_age_hours": float(horizon),
                "rq1_policy_state_agreement": rq1_policy_state_agreement(ticks),
                "rq2_event_episodes": rq2_flood_episodes(
                    ticks, acquisition_ticks=acquisition_ticks
                ),
                "rq4_abstention": rq4_abstention(ticks),
            }
        )
    return rows
