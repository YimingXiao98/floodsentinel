"""Safety-constrained verification trigger shared by live and replay schedulers.

An independently available official warning stream may *request* an immediate
official-data verification acquisition and a bounded, temporarily faster
acquisition cadence (a "verification burst"). That is the trigger's entire
authority. It never declares flooding, never changes the public alert level,
never establishes an all-clear, never folds recovery, and never substitutes
for official hydrologic evidence: the functions in this module return
scheduling decisions only, and nothing here constructs, admits, or scores an
evidence claim. Flood verdict, phase, alert, de-escalation, and recovery
decisions continue to run exclusively on the official gauge/warning claim path
in :mod:`watcher.phase_engine`.

Design constraints, shared verbatim by production and replay:

* **One decision function.** ``evaluate_verification_trigger`` is a pure
  function of ``(state, events, now, policy)``. The live heartbeat passes the
  wall clock and the current official alert feed; replay passes the virtual
  clock and a frozen, checksum-bound VTEC trigger corpus. Neither caller adds
  scheduling logic of its own.
* **No lookahead.** An event participates only when its ``available_at``
  (the product transmission time, not the hazard begin time) is at or before
  ``now``. Defensively, any future-timestamped event is ignored and counted so
  a corpus defect surfaces in the audit trail instead of leaking information.
* **Stale events cannot start a burst.** A warning transmitted long ago whose
  burst window has already lapsed is recorded as seen but activates nothing;
  discovering an old warning late must not cause a burst now.
* **Deterministic and JSON-serializable state.** The persisted trigger state
  is a small dict of ISO-8601 strings and sorted keys, safe to store on a
  ``MissionSpec`` and to replay byte-identically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TRIGGER_STATE_SCHEMA = "sentinel-verification-trigger-state/1"

#: Qualifying (phenomena, significance) pairs: flash flood, areal flood, and
#: river flood *warnings*. Watches/advisories are configurable for evaluation
#: but are not part of the default policy.
DEFAULT_QUALIFYING: Tuple[Tuple[str, str], ...] = (
    ("FF", "W"),
    ("FA", "W"),
    ("FL", "W"),
)

#: Temporary acquisition cadence while a verification burst is active.
DEFAULT_BURST_INTERVAL_SECONDS = 3600

#: How long one qualifying event keeps the burst alive, anchored to the
#: event's availability time so the window is independent of poll cadence.
DEFAULT_BURST_DURATION_SECONDS = 21600

#: Retain seen-event keys for this long past first sight. Pruning is safe for
#: scheduling (a lapsed event can never start a burst) and exists only to bound
#: persisted state on long-lived missions.
_SEEN_RETENTION_SECONDS = 7 * 24 * 3600

#: First-sight stamp for persisted state that predates age tracking.
_LEGACY_SEEN_SENTINEL = "0001-01-01T00:00:00Z"


def _parse_iso(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TriggerPolicy:
    """Frozen parameters of the verification-trigger policy."""

    enabled: bool = False
    qualifying: Tuple[Tuple[str, str], ...] = DEFAULT_QUALIFYING
    burst_interval_seconds: int = DEFAULT_BURST_INTERVAL_SECONDS
    burst_duration_seconds: int = DEFAULT_BURST_DURATION_SECONDS

    def qualifies(self, phenomena: Optional[str], significance: Optional[str]) -> bool:
        return (
            str(phenomena or "").strip().upper(),
            str(significance or "").strip().upper(),
        ) in {(p.upper(), s.upper()) for p, s in self.qualifying}


def _parse_qualifying(raw: str) -> Tuple[Tuple[str, str], ...]:
    pairs: List[Tuple[str, str]] = []
    for token in raw.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if "." not in token:
            raise ValueError(f"qualifying trigger token must be PH.S: {token!r}")
        phenomena, significance = token.split(".", 1)
        if not phenomena or not significance:
            raise ValueError(f"qualifying trigger token must be PH.S: {token!r}")
        pairs.append((phenomena, significance))
    if not pairs:
        raise ValueError("qualifying trigger set must not be empty")
    return tuple(pairs)


def trigger_policy_from_env() -> TriggerPolicy:
    """Build the policy from the environment; disabled unless explicitly on.

    Malformed numeric or qualifying values fall back to the defaults rather
    than silently disabling the feature, matching the clamped-parse idiom in
    :mod:`watcher.config`.
    """
    enabled = os.environ.get("SENTINEL_VERIFICATION_TRIGGER", "").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )
    qualifying = DEFAULT_QUALIFYING
    raw_qualifying = os.environ.get("SENTINEL_TRIGGER_QUALIFYING", "").strip()
    if raw_qualifying:
        try:
            qualifying = _parse_qualifying(raw_qualifying)
        except ValueError:
            qualifying = DEFAULT_QUALIFYING

    def _positive_int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            return default
        # "inf"/"nan" parse as floats but cannot become an interval; int(inf)
        # raises OverflowError, which would otherwise escape into the caller.
        if value != value or value in (float("inf"), float("-inf")):
            return default
        try:
            return max(60, int(value))
        except (OverflowError, ValueError):
            return default

    return TriggerPolicy(
        enabled=enabled,
        qualifying=qualifying,
        burst_interval_seconds=_positive_int(
            "SENTINEL_TRIGGER_BURST_INTERVAL_SECONDS", DEFAULT_BURST_INTERVAL_SECONDS
        ),
        burst_duration_seconds=_positive_int(
            "SENTINEL_TRIGGER_BURST_DURATION_SECONDS", DEFAULT_BURST_DURATION_SECONDS
        ),
    )


def trigger_anchor_mode_from_env() -> str:
    """Scheduling anchor semantics for the live heartbeat.

    ``reanchor`` (default) is the v3 behavior: every acquisition resets the
    cadence clock. ``additive`` is the v4 behavior: the base phase clock is
    credited only by base-due acquisitions, so verification acquisitions add
    information without moving the baseline schedule.
    """
    raw = os.environ.get("SENTINEL_TRIGGER_ANCHOR_MODE", "").strip().lower()
    return raw if raw in ("reanchor", "additive") else "reanchor"


def empty_trigger_state() -> Dict[str, Any]:
    return {
        "schema": TRIGGER_STATE_SCHEMA,
        "seen_events": {},
        "seen_event_keys": [],
        "burst_windows": [],
        "last_burst_active": False,
        "burst_started_at": None,
        "last_immediate_fire_at": None,
        "last_base_acquisition_at": None,
        "immediate_fires": 0,
        "events_seen_total": 0,
    }


def _normalize_window(row: Any) -> Optional[Dict[str, str]]:
    if not isinstance(row, dict):
        return None
    try:
        start = _utc_iso(_parse_iso(str(row.get("start"))))
        until = _utc_iso(_parse_iso(str(row.get("until"))))
    except (TypeError, ValueError):
        return None
    if until <= start:
        return None
    return {
        "start": start,
        "until": until,
        "event_key": str(row.get("event_key") or ""),
    }


def normalize_trigger_state(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce persisted or absent state into the current schema.

    Missions bootstrapped before this feature carry no trigger state; they
    must behave exactly like a fresh mission with the feature switched on.
    """
    normalized = empty_trigger_state()
    if not isinstance(state, dict):
        return normalized
    seen: Dict[str, str] = {}
    raw_seen = state.get("seen_events")
    if isinstance(raw_seen, dict):
        for key, first_seen in raw_seen.items():
            if not key:
                continue
            try:
                seen[str(key)] = _utc_iso(_parse_iso(str(first_seen)))
            except (TypeError, ValueError):
                continue
    legacy = state.get("seen_event_keys")
    if isinstance(legacy, (list, tuple)):
        # State written before first-sight times were recorded carries no age,
        # so it is stamped with a sentinel that always prunes. Re-presenting a
        # pruned key is harmless: if its window has lapsed the stale check
        # rejects it, and if the window is still open the burst it would arm is
        # already active. A fixed sentinel keeps replay deterministic where a
        # wall-clock stamp would not.
        for key in legacy:
            if key:
                seen.setdefault(str(key), _LEGACY_SEEN_SENTINEL)
    normalized["seen_events"] = dict(sorted(seen.items()))
    normalized["seen_event_keys"] = sorted(seen)
    windows = state.get("burst_windows")
    if isinstance(windows, (list, tuple)):
        normalized["burst_windows"] = sorted(
            (w for w in map(_normalize_window, windows) if w is not None),
            key=lambda w: (w["start"], w["until"], w["event_key"]),
        )
    normalized["last_burst_active"] = bool(state.get("last_burst_active"))
    for key in ("burst_started_at", "last_immediate_fire_at", "last_base_acquisition_at"):
        value = state.get(key)
        if value:
            try:
                normalized[key] = _utc_iso(_parse_iso(str(value)))
            except ValueError:
                normalized[key] = None
    for key in ("immediate_fires", "events_seen_total"):
        try:
            normalized[key] = max(0, int(state.get(key) or 0))
        except (TypeError, ValueError):
            normalized[key] = 0
    return normalized


