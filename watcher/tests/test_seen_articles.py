"""Tests for seen-article URL persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from watcher import seen_articles as sa


def test_record_and_load_seen_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "_STATE_DIR", Path(tmp_path))
    articles = [
        {"url": "https://news.example.com/a", "title": "A", "page_score": 0.9},
        {"url": "https://news.example.com/b/", "title": "B", "page_score": 0.8},
    ]
    assert sa.record_seen_articles("48201", articles) == 2
    seen = sa.load_seen_article_urls("48201")
    assert "https://news.example.com/a" in seen
    assert "https://news.example.com/b" in seen


def test_expired_urls_are_not_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "_STATE_DIR", Path(tmp_path))
    monkeypatch.setenv("SEEN_ARTICLES_TTL_HOURS", "24")
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    sa.record_seen_articles(
        "48201",
        [{"url": "https://news.example.com/old", "title": "Old"}],
        now=now - timedelta(hours=48),
    )
    assert sa.load_seen_article_urls("48201", now=now) == set()


def test_filter_new_articles_skips_seen():
    from watcher.article_evidence_bridge import _filter_new_articles

    articles = [
        {"url": "https://a.example/1", "page_score": 0.9},
        {"url": "https://b.example/2", "page_score": 0.9},
    ]
    fresh, skipped = _filter_new_articles(articles, {"https://a.example/1"})
    assert skipped == 1
    assert len(fresh) == 1
    assert fresh[0]["url"] == "https://b.example/2"
