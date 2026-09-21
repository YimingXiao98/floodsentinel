"""Data models for county flood aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GaugeReading:
    source: str = "nwps"
    shef_id: str = ""
    site_no: Optional[str] = None
    name: str = ""
    observed_primary: Optional[float] = None
    observed_primary_unit: Optional[str] = None
    observed_flood_category: Optional[str] = None
    observed_valid_time: Optional[str] = None
    forecast_primary: Optional[float] = None
    forecast_primary_unit: Optional[str] = None
    forecast_flood_category: Optional[str] = None
    forecast_valid_time: Optional[str] = None
    flood_state_score: Optional[float] = None
    streamflow_cfs: Optional[float] = None
    action_stage: Optional[float] = None
    flood_stage: Optional[float] = None
    moderate_stage: Optional[float] = None
    major_stage: Optional[float] = None
    csv_location_name: Optional[str] = None
    error: Optional[str] = None
    classification_method: Optional[str] = None
    threshold_source: Optional[str] = None
    data_qualifier: Optional[str] = None
    location_relation: Optional[str] = None
    location_basis: Optional[str] = None
    unscored_reason: Optional[str] = None
    priority_tier: Optional[int] = None


@dataclass
class FloodAlert:
    id: str = ""
    event: str = ""
    headline: str = ""
    severity: str = ""
    urgency: str = ""
    area_desc: str = ""
    effective: Optional[str] = None
    # When the hazard itself is forecast to begin, which for an advance-issued
    # river flood warning is later than the moment the product was sent.
    onset: Optional[str] = None
    expires: Optional[str] = None
    status: str = ""
    nws_zone: Optional[str] = None
    flood_state_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "headline": self.headline,
            "severity": self.severity,
            "urgency": self.urgency,
            "area_desc": self.area_desc,
            "effective": self.effective,
            "onset": self.onset,
            "expires": self.expires,
            "status": self.status,
            "nws_zone": self.nws_zone,
            "flood_state_score": self.flood_state_score,
        }


@dataclass
class DisasterDeclaration:
    disaster_number: Optional[int] = None
    fema_declaration_string: str = ""
    declaration_title: str = ""
    declaration_date: Optional[str] = None
    incident_begin_date: Optional[str] = None
    incident_end_date: Optional[str] = None
    designated_area: str = ""
    fips_state_code: Optional[str] = None
    fips_county_code: Optional[str] = None
    ia_program_declared: Optional[bool] = None
    pa_program_declared: Optional[bool] = None
    hm_program_declared: Optional[bool] = None
    flood_state_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "disaster_number": self.disaster_number,
            "fema_declaration_string": self.fema_declaration_string,
            "declaration_title": self.declaration_title,
            "declaration_date": self.declaration_date,
            "incident_begin_date": self.incident_begin_date,
            "incident_end_date": self.incident_end_date,
            "designated_area": self.designated_area,
            "fips_state_code": self.fips_state_code,
            "fips_county_code": self.fips_county_code,
            "ia_program_declared": self.ia_program_declared,
            "pa_program_declared": self.pa_program_declared,
            "hm_program_declared": self.hm_program_declared,
            "flood_state_score": self.flood_state_score,
        }


@dataclass
class SourceResult:
    source_id: str
    gauges: List[GaugeReading] = field(default_factory=list)
    fetch_errors: List[Dict[str, str]] = field(default_factory=list)
    discovered_flood_categories: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    alerts: List[FloodAlert] = field(default_factory=list)
    disasters: List[DisasterDeclaration] = field(default_factory=list)


@dataclass
class CountyFloodSummary:
    average_flood_state: Optional[float]
    average_primary_reading: Optional[float]
    average_primary_unit: Optional[str]
    gauges_queried: int
    gauges_with_data: int
    gauges_scored: int
    max_flood_state: Optional[float] = None
    regional_verdict: Optional[str] = None
    is_flooding: bool = False
    confidence: Optional[str] = None
    evaluation_mode: Optional[str] = None
    scoring_coverage: Optional[float] = None
    gauges_unscored: int = 0
    verdict_gauge_count: int = 0
    gauges_excluded: int = 0
    min_gauges_required: Optional[int] = None
    classification_methods: Optional[Dict[str, int]] = None
    flooding_gauge_ids: Optional[List[str]] = None
    supplementary_signals: Optional[List[Dict[str, Any]]] = None
    overall_confidence: Optional[float] = None
    confidence_components: Optional[Dict[str, float]] = None
    evidence_tier: Optional[str] = None
    api_weight_hint: Optional[str] = None
    csv_truncated: bool = False
    supplementary_scores: Optional[Dict[str, float]] = None
    scored_disaster_count: int = 0
    scored_alert_count: int = 0


@dataclass
class CountyFloodResult:
    county: str
    state: str
    fips: Optional[str]
    location: str
    generated_at: str
    summary: CountyFloodSummary
    sources: Dict[str, Any]
    gauges: List[Dict[str, Any]]
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    disasters: List[Dict[str, Any]] = field(default_factory=list)
    api_resolution: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "county": self.county,
            "state": self.state,
            "fips": self.fips,
            "location": self.location,
            "generated_at": self.generated_at,
            "api_resolution": self.api_resolution,
            "alerts": self.alerts,
            "disasters": self.disasters,
            "summary": {
                "average_flood_state": self.summary.average_flood_state,
                "average_primary_reading": self.summary.average_primary_reading,
                "average_primary_unit": self.summary.average_primary_unit,
                "gauges_queried": self.summary.gauges_queried,
                "gauges_with_data": self.summary.gauges_with_data,
                "gauges_scored": self.summary.gauges_scored,
                "max_flood_state": self.summary.max_flood_state,
                "regional_verdict": self.summary.regional_verdict,
                "is_flooding": self.summary.is_flooding,
                "confidence": self.summary.confidence,
                "evaluation_mode": self.summary.evaluation_mode,
                "scoring_coverage": self.summary.scoring_coverage,
                "gauges_unscored": self.summary.gauges_unscored,
                "verdict_gauge_count": self.summary.verdict_gauge_count,
                "gauges_excluded": self.summary.gauges_excluded,
                "min_gauges_required": self.summary.min_gauges_required,
                "classification_methods": self.summary.classification_methods,
                "flooding_gauge_ids": self.summary.flooding_gauge_ids,
                "supplementary_signals": self.summary.supplementary_signals,
                "overall_confidence": self.summary.overall_confidence,
                "confidence_components": self.summary.confidence_components,
                "evidence_tier": self.summary.evidence_tier,
                "api_weight_hint": self.summary.api_weight_hint,
                "csv_truncated": self.summary.csv_truncated,
                "supplementary_scores": self.summary.supplementary_scores,
                "scored_disaster_count": self.summary.scored_disaster_count,
                "scored_alert_count": self.summary.scored_alert_count,
            },
            "sources": self.sources,
            "gauges": self.gauges,
        }
