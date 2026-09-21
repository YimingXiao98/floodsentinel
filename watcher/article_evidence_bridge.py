"""Bridge flood-article-detector skill output into coordinator reports."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from watcher.seen_articles import (
    load_seen_article_urls,
    normalize_article_url,
    record_seen_articles,
)

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "flood-article-detector"
ARTICLE_CONFIDENCE_THRESHOLD = 0.75


def _ensure_skill_path() -> None:
    skill_dir = str(_SKILL_DIR)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)


def _high_confidence_articles(
    top_sources: List[Dict[str, Any]],
    *,
    threshold: float = ARTICLE_CONFIDENCE_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Return articles whose page_score exceeds the coordinator threshold."""
    out: List[Dict[str, Any]] = []
    for article in top_sources:
        try:
            score = float(article.get("page_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score > threshold:
            out.append(article)
    return out


def _filter_new_articles(
    articles: List[Dict[str, Any]],
    seen_urls: set[str],
) -> tuple[List[Dict[str, Any]], int]:
    """Drop articles already recorded as seen; return (new_articles, skipped_count)."""
    fresh: List[Dict[str, Any]] = []
    skipped = 0
    for article in articles:
        key = normalize_article_url(str(article.get("url") or ""))
        if key and key in seen_urls:
            skipped += 1
            continue
        fresh.append(article)
    return fresh, skipped


def _load_run_detection():
    """Import run_detection from the flood-article-detector skill package."""
    import importlib.util

    main_py = _SKILL_DIR / "main.py"
    spec = importlib.util.spec_from_file_location("flood_article_detector_main", main_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {main_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_detection


def fetch_article_evidence(
    *,
    county: str,
    state: str,
    fips: str = "",
    location: str = "",
    threshold: float = ARTICLE_CONFIDENCE_THRESHOLD,
) -> Dict[str, Any]:
    """Run flood-article-detector and return coordinator-mergeable article evidence.

    Previously selected article URLs for this county are loaded from disk and passed
    as exclude_urls so the detector does not re-fetch/re-score them. Newly selected
    articles above the detector page-score threshold are recorded after a successful
    run. The page score is a relevance/recency selector, not claim credibility.
    """
    _ensure_skill_path()
    county_fips = (fips or "").strip()
    seen_urls = load_seen_article_urls(county_fips) if county_fips else set()

    try:
        run_detection = _load_run_detection()
    except Exception as exc:
        return {
            "merge_key": "article_evidence",
            "skill": "flood-article-detector",
            "status": "error",
            "threshold": threshold,
            "detail": f"import failed: {exc}",
            "high_confidence_articles": [],
            "top_sources": [],
            "article_urls": [],
            "flood_outcome": None,
            "search_summary": None,
            "seen_urls_excluded": len(seen_urls),
            "articles_skipped_seen": 0,
            "articles_newly_recorded": 0,
        }

    try:
        payload = run_detection(
            location=location,
            county=county,
            state=state,
            fips=fips,
            disaster="flood",
            exclude_urls=sorted(seen_urls),
        )
    except Exception as exc:
        return {
            "merge_key": "article_evidence",
            "skill": "flood-article-detector",
            "status": "error",
            "threshold": threshold,
            "detail": str(exc),
            "high_confidence_articles": [],
            "flood_outcome": None,
            "search_summary": None,
            "seen_urls_excluded": len(seen_urls),
            "articles_skipped_seen": 0,
            "articles_newly_recorded": 0,
        }

    if payload.get("status") != "ok":
        return {
            "merge_key": "article_evidence",
            "skill": "flood-article-detector",
            "status": "error",
            "threshold": threshold,
            "detail": payload.get("detail") or "detection failed",
            "high_confidence_articles": [],
            "flood_outcome": payload.get("flood_outcome"),
            "search_summary": payload.get("search_summary"),
            "location": payload.get("location"),
            "seen_urls_excluded": len(seen_urls),
            "articles_skipped_seen": (payload.get("search_summary") or {}).get("skipped_seen", 0),
            "articles_newly_recorded": 0,
        }

    top_sources = payload.get("top_sources") or []
    # Defense in depth: also drop any seen URLs that slipped through discovery.
    top_sources, skipped_post = _filter_new_articles(top_sources, seen_urls)
    high_confidence = _high_confidence_articles(top_sources, threshold=threshold)
    newly_recorded = 0
    if county_fips and high_confidence:
        newly_recorded = record_seen_articles(county_fips, high_confidence)

    search_summary = dict(payload.get("search_summary") or {})
    discovery_skipped = int(search_summary.get("skipped_seen") or 0)
    search_summary["skipped_seen"] = discovery_skipped + skipped_post
    search_summary["seen_urls_excluded"] = len(seen_urls)
    search_summary["articles_newly_recorded"] = newly_recorded

    article_urls = [str(a.get("url")) for a in top_sources if a.get("url")]

    return {
        "merge_key": "article_evidence",
        "skill": "flood-article-detector",
        "status": "ok",
        "threshold": threshold,
        "generated_at": payload.get("generated_at"),
        "location": payload.get("location"),
        "search_summary": search_summary,
        "flood_outcome": payload.get("flood_outcome"),
        "high_confidence_articles": high_confidence,
        "top_sources": top_sources,
        "article_urls": article_urls,
        "articles_above_threshold": len(high_confidence),
        "seen_urls_excluded": len(seen_urls),
        "articles_skipped_seen": discovery_skipped + skipped_post,
        "articles_newly_recorded": newly_recorded,
    }
