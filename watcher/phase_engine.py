"""Evaluate phase transition rules against evidence claims."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from watcher.claims.models import severity_rank
from watcher.config import county_flood_min_gauges, role_layer_enabled
from watcher.ontology.bridge import phase_to_iri

PHASE_ORDER = [
    "early_watch",
    "elevated_watch",
    "imminent",
    "active_incident",
    "recovery",
    "folded",
]
TERMINAL_PHASES = frozenset({"folded"})
# Autonomous folding requires due all-clear evaluations whose first and latest
# timestamps span this many hours, with no intervening evaluated relapse.
# Elapsed time, rather than a cycle count, prevents the nominal horizon from
# changing when cadence changes or a fixed-cadence counterfactual is tested.
# It does not observe conditions between acquisitions or remove fold overshoot.
# The former two-cycle rule represented only 12 elapsed hours at the recovery
# cadence and folded the Hillsborough replay before flooding recurred.
RECOVERY_MIN_CLEAR_HOURS_TO_FOLD = 48.0

ALERT_FROM_SEVERITY = {
    0: "none",
    1: "advisory",
    2: "watch",
    3: "warning",
    4: "emergency",
}

# Official county-flood API claims rank above supplementary/news in top_evidence.
OFFICIAL_API_CLAIM_BOOST = 0.25
SUPPLEMENTARY_TREND_PENALTY = 0.15
NEWS_CLAIM_PENALTY = 0.20


def _phase_index(phase: str) -> int:
    try:
        return PHASE_ORDER.index(phase)
    except ValueError:
        return 0


def is_terminal_phase(phase: str) -> bool:
    return phase in TERMINAL_PHASES


def evidence_supports_all_clear(evidence_claims: List[Dict[str, Any]]) -> bool:
    """True when official gauges are clear and no active warnings/flooding claims."""
    dummy_rule = {"condition": "all clear (severity < minor, no_flooding)"}
    return _evidence_supports_deescalation(evidence_claims, dummy_rule)


def _is_news_or_crowd_claim(claim: Dict[str, Any]) -> bool:
    value = claim.get("value") or {}
    if not isinstance(value, dict):
        return False
    if value.get("source_class") == "crowd":
        return True
    if value.get("evidence_tier") in ("T2", "T3"):
        return True
    sid = str(claim.get("source_id") or "")
    return sid.startswith("src_news_")


def _claim_matches_rule(claim: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    # News / weak-signal claims corroborate reports but must not drive phase changes.
    if _is_news_or_crowd_claim(claim):
        return False

    claim_type = claim.get("claim_type", "")
    rule_type = rule.get("claim_type", "")
    legacy = rule.get("legacy_claim_type", "")
    if claim_type not in (rule_type, legacy):
        return False

    condition = (rule.get("condition") or "").lower()
    severity = claim.get("severity") or "unknown"
    rank = severity_rank(severity)

    if "all clear" in condition or "severity < minor" in condition:
        return False

    if "severity >= minor" in condition or "stage >= 2" in condition:
        return rank >= severity_rank("minor")
    if "severity >= moderate" in condition:
        return rank >= severity_rank("moderate")
    if "severity >= severe" in condition:
        return rank >= severity_rank("severe")
    if "severity >= extreme" in condition:
        return rank >= severity_rank("extreme")
    return rank >= severity_rank("minor")


def _is_deescalation_rule(rule: Dict[str, Any], current_phase: str, current_idx: int) -> bool:
    if rule.get("from") != current_phase:
        return False
    target_idx = _phase_index(rule.get("to", ""))
    return target_idx == current_idx - 1


def _is_watershed_neighbour_claim(claim: Dict[str, Any]) -> bool:
    """Whether the gauge sits outside the county, in a watershed it shares."""
    value = claim.get("value") or {}
    if not isinstance(value, dict):
        return False
    return value.get("location_relation") == "shared_watershed"


def _official_gauge_claims(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gauges whose silence can be read as this county being clear.

    A gauge in a neighbouring county tells us water is rising somewhere
    connected, which is worth acting on, but its calm says nothing about here.
    The asymmetry is deliberate and matches the rest of the design: one scorable
    gauge is enough to report flooding, while standing an event down requires
    sources that actually observed the county.
    """
    hydrology_types = ("GaugeObservation", "gauge_rising")
    official: List[Dict[str, Any]] = []
    for claim in claims:
        if claim.get("claim_type") not in hydrology_types:
            continue
        if _is_watershed_neighbour_claim(claim):
            continue
        value = claim.get("value") or {}
        if not isinstance(value, dict):
            continue
        # Unscored and stale readings are still retained as evidence records,
        # but neither can prove the present is clear.
        if value.get("flood_state_score") is None:
            continue
        if value.get("reading_current") is False:
            continue
        if (
            value.get("source_class") == "official_api"
            or value.get("source_backend") in ("nwps", "usgs")
            or value.get("flood_state_score") is not None
        ):
            official.append(claim)
    return official


