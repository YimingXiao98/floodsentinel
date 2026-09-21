from watcher.claims.confidence import build_confidence_breakdown, source_reliability_prior


def test_cold_start_delta_tier_higher_than_method_confidence():
    components = {
        "source": 0.75,
        "location": 0.85,
        "time": 0.70,
        "impact": 0.50,
    }
    result = build_confidence_breakdown(
        components,
        trust_tier_level=0,
        method="cold_start_delta",
        method_confidence=0.45,
        uncertainty_notes=["no rated threshold; delta-based"],
    )
    assert result["method"] == "cold_start_delta"
    assert result["method_confidence"] == 0.45
    assert result["source_reliability_prior"] > result["method_confidence"]
    assert result["overall_score"] > 0


def test_rated_threshold_method():
    components = {
        "source": 0.90,
        "location": 0.90,
        "time": 0.85,
        "impact": 0.88,
    }
    result = build_confidence_breakdown(
        components,
        trust_tier_level=0,
        method="rated_threshold",
        method_confidence=0.88,
    )
    assert result["method"] == "rated_threshold"
    assert result["source_reliability_prior"] == source_reliability_prior(0)
