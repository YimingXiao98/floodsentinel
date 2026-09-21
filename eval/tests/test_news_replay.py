"""The archived news feed must not leak the future into a replay.

Every detection-timing result depends on the feed refusing to show the system an
article before the moment it was published. These tests pin that property, the
bounded result page that keeps the replay comparable to a live search, and the
title filter that keeps identifier-only slugs out of the scorer.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval.news_archive import headline_from_url  # noqa: E402
from eval.news_replay import ArchivedNewsFeed, usable_title  # noqa: E402


def _corpus(stamps, *, geocoded=True):
    return {
        "county": "Harris",
        "state": "TX",
        "place_terms": ["Harris County"] if geocoded else [],
        "articles": [
            {
                "url": f"http://example.test/news/major-flooding-in-houston-{i}",
                "title": f"Major flooding in houston number {i}",
                "domain": "example.test",
                "published_at": s,
            }
            for i, s in enumerate(stamps)
        ],
    }


def _at(day, hour):
    return datetime(2017, 8, day, hour, 0, 0, tzinfo=timezone.utc)


class TestNoLookahead(unittest.TestCase):
    def setUp(self):
        self.feed = ArchivedNewsFeed(
            _corpus([
                "2017-08-27T06:00:00Z",
                "2017-08-27T12:00:00Z",
                "2017-08-27T18:00:00Z",
            ])
        )

    def test_only_published_articles_are_visible(self):
        got = self.feed.available_as_of(_at(27, 13))
        self.assertEqual(len(got), 2)
        self.assertTrue(all(a["published_at"] <= "2017-08-27T13:00:00Z" for a in got))

    def test_an_article_is_invisible_before_it_is_published(self):
        self.assertEqual(self.feed.available_as_of(_at(27, 5)), [])

    def test_an_article_published_exactly_now_is_visible(self):
        got = self.feed.available_as_of(_at(27, 6))
        self.assertEqual(len(got), 1)

    def test_the_lookback_window_drops_stale_articles(self):
        # A day later, nothing from the previous morning is still in a 6h window.
        self.assertEqual(
            self.feed.available_as_of(_at(28, 12), lookback_hours=6.0), []
        )

    def test_scoring_never_sees_a_future_article(self):
        result = self.feed.score_as_of(_at(27, 13))
        self.assertLessEqual(result["candidates_considered"], 2)


class TestBoundedResultPage(unittest.TestCase):
    def test_the_feed_returns_at_most_the_limit(self):
        feed = ArchivedNewsFeed(
            _corpus([f"2017-08-27T{h:02d}:00:00Z" for h in range(24)])
        )
        got = feed.available_as_of(_at(27, 23), limit=5)
        self.assertEqual(len(got), 5)

    def test_the_most_recent_articles_are_the_ones_kept(self):
        feed = ArchivedNewsFeed(
            _corpus([f"2017-08-27T{h:02d}:00:00Z" for h in range(24)])
        )
        got = feed.available_as_of(_at(27, 23), limit=3)
        self.assertEqual(
            [a["published_at"] for a in got],
            ["2017-08-27T21:00:00Z", "2017-08-27T22:00:00Z", "2017-08-27T23:00:00Z"],
        )


class TestTitleQuality(unittest.TestCase):
    def test_prose_headlines_are_usable(self):
        self.assertTrue(usable_title("Historic flooding leads to thousands of rescues"))

    def test_identifier_slugs_are_not_usable(self):
        for junk in (
            "B8428d2a38963b4cdf1ce6bb824562ab",
            "Article 0a361e20 83f4 5aeb 82e1 f362e8cf0281",
            "214673 2",
            "",
        ):
            self.assertFalse(usable_title(junk), junk)

    def test_unusable_titles_are_dropped_from_the_feed(self):
        corpus = _corpus(["2017-08-27T06:00:00Z"])
        corpus["articles"][0]["title"] = "a1b2c3d4e5f6a7b8"
        self.assertEqual(len(ArchivedNewsFeed(corpus)), 0)


class TestHeadlineRecovery(unittest.TestCase):
    def test_a_headline_is_recovered_from_the_path(self):
        self.assertEqual(
            headline_from_url(
                "http://www.chron.com/news/article/historic-flooding-in-houston.php"
            ),
            "Historic flooding in houston",
        )

    def test_a_trailing_article_id_is_stripped(self):
        self.assertEqual(
            headline_from_url(
                "http://x.test/news/article/evacuations-multiply-12159835.php"
            ),
            "Evacuations multiply",
        )


class TestGeocodedLocationContext(unittest.TestCase):
    """A geocoded corpus selected articles *for* a place, so it may say so."""

    def test_a_geocoded_corpus_supplies_the_county_dateline(self):
        feed = ArchivedNewsFeed(_corpus(["2017-08-27T06:00:00Z"], geocoded=True))
        item = feed._as_search_item(feed._articles[0])
        self.assertIn("Harris County, TX", item["html"])

    def test_an_ungeocoded_corpus_does_not_invent_one(self):
        feed = ArchivedNewsFeed(_corpus(["2017-08-27T06:00:00Z"], geocoded=False))
        item = feed._as_search_item(feed._articles[0])
        self.assertNotIn("Harris County, TX", item["html"])


if __name__ == "__main__":
    unittest.main()