def _event_available_at(event: Dict[str, Any]) -> Optional[datetime]:
    raw = event.get("available_at")
    if not raw:
        return None
    try:
        return _parse_iso(str(raw))
    except ValueError:
        return None


def _event_begins_at(event: Dict[str, Any]) -> Optional[datetime]:
    raw = event.get("begins_at")
    if not raw:
        return None
    try:
        return _parse_iso(str(raw))
    except ValueError:
        return None


def evaluate_verification_trigger(
    *,
    state: Optional[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    now: datetime,
    policy: TriggerPolicy,
) -> Dict[str, Any]:
    """One consultation of the warning stream at ``now``.

    ``events`` are candidate official warning-stream records, each with an
    ``event_key`` (stable VTEC identity), ``phenomena``, ``significance``,
    ``available_at`` (product transmission time), and optionally ``begins_at``
    (the hazard begin time published inside that same product). The function
    is pure: it returns a new state and a scheduling decision without mutating
    inputs.

    A qualifying event arms one burst window
    ``[max(available_at, begins_at), max(available_at, begins_at) + duration)``.
    Both anchor fields are public at transmission time, so an advance-issued
    river flood warning arms a window over the period the forecaster said the
    hazard would exist, while a radar-indicated warning (begin at or before
    transmission) bursts immediately. Published *end* times are deliberately
    unused: the archive's expiry reflects post-hoc cancellations a live
    consumer could not have known, so the burst length is the fixed policy
    duration.

    The decision can only shorten the acquisition schedule. ``fired_immediate``
    requests one verification acquisition at this consultation and is raised
    only on the inactive-to-active burst transition, so continuing warnings
    during an already-active burst cost nothing beyond the burst cadence, and
    a warning discovered after its window lapsed activates nothing.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    previous = normalize_trigger_state(state)
    seen: Dict[str, str] = dict(previous["seen_events"])
    windows: List[Dict[str, str]] = list(previous["burst_windows"])
    burst_started_at = previous["burst_started_at"]
    now_iso = _utc_iso(now)

    def _active(rows: Iterable[Dict[str, str]]) -> bool:
        return any(row["start"] <= now_iso < row["until"] for row in rows)

    # The immediate fire marks the transition between *consultations*: a
    # pending (advance-issued) window that became active since the last
    # consultation must fire now, so the baseline is the previous
    # consultation's activity flag, not the prior windows evaluated at ``now``.
    was_active = bool(previous["last_burst_active"])

    new_qualifying: List[Dict[str, Any]] = []
    ignored_future = 0
    ignored_unavailable = 0
    duration = timedelta(seconds=int(policy.burst_duration_seconds))
    if policy.enabled:
        for event in events:
            key = str(event.get("event_key") or "").strip()
            if not key or key in seen:
                continue
            if not policy.qualifies(event.get("phenomena"), event.get("significance")):
                continue
            available_at = _event_available_at(event)
            if available_at is None:
                ignored_unavailable += 1
                continue
            if available_at > now:
                # No-lookahead guard: a correctly built caller never supplies
                # these; count them so a corpus defect is visible in the audit.
                ignored_future += 1
                continue
            seen[key] = now_iso
            begins_at = _event_begins_at(event)
            window_start = max(available_at, begins_at or available_at)
            window_until = window_start + duration
            record = {
                "event_key": key,
                "available_at": _utc_iso(available_at),
                "begins_at": _utc_iso(begins_at) if begins_at else None,
                "burst_window_start": _utc_iso(window_start),
                "burst_window_end": _utc_iso(window_until),
                "stale_at_first_sight": _utc_iso(window_until) <= now_iso,
            }
            new_qualifying.append(record)
            if _utc_iso(window_until) > now_iso:
                windows.append(
                    {
                        "start": _utc_iso(window_start),
                        "until": _utc_iso(window_until),
                        "event_key": key,
                    }
                )

    is_active = _active(windows)
    fired_immediate = bool(policy.enabled and not was_active and is_active)
    if fired_immediate:
        burst_started_at = now_iso
    burst_until = max(
        (row["until"] for row in windows if row["start"] <= now_iso < row["until"]),
        default=None,
    )
    # Lapsed windows can never reactivate; drop them from persisted state.
    windows = sorted(
        (row for row in windows if row["until"] > now_iso),
        key=lambda row: (row["start"], row["until"], row["event_key"]),
    )

    # Prune by the recorded first-sight time rather than by parsing the key,
    # so any identifier scheme (VTEC identity in replay, CAP message id live)
    # prunes alike. A re-presented pruned event is stale by construction and
    # cannot start a burst, so dropping it is safe for scheduling.
    retention_floor = _utc_iso(now - timedelta(seconds=_SEEN_RETENTION_SECONDS))
    retained_seen = {
        key: first_seen
        for key, first_seen in seen.items()
        if first_seen >= retention_floor
    }

    next_state = {
        "schema": TRIGGER_STATE_SCHEMA,
        "seen_events": dict(sorted(retained_seen.items())),
        "seen_event_keys": sorted(retained_seen),
        "burst_windows": windows,
        "last_burst_active": is_active,
        "burst_started_at": burst_started_at if is_active else None,
        "last_immediate_fire_at": (
            now_iso if fired_immediate else previous["last_immediate_fire_at"]
        ),
        "last_base_acquisition_at": previous["last_base_acquisition_at"],
        "immediate_fires": previous["immediate_fires"] + int(fired_immediate),
        "events_seen_total": previous["events_seen_total"] + len(new_qualifying),
    }
    return {
        "state": next_state,
        "policy_enabled": policy.enabled,
        "burst_active": is_active,
        "burst_until": burst_until,
        "fired_immediate": fired_immediate,
        "new_qualifying_events": new_qualifying,
        "ignored_future_events": ignored_future,
        "ignored_unavailable_events": ignored_unavailable,
        "effective_interval_cap_seconds": (
            int(policy.burst_interval_seconds) if is_active else None
        ),
        "authority": "acquisition_scheduling_only",
    }


#: Live NWS alert event names mapped to VTEC (phenomena, significance).
#: Follow-up statements are omitted: they continue an event rather than
#: opening one, and the burst window of the opening product already covers
#: them within the policy duration.
_LIVE_EVENT_NAME_TO_VTEC: Dict[str, Tuple[str, str]] = {
    "flash flood warning": ("FF", "W"),
    "flood warning": ("FL", "W"),
    "river flood warning": ("FL", "W"),
    "areal flood warning": ("FA", "W"),
    "flash flood watch": ("FF", "A"),
    "flood watch": ("FA", "A"),
    "areal flood watch": ("FA", "A"),
    "river flood watch": ("FL", "A"),
    "flood advisory": ("FA", "Y"),
    "areal flood advisory": ("FA", "Y"),
    "river flood advisory": ("FL", "Y"),
}


def candidate_events_from_live_alerts(
    alerts: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Map live NWS alert dicts into trigger candidate events.

    Two live-stream fidelity caveats, both to be reviewed before any
    production enablement:

    * **Event identity.** The live payload carries the CAP message id rather
      than the underlying VTEC event identity, so an updated statement for the
      same hazard can present as a new key and re-arm a window. The replay
      corpus uses true VTEC identity. Coarser deduplication can only add
      acquisitions, never remove them.
    * **Hazard begin time.** The burst window is anchored at
      ``max(available_at, begins_at)``, and live ``begins_at`` comes from the
      CAP ``onset`` property when the product publishes one, falling back to
      ``effective``. An alert that omits ``onset`` therefore anchors at
      transmission rather than at a later forecast hazard begin, so for an
      advance-issued river flood warning the live burst can cover an earlier
      period than the replayed policy would. This direction is not
      safety-relevant (official evidence still governs every decision), but it
      means live and replay timing coincide only for products that publish an
      onset.
    """
    out: List[Dict[str, Any]] = []
    for alert in alerts or []:
        if str(alert.get("status") or "Actual").strip().lower() not in ("", "actual"):
            continue
        mapped = _LIVE_EVENT_NAME_TO_VTEC.get(
            str(alert.get("event") or "").strip().lower()
        )
        if mapped is None:
            continue
        effective = alert.get("effective")
        key = str(alert.get("id") or "").strip()
        if not key or not effective:
            continue
        out.append(
            {
                "event_key": key,
                "phenomena": mapped[0],
                "significance": mapped[1],
                "available_at": effective,
                "begins_at": alert.get("onset") or effective,
            }
        )
    return out


def effective_due_interval_seconds(
    *,
    phase_interval_seconds: int,
    trigger_decision: Optional[Dict[str, Any]],
) -> int:
    """Cadence after applying the burst cap. The trigger can only shorten it."""
    interval = max(60, int(phase_interval_seconds))
    if not trigger_decision:
        return interval
    cap = trigger_decision.get("effective_interval_cap_seconds")
    if cap is None:
        return interval
    return max(60, min(interval, int(cap)))


def acquisition_due(
    *,
    now: datetime,
    last_acquisition_at: Optional[datetime],
    phase_interval_seconds: int,
    trigger_decision: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Shared due-ness rule for one poll instant.

    Returns ``(due, reason)`` with reason one of ``first_run``, ``scheduled``,
    ``verification_immediate``, ``verification_burst``, or ``not_due``. The
    attribution is per-acquisition: ``verification_*`` means this acquisition
    would not have been due at this instant under the phase cadence alone,
    given the same last-acquisition time.
    """
    if last_acquisition_at is None:
        return True, "first_run"
    elapsed = (now - last_acquisition_at).total_seconds()
    phase_interval = max(60, int(phase_interval_seconds))
    if trigger_decision and trigger_decision.get("fired_immediate"):
        if elapsed >= phase_interval:
            return True, "scheduled"
        return True, "verification_immediate"
    effective = effective_due_interval_seconds(
        phase_interval_seconds=phase_interval,
        trigger_decision=trigger_decision,
    )
    if elapsed >= phase_interval:
        return True, "scheduled"
    if elapsed >= effective:
        return True, "verification_burst"
    return False, "not_due"


def acquisition_due_additive(
    *,
    now: datetime,
    last_any_acquisition_at: Optional[datetime],
    last_base_acquisition_at: Optional[datetime],
    phase_interval_seconds: int,
    trigger_decision: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, bool]:
    """Non-re-anchoring due rule: the trigger adds acquisitions, never moves them.

    Two clocks. The **base clock** is the phase cadence, credited only by
    acquisitions that were due under the phase cadence itself, so given the
    same phase trajectory the base acquisitions land at exactly the instants
    the phase-adaptive policy would have chosen. Verification acquisitions are
    purely additive: an immediate fire requests one acquisition, and an active
    burst caps the gap since the last acquisition of *any* kind at the burst
    cadence. Because a verification acquisition never advances the base clock,
    the trigger cannot shift a scheduled acquisition off an alignment the base
    policy already had — the re-anchoring pathology the v3 held-out study
    diagnosed (Montgomery: more acquisitions, later observation).

    Returns ``(due, reason, credits_base_clock)``.
    """
    if last_base_acquisition_at is None:
        return True, "first_run", True
    phase_interval = max(60, int(phase_interval_seconds))
    if (now - last_base_acquisition_at).total_seconds() >= phase_interval:
        return True, "scheduled", True
    if trigger_decision:
        if trigger_decision.get("fired_immediate"):
            return True, "verification_immediate", False
        cap = trigger_decision.get("effective_interval_cap_seconds")
        if (
            cap is not None
            and last_any_acquisition_at is not None
            and (now - last_any_acquisition_at).total_seconds() >= max(60, int(cap))
        ):
            return True, "verification_burst", False
    return False, "not_due", False
