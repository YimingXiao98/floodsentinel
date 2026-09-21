from datetime import datetime, timezone

import pytest

from watcher.ontology.adapters import (
    confidence_to_belief,
    evidence_claim_to_claim,
    phase_transition_to_phase,
    source_record_to_datasource,
)
from watcher.ontology.belief_schema import Belief, Claim, DataSource, Phase


def test_source_record_to_datasource_usgs():
    ds = source_record_to_datasource(
        {
            "source_id": "usgs_08075000",
            "source_type": "official",
            "ontology_source_type": "USGSGaugeSource",
            "trust_tier": {"level": 0, "name": "T0"},
            "source_name": "USGS Gauges",
        }
    )
    assert isinstance(ds, DataSource)
    assert ds.source_class == "gauge"
    assert ds.reliability_tier == "T0"


def test_source_record_to_datasource_unknown_type_defaults_to_model():
    ds = source_record_to_datasource(
        {
            "source_id": "x",
            "source_type": "some_new_source",
            "trust_tier": "T2",
        }
    )
    assert ds.source_class == "model"


def test_evidence_claim_to_claim_basic():
    claim = evidence_claim_to_claim(
        {
            "claim_id": "claim_001",
            "claim_type": "GaugeObservation",
            "source_id": "usgs_08075000",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "value": {"stage_ft": 12.3, "flood_state_score": 2},
        },
        location_id="loc_meyerland",
    )
    assert isinstance(claim, Claim)
    assert claim.info_type == "GaugeObservation"
    assert claim.status == "active"
    assert claim.supersedes is None
    assert "12.3" in claim.content


def test_evidence_claim_to_claim_with_supersession():
    claim = evidence_claim_to_claim(
        {
            "claim_id": "claim_002",
            "claim_type": "GaugeObservation",
            "source_id": "usgs_08075000",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "value": {"stage_ft": 13.0},
            "supersedes_claim_id": "claim_001",
        },
        location_id="loc_meyerland",
    )
    assert claim.supersedes == "claim_001"


def test_confidence_to_belief_pre_phase2_default():
    belief = confidence_to_belief("claim_001", {"overall_score": 0.72})
    assert isinstance(belief, Belief)
    assert belief.method == "corroborated"
    assert belief.confidence == 0.72


def test_confidence_to_belief_decomposed():
    belief = confidence_to_belief(
        "claim_001",
        {
            "overall_score": 0.72,
            "method": "cold_start_delta",
            "method_confidence": 0.45,
            "uncertainty_notes": ["no rated threshold; delta-based"],
        },
    )
    assert belief.method == "cold_start_delta"
    assert belief.confidence == 0.45
    assert belief.uncertainty == ["no rated threshold; delta-based"]


def test_phase_transition_to_phase():
    phase = phase_transition_to_phase(
        {
            "new_phase": "elevated_watch",
            "old_phase": "early_watch",
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        trigger_claim_id="claim_001",
    )
    assert isinstance(phase, Phase)
    assert phase.current == "elevated_watch"
    assert phase.changed_from == "early_watch"
    assert phase.trigger == "claim_001"
