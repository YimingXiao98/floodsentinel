"""Recover a news corpus for a past event from the Internet Archive.

Google News cannot return articles from a past event, and the GDELT article API
rate-limits historical queries too aggressively to sweep a window. The Wayback
Machine indexes archived pages by domain and date, which is enough: local
outlets encode the headline in the article path, so a title and a timestamp can
be recovered from the index alone, without fetching or storing page content.

The index is taken as it comes, with no filtering by keyword. The live system
scores whatever its search returns, so pre-selecting relevant articles here
would do the detector's job for it and remove exactly the disagreement the
belief layer exists to handle. Archived crawls of a local outlet during a flood
contain wrong-hazard, wrong-place, and wholly unrelated stories, and those
belong in the corpus.

Lives outside `watcher/` because it is evaluation scaffolding, not runtime.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

CDX_URL = "https://web.archive.org/cdx/search/cdx"
_HEADERS = {"User-Agent": "OpenClaw-disaster-sentinel-eval/1.0 (research)"}

#: The archive returns 503 under load far more often than it fails outright.
_BACKOFF_SECONDS = (5, 10, 20, 30, 45, 60)

#: Trailing article identifiers to strip when recovering a headline.
_ID_SUFFIX = re.compile(r"[-_]?\d{5,}$")
_EXTENSION = re.compile(r"\.(php|html?|shtml|amp)$", re.I)

#: Media and asset paths that sit alongside articles in the index.
_ASSET = re.compile(r"\.(jpe?g|png|gif|svg|webp|mp4|mp3|pdf|css|js|ico|woff2?)$", re.I)

#: Paths that are navigation rather than articles.
_NON_ARTICLE = re.compile(
    r"/(tag|tags|author|authors|category|categories|topic|topics|search|page|"
    r"about|contact|subscribe|newsletter|rss|feed|sitemap)(/|$)",
    re.I,
)


def _request(url: str, timeout: int = 90) -> Optional[Any]:
    last: Optional[Exception] = None
    for delay in (0,) + _BACKOFF_SECONDS:
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:  # 503 under load, or a transient network error
            last = exc
    print(f"  archive query failed after retries: {last}", flush=True)
    return None


def _looks_like_article(url: str) -> bool:
    if _NON_ARTICLE.search(url):
        return False
    path = urllib.parse.urlparse(url).path.rstrip("/")
    if _ASSET.search(path):
        return False
    if not path or path.count("/") < 2:
        return False
    slug = path.rsplit("/", 1)[-1]
    # A headline slug carries words; a bare id or a section name does not.
    return len(_EXTENSION.sub("", slug).replace("-", " ").split()) >= 3


def headline_from_url(url: str) -> str:
    """Recover a readable headline from an article path.

    Local outlets slug the headline into the path, so the index alone yields a
    usable title. Nothing is fetched, so no article text is stored.
    """
    path = urllib.parse.urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    slug = _EXTENSION.sub("", slug)
    slug = _ID_SUFFIX.sub("", slug)
    words = re.sub(r"[-_]+", " ", slug).strip()
    return words[:1].upper() + words[1:] if words else ""


def _stamp_to_iso(stamp: str) -> Optional[str]:
    try:
        dt = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def fetch_domain(
    domain: str, start: str, end: str, *, limit: int = 2000
) -> List[Dict[str, Any]]:
    """Archived pages for one domain over a date window, as article records."""
    query = urllib.parse.urlencode(
        {
            "url": f"{domain}/*",
            "from": start,
            "to": end,
            "output": "json",
            "filter": "statuscode:200",
            "collapse": "urlkey",
            "limit": limit,
            "fl": "timestamp,original",
        }
    )
    rows = _request(f"{CDX_URL}?{query}")
    if not rows or len(rows) < 2:
        return []

    out: List[Dict[str, Any]] = []
    for stamp, url in rows[1:]:
        if not _looks_like_article(url):
            continue
        seen_at = _stamp_to_iso(stamp)
        title = headline_from_url(url)
        if not seen_at or not title:
            continue
        out.append(
            {
                "url": url,
                "title": title,
                "domain": domain,
                "seen_at": seen_at,
                "source": "wayback_cdx",
            }
        )
    return out


def build_news_corpus(
    *,
    domains: Sequence[str],
    start: str,
    end: str,
    county: str = "",
    state: str = "",
) -> Dict[str, Any]:
    """Collect archived articles across outlets, de-duplicated by URL."""
    by_url: Dict[str, Dict[str, Any]] = {}
    for domain in domains:
        found = fetch_domain(domain, start, end)
        for article in found:
            by_url.setdefault(article["url"].lower().rstrip("/"), article)
        print(f"  {domain:28s} {len(found):>4d} articles (corpus {len(by_url)})", flush=True)

    articles = sorted(by_url.values(), key=lambda a: a["seen_at"])
    return {
        "schema": "sentinel-news-corpus/1",
        "county": county,
        "state": state,
        "window": {"start": start, "end": end},
        "source": "wayback_cdx",
        "domains": list(domains),
        "articles": articles,
    }


def save(obj: Any, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return target


def load(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
