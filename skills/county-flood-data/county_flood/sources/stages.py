"""Stage merge, flood category resolution, and county-level verdict logic."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

STAGE_KEYS = ("action_stage", "flood_stage", "moderate_stage", "major_stage")

FLOOD_CATEGORY_SCORES: Dict[str, float] = {
    "no_flooding": 0.0,
    "action": 1.0,
    "minor_flooding": 2.0,
    "moderate_flooding": 3.0,
    "major_flooding": 4.0,
    "record_flood": 5.0,
}

EXCLUDED_CATEGORIES = {
    "not_defined",
    "fcst_not_current",
    "low_water",
    "data_not_current",
    "out_of_service",
    "unknown",
    "none",
    "normal",
}


def flood_category_score(category: Optional[str]) -> Optional[float]:
    if not category:
        return None
    key = str(category).strip().lower()
    if key in EXCLUDED_CATEGORIES:
        return None
    if key in FLOOD_CATEGORY_SCORES:
        return FLOOD_CATEGORY_SCORES[key]
    if "flood" in key and key != "no_flooding":
        return 2.0
    return None

USGS_IV_BATCH_SIZE = 50

FLOODING_SCORE_THRESHOLD = 1.0  # action or worse


def clean_stage_value(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number in (-999, -9999):
        return None
    if number <= 0:
        return None
    return number


def stages_from_nwps_payload(payload: Dict[str, Any]) -> Dict[str, Optional[float]]:
    cats = (payload.get("flood") or {}).get("categories") or {}
    return {
        "action_stage": clean_stage_value((cats.get("action") or {}).get("stage")),
        "flood_stage": clean_stage_value((cats.get("minor") or {}).get("stage")),
        "moderate_stage": clean_stage_value((cats.get("moderate") or {}).get("stage")),
        "major_stage": clean_stage_value((cats.get("major") or {}).get("stage")),
    }


def merge_stages(
    csv_stages: Dict[str, Optional[float]],
    api_stages: Dict[str, Optional[float]],
) -> Tuple[Dict[str, Optional[float]], str]:
    """Merge stage dicts; CSV wins on conflict. Returns (merged, source label)."""
    merged: Dict[str, Optional[float]] = {}
    csv_used = False
    api_used = False
    for key in STAGE_KEYS:
        csv_val = csv_stages.get(key)
        api_val = api_stages.get(key)
        if csv_val is not None:
            merged[key] = csv_val
            csv_used = True
        elif api_val is not None:
            merged[key] = api_val
            api_used = True
        else:
            merged[key] = None
    if csv_used and api_used:
        return merged, "mixed"
    if csv_used:
        return merged, "csv"
    if api_used:
        return merged, "nwps_api"
    return merged, "none"


def stages_sufficient_for_classification(stages: Dict[str, Optional[float]]) -> bool:
    return any(stages.get(key) is not None for key in STAGE_KEYS)


def max_stage_value(stages: Dict[str, Optional[float]]) -> Optional[float]:
    values = [stages.get(key) for key in STAGE_KEYS if stages.get(key) is not None]
    return max(values) if values else None


def datum_compatible(
    observed_primary: Optional[float],
    stages: Dict[str, Optional[float]],
) -> bool:
    """Reject stage-derived classification when obs magnitude is incompatible with thresholds."""
    if observed_primary is None:
        return False
    max_stage = max_stage_value(stages)
    if max_stage is None:
        return False
    if max_stage <= 0:
        return False
    ratio = observed_primary / max_stage
    if ratio > 10.0 or ratio < 0.01:
        return False
    return True


def gage_height_flood_category(
    gage_height_ft: Optional[float],
    *,
    action_stage: Optional[float],
    flood_stage: Optional[float],
    moderate_stage: Optional[float],
    major_stage: Optional[float],
) -> Optional[str]:
    if gage_height_ft is None:
        return None
    if major_stage is not None and gage_height_ft >= major_stage:
        return "major_flooding"
    if moderate_stage is not None and gage_height_ft >= moderate_stage:
        return "moderate_flooding"
    if flood_stage is not None and gage_height_ft >= flood_stage:
        return "minor_flooding"
    if action_stage is not None and gage_height_ft >= action_stage:
        return "action"
    if any(
        stage is not None
        for stage in (action_stage, flood_stage, moderate_stage, major_stage)
    ):
        return "no_flooding"
    return None


@dataclass
class ClassificationResult:
    category: Optional[str]
    score: Optional[float]
    method: str
    threshold_source: str
    unscored_reason: Optional[str] = None


def resolve_flood_category(
    *,
    observed_primary: Optional[float],
    nwps_category: Optional[str],
    action_stage: Optional[float],
    flood_stage: Optional[float],
    moderate_stage: Optional[float],
    major_stage: Optional[float],
    threshold_source: str = "none",
) -> ClassificationResult:
    """Classify by comparing observed stage to action/minor/mod/major thresholds.

    Prefer stage-derived classification when thresholds + observation are
    datum-compatible. Fall back to NWPS category, then observation-only.
    """
    stages = {
        "action_stage": action_stage,
        "flood_stage": flood_stage,
        "moderate_stage": moderate_stage,
        "major_stage": major_stage,
    }

    # Tier 1 — compare reading to action / minor / moderate / major thresholds
    if observed_primary is not None and stages_sufficient_for_classification(stages):
        if datum_compatible(observed_primary, stages):
            derived = gage_height_flood_category(
                observed_primary,
                action_stage=action_stage,
                flood_stage=flood_stage,
                moderate_stage=moderate_stage,
                major_stage=major_stage,
            )
            if derived is not None:
                return ClassificationResult(
                    category=derived,
                    score=flood_category_score(derived),
                    method="stage_derived",
                    threshold_source=threshold_source,
                )
        elif nwps_category and str(nwps_category).strip().lower() in EXCLUDED_CATEGORIES:
            return ClassificationResult(
                category=None,
                score=None,
                method="observation_only",
                threshold_source=threshold_source,
                unscored_reason="datum_mismatch",
            )

    # Tier 2 — NWPS category when stage thresholds unavailable / incompatible
    if nwps_category:
        score = flood_category_score(nwps_category)
        if score is not None:
            return ClassificationResult(
                category=str(nwps_category).strip().lower(),
                score=score,
                method="nwps_api_category",
                threshold_source=threshold_source,
            )

    # Tier 3 — observation only
    if observed_primary is not None:
        reason = "not_defined_no_thresholds"
        if nwps_category and str(nwps_category).strip().lower() in EXCLUDED_CATEGORIES:
            reason = "not_defined_no_thresholds"
        elif not stages_sufficient_for_classification(stages):
            reason = "no_thresholds"
        return ClassificationResult(
            category=None,
            score=None,
            method="observation_only",
            threshold_source=threshold_source,
            unscored_reason=reason,
        )

    return ClassificationResult(
        category=None,
        score=None,
        method="observation_only",
        threshold_source=threshold_source,
        unscored_reason="no_observation",
    )


def threshold_status_label(
    observed: Optional[float],
    *,
    action_stage: Optional[float] = None,
    flood_stage: Optional[float] = None,
    moderate_stage: Optional[float] = None,
    major_stage: Optional[float] = None,
    flood_category: Optional[str] = None,
) -> str:
    """Human status: below_action / action / minor / moderate / major (+ margin)."""
    category = flood_category or gage_height_flood_category(
        observed,
        action_stage=action_stage,
        flood_stage=flood_stage,
        moderate_stage=moderate_stage,
        major_stage=major_stage,
    )
    label_map = {
        "no_flooding": "below_action",
        "action": "action",
        "minor_flooding": "minor",
        "moderate_flooding": "moderate",
        "major_flooding": "major",
        "record_flood": "record",
    }
    status = label_map.get(str(category or "").lower(), "unclassified")
    if observed is None:
        return status

    # Margin to the next higher threshold (or how far below action).
    ladder = [
        ("action", action_stage),
        ("minor", flood_stage),
        ("mod", moderate_stage),
        ("major", major_stage),
    ]
    try:
        obs = float(observed)
    except (TypeError, ValueError):
        return status

    for name, thresh in ladder:
        if thresh is None:
            continue
        try:
            t = float(thresh)
        except (TypeError, ValueError):
            continue
        if obs < t:
            return f"{status} ({t - obs:.1f}ft below {name})"
    # At or above the highest available threshold.
    highest = next((name for name, thresh in reversed(ladder) if thresh is not None), None)
    if highest:
        return f"{status} (at/above {highest})"
    return status


def impact_weight_for_category(
    category: Optional[str],
    *,
    flood_state_score: Optional[float] = None,
    observed: Optional[float] = None,
    action_stage: Optional[float] = None,
    flood_stage: Optional[float] = None,
) -> float:
    """Impact confidence weight from threshold classification (0.55–0.95)."""
    score = flood_state_score
    if score is None:
        score = flood_category_score(category)
    if score is None:
        return 0.70

    # Base by flood band
    if score >= 4.0:
        base = 0.95
    elif score >= 3.0:
        base = 0.92
    elif score >= 2.0:
        base = 0.88
    elif score >= 1.0:
        base = 0.82
    else:
        base = 0.62
        # Approaching action: bump slightly when within 10% of action stage
        if (
            observed is not None
            and action_stage is not None
            and action_stage > 0
            and observed < action_stage
        ):
            proximity = observed / action_stage
            if proximity >= 0.90:
                base = 0.72
            elif proximity >= 0.80:
                base = 0.68
        elif (
            observed is not None
            and flood_stage is not None
            and flood_stage > 0
            and observed < flood_stage
            and (action_stage is None or observed < action_stage)
        ):
            proximity = observed / flood_stage
            if proximity >= 0.90:
                base = 0.70
    return round(min(0.95, base), 2)


def is_flooding_score(score: Optional[float]) -> bool:
    return score is not None and score >= FLOODING_SCORE_THRESHOLD


def score_label(score: Optional[float]) -> str:
    if score is None:
        return "no_flooding"
    if score < 0.5:
        return "no_flooding"
    if score < 1.5:
        return "action"
    if score < 2.5:
        return "minor_flooding"
    if score < 3.5:
        return "moderate_flooding"
    if score < 4.5:
        return "major_flooding"
    return "record_flood"


def _confidence_from_scored_gauges(scored: List[Dict[str, Any]]) -> str:
    """Confidence from scored gauge count and classification method only."""
    if not scored:
        return "low"
    if len(scored) <= 2:
        return "medium"
    if any(g.get("classification_method") == "stage_derived" for g in scored):
        return "medium"
    if all(g.get("classification_method") == "nwps_api_category" for g in scored):
        return "high"
    return "medium"


def county_flood_max_gauges() -> Optional[int]:
    """Optional emergency cap via COUNTY_FLOOD_MAX_GAUGES; default 10."""
    raw = os.environ.get("COUNTY_FLOOD_MAX_GAUGES", "").strip()
    if not raw:
        return 10
    try:
        value = int(raw)
        return value if value > 0 else None
    except ValueError:
        return 10


def min_gauges_for_verdict() -> int:
    """Below this count of gauges-with-data, regional_verdict is insufficient_data."""
    raw = os.environ.get("COUNTY_FLOOD_MIN_GAUGES", "").strip()
    if not raw:
        return 5
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def apply_gauge_limit(items: List[Any]) -> Tuple[List[Any], bool]:
    """Apply COUNTY_FLOOD_MAX_GAUGES cap after priority sort (default 10)."""
    cap = county_flood_max_gauges()
    if cap is None or len(items) <= cap:
        return items, False
    return items[:cap], True


def regional_verdict_from_gauges(
    gauges: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute county verdict from scored gauges only; unscored gauges are excluded."""
    scored = [
        g for g in gauges
        if g.get("flood_state_score") is not None and not g.get("error")
    ]
    with_data = [g for g in gauges if not g.get("error") and g.get("observed_primary") is not None]
    unscored = [
        g for g in with_data
        if g.get("flood_state_score") is None
    ]

    scores = [float(g["flood_state_score"]) for g in scored]
    max_score = max(scores) if scores else 0.0
    avg_score = round(sum(scores) / len(scores), 3) if scores else None

    coverage = len(scored) / len(with_data) if with_data else 0.0
    is_flooding = any(is_flooding_score(s) for s in scores)

    # A verdict must rest on positive evidence for what it asserts, so the
    # coverage requirement is asymmetric:
    #
    #   flooding      one scorable gauge above action stage is evidence of
    #                 flooding no matter how few gauges exist. Sparse coverage
    #                 lowers confidence; it must never suppress a detection,
    #                 because a missed flood costs more than a thin one.
    #   no flooding   asserting an all-clear requires the coverage minimum,
    #                 counted over gauges reporting data.
    #   otherwise     insufficient_data. Absence of evidence is reported as
    #                 absence of evidence, not as absence of flooding.
    min_gauges = min_gauges_for_verdict()
    # A gauge without a compatible threshold cannot establish that it is below
    # flood stage.  Counting such a reading toward an all-clear silently turns
    # missing decision evidence into negative evidence, so the minimum applies
    # to fresh *scored* gauges.  Sparse counties remain one-gauge sensitive to a
    # positive but abstain instead of declaring a weakly observed all-clear.
    sparse = len(scored) < min_gauges

    if is_flooding:
        regional_verdict = score_label(max_score)
        evaluation_mode = "scored_gauges_sparse" if sparse else "scored_gauges"
        confidence = "low" if sparse else _confidence_from_scored_gauges(scored)
    elif not scored:
        # Gauges may be reporting, but without thresholds none can be judged.
        regional_verdict = "insufficient_data"
        evaluation_mode = "unscored_gauges"
        confidence = "low"
    elif sparse:
        regional_verdict = "insufficient_data"
        evaluation_mode = "insufficient_data"
        confidence = "low"
    else:
        regional_verdict = score_label(max_score)
        evaluation_mode = "scored_gauges"
        confidence = _confidence_from_scored_gauges(scored)

    flooding_gauges = [
        g for g in scored if is_flooding_score(g.get("flood_state_score"))
    ]

    methods: Dict[str, int] = {}
    for g in gauges:
        method = g.get("classification_method") or "unknown"
        methods[method] = methods.get(method, 0) + 1

    return {
        "regional_verdict": regional_verdict,
        "is_flooding": is_flooding,
        "max_flood_state": round(max_score, 3) if scored else 0.0,
        "average_flood_state": avg_score,
        "confidence": confidence,
        "evaluation_mode": evaluation_mode,
        "scoring_coverage": round(coverage, 3) if with_data else 0.0,
        "gauges_unscored": len(unscored),
        "verdict_gauge_count": len(scored),
        "gauges_excluded": len(unscored),
        "gauges_with_data": len(with_data),
        "min_gauges_required": min_gauges,
        "classification_methods": methods,
        "flooding_gauge_ids": [
            g.get("shef_id") or g.get("site_no") for g in flooding_gauges
        ],
    }
