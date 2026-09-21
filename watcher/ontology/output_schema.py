"""
output_schema.py -- canonical typed report object for channel renderers.

Producers (format_report / format_analyst / format_ontology) append Section
objects to an OutputReport; Slack and human renderers consume the same object.
Requires: pydantic>=2
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SectionType(str, Enum):
    MISSION_HEADER = "mission_header"  # county, FIPS, mission id, timestamp
    PHASE_STATUS = "phase_status"  # phase, alert_level, heartbeat
    OFFICIAL_EVIDENCE = "official_evidence"  # NWPS/USGS/NWS/FEMA readings
    NEWS_EVIDENCE = "news_evidence"  # T2 article claims
    BELIEF_STATE = "belief_state"  # method vs source-prior breakdown
    ONTOLOGY_CONTEXT = "ontology_context"  # matched rule-layer transition rule
    REPORT_DELTA = "report_delta"  # changes since last run
    INTELLIGENCE = "intelligence"  # analyst-only structured summary
    DELIVERY_FOOTER = "delivery_footer"  # confirmation / mission-folded notice


# Default priority: lower = earlier. Footer always last; header always first
# (also enforced by the renderer regardless of caller-supplied values).
DEFAULT_SECTION_PRIORITY: dict[SectionType, int] = {
    SectionType.MISSION_HEADER: 0,
    SectionType.PHASE_STATUS: 1,
    SectionType.OFFICIAL_EVIDENCE: 2,
    SectionType.NEWS_EVIDENCE: 3,
    SectionType.BELIEF_STATE: 4,
    SectionType.ONTOLOGY_CONTEXT: 5,
    SectionType.INTELLIGENCE: 6,
    SectionType.REPORT_DELTA: 7,
    SectionType.DELIVERY_FOOTER: 99,
}


class Section(BaseModel):
    section_type: SectionType
    title: str  # e.g. "Official Hydrology"
    emoji: Optional[str] = None  # rendered before title on Slack only
    lines: list[str] = Field(default_factory=list)  # already-formatted content lines
    priority: int = 50  # lower = earlier; ties broken by declaration order
    atomic: bool = False  # True = never split across messages, even if overflow
    splittable: bool = True  # if section itself > limit, may split on its own lines


class OutputReport(BaseModel):
    ontology_output_version: str = "1.0"
    mission_id: str
    report_kind: Literal["coordinator", "analyst"]
    generated_at_utc: str
    sections: list[Section] = Field(default_factory=list)

    def append(self, section: Section) -> None:
        self.sections.append(section)
