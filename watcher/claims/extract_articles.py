"""Extract T2 ontology claims from flood-article-detector evidence."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, List, Optional

from watcher.claims.confidence import (
    build_confidence_breakdown,
    overall_confidence,
    score_article_claim,
)
from watcher.claims.models import new_claim

_ARTICLE_TRUST_TIER = 2


def _claim_type_for_indication(indication: str) -> Optional[str]:
    """Map detector vocabulary to positive ontology claims.

    Neutral pages do not assert anything, and a denial/all-clear is the inverse
    of a warning.  Until the claim model carries explicit polarity, omitting
    those two classes is safer than turning them into positive flood evidence.
    Legacy positive labels remain supported for stored detector output.
    """
    ind = (indication or "").strip().lower()
    if ind in (
        "confirms_flood",
        "flooding",
        "flood",
        "yes",
        "positive",
        "confirmed",
    ):
        return "inundation"
    if ind in ("suggests_flood_risk", "warning", "watch", "risk"):
        return "warning"
    return None


def _source_id_for_url(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"src_news_{digest}"


def build_article_source_records(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Synthetic SourceRecords for article claims (ledger DataSource writes)."""
    seen: Dict[str, Dict[str, Any]] = {}
    for claim in claims:
        sid = claim.get("source_id", "")
        if not sid or sid in seen:
            continue
        value = claim.get("value") or {}
        seen[sid] = {
            "source_id": sid,
            "source_name": value.get("title") or "News article",
            "source_url": value.get("url") or "",
            "source_type": "flood_article",
            "ontology_source_type": "WeakSignalSource",
            "source_type_iri": "ds:WeakSignalSource",
            "trust_tier": {"level": _ARTICLE_TRUST_TIER, "name": "T2"},
            "health_status": "verified",
            "resolution_strategy": "flood_article_detector",
            "discovery_hint": "flood_article_detector",
        }
    return list(seen.values())


def extract_article_claims(
    article_evidence: Dict[str, Any],
    *,
    county: str,
    state: str,
    location: str,
    mission_id: Optional[str] = None,
    now: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert selected articles with positive indications into T2 claims.

    Selection uses the detector's page relevance/recency score. The resulting
    claim receives a separate credibility score before admission.
    """
    _ = mission_id
    articles = article_evidence.get("high_confidence_articles") or []
    if not articles:
        return []

    claims: List[Dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        try:
            page_score = float(article.get("page_score") or 0)
        except (TypeError, ValueError):
            continue
        url = article.get("url") or ""
        title = article.get("title") or "Untitled"
        indication = article.get("indication") or "neutral"
        published = article.get("published_at")

        claim_type = _claim_type_for_indication(indication)
        if claim_type is None:
            continue

        components = score_article_claim(
            page_score=page_score,
            published_at=str(published) if published else None,
            indication=indication,
            now=now,
        )
        overall = overall_confidence(components)
        breakdown = build_confidence_breakdown(
            components,
            trust_tier_level=_ARTICLE_TRUST_TIER,
            method="corroborated",
            method_confidence=overall,
            uncertainty_notes=["T2 curated news; page_score weighted"],
        )
        source_id = _source_id_for_url(url) if url else f"src_news_{uuid.uuid4().hex[:12]}"
        # Stable claim id from URL so the same article is not re-persisted as a new claim.
        if url:
            claim_id = f"claim_art_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"
        else:
            claim_id = f"claim_art_{uuid.uuid4().hex[:12]}"
        excerpt = f"{title} ({indication}, score={page_score:.2f})"
        claim = new_claim(
            claim_id=claim_id,
            source_id=source_id,
            claim_type=claim_type,
            excerpt=excerpt,
            evidence_url=url,
            location_text=location or f"{county} County, {state}".strip(", "),
            observed_at=published,
            published_at=str(published) if published else None,
            severity="moderate" if claim_type == "inundation" else "minor",
            value={
                "title": title,
                "url": url,
                "page_score": page_score,
                "indication": indication,
                "published_at": published,
                "evidence_tier": "T2",
                "source_class": "crowd",
                "weight_hint": "low",
            },
            confidence_components=components,
        )
        claim["confidence_breakdown"] = breakdown
        claim["source_reliability_prior"] = breakdown["source_reliability_prior"]
        claims.append(claim)

    return claims