def _evidence_supports_deescalation(
    evidence_claims: List[Dict[str, Any]],
    rule: Dict[str, Any],
) -> bool:
    """Aggregate check: hydrology clear, no active warnings, official gauges present."""
    condition = (rule.get("condition") or "").lower()
    if "all clear" not in condition and "severity < minor" not in condition:
        return False

    warning_types = ("Warning", "warning")
    hydrology_types = ("GaugeObservation", "gauge_rising", "gauge_trend")
    minor_rank = severity_rank("minor")

    # Ignore news/crowd claims for de-escalation blockers; only official signals
    # should hold the phase up.
    if any(
        severity_rank(c.get("severity")) >= minor_rank
        for c in evidence_claims
        if c.get("claim_type") in warning_types + hydrology_types
        and not _is_news_or_crowd_claim(c)
    ):
        return False

    official_gauges = _official_gauge_claims(evidence_claims)
    distinct_gauges = {
        str(
            (claim.get("value") or {}).get("site_no")
            or (claim.get("value") or {}).get("shef_id")
            or claim.get("source_id")
            or claim.get("claim_id")
        )
        for claim in official_gauges
    }
    if len(distinct_gauges) < county_flood_min_gauges():
        return False

    for claim in official_gauges:
        value = claim.get("value") or {}
        score = value.get("flood_state_score")
        if score is not None:
            try:
                if float(score) >= 1.0:
                    return False
            except (TypeError, ValueError):
                pass
        category = str(value.get("flood_category") or "").strip().lower()
        if category and category not in ("no_flooding", "unknown", ""):
            return False

    return True


def _max_severity_rank(claims: List[Dict[str, Any]]) -> int:
    if not claims:
        return 0
    return max(severity_rank(c.get("severity")) for c in claims)


def compute_alert_level(claims: List[Dict[str, Any]]) -> str:
    # Alert level follows official evidence only; news is corroboration.
    official = [c for c in claims if not _is_news_or_crowd_claim(c)]
    rank = _max_severity_rank(official)
    return ALERT_FROM_SEVERITY.get(rank, "none")


def _evidence_sort_score(claim: Dict[str, Any]) -> float:
    """Weighted sort key: official API gauge observations preferred over news."""
    base = float(claim.get("overall_confidence") or 0)
    value = claim.get("value") or {}
    if not isinstance(value, dict):
        return base
    if value.get("source_class") == "official_api" or value.get("source_backend") in ("nwps", "usgs"):
        if claim.get("claim_type") in ("GaugeObservation", "gauge_rising"):
            if value.get("flood_state_score") is not None:
                return base + OFFICIAL_API_CLAIM_BOOST
            return base + OFFICIAL_API_CLAIM_BOOST * 0.5
    if value.get("trend_source") == "unscored_delta" or claim.get("claim_type") == "gauge_trend":
        return max(0.0, base - SUPPLEMENTARY_TREND_PENALTY)
    if _is_news_or_crowd_claim(claim):
        return max(0.0, base - NEWS_CLAIM_PENALTY)
    return base


