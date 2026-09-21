"""Run a replayed event through the decision pipeline under one ablation.

Steps a virtual clock across the event window. At each tick the system sees
only what it could have seen then: gauge readings up to that moment, and news
articles inside the live recency window. Every decision (claim extraction,
confidence, phase transition) runs through the real code, so switching
SENTINEL_ABLATION changes the system rather than the harness.

Cadence is normally part of the role layer.  Evaluation may also override only
the acquisition schedule while leaving phase reasoning intact.  Those fixed
schedule runs are the primary cadence baselines; ``no_role`` remains a role
ablation and is not used as a proxy for a fixed-cadence scheduler.

Neo4j and Slack are not involved: the ontology's effect lives in the decision
path, not in persistence or delivery.

Usage::

    python3 eval/run_replay.py --ablation full --county Harris --state TX \\
        --corpus eval/corpus/harris_harvey_2017.json \\
        --start 2017-08-25T00:00:00Z --end 2017-09-01T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: Default fixed cadence for configurations with the role layer disabled.
FIXED_INTERVAL_SECONDS = 3600

#: Phase used for the whole event when the role layer is ablated.
STATIC_PHASE = "early_watch"


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _portable_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    resolved = Path(value).resolve()
    try:
        return str(resolved.relative_to(_ROOT))
    except ValueError:
        return str(resolved)


def run(
    *,
    corpus_path: str,
    county: str,
    state: str,
    fips: str,
    start: str,
    end: str,
    news_corpus_path: Optional[str] = None,
    nws_input_path: Optional[str] = None,
    truth_path: Optional[str] = None,
    max_staleness_hours: Optional[float] = 6.0,
    fixed_interval_seconds: Optional[int] = None,
    schedule_offset_seconds: int = 0,
    threshold_excluded_site_ids: Sequence[str] = (),
    recovery_min_clear_hours: Optional[float] = None,
    trigger: Optional[Dict[str, Any]] = None,
    trigger_corpus_path: Optional[str] = None,
    trigger_poll_interval_seconds: int = 900,
) -> Dict[str, Any]:
    from watcher.claims.extract_articles import extract_article_claims
    from watcher.claims.extract_nws import extract_nws_claims_from_alert_dicts
    from watcher.claims.extract_county_flood import extract_county_flood_claims
    from watcher.config import (
        county_flood_min_gauges,
        role_layer_enabled,
        sentinel_ablation,
    )
    from watcher.claims.validate import filter_claims_by_phase
    from watcher.report_delta import build_report_snapshot, compute_delta
    from watcher.ontology.bridge import get_monitoring_summary
    from watcher.phase_engine import (
        RECOVERY_MIN_CLEAR_HOURS_TO_FOLD,
        apply_recovery_fold_streak,
        evaluate_phase_transition,
    )
    from watcher.replay import (
        load_corpus,
        observation_age_hours,
        observation_as_of,
        parse_iso,
        payload_at,
        snapshot_at,
    )

    ablation = sentinel_ablation()
    corpus = load_corpus(corpus_path)
    from eval.ground_truth import apply_threshold_compatibility

    corpus, threshold_exclusions = apply_threshold_compatibility(
        corpus, excluded_site_ids=threshold_excluded_site_ids
    )

    def stage_source_availability_at(when: datetime) -> Dict[str, Dict[str, int]]:
        """Separate absent/missing-stage readings from stale stage readings."""

        def summarize(sites: Sequence[Dict[str, Any]]) -> Dict[str, int]:
            counts = {
                "sources_total": len(sites),
                "no_prior_observation": 0,
                "latest_observation_missing_stage": 0,
                "stale_stage": 0,
                "fresh_stage": 0,
            }
            for site in sites:
                observation = observation_as_of(site, when)
                if observation is None:
                    counts["no_prior_observation"] += 1
                elif observation.get("stage") is None:
                    counts["latest_observation_missing_stage"] += 1
                elif (
                    max_staleness_hours is not None
                    and observation_age_hours(observation, when)
                    > max_staleness_hours
                ):
                    counts["stale_stage"] += 1
                else:
                    counts["fresh_stage"] += 1
            return counts

        sites = list(corpus.get("sites") or [])
        return {
            "all_selected_sites": summarize(sites),
            "threshold_compatible_sites": summarize(
                [site for site in sites if site.get("flood_stage") is not None]
            ),
        }

    # The feed runs candidates through the detector's own entry point, so an
    # article admitted in replay is one the live system would have admitted.
    news = None
    if news_corpus_path and Path(news_corpus_path).is_file():
        from eval.news_replay import load_feed

        news = load_feed(news_corpus_path, county=county, state=state)

    if fixed_interval_seconds is not None and fixed_interval_seconds < 60:
        raise ValueError("fixed_interval_seconds must be at least 60")
    if schedule_offset_seconds < 0:
        raise ValueError("schedule_offset_seconds must be non-negative")
    if recovery_min_clear_hours is not None and recovery_min_clear_hours < 0:
        raise ValueError("recovery_min_clear_hours must be non-negative")
    if (
        fixed_interval_seconds is not None
        and schedule_offset_seconds >= fixed_interval_seconds
    ):
        raise ValueError("schedule_offset_seconds must be smaller than the fixed interval")

    # Verification-trigger (hybrid) scheduling. The trigger's only authority is
    # scheduling: it consults a frozen official warning stream on the same
    # 15-minute poll grid the production cron uses, and may request an
    # immediate verification acquisition plus a bounded temporary cadence.
    # Nothing from the trigger stream enters claim extraction or the phase
    # engine here; official gauge (and, when explicitly injected, NWS alert)
    # claims remain the only decision evidence.
    trigger_policy = None
    trigger_events: List[Dict[str, Any]] = []
    if trigger is not None:
        from watcher.verification_trigger import TriggerPolicy

        trigger_base_schedule = str(trigger.get("base_schedule") or "phase").lower()
        if trigger_base_schedule not in ("phase", "fixed"):
            raise ValueError(f"unknown trigger base_schedule: {trigger_base_schedule!r}")
        if fixed_interval_seconds is not None and trigger_base_schedule != "fixed":
            raise ValueError(
                "the verification trigger applies to the phase-adaptive schedule "
                "only; it cannot be combined with a fixed-interval override "
                "unless trigger['base_schedule'] == 'fixed' (phase-free control)"
            )
        if trigger_base_schedule == "fixed" and fixed_interval_seconds is None:
            raise ValueError("trigger base_schedule 'fixed' requires fixed_interval_seconds")
        if not role_layer_enabled():
            raise ValueError(
                "the verification trigger requires role-layer phase reasoning"
            )
        if trigger_poll_interval_seconds < 60:
            raise ValueError("trigger_poll_interval_seconds must be at least 60")
        qualifying = tuple(
            tuple(str(part).strip().upper() for part in str(token).split(".", 1))
            for token in (trigger.get("qualifying") or ("FF.W", "FA.W", "FL.W"))
        )
        if any(len(pair) != 2 or not pair[0] or not pair[1] for pair in qualifying):
            raise ValueError("trigger qualifying tokens must be PH.S pairs")
        trigger_policy = TriggerPolicy(
            enabled=bool(trigger.get("enabled", True)),
            qualifying=qualifying,
            burst_interval_seconds=int(
                trigger.get("burst_interval_seconds") or 3600
            ),
            burst_duration_seconds=int(
                trigger.get("burst_duration_seconds") or 21600
            ),
        )
        # Component-ablation switches (evaluation only): each may only remove
        # a way the trigger shortens the schedule, never add one.
        trigger_immediate_enabled = bool(trigger.get("immediate_fire_enabled", True))
        trigger_burst_cap_enabled = bool(trigger.get("burst_cap_enabled", True))
        # Scheduling anchor semantics. "reanchor" (v3): every acquisition
        # resets the cadence clock. "additive" (v4): the base phase clock is
        # credited only by base-due acquisitions, so verification acquisitions
        # add information without moving the baseline schedule.
        trigger_anchor_mode = str(trigger.get("anchor_mode") or "reanchor").lower()
        if trigger_anchor_mode not in ("reanchor", "additive"):
            raise ValueError(f"unknown trigger anchor_mode: {trigger_anchor_mode!r}")
        if trigger_policy.enabled:
            if not trigger_corpus_path:
                raise ValueError(
                    "an enabled verification trigger requires a frozen trigger corpus"
                )
            from eval.vtec_trigger import load_trigger_corpus, trigger_events_sorted

            trigger_events = trigger_events_sorted(
                load_trigger_corpus(trigger_corpus_path)
            )

    # Official NWS events have two deliberately separate roles. ``truth_path``
    # is only a held-out timing reference recorded in the run manifest.  Alerts
    # enter the system only when the caller explicitly supplies
    # ``nws_input_path``.  Pointing both arguments at the same archive is
    # allowed, but the resulting reference is then not an independent input.
    vtec_events: List[Dict[str, Any]] = []
    if nws_input_path:
        if not Path(nws_input_path).is_file():
            raise FileNotFoundError(f"NWS system-input archive not found: {nws_input_path}")
        from eval.ground_truth import load as truth_load

        vtec_events = (truth_load(nws_input_path) or {}).get("events") or []

    phase = STATIC_PHASE
    recovery_all_clear_streak = 0
    recovery_clear_since: Optional[str] = None
    prior_gauges: Optional[List[Dict[str, Any]]] = None
    prior_report: Optional[Dict[str, Any]] = None
    ticks: List[Dict[str, Any]] = []

    def _acquire(now: datetime, audit: Optional[Dict[str, Any]] = None) -> bool:
        """Run one full acquisition at virtual time ``now``; True when folded.

        This body is the entire decision path — payload assembly, claim
        extraction, admission, phase transition, recovery fold, and report
        delta — shared verbatim by every schedule driver below, so a hybrid
        run differs from the adaptive baseline only in *when* it acquires.
        """
        nonlocal phase, recovery_all_clear_streak, recovery_clear_since
        nonlocal prior_gauges, prior_report

        active_alerts: List[Dict[str, Any]] = []
        if vtec_events:
            from eval.ground_truth import alerts_active_at

            active_alerts = alerts_active_at(
                vtec_events, now, county=county, state=state
            )

        payload = payload_at(
            corpus,
            now,
            prior_gauges=prior_gauges,
            alerts=active_alerts,
            max_staleness_hours=max_staleness_hours,
        )
        source_availability = stage_source_availability_at(now)
        snap = snapshot_at(
            corpus, now, max_staleness_hours=max_staleness_hours, payload=payload
        )
        # Advance the delta baseline only after the snapshot is built, so this
        # tick is compared against the previous one rather than against itself.
        prior_gauges = payload["gauges"]
        gauge_claims, _failure = extract_county_flood_claims(
            snap, county=county, state=state, location=f"{county} County, {state}"
        )

        alert_claims: List[Dict[str, Any]] = []
        if active_alerts:
            alert_snap = {
                "source_id": f"src_county_flood_nws_alerts_{fips or 'unknown'}",
                "snapshot_id": f"snap_alerts_{_utc_iso(now)}",
                "snapshot_url": "https://api.weather.gov/alerts/active",
                "fetched_at": _utc_iso(now),
                "raw_text": json.dumps(
                    {"schema": "county-flood-nws-alerts/1", "alerts": active_alerts}
                ),
            }
            alert_claims = extract_nws_claims_from_alert_dicts(
                alert_snap,
                county=county,
                state=state,
                location=f"{county} County, {state}",
                source_record={
                    "api_resolution": {"county_match": True},
                    "trust_tier": {"level": 0, "name": "T0"},
                },
            )

        article_claims: List[Dict[str, Any]] = []
        admitted_articles: List[Dict[str, Any]] = []
        if news is not None:
            from watcher.article_evidence_bridge import ARTICLE_CONFIDENCE_THRESHOLD

            evidence = news.score_as_of(
                now, min_page_score=ARTICLE_CONFIDENCE_THRESHOLD, top_n=5
            )
            admitted_articles = evidence["high_confidence_articles"]
            article_claims = extract_article_claims(
                {"high_confidence_articles": admitted_articles},
                county=county,
                state=state,
                location=f"{county} County, {state}",
                # Freshness is measured against the moment being replayed, not
                # the wall clock, or every 2017 article scores as years stale.
                now=_utc_iso(now),
            )

        summary = get_monitoring_summary("flood", phase, county_fips=fips)
        # The live pipeline validates claims against the phase's
        # minimum_confidence before they inform any decision. That gate is where
        # the belief layer bites. With the belief layer ablated, structural
        # validation remains but confidence floors and tier discounts are
        # bypassed, so the arbitrary uniform display score cannot gate evidence.
        claims, rejected = filter_claims_by_phase(
            gauge_claims + alert_claims + article_claims, summary
        )
        decision = evaluate_phase_transition(
            current_phase=phase,
            monitoring_summary=summary,
            evidence_claims=claims,
        )
        fold_result = apply_recovery_fold_streak(
            current_phase=phase,
            phase_decision=decision,
            evidence_claims=claims,
            prior_streak=recovery_all_clear_streak,
            evaluated_at=now,
            prior_clear_since=recovery_clear_since,
            minimum_clear_hours=(
                float(recovery_min_clear_hours)
                if recovery_min_clear_hours is not None
                else float(RECOVERY_MIN_CLEAR_HOURS_TO_FOLD)
            ),
        )
        decision = fold_result["phase_decision"]
        recovery_all_clear_streak = int(
            fold_result["recovery_all_clear_streak"]
        )
        recovery_clear_since = fold_result.get("recovery_clear_since")

        # Current runtime delivery posts on every due cycle, so messages equal
        # acquisitions.  We still compute whether an acquisition is material to
        # estimate a clearly labelled counterfactual materiality gate; nothing
        # in replay or the live runtime suppresses delivery on this basis.
        report = build_report_snapshot(
            mission_id=f"replay_{fips or county}",
            phase=decision["new_phase"],
            alert_level=decision["alert_level"],
            evidence_claims=claims,
            source_snapshots=[snap],
            phase_decision=decision,
            source_records=[],
        )
        delta = compute_delta(report, prior_report)
        prior_report = report
        material = bool(
            delta.get("first_run")
            or delta.get("phase_changed")
            or delta.get("alert_from") != delta.get("alert_to")
            or delta.get("gauge_changes")
            or delta.get("flood_signal_delta")
        )

        payload_summary = payload["summary"]
        flood_stage_observed_sites = sorted(
            {
                str(gauge.get("site_no"))
                for gauge in (payload.get("gauges") or [])
                if gauge.get("site_no")
                and gauge.get("flood_state_score") is not None
                and float(gauge["flood_state_score"]) >= 2.0
            }
        )
        ticks.append(
            {
                **(audit or {}),
                "t": _utc_iso(now),
                "phase_before": phase,
                "phase_after": decision["new_phase"],
                "phase_changed": decision["phase_changed"],
                "recovery_all_clear_streak": recovery_all_clear_streak,
                "recovery_clear_since": recovery_clear_since,
                "recovery_clear_elapsed_hours": fold_result.get(
                    "recovery_clear_elapsed_hours", 0.0
                ),
                "recovery_all_clear": bool(fold_result["all_clear"]),
                "folded": bool(fold_result["folded"]),
                "alert_level": decision["alert_level"],
                "regional_verdict": payload_summary["regional_verdict"],
                "is_flooding": payload_summary["is_flooding"],
                # Keep the action-stage policy flag above separate from the
                # acquisition outcome used by cadence evaluation.  A schedule
                # observes a hydrologic flood episode only when this acquisition
                # contains a gauge classified at minor/flood stage or higher.
                "flood_stage_observed": bool(flood_stage_observed_sites),
                "flood_stage_observed_sites": flood_stage_observed_sites,
                "max_flood_state": payload_summary["max_flood_state"],
                "gauges_with_data": payload_summary["gauges_with_data"],
                "gauges_scored": payload_summary["gauges_scored"],
                "source_availability": source_availability,
                "api_confidence": payload_summary["overall_confidence"],
                "claims": [
                    {
                        "claim_id": c.get("claim_id"),
                        "claim_type": c.get("claim_type"),
                        "severity": c.get("severity"),
                        "confidence": c.get("overall_confidence"),
                        "tier": (c.get("value") or {}).get("evidence_tier", "T0"),
                        "site_no": (c.get("value") or {}).get("site_no"),
                        "url": (c.get("value") or {}).get("url"),
                    }
                    for c in claims
                ],
                "n_claims": len(claims),
                "n_rejected_claims": len(rejected),
                "all_claims": [
                    {
                        "claim_type": c.get("claim_type"),
                        "severity": c.get("severity"),
                        "confidence": c.get("overall_confidence"),
                        "tier": (c.get("value") or {}).get("evidence_tier", "T0"),
                        "site_no": (c.get("value") or {}).get("site_no"),
                        "admitted": c.get("claim_id") in {a.get("claim_id") for a in claims},
                        "unscored": bool((c.get("value") or {}).get("supplementary")),
                    }
                    for c in (gauge_claims + alert_claims + article_claims)
                ],
                "n_article_claims": len(article_claims),
                "n_alert_claims": len(alert_claims),
                "material_acquisition": material,
                # Legacy replay field retained for consumers of schema v1.
                "material_change": material,
                "n_active_alerts": len(active_alerts),
                "articles_admitted": [
                    {
                        "url": a["url"],
                        "title": a["title"],
                        "page_score": a["page_score"],
                        "indication": a["indication"],
                    }
                    for a in admitted_articles
                ],
            }
        )

        phase = decision["new_phase"]
        return bool(fold_result["folded"])

    def _base_interval_seconds() -> int:
        # Phase-free control: the trigger's base cadence is a fixed clock, so the
        # phase model still runs (verdict, alert, recovery) but never sets timing.
        if trigger_policy is not None and trigger_base_schedule == "fixed":
            return int(fixed_interval_seconds)
        return _phase_interval_seconds()

    def _phase_interval_seconds() -> int:
        if role_layer_enabled():
            return int(
                get_monitoring_summary("flood", phase, county_fips=fips).get(
                    "heartbeat_interval_seconds"
                )
                or FIXED_INTERVAL_SECONDS
            )
        return FIXED_INTERVAL_SECONDS

    stop = parse_iso(end)
    trigger_accounting: Optional[Dict[str, Any]] = None

    if trigger_policy is None:
        # Legacy exact-step driver: identical to the pre-hybrid replay.
        # Analysis windows are half-open: an acquisition at exactly ``end`` has
        # no exposure time and must not inflate a fixed schedule by one cycle.
        now = parse_iso(start) + timedelta(seconds=schedule_offset_seconds)
        while now < stop:
            folded = _acquire(now)
            # Production deactivates a mission immediately after the recovery
            # streak folds it.  The replay therefore emits the fold decision
            # once and stops acquiring; metric code carries that terminal
            # policy state to the end of the exogenous evaluation grid.
            if folded:
                break
            if fixed_interval_seconds is not None:
                interval = fixed_interval_seconds
            else:
                interval = _phase_interval_seconds()
            now = now + timedelta(seconds=max(interval, 60))
    else:
        # Poll-grid driver: walk the same 15-minute heartbeat grid the
        # production cron uses. Each poll consults the frozen warning stream
        # exactly once (one counted trigger-stream consultation), then applies
        # the shared due-ness rule. With the trigger disabled, or with no
        # qualifying warning, due-ness reduces to the phase cadence and the
        # realised acquisitions match the adaptive schedule, because every
        # phase interval is a multiple of the poll step.
        from watcher.verification_trigger import (
            acquisition_due,
            acquisition_due_additive,
            empty_trigger_state,
            evaluate_verification_trigger,
        )

        poll = parse_iso(start) + timedelta(seconds=schedule_offset_seconds)
        poll_step = timedelta(seconds=int(trigger_poll_interval_seconds))
        trig_state = empty_trigger_state()
        last_acq: Optional[datetime] = None
        last_base_acq: Optional[datetime] = None
        visible: List[Dict[str, Any]] = []
        visible_index = 0
        consultations = 0
        ignored_future_events = 0
        acquisitions_by_reason: Dict[str, int] = {}
        events_first_seen: List[Dict[str, Any]] = []
        burst_spans: List[Dict[str, Any]] = []
        prev_burst_active = False
        immediate_fires_acted_on = 0
        while poll < stop:
            decision = None
            if trigger_policy.enabled:
                consultations += 1
                while visible_index < len(trigger_events) and parse_iso(
                    str(trigger_events[visible_index]["available_at"])
                ) <= poll:
                    visible.append(trigger_events[visible_index])
                    visible_index += 1
                decision = evaluate_verification_trigger(
                    state=trig_state,
                    events=visible,
                    now=poll,
                    policy=trigger_policy,
                )
                trig_state = decision["state"]
                if not trigger_immediate_enabled and decision["fired_immediate"]:
                    decision = {**decision, "fired_immediate": False}
                if decision["fired_immediate"]:
                    immediate_fires_acted_on += 1
                if trigger_burst_cap_enabled is False:
                    decision = {
                        **decision,
                        "effective_interval_cap_seconds": None,
                    }
                ignored_future_events += int(decision["ignored_future_events"])
                for row in decision["new_qualifying_events"]:
                    events_first_seen.append(
                        {**row, "first_seen_poll": _utc_iso(poll)}
                    )
                if decision["burst_active"] and not prev_burst_active:
                    burst_spans.append(
                        {
                            "started_at_poll": _utc_iso(poll),
                            "burst_until_final": decision["burst_until"],
                        }
                    )
                elif decision["burst_active"] and burst_spans:
                    burst_spans[-1]["burst_until_final"] = decision["burst_until"]
                prev_burst_active = bool(decision["burst_active"])
            if trigger_anchor_mode == "additive":
                due, reason, credits_base = acquisition_due_additive(
                    now=poll,
                    last_any_acquisition_at=last_acq,
                    last_base_acquisition_at=last_base_acq,
                    phase_interval_seconds=_base_interval_seconds(),
                    trigger_decision=decision,
                )
            else:
                due, reason = acquisition_due(
                    now=poll,
                    last_acquisition_at=last_acq,
                    phase_interval_seconds=_base_interval_seconds(),
                    trigger_decision=decision,
                )
                credits_base = due
            if due:
                audit = {
                    "acquisition_due_reason": reason,
                    "verification_burst_active": bool(
                        decision and decision["burst_active"]
                    ),
                    "verification_burst_until": (
                        decision["burst_until"] if decision else None
                    ),
                    "trigger_new_event_keys": (
                        [
                            row["event_key"]
                            for row in decision["new_qualifying_events"]
                        ]
                        if decision
                        else []
                    ),
                }
                acquisitions_by_reason[reason] = (
                    acquisitions_by_reason.get(reason, 0) + 1
                )
                folded = _acquire(poll, audit=audit)
                last_acq = poll
                if credits_base:
                    last_base_acq = poll
                if folded:
                    break
            poll = poll + poll_step

        trigger_accounting = {
            "policy": {
                "enabled": trigger_policy.enabled,
                "qualifying": [
                    f"{p}.{s}" for p, s in trigger_policy.qualifying
                ],
                "burst_interval_seconds": trigger_policy.burst_interval_seconds,
                "burst_duration_seconds": trigger_policy.burst_duration_seconds,
                "poll_interval_seconds": int(trigger_poll_interval_seconds),
                "immediate_fire_enabled": trigger_immediate_enabled,
                "burst_cap_enabled": trigger_burst_cap_enabled,
                "anchor_mode": trigger_anchor_mode,
                "base_schedule": trigger_base_schedule,
                "base_fixed_interval_seconds": (
                    int(fixed_interval_seconds) if trigger_base_schedule == "fixed" else None
                ),
            },
            "authority": "acquisition_scheduling_only",
            "corpus": _portable_path(trigger_corpus_path),
            "corpus_events_total": len(trigger_events),
            "trigger_stream_consultations": consultations,
            "consultation_semantics": (
                "one bounded warning-stream poll per poll-grid instant while "
                "the mission is active; counted separately from official "
                "acquisitions and never free"
            ),
            "ignored_future_events": ignored_future_events,
            "events_first_seen": events_first_seen,
            # Count fires this arm actually acted on. The decision function's
            # own counter is computed before component ablation masks a fire,
            # so reporting it directly would credit a suppressed component.
            "immediate_fires": immediate_fires_acted_on,
            "immediate_fires_before_component_ablation": int(
                trig_state.get("immediate_fires") or 0
            ),
            "acquisitions_by_reason": acquisitions_by_reason,
            "trigger_attributable_acquisitions": (
                acquisitions_by_reason.get("verification_immediate", 0)
                + acquisitions_by_reason.get("verification_burst", 0)
            ),
            "attribution_semantics": (
                "an acquisition is trigger-attributable when it was not due "
                "under the phase cadence alone at that poll given the same "
                "last-acquisition time; exact counterfactuals come from the "
                "trigger-disabled ablation"
            ),
            "burst_spans": burst_spans,
            "final_state": trig_state,
        }

    return {
        "schema": "sentinel-replay-run/2",
        "ablation": ablation,
        "county": county,
        "state": state,
        "fips": fips,
        "window": {"start": start, "end": end},
        "analysis_interval_convention": "[start,end)",
        "news_corpus": _portable_path(news_corpus_path) if news is not None else None,
        "truth_reference": _portable_path(truth_path),
        "nws_system_input": _portable_path(nws_input_path),
        "nws_reference_is_system_input": bool(
            truth_path
            and nws_input_path
            and Path(truth_path).resolve() == Path(nws_input_path).resolve()
        ),
        "max_staleness_hours": max_staleness_hours,
        "policy_parameters": {
            "max_staleness_hours": max_staleness_hours,
            "county_flood_min_gauges": county_flood_min_gauges(),
        },
        "threshold_exclusions": threshold_exclusions,
        "verification_trigger": trigger_accounting,
        "schedule": {
            "kind": (
                "hybrid_verification_trigger"
                if trigger_policy is not None and trigger_policy.enabled
                else "fixed_override"
                if fixed_interval_seconds is not None
                else "phase_adaptive"
                if role_layer_enabled()
                else "ablation_fixed"
            ),
            "fixed_interval_seconds": (
                fixed_interval_seconds
                if fixed_interval_seconds is not None
                else FIXED_INTERVAL_SECONDS
                if not role_layer_enabled()
                else None
            ),
            "offset_seconds": schedule_offset_seconds,
            "phase_reasoning_enabled": role_layer_enabled(),
            "poll_walk": trigger_policy is not None,
            "poll_interval_seconds": (
                int(trigger_poll_interval_seconds)
                if trigger_policy is not None
                else None
            ),
        },
        "fixed_interval_seconds": (
            fixed_interval_seconds
            if fixed_interval_seconds is not None
            else FIXED_INTERVAL_SECONDS
            if not role_layer_enabled()
            else None
        ),
        "schedule_offset_seconds": schedule_offset_seconds,
        "recovery_fold_policy": {
            "kind": "continuous_all_clear_elapsed_time",
            "minimum_clear_hours": (
                float(recovery_min_clear_hours)
                if recovery_min_clear_hours is not None
                else float(RECOVERY_MIN_CLEAR_HOURS_TO_FOLD)
            ),
            "streak_is_audit_metadata_only": True,
            "uses_virtual_tick_time": True,
            "default_48h_status": (
                "post_audit_engineering_correction_motivated_by_hillsborough_recurrence;"
                "not_optimized_or_validated"
            ),
        },
        "initial_policy_state": {
            "phase_before": STATIC_PHASE,
            "phase_after": STATIC_PHASE,
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
            "recovery_all_clear_streak": 0,
            "recovery_clear_since": None,
            "recovery_clear_elapsed_hours": 0.0,
            "folded": False,
        },
        "termination": (
            {
                "reason": "recovery_continuous_clear",
                "t": ticks[-1]["t"],
            }
            if ticks and ticks[-1].get("folded")
            else {"reason": "window_end", "t": ticks[-1]["t"] if ticks else None}
        ),
        "n_ticks": len(ticks),
        "ticks": ticks,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay an event under one ablation")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--news-corpus", default="")
    parser.add_argument(
        "--truth",
        default="",
        help="held-out NWS truth/timeliness reference (never injected as input)",
    )
    parser.add_argument(
        "--nws-input",
        default="",
        help="optional NWS archive explicitly injected as a system input",
    )
    parser.add_argument("--county", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--fips", default="")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--ablation",
        default="",
        help="Sets SENTINEL_ABLATION for this run (full|no_belief|no_role|none)",
    )
    parser.add_argument("--max-staleness-hours", type=float, default=6.0)
    parser.add_argument(
        "--exclude-threshold-site",
        action="append",
        default=[],
        help="site id whose incompatible stage thresholds are disabled",
    )
    parser.add_argument(
        "--recovery-min-clear-hours",
        type=float,
        default=None,
        help="fold sensitivity override (primary shared-production default is 48h)",
    )
    parser.add_argument(
        "--fixed-interval-hours",
        type=float,
        default=None,
        help="override acquisition cadence only; phase reasoning remains enabled",
    )
    parser.add_argument(
        "--trigger-corpus",
        default="",
        help="frozen VTEC trigger-stream corpus; enables the hybrid schedule",
    )
    parser.add_argument(
        "--trigger-disabled",
        action="store_true",
        help="run the hybrid poll-grid driver with the trigger switched off",
    )
    parser.add_argument(
        "--trigger-qualifying",
        default="FF.W,FA.W,FL.W",
        help="comma-separated PH.S pairs that qualify as verification triggers",
    )
    parser.add_argument("--trigger-burst-interval-hours", type=float, default=1.0)
    parser.add_argument("--trigger-burst-duration-hours", type=float, default=6.0)
    parser.add_argument("--trigger-poll-minutes", type=float, default=15.0)
    parser.add_argument(
        "--schedule-offset-hours",
        type=float,
        default=0.0,
        help="offset the first fixed-schedule acquisition from the window start",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if args.ablation:
        os.environ["SENTINEL_ABLATION"] = args.ablation

    result = run(
        corpus_path=args.corpus,
        news_corpus_path=args.news_corpus or None,
        nws_input_path=args.nws_input or None,
        truth_path=args.truth or None,
        county=args.county,
        state=args.state,
        fips=args.fips,
        start=args.start,
        end=args.end,
        max_staleness_hours=args.max_staleness_hours,
        fixed_interval_seconds=(
            round(args.fixed_interval_hours * 3600)
            if args.fixed_interval_hours is not None
            else None
        ),
        schedule_offset_seconds=round(args.schedule_offset_hours * 3600),
        threshold_excluded_site_ids=args.exclude_threshold_site,
        recovery_min_clear_hours=args.recovery_min_clear_hours,
        trigger=(
            {
                "enabled": not args.trigger_disabled,
                "qualifying": [
                    token.strip()
                    for token in args.trigger_qualifying.split(",")
                    if token.strip()
                ],
                "burst_interval_seconds": round(
                    args.trigger_burst_interval_hours * 3600
                ),
                "burst_duration_seconds": round(
                    args.trigger_burst_duration_hours * 3600
                ),
            }
            if args.trigger_corpus or args.trigger_disabled
            else None
        ),
        trigger_corpus_path=args.trigger_corpus or None,
        trigger_poll_interval_seconds=max(60, round(args.trigger_poll_minutes * 60)),
    )

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {target} ({result['n_ticks']} ticks, ablation={result['ablation']})")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
