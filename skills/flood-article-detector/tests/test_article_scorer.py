"""Unit tests for flood-article-detector scoring."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from types import SimpleNamespace

from article_scorer import (  # noqa: E402
    OMIT_MISSING_KEYWORD,
    OMIT_OUTDATED,
    aggregate_flood_outcome,
    aggregate_flood_outcome_with_verdicts,
    article_eligibility,
    extract_publish_date,
    is_within_publish_window,
    recency_score,
    score_and_filter_articles,
    score_article,
    top_n,
)
from article_verdict import derive_final_verdict  # noqa: E402

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)

HTML_TODAY = """
<html><head>
<meta property="og:published_time" content="2026-06-17T08:30:00Z" />
<title>Flash flooding hits Miami-Dade roads</title>
</head><body>
<p>Heavy rains caused flash flooding across Miami-Dade County, Florida today.
Streets were submerged and evacuations were ordered.</p>
</body></html>
"""

HTML_YESTERDAY = """
<html><head>
<meta property="article:published_time" content="2026-06-16T14:30:00Z" />
<title>Flood watch issued for Miami-Dade</title>
</head><body>
<p>A flood watch remains in effect for Miami-Dade County, Florida.</p>
</body></html>
"""

HTML_OLD = """
<html><head>
<meta property="og:published_time" content="2026-06-10T14:30:00Z" />
<title>Flash flooding hits Miami-Dade roads</title>
</head><body><p>Old flooding report.</p></body></html>
"""

HTML_NO_KEYWORD = """
<html><head>
<meta property="og:published_time" content="2026-06-17T08:30:00Z" />
<title>Sunny weather in Miami-Dade</title>
</head><body><p>Clear skies expected all week.</p></body></html>
"""

HTML_DENY = """
<html><head>
<meta property="article:published_time" content="2026-06-17T08:00:00Z" />
</head><body>
<p>Officials report no flooding in Harris County, Texas. All clear for residents.</p>
</body></html>
"""


class TestPublishWindow(unittest.TestCase):
    def test_today_is_eligible(self):
        pub = NOW - timedelta(hours=4)
        self.assertTrue(is_within_publish_window(pub, now=NOW))

    def test_yesterday_is_eligible(self):
        pub = NOW - timedelta(days=1)
        self.assertTrue(is_within_publish_window(pub, now=NOW))

    def test_two_days_old_is_not_eligible(self):
        pub = NOW - timedelta(days=2)
        self.assertFalse(is_within_publish_window(pub, now=NOW))


class TestArticleEligibility(unittest.TestCase):
    def test_outdated_omitted(self):
        eligible, reason = article_eligibility(
            title="Flash flooding",
            snippet="flooding",
            body_text="flooding",
            published_at=NOW - timedelta(days=3),
            disaster="flood",
            now=NOW,
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, OMIT_OUTDATED)

    def test_missing_keyword_omitted(self):
        eligible, reason = article_eligibility(
            title="Sunny skies",
            snippet="clear weather",
            body_text="no rain",
            published_at=NOW - timedelta(hours=2),
            disaster="flood",
            now=NOW,
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, OMIT_MISSING_KEYWORD)


class TestRecencyScore(unittest.TestCase):
    def test_today_scores_high(self):
        published = NOW - timedelta(hours=3)
        self.assertEqual(recency_score(published, now=NOW), 1.0)

    def test_outdated_scores_zero(self):
        published = NOW - timedelta(days=5)
        self.assertEqual(recency_score(published, now=NOW), 0.0)


class TestScoreArticle(unittest.TestCase):
    def test_today_with_keyword_scored(self):
        article = score_article(
            url="https://example.com/a",
            title="Flash flooding in Miami-Dade",
            snippet="",
            html=HTML_TODAY,
            county="Miami-Dade",
            state="Florida",
            disaster="flood",
            now=NOW,
        )
        self.assertIsNotNone(article)
        self.assertGreater(article.page_score, 0)

    def test_old_article_returns_none(self):
        article = score_article(
            url="https://example.com/old",
            title="Flash flooding",
            snippet="flooding",
            html=HTML_OLD,
            county="Miami-Dade",
            state="Florida",
            disaster="flood",
            now=NOW,
        )
        self.assertIsNone(article)

    def test_no_keyword_returns_none(self):
        article = score_article(
            url="https://example.com/x",
            title="Sunny weather",
            snippet="",
            html=HTML_NO_KEYWORD,
            county="Miami-Dade",
            state="Florida",
            disaster="flood",
            now=NOW,
        )
        self.assertIsNone(article)


class TestScoreAndFilter(unittest.TestCase):
    def test_filters_old_and_keywordless(self):
        items = [
            {"url": "https://a", "title": "t", "snippet": "", "html": HTML_TODAY},
            {"url": "https://b", "title": "t", "snippet": "", "html": HTML_OLD},
            {"url": "https://c", "title": "t", "snippet": "", "html": HTML_NO_KEYWORD},
        ]
        scored, omitted = score_and_filter_articles(
            items, county="Miami-Dade", state="Florida", disaster="flood", now=NOW
        )
        self.assertEqual(len(scored), 1)
        self.assertGreater(omitted.get("outdated", 0), 0)
        self.assertGreater(omitted.get("missing_keyword", 0), 0)


class TestOutcomeAggregation(unittest.TestCase):
    def test_confirmed_outcome(self):
        article = score_article(
            url="https://example.com/a",
            title="Flash flooding",
            snippet="flooding",
            html=HTML_TODAY,
            county="Miami-Dade",
            state="Florida",
            now=NOW,
        )
        assert article is not None
        outcome = aggregate_flood_outcome([article])
        self.assertEqual(outcome["verdict"], "flood_confirmed")

    def test_top_n_limits_to_five(self):
        items = [
            {
                "url": f"https://example.com/{i}",
                "title": f"Flood news {i}",
                "snippet": "flooding",
                "html": HTML_TODAY.replace("2026-06-17T08:30:00Z", f"2026-06-17T0{i}:30:00Z"),
            }
            for i in range(8)
        ]
        scored, _ = score_and_filter_articles(
            items, county="Miami-Dade", state="Florida", now=NOW
        )
        self.assertEqual(len(top_n(scored, 5)), min(5, len(scored)))


def _article(page_score, final_verdict, flood_status, confidence):
    return SimpleNamespace(
        page_score=page_score,
        llm_verdict={
            "final_verdict": final_verdict,
            "flood_status": flood_status,
            "confidence": confidence,
        },
    )


class TestLlmVerdictAggregation(unittest.TestCase):
    def test_derive_final_verdict_requires_target_county(self):
        self.assertEqual(
            derive_final_verdict("at_target_county", "confirmed_flooding"),
            "flooding_at_target_county",
        )
        self.assertEqual(
            derive_final_verdict("nearby_different_county", "confirmed_flooding"),
            "flooding_elsewhere_not_target",
        )
        self.assertEqual(
            derive_final_verdict("state_or_region_general", "confirmed_flooding"),
            "flooding_elsewhere_not_target",
        )
        self.assertEqual(
            derive_final_verdict("at_target_county", "no_flooding_indicated"),
            "no_flood_evidence",
        )
        self.assertEqual(derive_final_verdict("unclear", "unclear"), "inconclusive")

    def test_aggregate_excludes_elsewhere_articles_from_denominator(self):
        target = _article(0.9, "flooding_at_target_county", "confirmed_flooding", 0.95)
        elsewhere = _article(0.95, "flooding_elsewhere_not_target", "confirmed_flooding", 0.9)
        result = aggregate_flood_outcome_with_verdicts([target, elsewhere])
        self.assertEqual(result["verdict"], "flood_confirmed")
        self.assertEqual(result["outcome_method"], "llm_verified")

    def test_aggregate_all_elsewhere_is_inconclusive_not_no_evidence(self):
        elsewhere1 = _article(0.9, "flooding_elsewhere_not_target", "confirmed_flooding", 0.9)
        elsewhere2 = _article(0.8, "flooding_elsewhere_not_target", "confirmed_flooding", 0.8)
        result = aggregate_flood_outcome_with_verdicts([elsewhere1, elsewhere2])
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertIn("elsewhere", result["rationale"])

    def test_aggregate_falls_back_to_legacy_when_no_verdicts_present(self):
        plain = SimpleNamespace(
            page_score=0.8,
            llm_verdict=None,
            indication_value=1.0,
            indication="confirms_flood",
            flood_mentioned=True,
            recency_score=1.0,
            published_at_raw="today",
            title="Flood report",
        )
        result = aggregate_flood_outcome_with_verdicts([plain])
        self.assertNotIn("outcome_method", result)


if __name__ == "__main__":
    unittest.main()
