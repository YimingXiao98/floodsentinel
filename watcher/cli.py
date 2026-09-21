"""Disaster Sentinel watcher CLI."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from datetime import datetime, timezone

from watcher.claims.extract import extract_claims_from_snapshots
from watcher.claims.extract_articles import (
    build_article_source_records,
    extract_article_claims,
)
from watcher.format_analyst import (
    format_analyst_report_with_slack_block,
    format_analyst_slack,
    format_analyst_slack_v1,
    format_situation_summary,
)
from watcher.format_report import (
    format_coordinator_report,
    format_coordinator_report_with_slack_block,
    format_error_report,
    format_slack_report,
    format_slack_report_v1,
)
from watcher.slack_delivery import (
    deliver_analyst_slack,
    deliver_coordinator_slack,
    deliver_slack_message,
    slack_channel_id,
)
from watcher.fetch import fetch_source_records
from watcher.geo_util import normalize_county_name, normalize_state
from watcher.heartbeat_cron import (
    disable_heartbeat_cron_if_idle,
    ensure_heartbeat_cron,
)
from watcher.neo4j_client import (
    apply_belief_state_update,
    bootstrap_mission,
    check_neo4j_health,
    deactivate_mission,
    get_last_coordinator_report,
    get_latest_source_snapshots,
    get_mission_context,
    get_mission_context_by_county,
    init_schema,
    list_active_missions,
    list_source_readings,
    persist_evidence_claims,
    persist_watcher_readings,
    save_coordinator_report,
    set_recovery_all_clear_streak,
    set_verification_trigger_state,
    update_mission_phase,
    verify_bootstrap,
)
from watcher.phase_engine import apply_recovery_fold_streak, evaluate_phase_transition
from watcher.report_delta import build_report_snapshot, compute_delta
from watcher.timezone_util import state_to_timezone, utc_iso_to_local
from watcher.config import sentinel_ontology_ledger, sentinel_output_ontology
from watcher.ontology.bridge import discover_sources_from_stub, get_monitoring_summary
from watcher.article_evidence_bridge import fetch_article_evidence
from watcher.county_flood_evidence import build_county_flood_evidence
from watcher.county_flood_bridge import fetch_county_flood_sources


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _attach_coordinator_slack(data: Dict[str, Any]) -> Dict[str, Any]:
    """Populate slack_message (+ slack_messages when SENTINEL_OUTPUT_ONTOLOGY=v1)."""
    if sentinel_output_ontology() == "v1":
        primary, messages = format_slack_report_v1(data)
        data["slack_message"] = primary
        data["slack_messages"] = messages
    else:
        data["slack_message"] = format_slack_report(data)
        data.pop("slack_messages", None)
    return data


def _attach_analyst_slack(data: Dict[str, Any]) -> Dict[str, Any]:
    """Populate analyst_slack_message (+ slack_messages when v1)."""
    if sentinel_output_ontology() == "v1":
        primary, messages = format_analyst_slack_v1(data)
        data["analyst_slack_message"] = primary
        data["slack_messages"] = messages
        data["analyst_slack_messages"] = messages
    else:
        data["analyst_slack_message"] = format_analyst_slack(data)
        data.pop("slack_messages", None)
        data.pop("analyst_slack_messages", None)
    return data



def _parse_utc(ts: str) -> datetime:
    normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mission_cycle_due(
    mission: Dict[str, Any],
    trigger_decision: Optional[Dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Return whether a mission is due for coordinator run based on phase interval.

    Interval is resolved from the phase monitoring stub so stale MissionSpec
    heartbeat_interval_seconds (e.g. leftover 800s) cannot force a 15-minute run.

    ``trigger_decision`` is the optional verification-trigger consultation for
    this poll. Its only possible effect is to make the mission due earlier
    (an immediate verification acquisition or a burst-capped interval); it can
    never delay a scheduled run, and it carries no evidence into the cycle.
    """
    phase = str(mission.get("current_event_phase") or "early_watch").strip().lower()
    if phase == "folded":
        return False, "skipped (folded mission)"

    try:
        summary = get_monitoring_summary("flood", phase)
        interval = int(summary.get("heartbeat_interval_seconds") or 0)
    except Exception:
        interval = 0
    if interval <= 0:
        interval = int(mission.get("heartbeat_interval_seconds") or 21600)

    last = mission.get("last_heartbeat_at") or mission.get("last_coordinator_report_at")
    if not last:
        return True, f"first_run ({phase}, interval {interval}s)"
    if trigger_decision is not None:
        from watcher.verification_trigger import (
            acquisition_due,
            acquisition_due_additive,
            trigger_anchor_mode_from_env,
        )

        now = datetime.now(timezone.utc)
        last_any = _parse_utc(str(last))
        if trigger_anchor_mode_from_env() == "additive":
            state = trigger_decision.get("state") or {}
            last_base_raw = state.get("last_base_acquisition_at")
            # Migration: missions that predate base-clock tracking treat the
            # ordinary heartbeat clock as the base anchor until one base-due
            # cycle records the real one.
            last_base = _parse_utc(str(last_base_raw)) if last_base_raw else last_any
            due, reason, credits_base = acquisition_due_additive(
                now=now,
                last_any_acquisition_at=last_any,
                last_base_acquisition_at=last_base,
                phase_interval_seconds=interval,
                trigger_decision=trigger_decision,
            )
            trigger_decision["_credits_base_clock"] = credits_base
        else:
            due, reason = acquisition_due(
                now=now,
                last_acquisition_at=last_any,
                phase_interval_seconds=interval,
                trigger_decision=trigger_decision,
            )
            trigger_decision["_credits_base_clock"] = due
        elapsed = (now - last_any).total_seconds()
        if due:
            return True, (
                f"due ({phase}: {int(elapsed)}s elapsed, interval {interval}s, "
                f"reason {reason})"
            )
        return False, (
            f"skipped ({phase}: {int(elapsed)}s elapsed, interval {interval}s)"
        )
    elapsed = (datetime.now(timezone.utc) - _parse_utc(str(last))).total_seconds()
    if elapsed >= interval:
        return True, f"due ({phase}: {int(elapsed)}s elapsed, interval {interval}s)"
    return False, f"skipped ({phase}: {int(elapsed)}s elapsed, interval {interval}s)"


