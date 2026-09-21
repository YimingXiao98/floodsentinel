"""Post coordinator reports to the configured Slack channel."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from watcher.slack_logger import log_slack_delivery

DEFAULT_SLACK_CHANNEL = "C0ATLAJTC0L"
MAX_SLACK_MESSAGE_CHARS = 3900
MULTI_MESSAGE_SEND_DELAY_SEC = 0.3


def slack_channel_id() -> str:
    """Resolve Slack channel id from SENTINEL_SLACK_CHANNEL (default: C0ATLAJTC0L)."""
    raw = os.environ.get("SENTINEL_SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL).strip()
    if raw.lower().startswith("channel:"):
        raw = raw.split(":", 1)[1].strip()
    return raw or DEFAULT_SLACK_CHANNEL


def deliver_slack_message(
    message: str,
    *,
    truncate: bool = True,
    kind: str = "message",
    part: Optional[int] = None,
    parts_total: Optional[int] = None,
) -> Dict[str, Any]:
    """Send a plain-text/markdown message to the configured Slack channel."""
    message = str(message or "").strip()
    if not message:
        result = {"ok": False, "skipped": True, "reason": "no_message"}
        log_slack_delivery("", result, kind=kind, part=part, parts_total=parts_total)
        return result

    if truncate and len(message) > MAX_SLACK_MESSAGE_CHARS:
        message = message[:MAX_SLACK_MESSAGE_CHARS] + "\n… (truncated)"

    channel = slack_channel_id()
    try:
        proc = subprocess.run(
            [
                "openclaw",
                "message",
                "send",
                "--channel",
                "slack",
                "--target",
                channel,
                "-m",
                message,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except Exception as exc:
        result = {"ok": False, "channel_id": channel, "error": str(exc)}
        log_slack_delivery(message, result, kind=kind, part=part, parts_total=parts_total)
        return result

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        result = {"ok": False, "channel_id": channel, "error": err}
        log_slack_delivery(message, result, kind=kind, part=part, parts_total=parts_total)
        return result

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        result = {"ok": True, "channel_id": channel, "raw": (proc.stdout or "").strip()}
        log_slack_delivery(message, result, kind=kind, part=part, parts_total=parts_total)
        return result

    send_result = (payload.get("payload") or {}).get("result") or {}
    result = {
        "ok": bool((payload.get("payload") or {}).get("ok", True)),
        "channel_id": send_result.get("channelId") or channel,
        "message_id": send_result.get("messageId"),
    }
    log_slack_delivery(message, result, kind=kind, part=part, parts_total=parts_total)
    return result


def deliver_slack_messages(
    messages: List[str],
    *,
    delay_sec: float = MULTI_MESSAGE_SEND_DELAY_SEC,
    kind: str = "message",
) -> Dict[str, Any]:
    """Send multiple Slack messages sequentially (order preserved; no gather)."""
    cleaned = [str(m or "").strip() for m in messages if str(m or "").strip()]
    if not cleaned:
        result = {"ok": False, "skipped": True, "reason": "no_message"}
        log_slack_delivery("", result, kind=kind)
        return result

    results: List[Dict[str, Any]] = []
    total = len(cleaned)
    for i, msg in enumerate(cleaned):
        # Parts are pre-chunked; skip legacy truncation so atomic overflow can pass.
        result = deliver_slack_message(
            msg,
            truncate=False,
            kind=kind,
            part=i + 1,
            parts_total=total,
        )
        results.append(result)
        if not result.get("ok") and not result.get("skipped"):
            return {
                "ok": False,
                "channel_id": result.get("channel_id"),
                "error": result.get("error") or "send_failed",
                "messages_attempted": i + 1,
                "messages_total": total,
                "results": results,
            }
        if i < total - 1 and delay_sec > 0:
            time.sleep(delay_sec)

    last = results[-1] if results else {}
    return {
        "ok": True,
        "channel_id": last.get("channel_id"),
        "message_id": last.get("message_id"),
        "messages_sent": len(results),
        "results": results,
    }


def deliver_coordinator_slack(data: Dict[str, Any]) -> Dict[str, Any]:
    """Send coordinator slack_message(s) to Slack via openclaw message send."""
    if data.get("error"):
        result = {"ok": False, "skipped": True, "reason": "error_result"}
        log_slack_delivery("", result, kind="coordinator")
        return result

    messages = data.get("slack_messages")
    if isinstance(messages, list) and messages:
        return deliver_slack_messages(messages, kind="coordinator")

    message = str(data.get("slack_message") or "").strip()
    if not message:
        result = {"ok": False, "skipped": True, "reason": "no_slack_message"}
        log_slack_delivery("", result, kind="coordinator")
        return result

    return deliver_slack_message(message, kind="coordinator")


def deliver_analyst_slack(data: Dict[str, Any]) -> Dict[str, Any]:
    """Send analyst situation summary to Slack."""
    if data.get("error"):
        result = {"ok": False, "skipped": True, "reason": "error_result"}
        log_slack_delivery("", result, kind="analyst")
        return result

    messages = data.get("slack_messages") or data.get("analyst_slack_messages")
    if isinstance(messages, list) and messages:
        return deliver_slack_messages(messages, kind="analyst")

    message = str(data.get("analyst_slack_message") or "").strip()
    if not message:
        result = {"ok": False, "skipped": True, "reason": "no_analyst_slack_message"}
        log_slack_delivery("", result, kind="analyst")
        return result

    return deliver_slack_message(message, kind="analyst")
