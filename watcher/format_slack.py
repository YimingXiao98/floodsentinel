"""Slack and human renderers for the OutputReport ontology (v1)."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from watcher.ontology.output_schema import (
    DEFAULT_SECTION_PRIORITY,
    OutputReport,
    Section,
    SectionType,
)

logger = logging.getLogger(__name__)

DEFAULT_SLACK_CHAR_LIMIT = 3500


def slack_char_limit(override: Optional[int] = None) -> int:
    if override is not None:
        return int(override)
    raw = os.environ.get("SENTINEL_SLACK_CHAR_LIMIT", str(DEFAULT_SLACK_CHAR_LIMIT))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SLACK_CHAR_LIMIT


def _ordered(sections: List[Section]) -> List[Section]:
    """Header always first, footer always last; else by priority then declaration order."""
    indexed = list(enumerate(sections))

    def sort_key(item: tuple[int, Section]) -> tuple[int, int, int]:
        idx, section = item
        if section.section_type == SectionType.MISSION_HEADER:
            bucket = 0
        elif section.section_type == SectionType.DELIVERY_FOOTER:
            bucket = 2
        else:
            bucket = 1
        priority = section.priority
        if priority == 50:
            priority = DEFAULT_SECTION_PRIORITY.get(section.section_type, 50)
        return (bucket, priority, idx)

    return [section for _, section in sorted(indexed, key=sort_key)]


def _section_header_slack(section: Section, *, continuation: bool = False) -> str:
    title = section.title
    if continuation:
        title = f"{title} (cont.)"
    emoji = (section.emoji or "").strip()
    if emoji:
        return f"{emoji} *{title}*"
    return f"*{title}*"


def _render_section(section: Section, *, continuation: bool = False) -> str:
    header = _section_header_slack(section, continuation=continuation)
    body = "\n".join(section.lines)
    if body:
        return f"{header}\n{body}"
    return header


def _split_section(section: Section, limit: int) -> List[str]:
    """Split an oversized section on line boundaries with a (cont.) marker."""
    cont_header = _section_header_slack(section, continuation=True)
    first_header = _section_header_slack(section, continuation=False)
    chunks: List[str] = []
    buf: List[str] = []
    active_header = first_header
    buf_len = len(active_header) + 1

    def flush() -> None:
        nonlocal buf, buf_len, active_header
        if buf:
            chunks.append(active_header + "\n" + "\n".join(buf))
        elif not chunks:
            chunks.append(active_header)
        buf = []
        active_header = cont_header
        buf_len = len(active_header) + 1

    for line in section.lines:
        # Hard line that alone exceeds limit: send it in its own chunk (may overflow).
        if len(active_header) + 1 + len(line) > limit and not buf:
            chunks.append(active_header + "\n" + line)
            active_header = cont_header
            buf_len = len(active_header) + 1
            continue
        if buf_len + len(line) + 1 > limit:
            flush()
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        chunks.append(active_header + "\n" + "\n".join(buf))
    elif not chunks:
        chunks.append(first_header)
    return chunks


def build_slack_messages(report: OutputReport, limit: Optional[int] = None) -> List[str]:
    """Render an OutputReport into one or more Slack-safe message strings."""
    limit = slack_char_limit(limit)
    sections = _ordered(report.sections)
    messages: List[str] = []
    buffer: List[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buffer, buf_len
        if buffer:
            messages.append("\n\n".join(buffer))
            buffer, buf_len = [], 0

    for section in sections:
        rendered = _render_section(section)
        separator = 2 if buffer else 0
        if buf_len + len(rendered) + separator <= limit:
            buffer.append(rendered)
            buf_len += len(rendered) + separator
            continue
        flush()
        if len(rendered) <= limit:
            buffer.append(rendered)
            buf_len = len(rendered)
        elif section.atomic or not section.splittable:
            logger.warning(
                "Atomic/unsplittable section %s exceeds Slack limit (%d > %d); sending oversized",
                section.section_type.value,
                len(rendered),
                limit,
            )
            messages.append(rendered)
        else:
            messages.extend(_split_section(section, limit))
    flush()

    if len(messages) > 1:
        short = (report.mission_id or "mission")[:8]
        total = len(messages)
        messages = [
            m if i == 0 else f"*[{short}] Part {i + 1}/{total}*\n\n{m}"
            for i, m in enumerate(messages)
        ]
    return messages


def render_human(report: OutputReport) -> str:
    """Concatenate all sections for logs / CLI — no length limit."""
    parts: List[str] = []
    for section in _ordered(report.sections):
        emoji = (section.emoji or "").strip()
        title = f"## {emoji} {section.title}".strip() if emoji else f"## {section.title}"
        body = "\n".join(section.lines)
        parts.append(f"{title}\n{body}" if body else title)
    return "\n\n".join(parts)