def _consult_verification_trigger(
    mission: Dict[str, Any],
    alert_cache: Dict[str, List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """One best-effort warning-stream consultation for a mission poll.

    Any failure returns a no-op decision rather than raising: a broken trigger
    stream must never block the scheduled cadence, and the trigger's authority
    is limited to scheduling either way. The caller persists the returned state
    only after the acquisition it authorized has actually run, so a cycle that
    errors leaves the fire pending for the next poll instead of consuming it.
    """
    from watcher.verification_trigger import (
        candidate_events_from_live_alerts,
        evaluate_verification_trigger,
        trigger_policy_from_env,
    )

    policy = trigger_policy_from_env()
    if not policy.enabled:
        return None
    fips = str(mission.get("fips") or "")
    if fips not in alert_cache:
        try:
            from watcher.county_flood_bridge import fetch_active_flood_alert_dicts

            alert_cache[fips] = fetch_active_flood_alert_dicts(
                county=str(mission.get("county") or ""),
                state=str(mission.get("state") or ""),
                fips=fips or None,
            )
        except Exception:
            alert_cache[fips] = []
    raw_state = mission.get("verification_trigger_state")
    if isinstance(raw_state, str) and raw_state.strip():
        try:
            raw_state = json.loads(raw_state)
        except (TypeError, ValueError):
            raw_state = None
    return evaluate_verification_trigger(
        state=raw_state if isinstance(raw_state, dict) else None,
        events=candidate_events_from_live_alerts(alert_cache[fips]),
        now=datetime.now(timezone.utc),
        policy=policy,
    )


def _persist_verification_trigger_state(
    mission: Dict[str, Any],
    trigger_decision: Optional[Dict[str, Any]],
    *,
    acquisition_ran: bool = False,
) -> None:
    """Store trigger scheduling state; failures degrade dedup, never safety."""
    if not trigger_decision:
        return
    state = dict(trigger_decision["state"])
    if acquisition_ran and trigger_decision.get("_credits_base_clock"):
        state["last_base_acquisition_at"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
    try:
        set_verification_trigger_state(str(mission.get("mission_id") or ""), state)
    except Exception:
        pass


def _source_mode_from_args(args: argparse.Namespace) -> str:
    mode = str(getattr(args, "source", "both") or "both").strip().lower()
    if mode not in ("nws", "usgs", "both"):
        return "both"
    return mode


def _print_coordinator_output(
    data: Dict[str, Any],
    output_format: str,
    *,
    deliver_slack: bool = True,
) -> None:
    if output_format == "json":
        _print_json(data)
        return

    if output_format == "slack":
        if data.get("error"):
            print(format_error_report(data))
        else:
            print(data.get("slack_message") or format_slack_report(data))
        return

    if data.get("error"):
        print(format_error_report(data))
        return

    print(format_coordinator_report_with_slack_block(data))

    if not deliver_slack:
        return

    slack_result = deliver_coordinator_slack(data)
    if slack_result.get("ok"):
        channel = slack_result.get("channel_id") or slack_channel_id()
        message_id = slack_result.get("message_id")
        suffix = f" (message {message_id})" if message_id else ""
        print(f"\nSlack: delivered to channel:{channel}{suffix}")
    elif slack_result.get("skipped"):
        return
    else:
        err = slack_result.get("error") or "unknown error"
        print(f"\nSlack: delivery failed — {err}", file=sys.stderr)


def _print_analyst_output(
    data: Dict[str, Any],
    output_format: str,
    *,
    deliver_slack: bool = True,
) -> None:
    if output_format == "json":
        _print_json(data)
        return

    if output_format == "slack":
        if data.get("error"):
            print(format_error_report(data))
        else:
            print(data.get("analyst_slack_message") or format_analyst_slack(data))
        return

    if data.get("error"):
        print(format_error_report(data))
        return

    print(format_analyst_report_with_slack_block(data))

    if not deliver_slack:
        return

    slack_result = deliver_analyst_slack(data)
    if slack_result.get("ok"):
        channel = slack_result.get("channel_id") or slack_channel_id()
        message_id = slack_result.get("message_id")
        suffix = f" (message {message_id})" if message_id else ""
        print(f"\nSlack: delivered to channel:{channel}{suffix}")
    elif slack_result.get("skipped"):
        return
    else:
        err = slack_result.get("error") or "unknown error"
        print(f"\nSlack: delivery failed — {err}", file=sys.stderr)


def _add_output_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("human", "slack", "json"),
        default="human",
        help="Output format: human (default), slack, or json",
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="Skip automatic Slack delivery after a successful coordinator run",
    )


def _add_county_flood_source_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        choices=["nws", "usgs", "both"],
        default="both",
        help="County flood APIs: nws (NOAA NWPS), usgs (USGS NWIS IV), or both (default)",
    )


def _add_mission_lookup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mission-id", default="")
    parser.add_argument("--fips", default="")
    parser.add_argument("--county", default="")
    parser.add_argument("--state", default="")


_MISSION_BOOTSTRAP_MAIN = (
    Path(__file__).resolve().parent.parent / "skills" / "mission-bootstrap" / "main.py"
)


def _resolve_geography(county: str, state: str, fips: str = "") -> Dict[str, Any]:
    """Resolve county FIPS via mission-bootstrap geography helper."""
    if not _MISSION_BOOTSTRAP_MAIN.is_file():
        return {
            "error": "config_error",
            "message": f"mission-bootstrap main.py not found at {_MISSION_BOOTSTRAP_MAIN}",
        }
    spec = importlib.util.spec_from_file_location("mission_bootstrap_main", _MISSION_BOOTSTRAP_MAIN)
    if not spec or not spec.loader:
        return {"error": "config_error", "message": "Could not load mission-bootstrap geography module"}
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_geography({"county": county, "state": state, "fips": fips})


def _resolve_mission_context(args: argparse.Namespace) -> Dict[str, Any]:
    """Resolve mission from --mission-id, --fips, or --county + --state."""
    if args.mission_id:
        return get_mission_context(args.mission_id)
    if args.fips:
        return get_mission_context_by_county(fips=args.fips)
    if args.county and args.state:
        return get_mission_context_by_county(
            county=normalize_county_name(args.county),
            state=normalize_state(args.state),
        )
    return {
        "error": "input_error",
        "message": "Provide --mission-id, --fips, or both --county and --state",
        "hint": "Run /skill mission-bootstrap for the county first",
    }


def _placeholder_county_name(name: str, fips: str = "") -> bool:
    cleaned = str(name or "").strip().lower()
    if not cleaned or cleaned in ("unknown", "unknown county"):
        return True
    if fips and cleaned == f"county_{fips}".lower():
        return True
    return False


def _overlay_geography_on_context(ctx: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """Use CLI args or geography lookup when mission_spec has placeholder county names."""
    if ctx.get("error"):
        return ctx
    ms = dict(ctx.get("mission_spec") or {})
    fips = str(getattr(args, "fips", None) or ctx.get("fips") or ms.get("fips") or "").strip()
    county = normalize_county_name(args.county) if getattr(args, "county", None) else ""
    state = normalize_state(args.state) if getattr(args, "state", None) else ""

    if (not county or not state) and _placeholder_county_name(str(ms.get("county") or ""), fips):
        geo = _resolve_geography(county, state, fips=fips)
        if not geo.get("error"):
            county = geo.get("county") or county
            state = normalize_state(geo.get("state") or state) or state

    if county and state:
        ms["county"] = county
        ms["state"] = state
        ms["location"] = f"{county} County, {state}"
        ctx = dict(ctx)
        ctx["mission_spec"] = ms
    return ctx


def _bootstrap_mission_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    """Run mission-bootstrap for county+state (or fips) lookup inputs."""
    county = normalize_county_name(args.county) if args.county else ""
    state = normalize_state(args.state) if args.state else ""
    fips = str(args.fips or "").strip()

    if not fips and county and state:
        geo = _resolve_geography(county, state)
        if geo.get("error"):
            return geo
        county = geo.get("county") or county
        state = normalize_state(geo.get("state") or state)
        fips = geo.get("fips") or geo.get("county_fips") or ""

    if not fips:
        return {
            "error": "input_error",
            "message": "fips is required for bootstrap; provide --fips or resolvable --county and --state",
        }

    health = check_neo4j_health()
    if not health.get("ok"):
        return {"error": "neo4j_unavailable", "health": health}

    if not county or not state:
        geo = _resolve_geography(county or "", state or "", fips=fips)
        if not geo.get("error"):
            county = geo.get("county") or county or f"county_{fips}"
            state = geo.get("state") or state or ""

    result = bootstrap_mission(county, state, fips, mode="shadow_live")
    if not result.get("neo4j_verification", {}).get("ok"):
        return {"error": "bootstrap_verification_failed", **result}
    if result.get("already_active") or result.get("reused_active_mission"):
        result["auto_bootstrapped"] = False
        result["reused_active_mission"] = True
    else:
        result["auto_bootstrapped"] = True
    return result


def _resolve_or_bootstrap_mission_context(args: argparse.Namespace) -> Dict[str, Any]:
    """Resolve mission context; auto-bootstrap when county+state (or fips) has no active mission."""
    ctx = _resolve_mission_context(args)
    if ctx.get("error") != "not_found":
        overlaid = _overlay_geography_on_context(ctx, args)
        if overlaid.get("mission_id") and not overlaid.get("error"):
            overlaid["reused_active_mission"] = True
        return overlaid

    can_bootstrap = bool(args.fips or (args.county and args.state))
    if not can_bootstrap:
        return ctx

    boot = _bootstrap_mission_from_args(args)
    if boot.get("error"):
        return boot

    mission_id = boot.get("mission_id")
    if not mission_id:
        return {"error": "bootstrap_error", "message": "Bootstrap succeeded but mission_id missing", **boot}

    ctx = get_mission_context(mission_id)
    ctx["bootstrap_result"] = boot
    if boot.get("reused_active_mission") or boot.get("already_active"):
        ctx["reused_active_mission"] = True
    return _overlay_geography_on_context(ctx, args)


def cmd_init_schema(_: argparse.Namespace) -> int:
    init_schema()
    print("Schema initialized.")
    return 0


def cmd_monitoring_summary(args: argparse.Namespace) -> int:
    summary = get_monitoring_summary(args.hazard, args.phase, county_fips=args.fips)
    _print_json(summary)
    return 0


def cmd_neo4j_health(_: argparse.Namespace) -> int:
    result = check_neo4j_health()
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_verify_bootstrap(args: argparse.Namespace) -> int:
    result = verify_bootstrap(args.mission_id)
    _print_json(result)
    return 0 if result.get("ok") else 1


def _resolve_county_geography(args: argparse.Namespace) -> Dict[str, Any]:
    """Resolve county FIPS from --fips or --county + --state (no Neo4j required)."""
    fips = str(getattr(args, "fips", "") or "").strip()
    county = normalize_county_name(args.county) if getattr(args, "county", "") else ""
    state = normalize_state(args.state) if getattr(args, "state", "") else ""

    if fips and len(fips) == 5 and fips.isdigit():
        if not county or not state:
            geo = _resolve_geography(county or "", state or "", fips=fips)
            if not geo.get("error"):
                county = geo.get("county") or county or f"county_{fips}"
                state = normalize_state(geo.get("state") or state)
        return {
            "fips": fips,
            "county": county or f"county_{fips}",
            "state": state or "",
            "location": f"{county} County, {state}".strip(", ") if county else f"FIPS {fips}",
        }

    if county and state:
        geo = _resolve_geography(county, state)
        if geo.get("error"):
            return geo
        fips_resolved = geo.get("fips") or geo.get("county_fips") or ""
        county_name = geo.get("county") or geo.get("county_name") or county
        state_name = normalize_state(geo.get("state") or state)
        return {
            "fips": fips_resolved,
            "county": county_name,
            "state": state_name,
            "location": geo.get("location") or f"{county_name} County, {state_name}",
        }

    return {
        "error": "input_error",
        "message": "Provide --fips or both --county and --state",
    }


_COUNTY_FLOOD_SKILL_DIR = (
    Path(__file__).resolve().parent.parent / "skills" / "county-flood-data"
)


def _run_county_flood_skill(**kwargs: Any) -> Dict[str, Any]:
    skill_dir = str(_COUNTY_FLOOD_SKILL_DIR)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    from county_flood.runner import run_county_flood

    return run_county_flood(**kwargs)


def _resolve_county_flood_sources(args: argparse.Namespace) -> list[str]:
    skill_dir = str(_COUNTY_FLOOD_SKILL_DIR)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    from main import resolve_sources

    return resolve_sources(args)


def _add_county_flood_source_args(parser: argparse.ArgumentParser) -> None:
    skill_dir = str(_COUNTY_FLOOD_SKILL_DIR)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    from main import add_source_args

    add_source_args(parser)


def cmd_county_flood_data(args: argparse.Namespace) -> int:
    """County flood fetch via NOAA NWPS + USGS NWIS IV + USGS OGC (skill-local implementation)."""
    geo = _resolve_county_geography(args)
    if geo.get("error"):
        county = normalize_county_name(getattr(args, "county", "") or "")
        state = normalize_state(getattr(args, "state", "") or "")
        if county and state:
            geo = {
                "fips": None,
                "county": county,
                "state": state,
                "location": f"{county}, {state}",
            }
        else:
            if args.format == "json":
                _print_json(geo)
            else:
                print(format_error_report(geo))
            return 1

    county = geo["county"]
    state = geo["state"]
    fips = geo.get("fips") or None
    location = geo["location"]

    gauge_ids_raw = str(getattr(args, "gauge_ids", "") or "").strip()
    gauge_ids = [g.strip() for g in gauge_ids_raw.split(",") if g.strip()] or None
    site_ids_raw = str(getattr(args, "site_ids", "") or "").strip()
    site_ids = [s.strip() for s in site_ids_raw.split(",") if s.strip()] or None
    sources = _resolve_county_flood_sources(args)

    try:
        payload = _run_county_flood_skill(
            county=county,
            state=state,
            fips=fips,
            location=location,
            gauge_ids=gauge_ids,
            site_ids=site_ids,
            sources=sources,
            output_format=args.format,
            discover_categories=bool(getattr(args, "discover_categories", False)),
            emit=True,
        )
    except FileNotFoundError as exc:
        err = {"error": "config_error", "message": str(exc)}
        if args.format == "json":
            _print_json(err)
        else:
            print(format_error_report(err))
        return 1

    gauges_with_data = (payload.get("summary") or {}).get("gauges_with_data", 0)
    return 0 if gauges_with_data > 0 else 1


def cmd_discover_sources(args: argparse.Namespace) -> int:
    ctx = _resolve_mission_context(args)
    if ctx.get("error"):
        _print_json(ctx)
        return 1
    ms = ctx["mission_spec"]
    discovery = discover_sources_from_stub(
        ctx["monitoring_summary"],
        mission_id=ctx["mission_id"],
        county_fips=ctx.get("fips"),
        county_name=ms.get("county"),
        location=ms.get("location"),
        state=ms.get("state"),
    )
    result = {
        "mission_id": ctx["mission_id"],
        "mission_spec": ctx["mission_spec"],
        "monitoring_summary": ctx["monitoring_summary"],
        "belief_state": ctx.get("belief_state", {}),
        **discovery,
    }
    _print_json(result)
    return 0


def _phase_aware_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Reload monitoring_summary for current BeliefState phase."""
    ms = ctx.get("mission_spec") or {}
    belief = ctx.get("belief_state") or {}
    phase = belief.get("event_phase") or ms.get("current_event_phase") or "early_watch"
    fips = ctx.get("fips") or ms.get("fips") or ""
    summary = get_monitoring_summary(
        "flood",
        phase,
        county_fips=fips,
        county_name=ms.get("county"),
        aoi_id=ms.get("aoi_id"),
    )
    ctx = dict(ctx)
    ctx["monitoring_summary"] = summary
    ms = dict(ms)
    ms["current_event_phase"] = phase
    ctx["mission_spec"] = ms
    return ctx


def _run_monitor_cycle(
    ctx: Dict[str, Any],
    *,
    coordinator: bool = False,
    source_mode: str = "both",
) -> Dict[str, Any]:
    """Run county-flood-data skill, persist readings, return merged envelope."""
    mission_id = ctx["mission_id"]
    ms = ctx["mission_spec"]
    prior = get_latest_source_snapshots(mission_id)
    fips = ctx.get("fips") or ms.get("fips") or ""
    location = ms.get("location") or f"{ms.get('county')}, {ms.get('state')}"

    fetch_result = fetch_county_flood_sources(
        county=ms.get("county") or "",
        state=ms.get("state") or "",
        fips=fips,
        location=location,
        mission_id=mission_id,
        source_mode=source_mode,
        prior_by_source=prior,
    )
    discovery = {
        "source_records": fetch_result["source_records"],
        "discovery_mode": fetch_result.get("discovery_mode", "county_flood_data_skill"),
        "county_api_mapping": fetch_result.get("county_api_mapping"),
        "credibility_assessments": fetch_result.get("credibility_assessments") or [],
        "dropped_sources": fetch_result.get("dropped_sources") or [],
        "degraded_sources": fetch_result.get("degraded_sources") or [],
    }
    tz_name = state_to_timezone(ms.get("state") or "")
    for snap in fetch_result.get("source_snapshots") or []:
        fetched = snap.get("fetched_at")
        if fetched and not snap.get("fetched_at_local"):
            snap["fetched_at_local"] = utc_iso_to_local(fetched, tz_name)
            snap["fetched_at_utc"] = fetched
    neo4j_persist = persist_watcher_readings(
        mission_id,
        fips=ctx.get("fips") or ms.get("fips") or "",
        county=ms.get("county") or "",
        state=ms.get("state") or "",
        source_records=discovery["source_records"],
        source_snapshots=fetch_result["source_snapshots"],
        failed_fetches=fetch_result.get("failed_fetches"),
    )
    stored_readings = list_source_readings(mission_id=mission_id, limit=10)
    result: Dict[str, Any] = {
        "mission_id": mission_id,
        "mission_spec": ms,
        "monitoring_summary": ctx["monitoring_summary"],
        "belief_state": ctx.get("belief_state", {}),
        "source_records": discovery["source_records"],
        "credibility_assessments": discovery.get("credibility_assessments", []),
        "dropped_sources": discovery.get("dropped_sources", []),
        "degraded_sources": discovery.get("degraded_sources", []),
        "county_api_mapping": discovery.get("county_api_mapping"),
        "county_flood_payload": fetch_result.get("county_flood_payload"),
        "county_flood_source_mode": source_mode,
        "discovery_mode": discovery["discovery_mode"],
        "prior_readings": prior,
        "neo4j_persistence": neo4j_persist,
        "stored_readings_summary": {
            "count": stored_readings.get("count", 0),
            "latest": stored_readings.get("readings", [])[:5],
        },
        **fetch_result,
    }
    if coordinator:
        result["coordinator_mode"] = True
    return result


def _ontology_ledger_enabled() -> bool:
    return sentinel_ontology_ledger().lower() == "on"


def _shadow_write_ontology_ledger(
    mission_id: str,
    mission_spec: Dict[str, Any],
    source_records: List[Dict[str, Any]],
    evidence_claims: List[Dict[str, Any]],
) -> None:
    """Append DataSource + Claim records; never raises."""
    if not _ontology_ledger_enabled():
        return
    try:
        from watcher.ontology.adapters import evidence_claim_to_claim, source_record_to_datasource
        from watcher.ontology.belief_schema import Location
        from watcher.ontology.ledger import write as ledger_write

        mission_location = Location(
            name=f"{mission_spec.get('county', '')}, {mission_spec.get('state', '')}".strip(", "),
            scope="county",
        )
        ledger_write(mission_id, mission_location)
        for source_record in source_records:
            try:
                ledger_write(mission_id, source_record_to_datasource(source_record))
            except Exception:
                pass
        for claim in evidence_claims:
            try:
                ledger_write(
                    mission_id,
                    evidence_claim_to_claim(claim, location_id=mission_location.id),
                )
                breakdown = claim.get("confidence_breakdown") or {
                    "overall_score": claim.get("overall_confidence", 0),
                    "method": "corroborated",
                    "method_confidence": claim.get("overall_confidence", 0),
                    "source_reliability_prior": claim.get("source_reliability_prior", 0.8),
                    "uncertainty_notes": [],
                }
                from watcher.ontology.adapters import confidence_to_belief

                ledger_write(
                    mission_id,
                    confidence_to_belief(claim["claim_id"], breakdown),
                )
            except Exception:
                pass
    except Exception:
        pass


def _shadow_write_ontology_phase(
    mission_id: str,
    phase_decision: Dict[str, Any],
    evidence_claims: List[Dict[str, Any]],
) -> None:
    """Append Phase record on transition; never raises."""
    if not _ontology_ledger_enabled():
        return
    if not phase_decision.get("phase_changed"):
        return
    try:
        from watcher.ontology.adapters import phase_transition_to_phase
        from watcher.ontology.ledger import write as ledger_write

        trigger_id = evidence_claims[0]["claim_id"] if evidence_claims else "unknown"
        transition = {
            **phase_decision,
            "old_phase": phase_decision.get("current_phase"),
            "evaluated_at_utc": phase_decision.get("evaluated_at_utc") or _utc_now(),
        }
        ledger_write(
            mission_id,
            phase_transition_to_phase(transition, trigger_id),
        )
    except Exception:
        pass


def _ontology_ledger_summary(mission_id: str) -> Optional[Dict[str, Any]]:
    if not _ontology_ledger_enabled():
        return None
    try:
        from watcher.ontology.ledger import ledger_path, read_all

        records = read_all(mission_id)
        counts: Dict[str, int] = {}
        for record in records:
            counts[record.type] = counts.get(record.type, 0) + 1
        latest_intel = next(
            (r for r in reversed(records) if r.type == "Intelligence"),
            None,
        )
        summary: Dict[str, Any] = {
            "ledger_path": ledger_path(mission_id),
            "record_counts": counts,
        }
        if latest_intel is not None:
            summary["latest_intelligence"] = {
                "assessment": latest_intel.assessment,
                "evidence": latest_intel.evidence,
                "confidence": latest_intel.confidence,
            }
        return summary
    except Exception:
        return None


def _run_coordinator_cycle(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Full phase-aware coordinator: fetch → extract → phase rules → delta report."""
    ctx = _phase_aware_context(ctx)
    mission_id = ctx["mission_id"]
    ms = ctx["mission_spec"]
    prior_report = get_last_coordinator_report(mission_id)

    result = _run_monitor_cycle(
        ctx,
        coordinator=True,
        source_mode="both",
    )

    article_evidence = fetch_article_evidence(
        county=ms.get("county") or "",
        state=ms.get("state") or "",
        fips=ctx.get("fips") or ms.get("fips") or "",
        location=ms.get("location") or "",
    )

    article_claims = extract_article_claims(
        article_evidence,
        county=ms.get("county") or "",
        state=ms.get("state") or "",
        location=ms.get("location") or "",
        mission_id=mission_id,
    )
    extraction = extract_claims_from_snapshots(
        result.get("source_snapshots") or [],
        monitoring_summary=ctx["monitoring_summary"],
        county=ms.get("county") or "",
        state=ms.get("state") or "",
        location=ms.get("location") or "",
        source_records=result.get("source_records") or [],
        additional_claims=article_claims,
        only_changed=False,
        mission_id=mission_id,
    )
    claims = extraction.get("evidence_claims") or []
    source_records = list(result.get("source_records") or [])
    source_records.extend(build_article_source_records(article_claims))
    # Only persist claims for snapshots that advanced Neo4j (cold start or |Δ|≥0.1).
    skip_snap_ids = {
        s.get("snapshot_id")
        for s in (result.get("source_snapshots") or [])
        if s.get("change_status") == "unchanged" and s.get("snapshot_id")
    }
    claims_to_persist = [
        c
        for c in claims
        if not c.get("snapshot_id") or c.get("snapshot_id") not in skip_snap_ids
    ]
    claim_persist = persist_evidence_claims(mission_id, claims_to_persist)

    _shadow_write_ontology_ledger(
        mission_id,
        ms,
        source_records,
        claims,
    )

    current_phase = ms.get("current_event_phase") or "early_watch"
    phase_decision = evaluate_phase_transition(
        current_phase=current_phase,
        monitoring_summary=ctx["monitoring_summary"],
        evidence_claims=claims,
    )

    prior_streak = int(ms.get("recovery_all_clear_streak") or 0)
    recovery_evaluated_at = _utc_now()
    fold_result = apply_recovery_fold_streak(
        current_phase=current_phase,
        phase_decision=phase_decision,
        evidence_claims=claims,
        prior_streak=prior_streak,
        evaluated_at=recovery_evaluated_at,
        prior_clear_since=ms.get("recovery_clear_since"),
    )
    phase_decision = fold_result["phase_decision"]
    streak = int(fold_result["recovery_all_clear_streak"])
    clear_since = fold_result.get("recovery_clear_since")
    streak_update = set_recovery_all_clear_streak(
        mission_id, streak, clear_since=clear_since
    )
    ms = dict(ms)
    ms["recovery_all_clear_streak"] = streak
    ms["recovery_clear_since"] = clear_since

    new_phase = phase_decision.get("new_phase", current_phase)
    belief_update = apply_belief_state_update(
        mission_id,
        event_phase=new_phase,
        event_phase_iri=phase_decision.get("event_phase_iri", ""),
        alert_level=phase_decision.get("alert_level", "none"),
        top_evidence=phase_decision.get("top_evidence") or [],
        state_diff=phase_decision.get("state_diff"),
    )

    phase_update: Dict[str, Any] = {"ok": False, "skipped": True}
    deactivate_result: Dict[str, Any] = {"ok": False, "skipped": True}
    if fold_result.get("folded"):
        phase_update = update_mission_phase(
            mission_id,
            new_phase="folded",
            fips=ctx.get("fips") or ms.get("fips") or "",
            county_name=ms.get("county") or "",
            aoi_id=ms.get("aoi_id"),
        )
        deactivate_result = deactivate_mission(
            mission_id, reason="recovery_continuous_clear"
        )
        ms["current_event_phase"] = "folded"
        ms["active"] = False
        ctx["monitoring_summary"] = phase_update.get(
            "monitoring_summary", ctx["monitoring_summary"]
        )
    elif phase_decision.get("phase_changed"):
        phase_update = update_mission_phase(
            mission_id,
            new_phase=new_phase,
            fips=ctx.get("fips") or ms.get("fips") or "",
            county_name=ms.get("county") or "",
            aoi_id=ms.get("aoi_id"),
        )
        ms["current_event_phase"] = new_phase
        ctx["monitoring_summary"] = phase_update.get("monitoring_summary", ctx["monitoring_summary"])

    _shadow_write_ontology_phase(mission_id, phase_decision, claims)

    county_flood_evidence = build_county_flood_evidence(result.get("county_flood_payload"))

    report_snap = build_report_snapshot(
        mission_id=mission_id,
        phase=new_phase,
        alert_level=phase_decision.get("alert_level", "none"),
        evidence_claims=claims,
        source_snapshots=result.get("source_snapshots") or [],
        phase_decision=phase_decision,
        source_records=result.get("source_records") or [],
        county_api_mapping=result.get("county_api_mapping"),
        county_flood_evidence=county_flood_evidence,
    )
    delta = compute_delta(report_snap, prior_report)
    save_coordinator_report(mission_id, report_snap)

    belief_state = {
        "state_id": belief_update.get("state_id"),
        "event_phase": new_phase,
        "event_phase_iri": phase_decision.get("event_phase_iri"),
        "alert_level": phase_decision.get("alert_level", "none"),
        "top_evidence": phase_decision.get("top_evidence") or [],
        "county_flood_confidence": county_flood_evidence.get("overall_confidence"),
        "api_evidence_weight": county_flood_evidence.get("weight_hint"),
    }

    merged: Dict[str, Any] = {
        **result,
        "mission_spec": ms,
        "monitoring_summary": ctx["monitoring_summary"],
        "belief_state": belief_state,
        "evidence_claims": claims,
        "article_evidence": article_evidence,
        "source_records": source_records,
        "county_flood_evidence": county_flood_evidence,
        "rejected_claims": extraction.get("rejected_claims", []),
        "extraction_failures": extraction.get("extraction_failures", []),
        "claims_accepted": extraction.get("claims_accepted", 0),
        "claims_extracted": extraction.get("claims_extracted", 0),
        "phase_decision": phase_decision,
        "recovery_all_clear_streak": streak,
        "recovery_clear_since": clear_since,
        "recovery_clear_elapsed_hours": fold_result.get(
            "recovery_clear_elapsed_hours", 0.0
        ),
        "mission_folded": bool(fold_result.get("folded")),
        "neo4j_claim_persistence": claim_persist,
        "neo4j_belief_update": belief_update,
        "neo4j_phase_update": phase_update,
        "neo4j_streak_update": streak_update,
        "neo4j_deactivate": deactivate_result,
        "report_delta": delta,
        "run_at": _utc_now(),
        "credibility_assessments": (result.get("credibility_assessments") or [])
        + (extraction.get("credibility_assessments") or []),
    }
    ledger_summary = _ontology_ledger_summary(mission_id)
    if ledger_summary:
        merged["ontology_ledger"] = ledger_summary
    _attach_coordinator_slack(merged)
    return merged


def cmd_extract_claims(args: argparse.Namespace) -> int:
    ctx = _phase_aware_context(_resolve_mission_context(args))
    if ctx.get("error"):
        _print_json(ctx)
        return 1
    prior = get_latest_source_snapshots(ctx["mission_id"])
    ms = ctx["mission_spec"]
    discovery = discover_sources_from_stub(
        ctx["monitoring_summary"],
        mission_id=ctx["mission_id"],
        county_fips=ctx.get("fips"),
        county_name=ms.get("county"),
        location=ms.get("location"),
        state=ms.get("state"),
    )
    fetch_result = fetch_source_records(discovery["source_records"], prior_by_source=prior)
    extraction = extract_claims_from_snapshots(
        fetch_result.get("source_snapshots") or [],
        monitoring_summary=ctx["monitoring_summary"],
        county=ms.get("county") or "",
        state=ms.get("state") or "",
        location=ms.get("location") or "",
        source_records=discovery["source_records"],
    )
    _print_json({**extraction, "mission_id": ctx["mission_id"]})
    return 0


def cmd_apply_phase_rules(args: argparse.Namespace) -> int:
    ctx = _phase_aware_context(_resolve_mission_context(args))
    if ctx.get("error"):
        _print_json(ctx)
        return 1
    claims = []
    if args.claims_file:
        with open(args.claims_file, encoding="utf-8") as f:
            payload = json.load(f)
            claims = payload.get("evidence_claims") or payload if isinstance(payload, list) else []
    current_phase = ctx["mission_spec"].get("current_event_phase", "early_watch")
    decision = evaluate_phase_transition(
        current_phase=current_phase,
        monitoring_summary=ctx["monitoring_summary"],
        evidence_claims=claims,
    )
    _print_json({"mission_id": ctx["mission_id"], **decision})
    return 0


def cmd_fetch_sources(args: argparse.Namespace) -> int:
    ctx = _resolve_mission_context(args)
    if ctx.get("error"):
        _print_json(ctx)
        return 1
    ctx["county_flood_source_mode"] = _source_mode_from_args(args)
    result = _run_monitor_cycle(ctx, source_mode=ctx["county_flood_source_mode"])
    _print_json(result)
    return 0 if result["sources_succeeded"] > 0 else 1


def cmd_run_heartbeat(args: argparse.Namespace) -> int:
    """Run due active missions (cron poller). Slack delivery is handled here."""
    deliver_slack = not getattr(args, "no_slack", False)
    missions = list_active_missions()
    run_at = _utc_now()
    if not missions:
        cron_idle = disable_heartbeat_cron_if_idle(0)
        if args.format == "json":
            _print_json(
                {
                    "ok": True,
                    "missions_total": 0,
                    "missions_ran": 0,
                    "missions_skipped": 0,
                    "run_at": run_at,
                    "cron": cron_idle,
                }
            )
        else:
            print(f"No active missions in Neo4j (cron run {run_at})")
            print(f"Heartbeat cron: {cron_idle.get('action')}")
        return 0

    ran: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    folded: List[Dict[str, Any]] = []

    trigger_alert_cache: Dict[str, List[Dict[str, Any]]] = {}
    for mission in missions:
        mission_id = mission.get("mission_id")
        try:
            trigger_decision = _consult_verification_trigger(
                mission, trigger_alert_cache
            )
        except Exception:
            # A trigger-stream defect must not deny the scheduled cadence.
            trigger_decision = None
        due, reason = _mission_cycle_due(mission, trigger_decision)
        if not due:
            # Nothing was authorized, so the consultation's own bookkeeping
            # (deduplication, armed windows) is safe to persist now.
            _persist_verification_trigger_state(mission, trigger_decision)
            skipped.append({"mission_id": mission_id, "reason": reason, **mission})
            continue
        ctx = get_mission_context(mission_id)
        if ctx.get("error"):
            errors.append(ctx)
            continue
        if not ctx.get("monitoring_summary"):
            errors.append(
                {
                    "error": "input_error",
                    "message": "monitoring_summary missing",
                    "mission_id": mission_id,
                }
            )
            continue
        result = _run_coordinator_cycle({**ctx, "county_flood_source_mode": "both"})
        # The authorized acquisition has run, so the fire it granted is now
        # genuinely consumed and the state may advance.
        _persist_verification_trigger_state(
            mission, trigger_decision, acquisition_ran=True
        )
        result["heartbeat_run"] = True
        result["cron_interval_minutes"] = 15
        result["cycle_due_reason"] = reason
        if trigger_decision is not None:
            result["verification_trigger"] = {
                "burst_active": trigger_decision["burst_active"],
                "burst_until": trigger_decision["burst_until"],
                "fired_immediate": trigger_decision["fired_immediate"],
                "new_qualifying_events": trigger_decision["new_qualifying_events"],
                "authority": trigger_decision["authority"],
            }
        result["county_under_monitor"] = mission.get("county") or ctx.get("mission_spec", {}).get("county")
        result["state_under_monitor"] = mission.get("state") or ctx.get("mission_spec", {}).get("state")
        _attach_coordinator_slack(result)
        if deliver_slack:
            slack_result = deliver_coordinator_slack(result)
            result["slack_delivery"] = slack_result
            if result.get("mission_folded"):
                county = result.get("county_under_monitor") or "unknown"
                state = result.get("state_under_monitor") or ""
                fold_msg = (
                    f"*Mission folded* — monitoring ended for {county}"
                    f"{', ' + state if state else ''} "
                    f"(mission `{mission_id}`). Consecutive due all-clear evaluations "
                    "spanned at least 48 hours without an intervening evaluated relapse."
                )
                fold_delivery = deliver_slack_message(fold_msg, kind="fold")
                result["fold_slack_delivery"] = fold_delivery
                folded.append({"mission_id": mission_id, "county": county, "state": state})
        ran.append({"mission_id": mission_id, "result": result})

    remaining = list_active_missions()
    cron_idle = disable_heartbeat_cron_if_idle(len(remaining))

    payload: Dict[str, Any] = {
        "ok": not errors,
        "run_at": run_at,
        "cron_interval_minutes": 15,
        "missions_total": len(missions),
        "missions_ran": len(ran),
        "missions_skipped": len(skipped),
        "missions_errored": len(errors),
        "missions_folded": folded,
        "skipped": skipped,
        "results": [r["result"] for r in ran],
        "errors": errors,
        "cron": cron_idle,
    }

    if args.format == "json":
        _print_json(payload)
        return 0 if not errors else 1

    blocks: List[str] = []
    for entry in ran:
        blocks.append(format_coordinator_report_with_slack_block(entry["result"]))
        slack_delivery = entry["result"].get("slack_delivery") or {}
        if slack_delivery.get("ok"):
            channel = slack_delivery.get("channel_id") or slack_channel_id()
            blocks.append(f"Slack: delivered to channel:{channel}")
        if entry["result"].get("mission_folded"):
            blocks.append(
                f"Mission folded: {entry['mission_id']} "
                f"(48-hour continuous-clear recovery period complete)"
            )
    if skipped:
        skip_lines = [
            f"- {s.get('mission_id')}: {s.get('reason')}" for s in skipped
        ]
        blocks.append("Skipped (not due):\n" + "\n".join(skip_lines))
    if errors:
        blocks.append(format_error_report(errors[0]))
    blocks.append(f"Heartbeat cron: {cron_idle.get('action')}")
    print("\n\n".join(blocks) if blocks else f"No due missions (cron run {run_at})")
    return 0 if not errors else 1


def cmd_list_missions(_: argparse.Namespace) -> int:
    missions = list_active_missions()
    out = []
    for mission in missions:
        due, reason = _mission_cycle_due(mission)
        out.append({**mission, "cycle_due": due, "cycle_reason": reason})
    _print_json({"count": len(out), "missions": out})
    return 0


def cmd_run_coordinator(args: argparse.Namespace) -> int:
    """Full coordinator chain: Neo4j resolve (auto-bootstrap) → discover → fetch → persist."""
    deliver_slack = not getattr(args, "no_slack", False)
    ctx = _resolve_or_bootstrap_mission_context(args)
    if ctx.get("error"):
        _print_coordinator_output(ctx, args.format, deliver_slack=False)
        return 1
    if not ctx.get("monitoring_summary"):
        err = {
            "error": "input_error",
            "message": "monitoring_summary required after bootstrap",
            "mission_id": ctx.get("mission_id"),
        }
        _print_coordinator_output(err, args.format, deliver_slack=False)
        return 1

    boot = ctx.get("bootstrap_result") or {}
    reused = bool(
        ctx.get("reused_active_mission")
        or boot.get("reused_active_mission")
        or boot.get("already_active")
    )

    ctx["county_flood_source_mode"] = "both"
    result = _run_coordinator_cycle(ctx)
    if boot and boot.get("auto_bootstrapped"):
        result["bootstrap_result"] = boot
        result["auto_bootstrapped"] = True
    if reused and not result.get("auto_bootstrapped"):
        result["reused_active_mission"] = True
        result["already_tracking_note"] = (
            f"Re-run for existing mission {result.get('mission_id')} (already tracking)."
        )

    cron_status = ensure_heartbeat_cron()
    result["heartbeat_cron"] = cron_status

    if result.get("mission_folded"):
        remaining = list_active_missions()
        result["heartbeat_cron_idle"] = disable_heartbeat_cron_if_idle(len(remaining))

    _print_coordinator_output(result, args.format, deliver_slack=deliver_slack)
    if args.format == "human" and result.get("already_tracking_note"):
        print(f"\n{result['already_tracking_note']}")
    if args.format == "human" and cron_status.get("ok"):
        print(
            f"\nHeartbeat cron: {cron_status.get('action')} "
            f"(job {cron_status.get('job_id') or 'n/a'})"
        )
    if args.format == "human" and result.get("mission_folded"):
        print("\nMission folded — monitoring ended for this county.")
    return 0 if result.get("sources_succeeded", 0) > 0 else 1


def cmd_run_analyst(args: argparse.Namespace) -> int:
    """Run coordinator cycle, produce situation summary, deliver analyst report to Slack."""
    deliver_slack = not getattr(args, "no_slack", False)
    ctx = _resolve_or_bootstrap_mission_context(args)
    if ctx.get("error"):
        _print_analyst_output(ctx, args.format, deliver_slack=False)
        return 1
    if not ctx.get("monitoring_summary"):
        err = {
            "error": "input_error",
            "message": "monitoring_summary required after bootstrap",
            "mission_id": ctx.get("mission_id"),
        }
        _print_analyst_output(err, args.format, deliver_slack=False)
        return 1
    ctx["county_flood_source_mode"] = "both"
    result = _run_coordinator_cycle(ctx)
    if ctx.get("bootstrap_result"):
        result["bootstrap_result"] = ctx["bootstrap_result"]
        result["auto_bootstrapped"] = True

    result["analyst_summary"] = format_situation_summary(result)
    _attach_analyst_slack(result)
    _print_analyst_output(result, args.format, deliver_slack=deliver_slack)
    return 0 if result.get("sources_succeeded", 0) > 0 else 1


def cmd_list_source_readings(args: argparse.Namespace) -> int:
    result = list_source_readings(
        mission_id=args.mission_id or None,
        fips=args.fips or None,
        county=args.county or None,
        state=args.state or None,
        source_id=args.source_id or None,
        limit=args.limit,
    )
    if result.get("error"):
        _print_json(result)
        return 1
    _print_json(result)
    return 0


def cmd_get_mission(args: argparse.Namespace) -> int:
    result = _resolve_mission_context(args)
    if result.get("error"):
        _print_json(result)
        return 1
    _print_json(result)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    if not args.fips:
        print(json.dumps({"error": "input_error", "message": "fips is required for bootstrap"}), file=sys.stderr)
        return 1
    health = check_neo4j_health()
    if not health.get("ok"):
        _print_json({"error": "neo4j_unavailable", "health": health})
        return 1
    result = bootstrap_mission(args.county, args.state, args.fips, mode=args.mode or "shadow_live")
    if not result.get("neo4j_verification", {}).get("ok"):
        _print_json({"error": "bootstrap_verification_failed", **result})
        return 1
    _print_json(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watcher.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-schema", help="Create Neo4j constraints")
    p_init.set_defaults(func=cmd_init_schema)

    p_sum = sub.add_parser("monitoring-summary", help="Print MonitoringSummary JSON")
    p_sum.add_argument("--hazard", required=True)
    p_sum.add_argument("--phase", required=True)
    p_sum.add_argument("--fips", required=True)
    p_sum.set_defaults(func=cmd_monitoring_summary)

    p_health = sub.add_parser("neo4j-health", help="Check Neo4j Bolt connectivity")
    p_health.set_defaults(func=cmd_neo4j_health)

    p_verify = sub.add_parser("verify-bootstrap", help="Verify MissionSpec nodes in Neo4j")
    p_verify.add_argument("--mission-id", required=True)
    p_verify.set_defaults(func=cmd_verify_bootstrap)

    p_boot = sub.add_parser("bootstrap", help="Bootstrap MissionSpec + BeliefState")
    p_boot.add_argument("--county", required=True)
    p_boot.add_argument("--state", required=True)
    p_boot.add_argument("--fips", default="")
    p_boot.add_argument("--mode", default="shadow_live")
    p_boot.set_defaults(func=cmd_bootstrap)

    p_get = sub.add_parser("get-mission", help="Load mission context from Neo4j")
    _add_mission_lookup_args(p_get)
    p_get.set_defaults(func=cmd_get_mission)

    p_cfd = sub.add_parser(
        "county-flood-data",
        help="County flood status via NOAA NWPS + USGS NWIS IV + USGS OGC — no Neo4j mission required",
    )
    _add_mission_lookup_args(p_cfd)
    _add_output_format_arg(p_cfd)
    p_cfd.add_argument(
        "--gauge-ids",
        default="",
        help="Comma-separated NWPS SHEF IDs (skip CSV county lookup)",
    )
    p_cfd.add_argument(
        "--site-ids",
        default="",
        help="Comma-separated USGS NWIS site numbers",
    )
    _add_county_flood_source_args(p_cfd)
    p_cfd.add_argument(
        "--discover-categories",
        action="store_true",
        help="Sample national gauges to discover NWPS floodCategory values",
    )
    p_cfd.set_defaults(func=cmd_county_flood_data)

    p_disc = sub.add_parser("discover-sources", help="Ontology stub scout — materialize T0 sources from JSON mock")
    _add_mission_lookup_args(p_disc)
    p_disc.set_defaults(func=cmd_discover_sources)

    p_fetch = sub.add_parser("fetch-sources", help="Fetch county-flood-data readings and persist to Neo4j")
    _add_mission_lookup_args(p_fetch)
    _add_county_flood_source_arg(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch_sources)

    p_coord = sub.add_parser(
        "run-coordinator",
        help="Full coordinator cycle: bootstrap mission, county-flood-data (both APIs), persist, phase",
    )
    _add_mission_lookup_args(p_coord)
    _add_output_format_arg(p_coord)
    p_coord.set_defaults(func=cmd_run_coordinator)

    p_analyst = sub.add_parser(
        "run-analyst",
        help="Coordinator cycle + plain-language situation summary + Slack delivery",
    )
    _add_mission_lookup_args(p_analyst)
    _add_output_format_arg(p_analyst)
    p_analyst.set_defaults(func=cmd_run_analyst)

    p_hb = sub.add_parser(
        "run-heartbeat",
        help="Run coordinator for all active missions whose phase interval is due",
    )
    _add_output_format_arg(p_hb)
    p_hb.set_defaults(func=cmd_run_heartbeat)

    p_list = sub.add_parser("list-missions", help="List active MissionSpec nodes and due status")
    p_list.set_defaults(func=cmd_list_missions)

    p_extract = sub.add_parser("extract-claims", help="Extract typed evidence claims from source snapshots")
    _add_mission_lookup_args(p_extract)
    p_extract.set_defaults(func=cmd_extract_claims)

    p_phase = sub.add_parser("apply-phase-rules", help="Evaluate phase transition rules against claims")
    _add_mission_lookup_args(p_phase)
    p_phase.add_argument("--claims-file", default="", help="JSON file with evidence_claims array")
    p_phase.set_defaults(func=cmd_apply_phase_rules)

    p_read = sub.add_parser("list-source-readings", help="List persisted source readings from Neo4j")
    _add_mission_lookup_args(p_read)
    p_read.add_argument("--source-id", default="")
    p_read.add_argument("--limit", type=int, default=20)
    p_read.set_defaults(func=cmd_list_source_readings)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"error": "cli_error", "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
