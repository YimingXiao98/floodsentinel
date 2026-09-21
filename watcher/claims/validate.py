"""Validate and filter evidence claims."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from watcher.claims.confidence import overall_confidence
from watcher.config import belief_layer_enabled, sentinel_tier_weight
from watcher.claims.models import CLAIM_TYPE_IRI


REQUIRED_CLAIM_KEYS = {
    "claim_id",
    "source_id",
    "claim_type",
    "excerpt",
    "evidence_url",
    "confidence_components",
}


def validate_claim(claim: Dict[str, Any]) -> Tuple[bool, str]:
    missing = REQUIRED_CLAIM_KEYS - set(claim.keys())
    if missing:
        return False, f"missing keys: {sorted(missing)}"
    if claim["claim_type"] not in CLAIM_TYPE_IRI and not claim.get("claim_type_iri"):
        return False, f"unknown claim_type: {claim['claim_type']}"
    comp = claim.get("confidence_components") or {}
    # A claim must carry the terms its confidence was actually built from.
    # Credibility is always composed of who reported it and whether it concerns
    # the monitored area; the remaining terms depend on the claim type, and
    # severity is recorded beside the confidence rather than inside it.
    for key in ("source", "location"):
        if key not in comp:
            return False, f"missing confidence component: {key}"
    return True, "ok"


def _tier_adjusted(overall: float, claim: Dict[str, Any]) -> float:
    """Discount a claim's admission score by its source reliability tier.

    Applied to the admission comparison only; the reported confidence is left
    untouched. Note that the ``source`` component of the composite already
    varies coarsely with tier, so this reinforces an existing signal rather
    than introducing an independent one, and the admission floors may warrant
    retuning alongside the weight.

    The prior and the result are clamped to [0, 1] so that a malformed value
    cannot raise a claim's admission score or push it out of range.
    """
    weight = sentinel_tier_weight()
    if weight <= 0:
        return overall
    prior = claim.get("source_reliability_prior")
    if prior is None:
        return overall
    try:
        prior_value = float(prior)
    except (TypeError, ValueError):
        return overall
    prior_value = max(0.0, min(1.0, prior_value))
    adjusted = overall * (1.0 - weight * (1.0 - prior_value))
    return round(max(0.0, min(1.0, adjusted)), 4)


def filter_claims_by_phase(
    claims: List[Dict[str, Any]],
    monitoring_summary: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (accepted, rejected) against required_claim_types minimum_confidence."""
    rules = monitoring_summary.get("required_claim_types") or []
    min_by_type: Dict[str, float] = {}
    legacy_map: Dict[str, str] = {}
    for rule in rules:
        ctype = rule.get("type", "")
        legacy = rule.get("legacy_type", "")
        minimum = float(rule.get("minimum_confidence", 0.55))
        min_by_type[ctype] = minimum
        if legacy:
            min_by_type[legacy] = minimum
            legacy_map[legacy] = ctype

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for claim in claims:
        ok, reason = validate_claim(claim)
        if not ok:
            rejected.append({**claim, "rejection_reason": reason})
            continue
        overall = claim.get("overall_confidence")
        if overall is None:
            overall = overall_confidence(claim.get("confidence_components", {}))
            claim["overall_confidence"] = overall
        # ``no_belief`` is an ablation of epistemic admission, not a constant-
        # confidence admission policy.  Keep schema/type validation above, but
        # do not let the arbitrary uniform score (or its equally arbitrary
        # collapsed tier prior) determine which evidence reaches the system.
        if not belief_layer_enabled():
            accepted.append(claim)
            continue
        ctype = claim.get("claim_type", "")
        minimum = min_by_type.get(ctype, min_by_type.get(legacy_map.get(ctype, ""), 0.50))
        effective = _tier_adjusted(overall, claim)
        if effective < minimum:
            reason = f"below minimum_confidence {minimum}"
            if effective != overall:
                reason += f" (tier-adjusted {round(effective, 3)} from {overall})"
            rejected.append({**claim, "rejection_reason": reason})
            continue
        accepted.append(claim)
    return accepted, rejected