def evaluate_phase_transition(
    *,
    current_phase: str,
    monitoring_summary: Dict[str, Any],
    evidence_claims: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return phase decision: stay, escalate, or deescalate (max one step)."""
    alert_level = compute_alert_level(evidence_claims)
    top_evidence = sorted(
        evidence_claims,
        key=lambda c: (severity_rank(c.get("severity")), _evidence_sort_score(c)),
        reverse=True,
    )[:5]
    top_ids = [c.get("claim_id") for c in top_evidence if c.get("claim_id")]

    # Role layer ablated: no phase reasoning at all. The mission holds its
    # starting phase for the whole event, so cadence and claim filtering never
    # adapt to the situation. Alert level is still derived from claim severity
    # so the naive baseline still reports what it sees.
    if not role_layer_enabled():
        return {
            "phase_decision": "stay",
            "current_phase": current_phase,
            "new_phase": current_phase,
            "alert_level": alert_level,
            "event_phase_iri": phase_to_iri(current_phase),
            "top_evidence": top_ids,
            "top_evidence_claims": top_evidence,
            "matched_rule": None,
            "state_diff": {},
            "phase_changed": False,
            "ablated": "no_role",
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    if is_terminal_phase(current_phase):
        return {
            "phase_decision": "stay",
            "current_phase": current_phase,
            "new_phase": current_phase,
            "alert_level": alert_level,
            "event_phase_iri": phase_to_iri(current_phase),
            "top_evidence": top_ids,
            "top_evidence_claims": top_evidence,
            "matched_rule": None,
            "state_diff": {},
            "phase_changed": False,
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    rules = monitoring_summary.get("phase_transition_rules") or []
    current_idx = _phase_index(current_phase)

    matched_rule: Optional[Dict[str, Any]] = None
    for rule in rules:
        if rule.get("from") != current_phase:
            continue
        condition = (rule.get("condition") or "").lower()
        # Streak folds are enforced in the coordinator cycle, not via claim match.
        if "streak" in condition:
            continue
        for claim in evidence_claims:
            if _claim_matches_rule(claim, rule):
                matched_rule = rule
                break
        if matched_rule:
            break

    new_phase = current_phase
    decision = "stay"
    if matched_rule:
        target = matched_rule.get("to", current_phase)
        target_idx = _phase_index(target)
        # Recovery is ordered after active_incident in the lifecycle, but a
        # renewed official warning or flood observation is a relapse, not a
        # de-escalation.  Handle it before the adjacent-phase logic so recovery
        # can safely resume incident monitoring instead of remaining trapped
        # until the mission folds.
        if current_phase == "recovery" and target in {
            "elevated_watch",
            "imminent",
            "active_incident",
        }:
            new_phase = target
            decision = "re_escalate"
        elif target_idx == current_idx + 1:
            new_phase = target
            decision = "escalate"
        elif target_idx > current_idx:
            # Never auto-jump into terminal folded via claim escalation.
            max_idx = len(PHASE_ORDER) - 2 if "folded" in PHASE_ORDER else len(PHASE_ORDER) - 1
            new_phase = PHASE_ORDER[min(current_idx + 1, max_idx)]
            decision = "escalate"

    if decision == "stay" and current_idx > 0:
        for rule in rules:
            if not _is_deescalation_rule(rule, current_phase, current_idx):
                continue
            if _evidence_supports_deescalation(evidence_claims, rule):
                matched_rule = rule
                new_phase = rule.get("to", current_phase)
                decision = "deescalate"
                break

    # Forward all-clear transitions (e.g. elevated/imminent/active → recovery).
    if decision == "stay":
        for rule in rules:
            if rule.get("from") != current_phase:
                continue
            condition = (rule.get("condition") or "").lower()
            if "streak" in condition:
                continue
            if "all clear" not in condition and "severity < minor" not in condition:
                continue
            target = rule.get("to") or current_phase
            if is_terminal_phase(target):
                continue
            if _phase_index(target) <= current_idx:
                continue
            if _evidence_supports_deescalation(evidence_claims, rule):
                matched_rule = rule
                new_phase = target
                decision = "deescalate"
                break

    # early_watch is pre-flood only — never return there after escalation.
    if decision == "deescalate" and new_phase == "early_watch":
        new_phase = "recovery"
        if matched_rule is not None:
            matched_rule = {**matched_rule, "to": "recovery", "rewritten_from": "early_watch"}

    state_diff: Dict[str, Any] = {}
    if decision in ("escalate", "re_escalate", "deescalate"):
        state_diff = {
            "event_phase": {"from": current_phase, "to": new_phase},
            "alert_level": {"from": compute_alert_level([]), "to": alert_level},
            "trigger_rule": matched_rule,
        }

    return {
        "phase_decision": decision,
        "current_phase": current_phase,
        "new_phase": new_phase,
        "alert_level": alert_level,
        "event_phase_iri": phase_to_iri(new_phase),
        "top_evidence": top_ids,
        "top_evidence_claims": top_evidence,
        "matched_rule": matched_rule,
        "state_diff": state_diff,
        "phase_changed": new_phase != current_phase,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def apply_recovery_fold_streak(
    *,
    current_phase: str,
    phase_decision: Dict[str, Any],
    evidence_claims: List[Dict[str, Any]],
    prior_streak: int,
    evaluated_at: Optional[datetime | str] = None,
    prior_clear_since: Optional[str] = None,
    minimum_clear_hours: float = RECOVERY_MIN_CLEAR_HOURS_TO_FOLD,
) -> Dict[str, Any]:
    """Track consecutive due all-clear evaluations over an elapsed horizon.

    ``prior_streak`` is retained as useful audit metadata, but it does not
    determine folding: otherwise changing a schedule would silently change the
    real-time safety horizon.  Replays must pass their virtual tick through
    ``evaluated_at``; live callers may omit it and use the phase decision time.
    """

    def _as_utc(value: Optional[datetime | str]) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            raw = str(value).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    now = _as_utc(evaluated_at or phase_decision.get("evaluated_at_utc"))
    if now is None:
        now = datetime.now(timezone.utc)
    working_phase = phase_decision.get("new_phase") or current_phase
    streak = int(prior_streak or 0)
    all_clear = evidence_supports_all_clear(evidence_claims)
    clear_since = _as_utc(prior_clear_since)

    # ``no_role`` promises that phase state is inert.  Keep any persisted
    # recovery metadata for auditability, but do not advance its clock or fold
    # a mission through this helper behind the ablation's back.
    if phase_decision.get("ablated") == "no_role":
        return {
            "recovery_all_clear_streak": streak,
            "recovery_clear_since": (
                clear_since.isoformat().replace("+00:00", "Z") if clear_since else None
            ),
            "recovery_clear_elapsed_hours": 0.0,
            "recovery_min_clear_hours_to_fold": float(minimum_clear_hours),
            "all_clear": all_clear,
            "folded": False,
            "phase_decision": dict(phase_decision),
        }

    if working_phase == "recovery" and all_clear:
        streak += 1
        if clear_since is None or clear_since > now:
            clear_since = now
    elif working_phase != "folded":
        streak = 0
        clear_since = None

    elapsed_hours = (
        max(0.0, (now - clear_since).total_seconds() / 3600.0)
        if clear_since is not None and working_phase == "recovery" and all_clear
        else 0.0
    )

    folded = False
    decision = dict(phase_decision)
    if (
        working_phase == "recovery"
        and all_clear
        and elapsed_hours >= max(0.0, float(minimum_clear_hours))
    ):
        folded = True
        decision = {
            **phase_decision,
            "phase_decision": "fold",
            "current_phase": current_phase,
            "new_phase": "folded",
            "event_phase_iri": phase_to_iri("folded"),
            "matched_rule": {
                "from": "recovery",
                "to": "folded",
                "condition": (
                    "due all-clear evaluations span >= "
                    f"{minimum_clear_hours:g} hours"
                ),
            },
            "state_diff": {
                "event_phase": {"from": current_phase, "to": "folded"},
                "recovery_all_clear_streak": streak,
                "recovery_clear_since": clear_since.isoformat().replace("+00:00", "Z"),
                "recovery_clear_elapsed_hours": round(elapsed_hours, 3),
            },
            "phase_changed": True,
        }

    return {
        "recovery_all_clear_streak": streak,
        "recovery_clear_since": (
            clear_since.isoformat().replace("+00:00", "Z") if clear_since else None
        ),
        "recovery_clear_elapsed_hours": round(elapsed_hours, 3),
        "recovery_min_clear_hours_to_fold": float(minimum_clear_hours),
        "all_clear": all_clear,
        "folded": folded,
        "phase_decision": decision,
    }
