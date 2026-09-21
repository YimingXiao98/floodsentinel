"""Tests for phase transition and de-escalation logic."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watcher.claims.models import new_claim
from watcher.phase_engine import (
    RECOVERY_MIN_CLEAR_HOURS_TO_FOLD,
    apply_recovery_fold_streak,
    evaluate_phase_transition,
    is_terminal_phase,
)

MONITORING_PATH = Path(__file__).resolve().parents[1] / "ontology" / "stub_monitoring.json"


def _gauge_claim(
    site: str,
    *,
    category: str = "no_flooding",
    score: float = 0.0,
    severity: str = "unknown",
) -> dict:
    return new_claim(
        claim_id=f"claim_{site}",
        source_id="usgs",
        claim_type="GaugeObservation",
        excerpt=f"usgs {site}; category={category}",
        evidence_url="https://example.com",
        location_text="Harris County, TX",
        observed_at="2026-06-29T03:00:00Z",
        severity=severity,
        value={
            "site_no": site,
            "flood_category": category,
            "flood_state_score": score,
            "source_class": "official_api",
            "source_backend": "usgs",
        },
        overall_confidence=0.89,
    )


def _official_warning_claim(*, severity: str = "minor") -> dict:
    return new_claim(
        claim_id="claim_nws_warning",
        source_id="src_county_flood_nws_alerts_48201",
        claim_type="warning",
        excerpt="NWS flood warning for Harris County",
        evidence_url="https://api.weather.gov/alerts/active",
        location_text="Harris County, TX",
        observed_at="2026-06-29T03:00:00Z",
        severity=severity,
        value={
            "event": "Flood Warning",
            "evidence_tier": "T0",
            "source_class": "official_feed",
        },
        overall_confidence=0.91,
    )


def _clear_gauge_claims(count: int = 5) -> list[dict]:
    return [_gauge_claim(f"clear-{index}") for index in range(count)]


class PhaseEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(MONITORING_PATH, encoding="utf-8") as fh:
            cls.monitoring = json.load(fh)

    def test_elevated_watch_deescalates_to_recovery_not_early_watch(self) -> None:
        """Post-flood all-clear goes to recovery; early_watch is pre-flood only."""
        claims = _clear_gauge_claims()
        result = evaluate_phase_transition(
            current_phase="elevated_watch",
            monitoring_summary=self.monitoring["elevated_watch"],
            evidence_claims=claims,
        )
        self.assertEqual(result["phase_decision"], "deescalate")
        self.assertEqual(result["new_phase"], "recovery")
        self.assertTrue(result["phase_changed"])
        self.assertEqual(result["alert_level"], "none")

    def test_elevated_watch_stays_when_gauge_still_flooding(self) -> None:
        claims = [
            _gauge_claim("08073000", category="minor_flooding", score=2.0, severity="minor"),
            _gauge_claim("08072500"),
        ]
        result = evaluate_phase_transition(
            current_phase="elevated_watch",
            monitoring_summary=self.monitoring["elevated_watch"],
            evidence_claims=claims,
        )
        self.assertEqual(result["phase_decision"], "stay")
        self.assertEqual(result["new_phase"], "elevated_watch")
        self.assertFalse(result["phase_changed"])

    def test_early_watch_escalates_on_minor_flooding(self) -> None:
        claims = [
            _gauge_claim("08073000", category="minor_flooding", score=2.0, severity="minor"),
        ]
        result = evaluate_phase_transition(
            current_phase="early_watch",
            monitoring_summary=self.monitoring["early_watch"],
            evidence_claims=claims,
        )
        self.assertEqual(result["phase_decision"], "escalate")
        self.assertEqual(result["new_phase"], "elevated_watch")
        self.assertTrue(result["phase_changed"])

    def test_news_claim_alone_does_not_escalate(self) -> None:
        news = new_claim(
            claim_id="claim_art_abc",
            source_id="src_news_abc",
            claim_type="warning",
            excerpt="Flooding reported downtown",
            evidence_url="https://news.example.com/flood",
            location_text="Harris County, TX",
            observed_at="2026-07-26T12:00:00Z",
            severity="minor",
            value={
                "title": "Flooding downtown",
                "url": "https://news.example.com/flood",
                "evidence_tier": "T2",
                "source_class": "crowd",
                "weight_hint": "low",
            },
            overall_confidence=0.55,
        )
        result = evaluate_phase_transition(
            current_phase="early_watch",
            monitoring_summary=self.monitoring["early_watch"],
            evidence_claims=[news],
        )
        self.assertEqual(result["phase_decision"], "stay")
        self.assertEqual(result["new_phase"], "early_watch")
        self.assertEqual(result["alert_level"], "none")

    def test_news_warning_does_not_block_deescalation(self) -> None:
        clear_gauges = _clear_gauge_claims()
        news_warning = new_claim(
            claim_id="claim_art_warn",
            source_id="src_news_warn",
            claim_type="warning",
            excerpt="Neighborhood flooding mentioned in article",
            evidence_url="https://news.example.com/harris-flood",
            location_text="Harris County, TX",
            observed_at="2026-07-26T12:00:00Z",
            severity="minor",
            value={
                "title": "Neighborhood flooding",
                "url": "https://news.example.com/harris-flood",
                "evidence_tier": "T2",
                "source_class": "crowd",
            },
            overall_confidence=0.6,
        )
        result = evaluate_phase_transition(
            current_phase="elevated_watch",
            monitoring_summary=self.monitoring["elevated_watch"],
            evidence_claims=clear_gauges + [news_warning],
        )
        self.assertEqual(result["phase_decision"], "deescalate")
        self.assertEqual(result["new_phase"], "recovery")

    def test_imminent_all_clear_goes_to_recovery(self) -> None:
        claims = _clear_gauge_claims()
        result = evaluate_phase_transition(
            current_phase="imminent",
            monitoring_summary=self.monitoring["imminent"],
            evidence_claims=claims,
        )
        self.assertEqual(result["phase_decision"], "deescalate")
        self.assertEqual(result["new_phase"], "recovery")

    def test_never_deescalate_to_early_watch_even_if_rule_says_so(self) -> None:
        """Hard guard: early_watch is pre-flood only."""
        claims = _clear_gauge_claims()
        bad_summary = {
            "phase": "elevated_watch",
            "phase_transition_rules": [
                {
                    "from": "elevated_watch",
                    "to": "early_watch",
                    "claim_type": "GaugeObservation",
                    "condition": "all clear (severity < minor, no_flooding)",
                }
            ],
        }
        result = evaluate_phase_transition(
            current_phase="elevated_watch",
            monitoring_summary=bad_summary,
            evidence_claims=claims,
        )
        self.assertEqual(result["phase_decision"], "deescalate")
        self.assertEqual(result["new_phase"], "recovery")

    def test_active_incident_moves_to_recovery_on_all_clear(self) -> None:
        claims = _clear_gauge_claims()
        result = evaluate_phase_transition(
            current_phase="active_incident",
            monitoring_summary=self.monitoring["active_incident"],
            evidence_claims=claims,
        )
        self.assertEqual(result["phase_decision"], "deescalate")
        self.assertEqual(result["new_phase"], "recovery")
        self.assertTrue(result["phase_changed"])

    def test_folded_is_terminal(self) -> None:
        self.assertTrue(is_terminal_phase("folded"))
        result = evaluate_phase_transition(
            current_phase="folded",
            monitoring_summary=self.monitoring["folded"],
            evidence_claims=[_gauge_claim("08073000")],
        )
        self.assertEqual(result["new_phase"], "folded")
        self.assertFalse(result["phase_changed"])

    def test_recovery_reescalates_on_renewed_flooding(self) -> None:
        claims = [
            _gauge_claim(
                "08073000",
                category="minor_flooding",
                score=2.0,
                severity="minor",
            )
        ]
        result = evaluate_phase_transition(
            current_phase="recovery",
            monitoring_summary=self.monitoring["recovery"],
            evidence_claims=claims,
        )
        self.assertEqual(result["phase_decision"], "re_escalate")
        self.assertEqual(result["new_phase"], "active_incident")
        self.assertTrue(result["phase_changed"])

    def test_sparse_or_stale_clear_evidence_cannot_deescalate(self) -> None:
        sparse = evaluate_phase_transition(
            current_phase="active_incident",
            monitoring_summary=self.monitoring["active_incident"],
            evidence_claims=_clear_gauge_claims(4),
        )
        self.assertEqual(sparse["phase_decision"], "stay")

        claims = _clear_gauge_claims()
        claims[-1]["value"]["reading_current"] = False
        stale = evaluate_phase_transition(
            current_phase="active_incident",
            monitoring_summary=self.monitoring["active_incident"],
            evidence_claims=claims,
        )
        self.assertEqual(stale["phase_decision"], "stay")

    def test_recovery_reescalates_on_official_warning_and_resets_clearance(self) -> None:
        claims = [_official_warning_claim()]
        decision = evaluate_phase_transition(
            current_phase="recovery",
            monitoring_summary=self.monitoring["recovery"],
            evidence_claims=claims,
        )
        self.assertEqual(decision["phase_decision"], "re_escalate")
        self.assertEqual(decision["new_phase"], "active_incident")
        self.assertEqual(decision["alert_level"], "advisory")

        result = apply_recovery_fold_streak(
            current_phase="recovery",
            phase_decision=decision,
            evidence_claims=claims,
            prior_streak=4,
            prior_clear_since="2026-06-27T03:00:00Z",
            evaluated_at="2026-06-29T02:45:00Z",
        )
        self.assertEqual(result["recovery_all_clear_streak"], 0)
        self.assertIsNone(result["recovery_clear_since"])
        self.assertEqual(result["recovery_clear_elapsed_hours"], 0.0)
        self.assertFalse(result["folded"])

    def test_recovery_fold_boundary_is_47_75_then_48_hours_not_count(self) -> None:
        self.assertEqual(RECOVERY_MIN_CLEAR_HOURS_TO_FOLD, 48.0)
        claims = _clear_gauge_claims()
        enter_recovery = evaluate_phase_transition(
            current_phase="active_incident",
            monitoring_summary=self.monitoring["active_incident"],
            evidence_claims=claims,
        )
        first = apply_recovery_fold_streak(
            current_phase="active_incident",
            phase_decision=enter_recovery,
            evidence_claims=claims,
            prior_streak=0,
            evaluated_at="2026-06-27T03:00:00Z",
        )
        self.assertEqual(first["recovery_clear_since"], "2026-06-27T03:00:00Z")
        self.assertEqual(first["recovery_clear_elapsed_hours"], 0.0)
        self.assertEqual(first["recovery_all_clear_streak"], 1)

        stay = evaluate_phase_transition(
            current_phase="recovery",
            monitoring_summary=self.monitoring["recovery"],
            evidence_claims=claims,
        )
        before = apply_recovery_fold_streak(
            current_phase="recovery",
            phase_decision=stay,
            evidence_claims=claims,
            # Even an implausibly high count cannot fold before the time bound.
            prior_streak=999,
            prior_clear_since=first["recovery_clear_since"],
            evaluated_at="2026-06-29T02:45:00Z",
        )
        self.assertEqual(before["recovery_clear_elapsed_hours"], 47.75)
        self.assertFalse(before["folded"])
        self.assertEqual(before["phase_decision"]["new_phase"], "recovery")

        boundary = apply_recovery_fold_streak(
            current_phase="recovery",
            phase_decision=stay,
            evidence_claims=claims,
            # Conversely, a low count folds once continuous elapsed time is met.
            prior_streak=1,
            prior_clear_since=first["recovery_clear_since"],
            evaluated_at="2026-06-29T03:00:00Z",
        )
        self.assertEqual(boundary["recovery_clear_elapsed_hours"], 48.0)
        self.assertTrue(boundary["folded"])
        self.assertEqual(boundary["phase_decision"]["new_phase"], "folded")
        self.assertEqual(
            boundary["phase_decision"]["matched_rule"]["condition"],
            "due all-clear evaluations span >= 48 hours",
        )

    def test_recovery_fold_time_is_invariant_to_one_or_twelve_hour_cadence(self) -> None:
        claims = _clear_gauge_claims()
        start = datetime(2026, 6, 27, 3, tzinfo=timezone.utc)

        for cadence_hours in (1, 12):
            with self.subTest(cadence_hours=cadence_hours):
                decision = evaluate_phase_transition(
                    current_phase="active_incident",
                    monitoring_summary=self.monitoring["active_incident"],
                    evidence_claims=claims,
                )
                result = apply_recovery_fold_streak(
                    current_phase="active_incident",
                    phase_decision=decision,
                    evidence_claims=claims,
                    prior_streak=0,
                    evaluated_at=start,
                )
                phase = "recovery"

                for elapsed in range(cadence_hours, 49, cadence_hours):
                    decision = evaluate_phase_transition(
                        current_phase=phase,
                        monitoring_summary=self.monitoring[phase],
                        evidence_claims=claims,
                    )
                    result = apply_recovery_fold_streak(
                        current_phase=phase,
                        phase_decision=decision,
                        evidence_claims=claims,
                        prior_streak=result["recovery_all_clear_streak"],
                        prior_clear_since=result["recovery_clear_since"],
                        evaluated_at=start + timedelta(hours=elapsed),
                    )
                    if elapsed < 48:
                        self.assertFalse(result["folded"])
                    phase = result["phase_decision"]["new_phase"]

                self.assertTrue(result["folded"])
                self.assertEqual(result["recovery_clear_elapsed_hours"], 48.0)
                self.assertEqual(phase, "folded")

    def test_recovery_gauge_relapse_resets_streak_and_clear_since(self) -> None:
        claims = [
            _gauge_claim(
                "08073000",
                category="minor_flooding",
                score=2.0,
                severity="minor",
            )
        ]
        decision = evaluate_phase_transition(
            current_phase="recovery",
            monitoring_summary=self.monitoring["recovery"],
            evidence_claims=claims,
        )
        self.assertEqual(decision["phase_decision"], "re_escalate")
        self.assertEqual(decision["new_phase"], "active_incident")
        result = apply_recovery_fold_streak(
            current_phase="recovery",
            phase_decision=decision,
            evidence_claims=claims,
            prior_streak=4,
            prior_clear_since="2026-06-27T03:00:00Z",
            evaluated_at="2026-06-29T02:45:00Z",
        )
        self.assertEqual(result["recovery_all_clear_streak"], 0)
        self.assertIsNone(result["recovery_clear_since"])
        self.assertEqual(result["recovery_clear_elapsed_hours"], 0.0)
        self.assertFalse(result["folded"])

    def test_recovery_source_contract_can_prove_clear_and_detect_relapse(self) -> None:
        source_types = {
            source["type"] for source in self.monitoring["recovery"]["required_source_types"]
        }
        self.assertTrue(
            {"NWPSSource", "USGSGaugeSource", "NWSSource"}.issubset(source_types)
        )

        fold_rule = next(
            rule
            for rule in self.monitoring["recovery"]["phase_transition_rules"]
            if rule["to"] == "folded"
        )
        self.assertEqual(
            fold_rule["condition"],
            "due all-clear evaluations span >= "
            f"{RECOVERY_MIN_CLEAR_HOURS_TO_FOLD:g} hours",
        )

    def test_no_role_ablation_cannot_fold_or_advance_clearance(self) -> None:
        claims = _clear_gauge_claims()
        decision = {
            "phase_decision": "stay",
            "current_phase": "recovery",
            "new_phase": "recovery",
            "phase_changed": False,
            "ablated": "no_role",
            "evaluated_at_utc": "2026-06-29T03:00:00Z",
        }
        result = apply_recovery_fold_streak(
            current_phase="recovery",
            phase_decision=decision,
            evidence_claims=claims,
            prior_streak=99,
            prior_clear_since="2026-06-20T03:00:00Z",
            evaluated_at="2026-06-29T03:00:00Z",
        )
        self.assertFalse(result["folded"])
        self.assertEqual(result["phase_decision"], decision)
        self.assertEqual(result["recovery_all_clear_streak"], 99)
        self.assertEqual(result["recovery_clear_since"], "2026-06-20T03:00:00Z")
        self.assertEqual(result["recovery_clear_elapsed_hours"], 0.0)

    def test_every_all_clear_phase_declares_a_gauge_source(self) -> None:
        for phase_name, summary in self.monitoring.items():
            has_gauge_clear_rule = any(
                rule.get("claim_type") == "GaugeObservation"
                and "all clear" in str(rule.get("condition", "")).lower()
                for rule in summary.get("phase_transition_rules", [])
            )
            if not has_gauge_clear_rule:
                continue
            with self.subTest(phase=phase_name):
                source_types = {
                    source["type"] for source in summary.get("required_source_types", [])
                }
                self.assertTrue(
                    {"NWPSSource", "USGSGaugeSource"}.issubset(source_types),
                    f"{phase_name} has a gauge all-clear rule without gauge sources",
                )


if __name__ == "__main__":
    unittest.main()
