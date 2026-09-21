"""Tests for OutputReport Slack chunking (SENTINEL_OUTPUT_ONTOLOGY=v1)."""

from __future__ import annotations

import logging

from watcher.format_report import build_coordinator_output_report, format_slack_report
from watcher.format_slack import build_slack_messages, render_human
from watcher.ontology.output_schema import (
    DEFAULT_SECTION_PRIORITY,
    OutputReport,
    Section,
    SectionType,
)


def _section(
    section_type: SectionType,
    title: str,
    lines: list[str],
    *,
    atomic: bool = False,
    splittable: bool = True,
    priority: int | None = None,
) -> Section:
    return Section(
        section_type=section_type,
        title=title,
        lines=lines,
        priority=priority if priority is not None else DEFAULT_SECTION_PRIORITY[section_type],
        atomic=atomic,
        splittable=splittable,
    )


def _report(*sections: Section, mission_id: str = "mission_abcdef12") -> OutputReport:
    return OutputReport(
        mission_id=mission_id,
        report_kind="coordinator",
        generated_at_utc="2026-07-22T12:00:00Z",
        sections=list(sections),
    )


def test_single_section_under_limit_one_message_no_part_header():
    report = _report(
        _section(SectionType.MISSION_HEADER, "Header", ["line1"], atomic=True, splittable=False),
        _section(SectionType.PHASE_STATUS, "Phase", ["ok"], atomic=True, splittable=False),
    )
    messages = build_slack_messages(report, limit=3500)
    assert len(messages) == 1
    assert "Part " not in messages[0]
    assert "*Header*" in messages[0]
    assert "*Phase*" in messages[0]


def test_multi_section_over_limit_splits_on_section_boundaries():
    big_official = ["gauge reading " + ("x" * 80) for _ in range(40)]
    report = _report(
        _section(SectionType.MISSION_HEADER, "Header", ["meta"], atomic=True, splittable=False),
        _section(SectionType.PHASE_STATUS, "Phase", ["stay"], atomic=True, splittable=False),
        _section(SectionType.OFFICIAL_EVIDENCE, "Official Hydrology", big_official),
        _section(SectionType.NEWS_EVIDENCE, "News", ["article A", "article B"]),
        _section(SectionType.DELIVERY_FOOTER, "Delivery", ["footer"], atomic=True, splittable=False),
    )
    messages = build_slack_messages(report, limit=1200)
    assert len(messages) >= 2
    assert "Part " not in messages[0] or "Part 1/" not in messages[0]
    assert "*[mission_] Part 2/" in messages[1] or "*[mission_a] Part 2/" in messages[1]
    # Header+phase should land in message 1
    assert "*Header*" in messages[0]
    assert "*Phase*" in messages[0]
    # No section title should be half-cut mid-word without (cont.)
    joined = "\n".join(messages)
    assert "Official Hydrology" in joined


def test_oversized_section_splits_on_lines_with_cont_marker():
    lines = [f"claim line {i} " + ("y" * 60) for i in range(50)]
    report = _report(
        _section(SectionType.MISSION_HEADER, "Header", ["h"], atomic=True, splittable=False),
        _section(SectionType.NEWS_EVIDENCE, "News article evidence", lines),
    )
    messages = build_slack_messages(report, limit=500)
    assert len(messages) >= 2
    cont = [m for m in messages if "(cont.)" in m]
    assert cont, "expected continuation marker for oversized section"
    # Part numbering across whole set
    assert any("Part " in m for m in messages[1:])


def test_atomic_oversized_section_sent_as_one_with_warning(caplog):
    huge = "Z" * 800
    report = _report(
        _section(
            SectionType.MISSION_HEADER,
            "Header",
            [huge],
            atomic=True,
            splittable=False,
        ),
    )
    with caplog.at_level(logging.WARNING):
        messages = build_slack_messages(report, limit=200)
    assert len(messages) == 1
    assert len(messages[0]) > 200
    assert any("exceeds Slack limit" in r.message for r in caplog.records)


