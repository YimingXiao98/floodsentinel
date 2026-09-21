import sys
from unittest.mock import MagicMock, patch

import pytest

from watcher.ontology.belief_schema import Belief, Claim, Intelligence


def test_rejects_fabricated_evidence_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_LEDGER_DIR", str(tmp_path))

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    resp = MagicMock()
    resp.parsed_output = Intelligence(
        assessment="Rapid rise near Meyerland; elevated flood watch.",
        confidence=0.6,
        evidence=["c1", "c_FABRICATED"],
        location="loc1",
    )
    mock_client.messages.parse.return_value = resp
    mock_anthropic_mod.Anthropic.return_value = mock_client
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic_mod)

    from watcher.ontology.ledger import read_all, write
    from watcher.format_analyst import _build_structured_intelligence_summary

    write(
        "mission_x",
        Claim(
            id="c1",
            content="stage 10 ft",
            info_type="river-stage observation",
            location="loc1",
            timestamp="2026-06-30T14:05:00Z",
            source_id="ds1",
            raw_reliability=0.8,
        ),
    )
    write(
        "mission_x",
        Belief(claim_id="c1", score=0.7, method="corroborated", confidence=0.7),
    )

    data = {"mission_id": "mission_x", "mission_spec": {"county": "Harris", "state": "TX"}}
    _build_structured_intelligence_summary(data)
    assert data.get("intelligence") is None

    records = read_all("mission_x")
    assert not any(r.type == "Intelligence" for r in records)
