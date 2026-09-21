"""Persist previously seen news article URLs per county to avoid re-retrieval."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

_STATE_DIR = Path(__file__).resolve().parent / "state"

# How long a URL stays "seen" and is skipped on rediscovery (default 7 days).
_DEFAULT_TTL_HOURS = 168


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def normalize_article_url(url: str) -> str:
    return (url or "").strip().lower().rstrip("/")


def seen_articles_ttl_hours() -> float:
    raw = os.environ.get("SEEN_ARTICLES_TTL_HOURS", "").strip()
    if not raw:
        return float(_DEFAULT_TTL_HOURS)
    try:
        return max(1.0, float(raw))
    except ValueError:
        return float(_DEFAULT_TTL_HOURS)


def _cache_path(county_fips: str) -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = (county_fips or "unknown").strip() or "unknown"
    return _STATE_DIR / f"seen_articles_{key}.json"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
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


def _load_raw(county_fips: str) -> Dict[str, Any]:
    path = _cache_path(county_fips)
    if not path.exists():
        return {"county_fips": county_fips, "articles": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"county_fips": county_fips, "articles": {}}
    if not isinstance(data, dict):
        return {"county_fips": county_fips, "articles": {}}
    articles = data.get("articles")
    if not isinstance(articles, dict):
        data["articles"] = {}
    return data


def _save_raw(county_fips: str, data: Dict[str, Any]) -> None:
    path = _cache_path(county_fips)
    data["county_fips"] = county_fips
    data["updated_at"] = _utc_now_iso()
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _is_fresh(entry: Dict[str, Any], *, now: datetime, ttl_hours: float) -> bool:
    seen_at = _parse_iso(entry.get("last_seen") or entry.get("first_seen"))
    if seen_at is None:
        return False
    age_h = (now - seen_at).total_seconds() / 3600.0
    return age_h <= ttl_hours


def load_seen_article_urls(
    county_fips: str,
    *,
    now: Optional[datetime] = None,
    ttl_hours: Optional[float] = None,
) -> Set[str]:
    """Return normalized URLs still within the seen TTL for this county."""
    now = now or _utc_now()
    ttl = seen_articles_ttl_hours() if ttl_hours is None else ttl_hours
    data = _load_raw(county_fips)
    out: Set[str] = set()
    for key, entry in (data.get("articles") or {}).items():
        if not isinstance(entry, dict):
            continue
        if _is_fresh(entry, now=now, ttl_hours=ttl):
            out.add(normalize_article_url(str(key)))
    return out


def record_seen_articles(
    county_fips: str,
    articles: Iterable[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> int:
    """Upsert article URLs into the seen store. Returns count newly recorded."""
    now = now or _utc_now()
    now_iso = now.isoformat().replace("+00:00", "Z")
    data = _load_raw(county_fips)
    store: Dict[str, Any] = data.setdefault("articles", {})
    new_count = 0

    for article in articles:
        if not isinstance(article, dict):
            continue
        url = normalize_article_url(str(article.get("url") or ""))
        if not url:
            continue
        existing = store.get(url)
        if not isinstance(existing, dict):
            store[url] = {
                "first_seen": now_iso,
                "last_seen": now_iso,
                "title": article.get("title") or "",
                "page_score": article.get("page_score"),
                "indication": article.get("indication"),
            }
            new_count += 1
        else:
            existing["last_seen"] = now_iso
            if article.get("title"):
                existing["title"] = article.get("title")
            if article.get("page_score") is not None:
                existing["page_score"] = article.get("page_score")
            if article.get("indication") is not None:
                existing["indication"] = article.get("indication")

    # Drop expired entries so the file does not grow forever.
    ttl = seen_articles_ttl_hours()
    pruned = {
        k: v
        for k, v in store.items()
        if isinstance(v, dict) and _is_fresh(v, now=now, ttl_hours=ttl)
    }
    data["articles"] = pruned
    _save_raw(county_fips, data)
    return new_count
