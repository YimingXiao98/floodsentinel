"""Tests for fetch_full_text_for_verdict helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from article_search import (  # noqa: E402
    _looks_like_google_news_wrapper,
    _strip_html_to_text,
    fetch_full_text_for_verdict,
)


class TestFetchFullTextHelpers(unittest.TestCase):
    def test_google_news_wrapper_detection(self):
        self.assertTrue(_looks_like_google_news_wrapper("https://news.google.com/rss/articles/xyz", ""))
        self.assertFalse(
            _looks_like_google_news_wrapper(
                "https://abc13.com/flooding",
                "<html>...long article body here..." + "x" * 2000,
            )
        )

    def test_strip_html_to_text(self):
        text = _strip_html_to_text(
            "<html><body><p>Flooding hit <b>Harris County</b> today.</p></body></html>"
        )
        self.assertIn("Flooding hit Harris County today.", text)

    def test_fetch_full_text_fallback_on_bad_url(self):
        article = {"url": "", "title": "Flood headline", "snippet": "Some flooding reported."}
        text, source = fetch_full_text_for_verdict(article)
        self.assertEqual(source, "snippet_fallback")
        self.assertIn("Flood headline", text)


if __name__ == "__main__":
    unittest.main()
