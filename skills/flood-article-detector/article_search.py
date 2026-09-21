"""Article discovery via Google News RSS, web search, and page fetch."""

from __future__ import annotations

import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from typing import Any, Dict, List, Optional, Tuple

from article_scorer import contains_disaster_keyword, is_blocked_url, is_within_publish_window, parse_datetime

_SERVICES_DIR = Path(__file__).resolve().parent.parent / "disaster-scout" / "services"
if str(_SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICES_DIR))

try:
    from web_explorer import _web_search_curl  # type: ignore
except ImportError:
    _web_search_curl = None  # type: ignore

_GEO_DIR = Path(__file__).resolve().parent.parent / "county-flood-data"
if str(_GEO_DIR) not in sys.path:
    sys.path.insert(0, str(_GEO_DIR))

try:
    from county_flood.geo import parse_location  # type: ignore
except ImportError:
    parse_location = None  # type: ignore

try:
    from county_resolver import resolve_county, resolve_from_prompt  # type: ignore
except ImportError:
    resolve_county = None  # type: ignore
    resolve_from_prompt = None  # type: ignore


def build_article_queries(county: str, state: str, disaster: str = "flood") -> List[str]:
    return [
        f'"{county} County" "{state}" {disaster} news',
        f'"{county}" "{state}" flash flood today',
        f'"{county}" "{state}" flooding roads evacuations',
        f'site:.gov "{county}" {disaster}',
        f'"{state}" local news {disaster} "{county}"',
    ]


def _rss_pubdate_to_datetime(pub_date: str) -> Optional[datetime]:
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return parse_datetime(pub_date)


def _rss_pubdate_to_iso(pub_date: str) -> str:
    dt = _rss_pubdate_to_datetime(pub_date)
    if dt:
        return dt.isoformat().replace("+00:00", "Z")
    return pub_date


def _rss_item_is_recent(pub_date: str, now: Optional[datetime] = None) -> bool:
    dt = _rss_pubdate_to_datetime(pub_date)
    return is_within_publish_window(dt, now=now)


def search_google_news_rss(
    county: str,
    state: str,
    disaster: str = "flood",
    max_results: int = 15,
    now: Optional[datetime] = None,
) -> List[Dict[str, str]]:
    """Discover articles via Google News RSS (today/yesterday only, keyword required)."""
    now = now or datetime.now(timezone.utc)
    query = f"{county} County {state} {disaster} when:2d"
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        resp = subprocess.run(
            ["curl", "-s", "-L", "-A", "DisasterSentinel/1.0", "--max-time", "15", url],
            capture_output=True,
            text=True,
            timeout=18,
        )
        if resp.returncode != 0 or not resp.stdout.strip():
            return []
        root = ET.fromstring(resp.stdout)
        channel = root.find("channel")
        if channel is None:
            return []
        results: List[Dict[str, str]] = []
        for item in channel.findall("item")[:max_results]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", desc)
            if not title or not link:
                continue
            if is_blocked_url(link):
                continue
            if not pub or not _rss_item_is_recent(pub, now=now):
                continue
            combined = f"{title} {desc}"
            if not contains_disaster_keyword(combined, disaster):
                continue
            iso_pub = _rss_pubdate_to_iso(pub) if pub else ""
            synthetic_html = (
                f'<html><head><meta property="article:published_time" content="{iso_pub}" />'
                f"<title>{title}</title></head><body><p>{desc}</p></body></html>"
            )
            results.append(
                {
                    "url": link,
                    "title": title,
                    "snippet": desc,
                    "html": synthetic_html,
                    "source_origin": "google_news_rss",
                }
            )
        return results
    except Exception:
        return []


