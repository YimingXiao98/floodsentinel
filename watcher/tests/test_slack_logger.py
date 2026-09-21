"""Tests for Slack delivery text logger."""

from __future__ import annotations

from pathlib import Path

from watcher.slack_logger import log_slack_delivery, slack_log_path


def test_log_slack_delivery_appends_body(tmp_path: Path, monkeypatch) -> None:
    log_file = tmp_path / "slack_delivery.log"
    monkeypatch.setenv("SENTINEL_SLACK_LOG_FILE", str(log_file))

    path = log_slack_delivery(
        "Hello Slack\nLine 2",
        {"ok": True, "channel_id": "C123", "message_id": "m1"},
        kind="coordinator",
        part=1,
        parts_total=2,
    )
    assert path == log_file
    assert log_file.is_file()
    text = log_file.read_text(encoding="utf-8")
    assert "kind: coordinator" in text
    assert "part: 1/2" in text
    assert "channel: C123" in text
    assert "message_id: m1" in text
    assert "Hello Slack\nLine 2" in text
    assert "chars: 18" in text

    log_slack_delivery(
        "second",
        {"ok": False, "channel_id": "C123", "error": "boom"},
        kind="analyst",
    )
    text2 = log_file.read_text(encoding="utf-8")
    assert text2.count("========") >= 4
    assert "kind: analyst" in text2
    assert "error: boom" in text2
    assert "second" in text2


def test_slack_log_path_env(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.log"
    monkeypatch.setenv("SENTINEL_SLACK_LOG_FILE", str(target))
    assert slack_log_path() == target
