"""Frozen news corpus for retrospective replay of the T2 channel.

Google News RSS cannot return articles from a past event, so the news channel
is not replayable live. Without it the belief layer's main advantage is
untestable: reliability tiering only changes an outcome where sources disagree,
and news is the low-tier source that disagrees with the gauges.

Articles come from the GDELT 2.0 DOC API (coverage from 2017 onward), are
frozen to disk once, and are served back at replay time using the same recency
window the live system applies. Scoring uses the real
`flood-article-detector` scorer, so an admitted article in replay is an article
the live system would have admitted.

GDELT rate-limits aggressively; every call backs off and retries.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_HEADERS = {"User-Agent": "OpenClaw-disaster-sentinel-eval/1.0"}
_MAX_RECORDS = 250
_BACKOFF_SECONDS = (10, 25, 45, 70, 100)

_DETECTOR_DIR = (
    Path(__file__).resolve().parent.parent / "skills" / "flood-article-detector"
)


def _detector_on_path() -> None:
    path = str(_DETECTOR_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def parse_iso(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _gdelt_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _parse_seendate(value: str) -> Optional[datetime]:
    """GDELT seendate looks like 20170826T051500Z."""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_window(
    query: str, start: datetime, end: datetime, *, max_records: int = _MAX_RECORDS
) -> List[Dict[str, Any]]:
    """One GDELT DOC query, retrying through rate limits."""
    params = urllib.parse.urlencode(
        {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "startdatetime": _gdelt_stamp(start),
            "enddatetime": _gdelt_stamp(end),
        }
    )
    url = f"{GDELT_DOC_URL}?{params}"
    last_error: Optional[Exception] = None
    for delay in (0,) + _BACKOFF_SECONDS:
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            return (json.loads(body) or {}).get("articles") or []
        except Exception as exc:  # rate limit, transient network, or bad JSON
            last_error = exc
    print(f"  giving up on window {_utc_iso(start)}: {last_error}", file=sys.stderr)
    return []


def build_news_corpus(
    *,
    queries: Sequence[str],
    start: str,
    end: str,
    window_hours: int = 24,
    county: str = "",
    state: str = "",
) -> Dict[str, Any]:
    """Fetch and de-duplicate articles across the event window."""
    begin = parse_iso(start)
    finish = parse_iso(end)
    step = timedelta(hours=window_hours)
    by_url: Dict[str, Dict[str, Any]] = {}

    for query in queries:
        cursor = begin
        while cursor < finish:
            upper = min(cursor + step, finish)
            articles = fetch_window(query, cursor, upper)
            for article in articles:
                url = (article.get("url") or "").strip()
                if not url:
                    continue
                seen = _parse_seendate(article.get("seendate") or "")
                if seen is None:
                    continue
                key = url.lower().rstrip("/")
                if key in by_url:
                    continue
                by_url[key] = {
                    "url": url,
                    "title": article.get("title") or "",
                    "domain": article.get("domain") or "",
                    "language": article.get("language") or "",
                    "seen_at": _utc_iso(seen),
                    "query": query,
                }
            print(
                f"  {query!r} {_utc_iso(cursor)[:13]}: +{len(articles)} "
                f"(corpus {len(by_url)})",
                flush=True,
            )
            cursor = upper

    articles = sorted(by_url.values(), key=lambda a: a["seen_at"])
    return {
        "schema": "sentinel-news-corpus/1",
        "county": county,
        "state": state,
        "window": {"start": start, "end": end},
        "queries": list(queries),
        "source": "gdelt_doc_v2",
        "articles": articles,
    }


# --------------------------------------------------------------------------
# Replay-time access
# --------------------------------------------------------------------------


def articles_as_of(
    corpus: Dict[str, Any], when: datetime, *, window_hours: float = 48.0
) -> List[Dict[str, Any]]:
    """Articles visible at virtual time `when`. Never looks ahead.

    `window_hours` mirrors the live recency window: the detector only considers
    articles published today or yesterday.
    """
    lower = when - timedelta(hours=window_hours)
    out = []
    for article in corpus.get("articles") or []:
        seen = parse_iso(article["seen_at"])
        if lower <= seen <= when:
            out.append(article)
    return out


def score_articles(
    articles: Sequence[Dict[str, Any]],
    *,
    when: datetime,
    county: str,
    state: str,
) -> List[Dict[str, Any]]:
    """Score with the real detector so replay admits what production would.

    The scorer's recency term is relative to *now*, so each article's publish
    time is expressed relative to the virtual clock.
    """
    _detector_on_path()
    from article_scorer import (  # type: ignore
        compute_page_score,
        flood_mention_score,
        location_score,
        INDICATION_SCORES,
    )

    scored: List[Dict[str, Any]] = []
    for article in articles:
        title = article.get("title") or ""
        seen = parse_iso(article["seen_at"])
        age_hours = (when - seen).total_seconds() / 3600.0
        # Mirror recency_score() without depending on wall-clock "today".
        if age_hours <= 6:
            recency = 1.0
        elif age_hours <= 24:
            recency = 0.92
        elif age_hours <= 48:
            recency = 0.75
        else:
            recency = 0.0
        mention = flood_mention_score(title)
        loc = location_score(title, county=county, state=state)
        indication = _indication_from_title(title)
        page_score = compute_page_score(
            recency=recency,
            mention=mention,
            indication_value=INDICATION_SCORES.get(indication, 0.0),
            location=loc,
        )
        scored.append(
            {
                **article,
                "page_score": round(page_score, 4),
                "indication": indication,
                "published_at": article["seen_at"],
            }
        )
    return sorted(scored, key=lambda a: a["page_score"], reverse=True)


_CONFIRM = ("flooding", "flooded", "inundat", "evacuat", "rescue", "under water", "submerged")
_RISK = ("flood warning", "flood watch", "flash flood", "could flood", "expected to flood", "braces")
_CLEAR = ("recede", "receding", "cleanup", "clean up", "reopened", "all clear", "subsided")


def _indication_from_title(title: str) -> str:
    text = (title or "").lower()
    if any(term in text for term in _CLEAR):
        return "denies_or_clear"
    if any(term in text for term in _CONFIRM):
        return "confirms_flood"
    if any(term in text for term in _RISK):
        return "suggests_flood_risk"
    return "neutral"


def save(obj: Any, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return target


def load(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
