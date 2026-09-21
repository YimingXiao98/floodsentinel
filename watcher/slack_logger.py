"""Append-only text logger for every Slack outbound payload."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

_LOCK = threading.Lock()

DEFAULT_SLACK_LOG_FILE = (
    "/mnt/secondary/home/vijaymuruganprimary/.openclaw/workspace/logs/slack_delivery.log"
)


def slack_log_path() -> Path:
    """Resolve the Slack delivery log file (SENTINEL_SLACK_LOG_FILE)."""
    try:
        from watcher.config import sentinel_slack_log_file

        raw = (sentinel_slack_log_file() or "").strip()
    except Exception:
        raw = (os.environ.get("SENTINEL_SLACK_LOG_FILE") or "").strip()
    return Path(raw or DEFAULT_SLACK_LOG_FILE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def log_slack_delivery(
    message: str,
    result: Mapping[str, Any],
    *,
    kind: str = "message",
    part: Optional[int] = None,
    parts_total: Optional[int] = None,
) -> Path:
    """Append one Slack send attempt (body + result metadata) to the log file.

    Never raises — logging must not break delivery.
    """
    path = slack_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = str(message or "")
        channel = result.get("channel_id") or ""
        ok = result.get("ok")
        skipped = result.get("skipped")
        error = result.get("error") or result.get("reason") or ""
        message_id = result.get("message_id") or ""

        part_label = ""
        if part is not None:
            if parts_total is not None:
                part_label = f"part: {part}/{parts_total}\n"
            else:
                part_label = f"part: {part}\n"

        block = (
            f"{'=' * 72}\n"
            f"ts: {_utc_now()}\n"
            f"kind: {kind}\n"
            f"{part_label}"
            f"channel: {channel}\n"
            f"ok: {ok}\n"
            f"skipped: {skipped}\n"
            f"message_id: {message_id}\n"
            f"error: {error}\n"
            f"chars: {len(body)}\n"
            f"--- message ---\n"
            f"{body}\n"
            f"{'=' * 72}\n\n"
        )
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(block)
    except Exception:
        # Best-effort only.
        return path
    return path
