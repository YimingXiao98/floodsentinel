"""Page scoring, publish-date extraction, and flood-outcome aggregation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Scoring weights — recency dominates per plan.
WEIGHT_RECENCY = 0.60
WEIGHT_FLOOD_MENTION = 0.20
WEIGHT_INDICATION = 0.12
WEIGHT_LOCATION = 0.08

INDICATION_SCORES = {
    "confirms_flood": 1.0,
    "suggests_flood_risk": 0.5,
    "neutral": 0.0,
    "denies_or_clear": -0.8,
}

CONFIRM_TERMS = (
    "flash flood",
    "flooding",
    "flooded",
    "submerged",
    "evacuat",
    "rescued",
    "damaged",
    "inundat",
    "water rescu",
    "roads underwater",
    "streets flooded",
)

RISK_TERMS = (
    "flood warning",
    "flood watch",
    "river crest",
    "rising water",
    "high water",
    "flood advisory",
    "flood risk",
)

DENY_TERMS = (
    "no flooding",
    "no flood",
    "all clear",
    "flood threat has passed",
    "waters receding",
    "dry conditions",
)

GENERIC_FLOOD_TERMS = ("flood", "flooding", "high water")

# Only score articles published today or yesterday (UTC calendar dates).
MAX_PUBLISH_AGE_DAYS = 1

OMIT_OUTDATED = "outdated"
OMIT_MISSING_KEYWORD = "missing_keyword"
OMIT_MISSING_DATE = "missing_publish_date"

BLOCKED_DOMAINS = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "pinterest.com",
    "amazon.com",
    "ebay.com",
    "wikipedia.org",
)

DATE_META_PATTERNS = [
    (r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']', "article:published_time"),
    (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time["\']', "article:published_time"),
    (r'<meta[^>]+property=["\']og:published_time["\'][^>]+content=["\']([^"\']+)["\']', "og:published_time"),
    (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:published_time["\']', "og:published_time"),
    (r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']', "pubdate"),
    (r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']', "meta:date"),
    (r'"datePublished"\s*:\s*"([^"]+)"', "json-ld:datePublished"),
    (r'<time[^>]+datetime=["\']([^"\']+)["\']', "time:datetime"),
]

VISIBLE_DATE_PATTERN = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},?\s+\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?",
    re.I,
)


@dataclass
class ScoredArticle:
    url: str
    title: str
    snippet: str
    body_excerpt: str
    published_at: Optional[datetime]
    published_at_raw: Optional[str]
    published_at_source: Optional[str]
    recency_score: float
    flood_mention_score: float
    indication: str
    indication_value: float
    indication_snippet: str
    location_score: float
    page_score: float
    flood_mentioned: bool
    llm_verdict: Optional[Dict[str, Any]] = None

    def to_dict(self, rank: int) -> Dict[str, Any]:
        return {
            "rank": rank,
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "published_at": (
                self.published_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if self.published_at
                else None
            ),
            "published_at_raw": self.published_at_raw,
            "published_at_source": self.published_at_source,
            "page_score": round(self.page_score, 4),
            "score_breakdown": {
                "recency": round(self.recency_score, 4),
                "flood_mention": round(self.flood_mention_score, 4),
                "indication": round(self.indication_value, 4),
                "location": round(self.location_score, 4),
            },
            "flood_mentioned": self.flood_mentioned,
            "indication": self.indication,
            "indication_snippet": self.indication_snippet,
            "llm_verdict": self.llm_verdict,
        }


def disaster_keywords(disaster: str) -> Tuple[str, ...]:
    """Keywords that must appear in title/body for an article to be scored."""
    d = (disaster or "flood").lower().strip()
    if d in {"flood", "flooding", "floods", "flash flood", "flash-flood"}:
        return GENERIC_FLOOD_TERMS + CONFIRM_TERMS + RISK_TERMS
    return (d,)


def contains_disaster_keyword(text: str, disaster: str = "flood") -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in disaster_keywords(disaster))


def is_within_publish_window(
    published_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> bool:
    """True when publish date is today or at most one calendar day old (UTC)."""
    if published_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    pub_date = published_at.astimezone(timezone.utc).date()
    today = now.astimezone(timezone.utc).date()
    oldest = today - timedelta(days=MAX_PUBLISH_AGE_DAYS)
    return oldest <= pub_date <= today


def article_eligibility(
    *,
    title: str,
    snippet: str,
    body_text: str,
    published_at: Optional[datetime],
    disaster: str = "flood",
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[str]]:
    """Return (eligible, omit_reason). Ineligible articles are omitted (score 0)."""
    combined = " ".join(filter(None, [title, snippet, body_text]))
    if published_at is None:
        return False, OMIT_MISSING_DATE
    if not is_within_publish_window(published_at, now=now):
        return False, OMIT_OUTDATED
    if not contains_disaster_keyword(combined, disaster):
        return False, OMIT_MISSING_KEYWORD
    return True, None


def _county_slug(county: str) -> str:
    lowered = county.lower()
    for suffix in (" county", " parish", " borough", " census area", " municipality"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
    lowered = re.sub(r"[^a-z0-9\s-]", "", lowered)
    return re.sub(r"\s+", "-", lowered)


def parse_datetime(raw: str) -> Optional[datetime]:
    """Best-effort parse for common publish-date formats."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    formats = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%B %d, %Y %I:%M %p",
        "%B %d, %Y",
        "%b %d, %Y %I:%M %p",
        "%b %d, %Y",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # ISO with fractional seconds
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2}|Z)?$", text)
    if m:
        base = m.group(1)
        tz = m.group(3) or "+00:00"
        if tz == "Z":
            tz = "+00:00"
        try:
            return datetime.strptime(base + tz, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def extract_publish_date(html: str) -> Tuple[Optional[datetime], Optional[str], Optional[str]]:
    """Return (datetime, raw, source_field)."""
    head = html[:12000]
    for pattern, source in DATE_META_PATTERNS:
        m = re.search(pattern, head, re.I | re.S)
        if m:
            raw = m.group(1).strip()
            dt = parse_datetime(raw)
            if dt:
                return dt, raw, source
    m = VISIBLE_DATE_PATTERN.search(head)
    if m:
        raw = m.group(0).strip()
        dt = parse_datetime(raw)
        if dt:
            return dt, raw, "visible_date"
    return None, None, None


def recency_score(published_at: Optional[datetime], now: Optional[datetime] = None) -> float:
    """Recency within the allowed today/yesterday window only."""
    if published_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if not is_within_publish_window(published_at, now=now):
        return 0.0
    pub_date = published_at.astimezone(timezone.utc).date()
    today = now.astimezone(timezone.utc).date()
    if pub_date == today:
        age_hours = max(0.0, (now - published_at).total_seconds() / 3600.0)
        if age_hours <= 6:
            return 1.0
        return 0.92
    # Yesterday
    return 0.75


def flood_mention_score(text: str) -> float:
    lowered = text.lower()
    if any(t in lowered for t in CONFIRM_TERMS):
        return 1.0
    if any(t in lowered for t in RISK_TERMS):
        return 0.7
    if any(t in lowered for t in GENERIC_FLOOD_TERMS):
        return 0.4
    return 0.0


def classify_indication(text: str) -> Tuple[str, float, str]:
    lowered = text.lower()
    for term in DENY_TERMS:
        if term in lowered:
            idx = lowered.index(term)
            snippet = text[max(0, idx - 40) : idx + len(term) + 60].strip()
            return "denies_or_clear", INDICATION_SCORES["denies_or_clear"], _clean_snippet(snippet)
    for term in CONFIRM_TERMS:
        if term in lowered:
            idx = lowered.index(term)
            snippet = text[max(0, idx - 40) : idx + len(term) + 60].strip()
            return "confirms_flood", INDICATION_SCORES["confirms_flood"], _clean_snippet(snippet)
    for term in RISK_TERMS:
        if term in lowered:
            idx = lowered.index(term)
            snippet = text[max(0, idx - 40) : idx + len(term) + 60].strip()
            return "suggests_flood_risk", INDICATION_SCORES["suggests_flood_risk"], _clean_snippet(snippet)
    return "neutral", INDICATION_SCORES["neutral"], ""


def _clean_snippet(snippet: str) -> str:
    snippet = re.sub(r"\s+", " ", snippet)
    return snippet[:200]


def location_score(text: str, county: str, state: str) -> float:
    lowered = text.lower()
    score = 0.0
    cslug = _county_slug(county)
    if cslug in lowered or county.lower() in lowered:
        score += 0.6
    if state.lower() in lowered:
        score += 0.4
    return min(1.0, score)


def is_blocked_url(url: str) -> bool:
    u = url.lower()
    return any(d in u for d in BLOCKED_DOMAINS)


def compute_page_score(
    recency: float,
    mention: float,
    indication_value: float,
    location: float,
) -> float:
    indication_norm = (indication_value + 1.0) / 2.0
    return (
        WEIGHT_RECENCY * recency
        + WEIGHT_FLOOD_MENTION * mention
        + WEIGHT_INDICATION * indication_norm
        + WEIGHT_LOCATION * location
    )


def score_article(
    *,
    url: str,
    title: str,
    snippet: str,
    html: str,
    county: str,
    state: str,
    disaster: str = "flood",
    now: Optional[datetime] = None,
) -> Optional[ScoredArticle]:
    body_excerpt = re.sub(r"<[^>]+>", " ", html[:15000])
    body_excerpt = re.sub(r"\s+", " ", body_excerpt).strip()
    combined = " ".join(filter(None, [title, snippet, body_excerpt[:4000]]))

    published_at, published_raw, published_source = extract_publish_date(html)
    eligible, _omit_reason = article_eligibility(
        title=title,
        snippet=snippet,
        body_text=body_excerpt[:4000],
        published_at=published_at,
        disaster=disaster,
        now=now,
    )
    if not eligible:
        return None

    rec = recency_score(published_at, now=now)
    mention = flood_mention_score(combined)
    indication, indication_val, indication_snippet = classify_indication(combined)
    loc = location_score(combined, county, state)
    page = compute_page_score(rec, mention, indication_val, loc)

    return ScoredArticle(
        url=url,
        title=title or url,
        snippet=snippet,
        body_excerpt=body_excerpt[:500],
        published_at=published_at,
        published_at_raw=published_raw,
        published_at_source=published_source,
        recency_score=rec,
        flood_mention_score=mention,
        indication=indication,
        indication_value=indication_val,
        indication_snippet=indication_snippet,
        location_score=loc,
        page_score=page,
        flood_mentioned=mention > 0.0,
    )


def sort_articles(articles: List[ScoredArticle]) -> List[ScoredArticle]:
    def sort_key(a: ScoredArticle) -> Tuple[float, float]:
        ts = a.published_at.timestamp() if a.published_at else 0.0
        return (a.page_score, ts)

    return sorted(articles, key=sort_key, reverse=True)


def top_n(articles: List[ScoredArticle], n: int = 5) -> List[ScoredArticle]:
    eligible = [a for a in articles if a.page_score > 0]
    return sort_articles(eligible)[:n]


def score_and_filter_articles(
    items: List[Dict[str, Any]],
    *,
    county: str,
    state: str,
    disaster: str = "flood",
    now: Optional[datetime] = None,
) -> Tuple[List[ScoredArticle], Dict[str, int]]:
    """Score articles; omit outdated or keyword-missing items (implicit score 0)."""
    scored: List[ScoredArticle] = []
    omitted = {
        "outdated": 0,
        "missing_keyword": 0,
        "missing_publish_date": 0,
    }
    for item in items:
        body_excerpt = re.sub(r"<[^>]+>", " ", (item.get("html") or "")[:15000])
        body_excerpt = re.sub(r"\s+", " ", body_excerpt).strip()
        published_at, _, _ = extract_publish_date(item.get("html") or "")
        eligible, reason = article_eligibility(
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
            body_text=body_excerpt[:4000],
            published_at=published_at,
            disaster=disaster,
            now=now,
        )
        if not eligible:
            if reason:
                omitted[reason] = omitted.get(reason, 0) + 1
            continue
        article = score_article(
            url=item["url"],
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
            html=item.get("html", ""),
            county=county,
            state=state,
            disaster=disaster,
            now=now,
        )
        if article is not None and article.page_score > 0:
            scored.append(article)
    return scored, omitted


def aggregate_flood_outcome(articles: List[ScoredArticle]) -> Dict[str, Any]:
    if not articles:
        return {
            "verdict": "inconclusive",
            "outcome_score": 0.0,
            "confidence": "low",
            "rationale": "No eligible articles from today or yesterday with the requested disaster keyword.",
        }

    weight_sum = sum(max(a.page_score, 0.01) for a in articles)
    outcome_score = sum(a.page_score * a.indication_value for a in articles) / weight_sum

    confirms = sum(1 for a in articles if a.indication == "confirms_flood")
    denies = sum(1 for a in articles if a.indication == "denies_or_clear")
    recent_with_flood = sum(
        1 for a in articles if a.flood_mentioned and a.recency_score >= 0.75
    )

    if outcome_score >= 0.55:
        verdict = "flood_confirmed"
        confidence = "high"
    elif outcome_score >= 0.30:
        verdict = "flood_likely"
        confidence = "medium"
    elif outcome_score >= 0.10:
        verdict = "flood_possible"
        confidence = "low"
    elif outcome_score <= -0.15:
        verdict = "no_flood_evidence"
        confidence = "medium"
    else:
        verdict = "inconclusive"
        confidence = "low"

    rationale_parts = [
        f"Weighted outcome score {outcome_score:.2f} from top {len(articles)} article(s).",
        f"{confirms} confirm flooding, {denies} deny/clear, {recent_with_flood} recent with flood terms.",
    ]
    if articles:
        top = articles[0]
        pub = top.published_at_raw or "unknown date"
        rationale_parts.append(
            f"Top source: {top.title[:80]} ({pub}, indication={top.indication})."
        )

    return {
        "verdict": verdict,
        "outcome_score": round(outcome_score, 4),
        "confidence": confidence,
        "rationale": " ".join(rationale_parts),
    }


# Value assigned to each (final_verdict, flood_status) combination for the
# weighted outcome score. Deliberately more conservative than a simple
# indication_value because this is now backed by actual reading comprehension,
# not just keyword presence -- errors here are more consequential.
_VERDICT_VALUE = {
    ("flooding_at_target_county", "confirmed_flooding"): 1.0,
    ("flooding_at_target_county", "flood_risk_or_warning"): 0.6,
    ("flooding_at_target_county", "flooding_receding_or_resolved"): 0.3,
    ("no_flood_evidence", "no_flooding_indicated"): -0.5,
}
_DEFAULT_VERDICT_VALUE = 0.0  # inconclusive, or any combination not in the table above


def aggregate_flood_outcome_with_verdicts(top_articles) -> Dict[str, Any]:
    """Aggregate outcome using LLM verdicts where present, falling back to
    the legacy rule-based aggregate_flood_outcome() if no article in
    top_articles has an llm_verdict (e.g. the flag was off, or every call failed).

    County-specificity rule: articles verdicted "flooding_elsewhere_not_target"
    are EXCLUDED from the weighted sum entirely -- they carry no signal about
    the target county either way, and including them would incorrectly dilute
    the outcome toward "inconclusive" for a county that may well be flooding.
    """
    verdicted = [a for a in top_articles if getattr(a, "llm_verdict", None) is not None]
    if not verdicted:
        return aggregate_flood_outcome(top_articles)

    relevant = [a for a in verdicted if a.llm_verdict["final_verdict"] != "flooding_elsewhere_not_target"]
    if not relevant:
        return {
            "verdict": "inconclusive",
            "outcome_score": 0.0,
            "confidence": "low",
            "rationale": "All top articles concerned flooding elsewhere, not the target county.",
            "outcome_method": "llm_verified",
        }

    total_weight = 0.0
    weighted_sum = 0.0
    for a in relevant:
        v = a.llm_verdict
        weight = max(a.page_score, 0.01) * max(v["confidence"], 0.05)
        value = _VERDICT_VALUE.get((v["final_verdict"], v["flood_status"]), _DEFAULT_VERDICT_VALUE)
        weighted_sum += weight * value
        total_weight += weight

    outcome_score = weighted_sum / total_weight if total_weight else 0.0

    if outcome_score >= 0.55:
        verdict, confidence = "flood_confirmed", "high"
    elif outcome_score >= 0.30:
        verdict, confidence = "flood_likely", "medium"
    elif outcome_score >= 0.10:
        verdict, confidence = "flood_possible", "low"
    elif outcome_score <= -0.15:
        verdict, confidence = "no_flood_evidence", "medium"
    else:
        verdict, confidence = "inconclusive", "low"

    at_target = [a for a in relevant if a.llm_verdict["final_verdict"] == "flooding_at_target_county"]
    rationale = (
        f"{len(at_target)} of {len(top_articles)} top articles verdicted as describing flooding "
        f"at the target county; outcome_score={outcome_score:.2f}."
    )

    return {
        "verdict": verdict,
        "outcome_score": round(outcome_score, 4),
        "confidence": confidence,
        "rationale": rationale,
        "outcome_method": "llm_verified",
    }
