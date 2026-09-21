import os
import tempfile

from watcher.ontology.belief_schema import Belief, Claim, DataSource
from watcher.ontology.ledger import read_all, write


def test_ledger_write_and_read_roundtrip(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SENTINEL_LEDGER_DIR", tmp)

        ds = DataSource(
            id="ds1",
            name="Test Gauge",
            source_class="gauge",
            reliability_tier="T1",
        )
        write("mission_abc", ds)

        claim = Claim(
            id="c1",
            content="stage 10 ft",
            info_type="river-stage observation",
            location="loc1",
            timestamp="2026-06-30T14:05:00Z",
            source_id="ds1",
            raw_reliability=0.8,
        )
        write("mission_abc", claim)

        belief = Belief(
            claim_id="c1",
            score=0.7,
            method="corroborated",
            confidence=0.7,
        )
        write("mission_abc", belief)

        records = read_all("mission_abc")
        assert len(records) == 3
        assert {r.type for r in records} == {"DataSource", "Claim", "Belief"}
        assert os.path.exists(os.path.join(tmp, "mission_abc.jsonl"))


def test_ledger_isolates_missions(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SENTINEL_LEDGER_DIR", tmp)
        write(
            "mission_a",
            DataSource(id="d1", name="A", source_class="gauge", reliability_tier="T1"),
        )
        write(
            "mission_b",
            DataSource(id="d2", name="B", source_class="gauge", reliability_tier="T1"),
        )
        assert len(read_all("mission_a")) == 1
        assert len(read_all("mission_b")) == 1