def resolve_location(
    location_text: str,
    county: str = "",
    state: str = "",
    fips: str = "",
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (county, state, fips)."""
    if county and state:
        resolved_fips = fips or None
        if not resolved_fips and resolve_county:
            info = resolve_county(county, state)
            if info:
                resolved_fips = info.get("county_fips")
        return county, state, resolved_fips

    if location_text and parse_location:
        parsed = parse_location(location_text)
        if parsed:
            c, s = parsed
            resolved_fips = None
            if resolve_county:
                info = resolve_county(c, s)
                if info:
                    resolved_fips = info.get("county_fips")
            return c, s, resolved_fips

    if location_text and resolve_from_prompt:
        info = resolve_from_prompt(location_text)
        if info:
            return (
                info.get("county_name"),
                info.get("state"),
                info.get("county_fips"),
            )

    return None, None, None


def search_articles(
    county: str,
    state: str,
    disaster: str = "flood",
    max_queries: int = 5,
    max_results_per_query: int = 8,
    max_candidates: int = 20,
) -> Tuple[List[Dict[str, str]], int]:
    if _web_search_curl is None:
        return [], 0

    queries = build_article_queries(county, state, disaster)[:max_queries]
    seen: set[str] = set()
    candidates: List[Dict[str, str]] = []

    for query in queries:
        try:
            results = _web_search_curl(query, max_results=max_results_per_query)
            for r in results:
                url = (r.get("url") or "").strip()
                key = url.lower().rstrip("/")
                if not url.startswith(("http://", "https://")):
                    continue
                if key in seen or is_blocked_url(url):
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "url": url,
                        "title": r.get("title", ""),
                        "snippet": r.get("snippet", ""),
                    }
                )
                if len(candidates) >= max_candidates:
                    return candidates, len(queries)
            time.sleep(0.4)
        except Exception:
            continue

    return candidates, len(queries)


def fetch_page(url: str, timeout: int = 12, max_bytes: int = 20000) -> Tuple[bool, str]:
    ok, html = _fetch_page_html(url, timeout=timeout, max_bytes=max_bytes)
    return ok, html


_GOOGLE_NEWS_HOST_HINT = "news.google.com"


def _looks_like_google_news_wrapper(url: str, html: str) -> bool:
    if _GOOGLE_NEWS_HOST_HINT in (url or ""):
        return True
    # Google News interstitial pages are typically very short and contain a
    # client-side redirect script rather than real article content.
    return bool(html) and len(html) < 2000 and "google" in html.lower()


def _strip_html_to_text(html: str) -> str:
    """Minimal HTML->text for LLM input only (not for display/scoring)."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _fetch_page_html(url: str, *, timeout: int = 12, max_bytes: int = 20000) -> Tuple[bool, str]:
    try:
        resp = subprocess.run(
            [
                "curl",
                "-s",
                "-L",
                "-A",
                "DisasterSentinel/1.0",
                "--max-time",
                str(timeout),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 3,
        )
        if resp.returncode != 0:
            return False, ""
        return True, resp.stdout[:max_bytes]
    except Exception:
        return False, ""


def fetch_full_text_for_verdict(article) -> Tuple[str, str]:
    """Best-effort full article text for the LLM verdict step ONLY.
    Bounded to top-N articles by the caller (main.py) -- this function
    itself does not enforce that bound.

    Returns (text, text_source) where text_source is one of:
      "fetched_full_page"  -- successfully fetched and extracted real body text
      "snippet_fallback"   -- fetch failed or looked like a Google News wrapper;
                              fell back to title + snippet/description

    Never raises -- always returns something usable, degrading gracefully.
    """
    url = getattr(article, "url", None) or article.get("url")
    title = getattr(article, "title", None) or article.get("title", "")
    snippet = getattr(article, "snippet", None) or article.get("snippet", "")

    try:
        ok, html = _fetch_page_html(url, timeout=12, max_bytes=40_000)
        if not ok or not html or _looks_like_google_news_wrapper(url, html):
            return (f"{title}\n\n{snippet}", "snippet_fallback")
        text = _strip_html_to_text(html)
        if len(text.strip()) < 200:
            return (f"{title}\n\n{snippet}", "snippet_fallback")
        return (text, "fetched_full_page")
    except Exception:
        return (f"{title}\n\n{snippet}", "snippet_fallback")


def discover_and_fetch(
    county: str,
    state: str,
    disaster: str = "flood",
    max_candidates: int = 20,
    now: Optional[datetime] = None,
    exclude_urls: Optional[set[str]] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    seen: set[str] = set()
    excluded = {(u or "").strip().lower().rstrip("/") for u in (exclude_urls or set()) if u}
    articles: List[Dict[str, Any]] = []
    skipped_seen = 0

    # Primary: Google News RSS (reliable article feed with publish dates)
    for item in search_google_news_rss(
        county, state, disaster=disaster, max_results=max_candidates, now=now
    ):
        key = item["url"].lower().rstrip("/")
        if key in seen:
            continue
        if key in excluded:
            skipped_seen += 1
            continue
        seen.add(key)
        articles.append(item)

    # Secondary: HTML web search (DuckDuckGo/Bing/Yahoo)
    candidates, queries_run = search_articles(
        county, state, disaster=disaster, max_candidates=max_candidates
    )
    for c in candidates:
        key = c["url"].lower().rstrip("/")
        if key in seen:
            continue
        if key in excluded:
            skipped_seen += 1
            continue
        seen.add(key)
        ok, html = fetch_page(c["url"])
        if not ok or not html.strip():
            continue
        articles.append({**c, "html": html, "source_origin": "web_search"})
        if len(articles) >= max_candidates:
            break

    return {
        "queries_run": max(1, len(build_article_queries(county, state, disaster))),
        "candidates_found": len(articles) + len(candidates),
        "candidates_fetched": len(articles),
        "skipped_seen": skipped_seen,
        "articles": articles[:max_candidates],
    }
