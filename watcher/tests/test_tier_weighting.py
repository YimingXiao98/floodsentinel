"""Tests for tier-weighted claim admission.

The T0-T3 reliability tier was intended to gate decisions but reached them only
through the coarse binary carried by the per-extractor `source` sub-score.
SENTINEL_TIER_WEIGHT applies the finer gradient at admission, and is on by
default. Setting it to 0.0 restores the earlier behaviour.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from watcher.claims.validate import _tier_adjusted, filter_claims_by_phase
from watcher.claims.models import new_claim
from watcher.config import sentinel_tier_weight

_RULES = {
    "required_claim_types": [
        {"type": "warning", "minimum_confidence": 0.55},
        {"type": "GaugeObservation", "minimum_confidence": 0.55},
    ]
}


def _env(weight=None):
    environ = {k: v for k, v in os.environ.items() if k != "SENTINEL_TIER_WEIGHT"}
    if weight is not None:
        environ["SENTINEL_TIER_WEIGHT"] = str(weight)
    return mock.patch.dict(os.environ, environ, clear=True)


def _claim(claim_type="warning", *, prior, components):
    c = new_claim(
        claim_id=f"claim_{claim_type}_x",
        source_id="src_x",
        claim_type=claim_type,
        excerpt="e",
        evidence_url="https://example.test",
        location_text="Harris County, TX",
        observed_at="2017-08-27T12:00:00Z",
        severity="moderate",
        value={},
        confidence_components=components,
    )
    c["source_reliability_prior"] = prior
    return c


#: A strong news item: composite 0.594 against a 0.55 gate.
_NEWS = {"source": 0.53, "location": 0.65, "time": 0.55, "impact": 0.68}
#: An NWS alert: composite 0.850.
_ALERT = {"source": 0.90, "location": 0.90, "time": 0.85, "impact": 0.85}


class TestDefault(unittest.TestCase):
    def test_weight_defaults_on(self):
        with _env():
            self.assertEqual(sentinel_tier_weight(), 1.0)

    def test_scores_are_untouched_when_explicitly_disabled(self):
        with _env(0.0):
            self.assertEqual(_tier_adjusted(0.594, {"source_reliability_prior": 0.45}), 0.594)

    def test_invalid_value_falls_back_to_the_default(self):
        with _env("nonsense"):
            self.assertEqual(sentinel_tier_weight(), 1.0)

    def test_weight_is_clamped(self):
        with _env(5):
            self.assertEqual(sentinel_tier_weight(), 1.0)
        with _env(-2):
            self.assertEqual(sentinel_tier_weight(), 0.0)


class TestTierDiscount(unittest.TestCase):
    def test_low_tier_is_discounted_more_than_high_tier(self):
        with _env(0.5):
            t0 = _tier_adjusted(0.850, {"source_reliability_prior": 0.95})
            t2 = _tier_adjusted(0.594, {"source_reliability_prior": 0.45})
            self.assertGreater(0.850 - t0, -0.001)
            self.assertLess(t0, 0.850)
            self.assertLess(t2 / 0.594, t0 / 0.850)

    def test_claim_without_a_prior_is_untouched(self):
        with _env(1.0):
            self.assertEqual(_tier_adjusted(0.7, {}), 0.7)


class TestAdmissionEffect(unittest.TestCase):
    def test_news_admitted_when_tier_weighting_is_off(self):
        claims = [_claim(prior=0.45, components=_NEWS)]
        with _env(0.0):
            accepted, rejected = filter_claims_by_phase(claims, _RULES)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(rejected, [])

    def test_news_rejected_under_the_default(self):
        # Consequence of the default: a strong news item scores 0.594 and falls
        # to 0.267 against a 0.55 floor, so the T2 channel no longer supplies
        # admitted claims unless the floors are retuned for it.
        claims = [_claim(prior=0.45, components=_NEWS)]
        with _env():
            accepted, rejected = filter_claims_by_phase(claims, _RULES)
            self.assertEqual(accepted, [])
            self.assertIn("tier-adjusted", rejected[0]["rejection_reason"])

    def test_official_sources_survive_the_default(self):
        claims = [_claim(prior=0.95, components=_ALERT)]
        with _env():
            accepted, _ = filter_claims_by_phase(claims, _RULES)
            self.assertEqual(len(accepted), 1)

    def test_malformed_prior_cannot_raise_the_score(self):
        claims = [_claim(prior=5.0, components=_NEWS)]
        with _env():
            self.assertLessEqual(
                _tier_adjusted(0.594, {"source_reliability_prior": 5.0}), 0.594
            )

    def test_news_rejected_above_the_crossover(self):
        claims = [_claim(prior=0.45, components=_NEWS)]
        with _env(0.5):
            accepted, rejected = filter_claims_by_phase(claims, _RULES)
            self.assertEqual(accepted, [])
            self.assertIn("tier-adjusted", rejected[0]["rejection_reason"])

    def test_official_alert_survives_full_weighting(self):
        claims = [_claim(prior=0.95, components=_ALERT)]
        with _env(1.0):
            accepted, _ = filter_claims_by_phase(claims, _RULES)
            self.assertEqual(len(accepted), 1)

    def test_reported_confidence_is_not_double_discounted(self):
        # The discount applies to admission only, so the confidence shown to a
        # reader still reflects the composite the ontology computed.
        from watcher.claims.confidence import overall_confidence

        claims = [_claim(prior=0.95, components=_ALERT)]
        with _env(1.0):
            accepted, _ = filter_claims_by_phase(claims, _RULES)
            self.assertEqual(
                accepted[0]["overall_confidence"], overall_confidence(_ALERT)
            )


if __name__ == "__main__":
    unittest.main()
