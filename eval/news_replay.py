"""Serve an archived news corpus to a replay as though it were a live search.

The live system queries a news feed and scores whatever comes back. A replay of
a past event cannot issue that query, so an archived corpus stands in for it: at
each tick, the articles already published are handed to the *real* scorer from
`flood-article-detector`. None of the scoring is reimplemented here, so a replay
exercises the code path that runs in production.

Three properties matter for the result to mean anything.

No lookahead. An article becomes visible only once its publication timestamp has
passed. Corpora built from a web archive fall back to capture time, which
necessarily follows publication and therefore errs late rather than early; a
replay may credit the system with seeing an article after it could really have
seen it, never before.

Bounded results. A news query returns a bounded, recency-ordered result page,
not everything ever written. The feed mirrors that: the most recent articles
before the tick, capped, rather than the whole window.

Titles only. Corpora store no page content, so an article is scored on its
headline. The detector's location term looks for the county name in the text,
which headlines rarely carry, so the location the article was *selected* for is
supplied as the dateline that page text would otherwise provide.
"""

from __future__ import annotations

import bisect
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_DETECTOR = Path(__file__).resolve().parent.parent / "skills" / "flood-article-detector"
if str(_DETECTOR) not in sys.path:
    sys.path.insert(0, str(_DETECTOR))

from article_scorer import score_and_filter_articles  # noqa: E402

#: Outlets whose routine coverage area is the county being monitored, used only
#: for corpora that carry no geocoding of their own.
LOCAL_OUTLETS: Dict[str, Sequence[str]] = {
    "houstonchronicle.com": ("Harris", "TX"),
    "chron.com": ("Harris", "TX"),
    "khou.com": ("Harris", "TX"),
    "abc13.com": ("Harris", "TX"),
    "click2houston.com": ("Harris", "TX"),
    "fox26houston.com": ("Harris", "TX"),
}

#: A word is prose only if it is letters throughout. Identifier fragments mix
#: digits into short tokens (``83f4``, ``0a361e20``), so a length-based hex test
#: lets the short ones through; requiring pure letters rejects them all.
_WORD = re.compile(r"^[a-z]{3,}$")


def usable_title(title: str) -> bool:
    """Whether a recovered headline carries enough prose to be scored.

    Opaque URLs yield slugs that are identifiers rather than sentences, and an
    article scored on a hex string is noise of a kind the live system never
    sees, because it reads the page rather than the address.
    """
    words = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split()
    return sum(1 for w in words if _WORD.match(w)) >= 4


class ArchivedNewsFeed:
    """A corpus indexed by time, queryable as of a moment.

    Accepts either a corpus timestamped by publication (`published_at`, from
    GDELT) or by archive capture (`seen_at`, from the Wayback index).
    """

    def __init__(
        self,
        corpus: Dict[str, Any],
        *,
        county: str = "",
        state: str = "",
        require_usable_title: bool = True,
    ):
        self.county = county or corpus.get("county", "")
        self.state = state or corpus.get("state", "")
        #: The corpus was selected for a place, so that place is a recorded
        #: property of every article rather than an assumption about the outlet.
        self.geocoded = bool(corpus.get("place_terms"))

        articles = corpus.get("articles", [])
        if require_usable_title:
            articles = [a for a in articles if usable_title(a.get("title", ""))]
        self._key = "published_at" if articles and "published_at" in articles[0] else "seen_at"
        self._articles = sorted(articles, key=lambda a: a[self._key])
        self._times = [a[self._key] for a in self._articles]

    def __len__(self) -> int:
        return len(self._articles)

    def available_as_of(
        self, when: datetime, *, lookback_hours: float = 24.0, limit: int = 120
    ) -> List[Dict[str, Any]]:
        """The most recent articles published before `when`. Never looks ahead."""
        hi = bisect.bisect_right(self._times, when.strftime("%Y-%m-%dT%H:%M:%SZ"))
        lo = bisect.bisect_left(
            self._times,
            (when - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        window = self._articles[lo:hi]
        return window[-limit:] if limit else window

    def _as_search_item(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one corpus entry like a fetched page, for the real scorer.

        The scorer reads the publication date out of page markup, so the
        timestamp is written into the markup it expects rather than passed
        alongside it.
        """
        title = article.get("title", "")
        stamp = article.get(self._key, "")
        if self.geocoded:
            dateline = f"{self.county} County, {self.state}."
        else:
            place = LOCAL_OUTLETS.get(article.get("domain", ""))
            dateline = f"{place[0]} County, {place[1]}." if place else ""
        return {
            "url": article.get("url", ""),
            "title": title,
            "snippet": title,
            "html": (
                f'<meta property="article:published_time" content="{stamp}" />'
                f"<title>{title}</title>{title}. {dateline}"
            ),
        }

    def score_as_of(
        self,
        when: datetime,
        *,
        lookback_hours: float = 24.0,
        limit: int = 120,
        top_n: int = 5,
        min_page_score: float = 0.75,
    ) -> Dict[str, Any]:
        """Run the production scorer over what was available at `when`.

        Returns the evidence structure `extract_article_claims` consumes, so a
        replay can feed it straight through without translation.
        """
        candidates = self.available_as_of(
            when, lookback_hours=lookback_hours, limit=limit
        )
        scored, omitted = score_and_filter_articles(
            [self._as_search_item(a) for a in candidates],
            county=self.county,
            state=self.state,
            disaster="flood",
            now=when,
        )
        scored.sort(key=lambda a: a.page_score, reverse=True)
        admitted = [a for a in scored if a.page_score > min_page_score][:top_n]
        return {
            "high_confidence_articles": [
                a.to_dict(rank=i + 1) for i, a in enumerate(admitted)
            ],
            "candidates_considered": len(candidates),
            "scored": len(scored),
            "omitted": omitted,
        }


def load_feed(path: str | Path, **kwargs: Any) -> ArchivedNewsFeed:
    from eval.news_archive import load

    return ArchivedNewsFeed(load(path), **kwargs)