def test_slack_char_limit_env_override(monkeypatch):
    monkeypatch.setenv("SENTINEL_SLACK_CHAR_LIMIT", "400")
    lines = [f"row {i} " + ("a" * 50) for i in range(30)]
    report = _report(
        _section(SectionType.OFFICIAL_EVIDENCE, "Official", lines),
        _section(SectionType.NEWS_EVIDENCE, "News", lines),
    )
    messages = build_slack_messages(report)  # uses env
    assert len(messages) >= 2
    # Each non-atomic chunked message should respect the limit (part prefix adds a bit on 2+)
    assert all(len(m) <= 400 + 80 for m in messages)  # margin for part header on later msgs


def test_render_human_concatenates_with_markdown_headers():
    report = _report(
        _section(SectionType.MISSION_HEADER, "Header", ["a"], atomic=True),
        _section(SectionType.OFFICIAL_EVIDENCE, "Official", ["b"]),
    )
    text = render_human(report)
    assert "## Header" in text
    assert "## Official" in text
    assert "a" in text and "b" in text


def test_flag_off_format_slack_report_unchanged(monkeypatch):
    """SENTINEL_OUTPUT_ONTOLOGY=off keeps legacy format_slack_report path byte-stable for callers."""
    monkeypatch.setenv("SENTINEL_OUTPUT_ONTOLOGY", "off")
    payload = {
        "mission_id": "mission_test",
        "mission_spec": {
            "county": "Harris",
            "state": "TX",
            "fips": "48201",
            "current_event_phase": "early_watch",
        },
        "belief_state": {"alert_level": "none"},
        "phase_decision": {"phase_changed": False, "alert_level": "none"},
        "evidence_claims": [],
        "report_delta": {"summary_bullets": ["No changes"]},
        "run_at": "2026-07-07T12:00:00Z",
        "source_records": [],
    }
    a = format_slack_report(payload)
    b = format_slack_report(payload)
    assert a == b
    assert "*Disaster Sentinel — Flood Monitoring Update*" in a
    # v1 builder is additive and does not alter legacy function
    report = build_coordinator_output_report(payload)
    assert report.report_kind == "coordinator"
    assert any(s.section_type == SectionType.MISSION_HEADER for s in report.sections)


def test_header_and_footer_ordering_enforced():
    report = _report(
        _section(SectionType.DELIVERY_FOOTER, "Delivery", ["bye"], priority=0),
        _section(SectionType.OFFICIAL_EVIDENCE, "Official", ["mid"], priority=99),
        _section(SectionType.MISSION_HEADER, "Header", ["hi"], priority=50, atomic=True),
    )
    messages = build_slack_messages(report, limit=3500)
    assert len(messages) == 1
    header_pos = messages[0].index("*Header*")
    official_pos = messages[0].index("*Official*")
    footer_pos = messages[0].index("*Delivery*")
    assert header_pos < official_pos < footer_pos


def test_default_output_ontology_is_v1(monkeypatch):
    monkeypatch.delenv("SENTINEL_OUTPUT_ONTOLOGY", raising=False)
    from watcher.config import sentinel_output_ontology

    assert sentinel_output_ontology() == "v1"


def test_attach_coordinator_slack_populates_messages_by_default(monkeypatch):
    monkeypatch.delenv("SENTINEL_OUTPUT_ONTOLOGY", raising=False)
    from watcher.cli import _attach_coordinator_slack

    data = {
        "mission_id": "mission_abcdef12",
        "mission_spec": {
            "county": "Harris",
            "state": "TX",
            "fips": "48201",
            "current_event_phase": "early_watch",
        },
        "belief_state": {"alert_level": "none"},
        "phase_decision": {"phase_changed": False, "alert_level": "none"},
        "evidence_claims": [],
        "report_delta": {"summary_bullets": ["No changes"]},
        "run_at": "2026-07-22T12:00:00Z",
        "source_records": [],
    }
    _attach_coordinator_slack(data)
    assert isinstance(data.get("slack_messages"), list)
    assert data["slack_messages"]
    assert data.get("slack_message")
    assert "… (truncated)" not in data["slack_message"]
