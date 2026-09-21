"""Confidence scoring per CONFIDENCE_SCORING.md."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from watcher.config import (
    ablated_uniform_confidence,
    belief_layer_enabled,
    sentinel_reading_max_age_hours,
)

_TIER_RELIABILITY_PRIOR = {
    0: 0.95,
    1: 0.80,
    2: 0.45,  # curated news — corroboration only, below T1
    3: 0.35,
}


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _hours_since(timestamp: Optional[str], now: Optional[str] = None) -> Optional[float]:
    """Age of an observation at the moment it was fetched.

    ``now`` is the time the system looked. It defaults to the wall clock, which
    is correct in live operation, but a replay must pass the moment being
    replayed: measuring a 2017 reading against today's date reports every
    observation as years stale and flattens the freshness term to a constant.
    """
    dt = _parse_time(timestamp)
    if not dt:
        return None
    reference = _parse_time(now) if now else None
    if reference is None:
        reference = datetime.now(timezone.utc)
    return (reference - dt).total_seconds() / 3600.0


def score_t0_claim(
    *,
    county_match: bool,
    has_timestamp: bool,
    severity_present: bool,
    trust_tier_level: int = 0,
) -> Dict[str, float]:
    """T0 official sources: high base with small adjustments."""
    source = 0.90 if trust_tier_level == 0 else 0.75
    location = 0.90 if county_match else 0.55
    time_conf = 0.85 if has_timestamp else 0.70
    impact = 0.80 if severity_present else 0.60
    return {
        "source": round(source, 2),
        "location": round(location, 2),
        "time": round(time_conf, 2),
        "impact": round(impact, 2),
    }


def score_usgs_hydrology_claim(
    *,
    county_match: bool,
    observation_time: Optional[str] = None,
    gage_height_ft: Optional[float] = None,
    streamflow_cfs: Optional[float] = None,
    trust_tier_level: int = 0,
    flood_category: Optional[str] = None,
    flood_state_score: Optional[float] = None,
    action_stage: Optional[float] = None,
    flood_stage: Optional[float] = None,
    now: Optional[str] = None,
) -> Dict[str, float]:
    """Confidence for USGS/NWPS gauge observations.

    Impact weight is driven by action/minor/mod/major threshold classification.
    """
    source = 0.90 if trust_tier_level == 0 else 0.75
    location = 0.90 if county_match else 0.55

    hours = _hours_since(observation_time, now)
    if hours is not None:
        if hours < 2:
            time_conf = 0.90
        elif hours < 6:
            time_conf = 0.85
        else:
            time_conf = 0.70
    elif observation_time:
        time_conf = 0.85
    else:
        time_conf = 0.70

    impact = _threshold_impact_weight(
        flood_category=flood_category,
        flood_state_score=flood_state_score,
        observed=gage_height_ft,
        action_stage=action_stage,
        flood_stage=flood_stage,
        streamflow_cfs=streamflow_cfs,
    )

    return {
        "source": round(source, 2),
        "location": round(location, 2),
        "time": round(time_conf, 2),
        "impact": round(impact, 2),
    }


def _threshold_impact_weight(
    *,
    flood_category: Optional[str],
    flood_state_score: Optional[float],
    observed: Optional[float],
    action_stage: Optional[float],
    flood_stage: Optional[float],
    streamflow_cfs: Optional[float],
) -> float:
    """Map threshold band → impact weight (higher when at/above flood stages)."""
    score = flood_state_score
    if score is None and flood_category:
        cat = str(flood_category).strip().lower()
        score = {
            "no_flooding": 0.0,
            "action": 1.0,
            "minor_flooding": 2.0,
            "moderate_flooding": 3.0,
            "major_flooding": 4.0,
            "record_flood": 5.0,
        }.get(cat)

    if score is not None:
        if score >= 4.0:
            return 0.95
        if score >= 3.0:
            return 0.92
        if score >= 2.0:
            return 0.88
        if score >= 1.0:
            return 0.82
        # Below action: slight bump when approaching action/minor
        base = 0.62
        if observed is not None and action_stage is not None and action_stage > 0 and observed < action_stage:
            proximity = observed / action_stage
            if proximity >= 0.90:
                return 0.72
            if proximity >= 0.80:
                return 0.68
        elif observed is not None and flood_stage is not None and flood_stage > 0 and observed < flood_stage:
            proximity = observed / flood_stage
            if proximity >= 0.90:
                return 0.70
        return base

    if observed is not None:
        return 0.75
    if streamflow_cfs is not None:
        return 0.70
    return 0.60


def score_nws_alert_claim(
    *,
    zone_validated: bool,
    area_desc_match: bool,
    has_geometry: bool,
    published_at: Optional[str] = None,
    severity: Optional[str] = None,
    trust_tier_level: int = 0,
    now: Optional[str] = None,
) -> Dict[str, float]:
    """Confidence for NWS flood/hydrology alerts."""
    source = 0.90 if trust_tier_level == 0 else 0.75

    if zone_validated:
        location = 0.90
    elif area_desc_match:
        location = 0.70
    else:
        location = 0.55
    if has_geometry:
        location = min(0.99, round(location + 0.05, 2))

    hours = _hours_since(published_at, now)
    if published_at:
        time_conf = 0.70 if hours is not None and hours > 12 else 0.85
    else:
        time_conf = 0.70

    sev = (severity or "unknown").lower()
    impact_map = {
        "extreme": 0.90,
        "severe": 0.85,
        "moderate": 0.82,
        "minor": 0.80,
    }
    impact = impact_map.get(sev, 0.60)

    return {
        "source": round(source, 2),
        "location": round(location, 2),
        "time": round(time_conf, 2),
        "impact": round(impact, 2),
    }


def score_article_claim(
    *,
    page_score: float,
    published_at: Optional[str] = None,
    indication: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, float]:
    """Confidence for T2 curated news articles (page_score weighted).

    Intentionally lower than T0 official API claims so news corroborates
    rather than dominating phase / ranking decisions.
    """
    score = max(0.0, min(1.0, float(page_score)))
    source = round(0.35 + 0.20 * score, 2)
    location = 0.65
    hours = _hours_since(published_at, now)
    if published_at and hours is not None:
        if hours <= 24:
            time_conf = 0.80
        elif hours <= 48:
            time_conf = 0.70
        else:
            time_conf = 0.55
    else:
        time_conf = 0.50
    ind = (indication or "").strip().lower()
    if ind in (
        "confirms_flood",
        "flooding",
        "flood",
        "yes",
        "positive",
        "confirmed",
    ):
        impact = round(0.50 + 0.20 * score, 2)
    elif ind in ("denies_or_clear", "no", "negative", "none"):
        impact = round(0.30 + 0.10 * score, 2)
    elif ind in ("suggests_flood_risk", "warning", "watch", "risk"):
        impact = round(0.40 + 0.15 * score, 2)
    else:
        impact = 0.0
    return {
        "source": source,
        "location": location,
        "time": time_conf,
        "impact": impact,
    }


#: Legacy two-term weighting, retained for components that predate
#: ``score_credibility`` and carry only source and location.
_CREDIBILITY_WEIGHTS = {"source": 0.5833, "location": 0.4167}


def overall_confidence(components: Dict[str, float]) -> float:
    """How likely the claim is to be true.

    Composed only of terms that bear on credibility: the reliability of the
    source, and whether the claim resolves to the place being monitored.

    Two things are deliberately excluded. Severity answers how bad the reported
    condition is, not how likely the report is correct; mixing it in ranks a
    certain but mild reading below an uncertain but severe one, so a request
    for high-confidence material silently returns the worst news rather than
    the most reliable. Age is excluded for a related reason: a timestamped
    reading does not become false as it ages, it stops describing the present,
    and currency is enforced by a validity window rather than by discounting a
    truth score. Both remain available, through ``severity_score`` and
    ``reading_age_hours``.
    """
    # Belief layer ablated: every claim carries the same confidence, so the
    # weighted composite cannot discriminate between sources.
    if not belief_layer_enabled():
        return ablated_uniform_confidence()
    weights = (
        _CREDIBILITY_TERM_WEIGHTS
        if "method" in components or "quality" in components
        else _CREDIBILITY_WEIGHTS
    )
    total = sum(w for k, w in weights.items() if k in components)
    if total <= 0:
        return 0.0
    return round(
        sum(w * components[k] for k, w in weights.items() if k in components) / total, 3
    )


#: How the flood state behind a claim was established. An authority declaring a
#: category is stronger evidence than a category we derived by comparing a
#: reading to a threshold, which is in turn stronger than a bare reading with no
#: category at all.
_METHOD_CREDIBILITY = {
    "nwps_api_category": 0.95,
    "stage_derived": 0.85,
    "observation_only": 0.55,
}

#: Where the flood threshold came from. A published NWS threshold table is
#: firmer ground than one read live from an API, which is firmer than none.
_THRESHOLD_CREDIBILITY = {"csv": 0.95, "nwps_api": 0.90, "mixed": 0.90, "none": 0.50}

#: What the publishing agency says about a reading. Real-time data is always
#: provisional, so ``P`` is the normal case rather than a mark against a gauge,
#: and it shares the value used when no qualifier is recorded. The term earns its
#: place on ``e``: during a severe event a damaged or obstructed sensor is
#: estimated rather than measured, and an estimate is weaker evidence.
#:
#: ``A`` and ``R`` describe a review that happens months after the fact. A
#: monitoring system never has them at the moment it must decide, so a replay
#: that reads today's approved status for a 2017 reading would be using
#: information the live system could not have had. `provisional_at_observation`
#: exists to collapse them back.
_QUALIFIER_CREDIBILITY = {"A": 1.00, "R": 0.95, "P": 0.85, "e": 0.70}

#: Ranked worst to best, so a reading carrying several codes is scored on the
#: weakest one: ``["P", "e"]`` is an estimate, whatever else accompanies it.
_QUALIFIER_RANK = ("e", "P", "R", "A")

#: Codes that only exist after post-hoc review.
_POST_REVIEW_QUALIFIERS = frozenset({"A", "R"})


def worst_qualifier(qualifiers: Any) -> Optional[str]:
    """The weakest code among those attached to one reading.

    USGS returns a list, and a reading may be both provisional and estimated.
    Scoring it on the weakest code keeps the term conservative.
    """
    if not qualifiers:
        return None
    if isinstance(qualifiers, str):
        codes = [qualifiers]
    else:
        try:
            codes = [str(q) for q in qualifiers]
        except TypeError:
            return None
    present = [c for c in _QUALIFIER_RANK if c in codes]
    if present:
        return present[0]
    return codes[0] if codes else None


def provisional_at_observation(qualifier: Optional[str]) -> Optional[str]:
    """Strip a review status the live system could not have known.

    Approval and revision are recorded long after an event. A retrospective run
    must score a reading as it stood when it was published, so a post-review
    code is reported as provisional instead. Anything else, including an
    estimate, was already attached at publication and is preserved.
    """
    if qualifier in _POST_REVIEW_QUALIFIERS:
        return "P"
    return qualifier

#: Weights of the credibility terms. Who reported it matters most, then whether
#: it is about the right place, then how the assertion was reached, then whether
#: the underlying value has been reviewed.
#: How the claim was tied to the county being monitored.
#:
#: The location term asks whether a claim resolves to the monitored area, so
#: what grades it is the strength of that resolution. A site returned by a query
#: keyed on the county's FIPS code is one the agency itself places in the
#: county. A row matched on the county's *name* is usually the same site, but
#: name matching is fallible in a way FIPS is not: duplicated county names
#: across states, and the independent-city cases where a "city" and a "County"
#: share a name and are different jurisdictions.
_LOCATION_BASIS = {
    "fips_verified": 0.92,
    "name_matched": 0.85,
    "shared_watershed": 0.65,
    "unresolved": 0.55,
}


_CREDIBILITY_TERM_WEIGHTS = {
    "source": 0.40,
    "location": 0.25,
    "method": 0.20,
    "quality": 0.15,
}


def score_credibility(
    *,
    trust_tier_level: int = 0,
    county_match: bool = True,
    classification_method: Optional[str] = None,
    threshold_source: Optional[str] = None,
    data_qualifier: Optional[str] = None,
    location_precision: Optional[float] = None,
    location_basis: Optional[str] = None,
) -> Dict[str, float]:
    """Credibility terms for a claim: how likely is what it asserts to be true.

    Every term answers that one question. How serious the reported condition is
    and how old the reading is are deliberately absent: the first is severity,
    reported separately, and the second is currency, enforced by a validity
    window rather than by discounting a truth score.

    The terms are drawn from information the pipeline already establishes: the
    reliability tier of the feed, whether the claim resolves to the monitored
    area, how the flood state was arrived at, and what the publishing agency
    says about the reading itself.
    """
    source = _TIER_RELIABILITY_PRIOR.get(trust_tier_level, 0.80)
    # An explicit precision wins, then how the claim was resolved to the county,
    # and only then the bare did-it-match flag.
    if location_precision is not None:
        location = max(0.0, min(1.0, location_precision))
    elif location_basis in _LOCATION_BASIS:
        location = _LOCATION_BASIS[location_basis]
    else:
        location = 0.90 if county_match else 0.55

    method = _METHOD_CREDIBILITY.get(str(classification_method or ""), 0.70)
    threshold = _THRESHOLD_CREDIBILITY.get(str(threshold_source or ""), 0.70)
    # A derived category is only as firm as the threshold it was derived from.
    if classification_method == "stage_derived":
        method = round(method * threshold / 0.95, 3)

    quality = _QUALIFIER_CREDIBILITY.get(
        str(worst_qualifier(data_qualifier) or ""), 0.85
    )

    return {
        "source": round(source, 3),
        "location": round(location, 3),
        "method": round(method, 3),
        "quality": round(quality, 3),
    }


def reading_age_hours(observed_at: Optional[str], now: Optional[str] = None) -> Optional[float]:
    """Age of a reading at the moment the system looked, in hours."""
    return _hours_since(observed_at, now)


def reading_is_current(
    observed_at: Optional[str], now: Optional[str] = None, max_age_hours: Optional[float] = None
) -> bool:
    """Whether a reading is recent enough to describe the present.

    A gauge that stops reporting keeps returning its last value, so without a
    validity window a frozen reading is treated as live evidence indefinitely.
    An unparseable or missing timestamp is treated as current, since refusing
    every claim that lacks one would be a larger failure than admitting it.
    """
    limit = sentinel_reading_max_age_hours() if max_age_hours is None else max_age_hours
    if limit is None or limit <= 0:
        return True
    age = _hours_since(observed_at, now)
    return True if age is None else age <= limit


def severity_score(components: Dict[str, float]) -> float:
    """How serious the reported condition is, on the same 0 to 1 scale.

    Kept apart from confidence so the two can be reported and acted on
    independently: a severe claim from a weak source is a lead to verify, not a
    fact to act on, and neither reading should be hidden inside a single number.
    """
    return round(float(components.get("impact", 0.0)), 3)


def apply_belief_ablation(value: float) -> float:
    """Collapse a directly-assigned confidence when the belief layer is off.

    Some claims (cold-start and unscored gauge observations) carry a fixed
    supplementary confidence instead of going through the weighted composite.
    Those assignments must respect the ablation too, otherwise the no_belief
    configuration still discriminates between claim kinds.
    """
    if not belief_layer_enabled():
        return ablated_uniform_confidence()
    return value


def source_reliability_prior(trust_tier_level: int = 0) -> float:
    # Belief layer ablated: reliability tiers collapse, so an official gauge and
    # a news article are treated as equally credible.
    if not belief_layer_enabled():
        return ablated_uniform_confidence()
    return _TIER_RELIABILITY_PRIOR.get(trust_tier_level, 0.80)


def build_confidence_breakdown(
    components: Dict[str, float],
    *,
    trust_tier_level: int = 0,
    method: str = "corroborated",
    method_confidence: Optional[float] = None,
    uncertainty_notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Decomposed confidence for ontology Belief records; overall_score unchanged."""
    overall = overall_confidence(components)
    return {
        "overall_score": overall,
        "source_reliability_prior": source_reliability_prior(trust_tier_level),
        "method": method,
        "method_confidence": (
            round(method_confidence, 3)
            if method_confidence is not None
            else overall
        ),
        "uncertainty_notes": list(uncertainty_notes or []),
    }


def credibility_for_claim(claim: Dict[str, Any]) -> Dict[str, Any]:
    comp = claim.get("confidence_components") or {}
    return {
        "credibility_assessment_id": f"cred_{claim.get('claim_id', 'unknown')}",
        "target_type": "claim",
        "target_id": claim.get("claim_id"),
        "source_confidence": comp.get("source", 0),
        "location_confidence": comp.get("location", 0),
        "time_confidence": comp.get("time", 0),
        "impact_confidence": comp.get("impact", 0),
        "overall_confidence": claim.get("overall_confidence", overall_confidence(comp)),
        "rationale": "T0 official source with decomposition scoring",
    }
