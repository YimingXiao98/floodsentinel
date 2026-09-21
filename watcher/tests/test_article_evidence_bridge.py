"""Unit tests for article evidence bridge."""

from __future__ import annotations

import unittest

from watcher.article_evidence_bridge import (
    ARTICLE_CONFIDENCE_THRESHOLD,
    _high_confidence_articles,
)


class ArticleEvidenceBridgeTests(unittest.TestCase):
    def test_filters_by_page_score_threshold(self):
        sources = [
            {"url": "https://a.example", "title": "High", "page_score": 0.82},
            {"url": "https://b.example", "title": "At threshold", "page_score": 0.75},
            {"url": "https://c.example", "title": "Low", "page_score": 0.40},
        ]
        result = _high_confidence_articles(sources, threshold=ARTICLE_CONFIDENCE_THRESHOLD)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://a.example")

    def test_empty_when_none_above_threshold(self):
        sources = [{"url": "https://x.example", "page_score": 0.5}]
        self.assertEqual(_high_confidence_articles(sources), [])


if __name__ == "__main__":
    unittest.main()
