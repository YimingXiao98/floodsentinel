"""
belief_schema.py -- the Disaster Sentinel belief-state schema (Figure 1), as code.

Single source of truth for:
  - the typed objects the agent writes to the event ledger (Claim, Belief, Phase, ...)
  - validation at the ledger boundary
  - the JSON schema handed to the analyst-skill LLM for structured Intelligence output

Field names match Figure 1 / the position paper's Layer A belief state.
Requires: pydantic>=2
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field


# ---- controlled vocabularies -------------------------------------------------

class SourceClass(str, Enum):
    gauge = "gauge"                  # USGS / rain gauges
    official_feed = "official_feed"  # NWS, FEMA declaration, TxDOT closures, AWS weather
    model = "model"                  # flood / surge / fire-spread models
    crowd = "crowd"                  # social posts, citizen reports, news feeds
    imagery = "imagery"              # cameras, drones, satellite


class ReliabilityTier(str, Enum):
    T0 = "T0"  # authoritative (rated gauge, federally-declared FEMA)
    T1 = "T1"  # high (official feed / rated sensor), may be stale
    T2 = "T2"  # medium (model output, curated crowd)
    T3 = "T3"  # low (raw social / news)


class ClaimStatus(str, Enum):
    active = "active"
    superseded = "superseded"
    unresolved = "unresolved"  # held open in a contradiction


class BeliefMethod(str, Enum):
    rated_threshold = "rated_threshold"
    cold_start_delta = "cold_start_delta"
    fema_declared = "fema_declared"
    model_probability = "model_probability"
    corroborated = "corroborated"


class DisasterPhase(str, Enum):
    early_watch = "early_watch"
    elevated_watch = "elevated_watch"
    imminent = "imminent"  # TODO(ontology-integration): confirm with ontology owner
    active_incident = "active_incident"
    response = "response"
    recovery = "recovery"
    folded = "folded"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


# ---- entities ----------------------------------------------------------------

class Location(BaseModel):
    type: Literal["Location"] = "Location"
    id: str = Field(default_factory=lambda: _id("loc"))
    name: str
    scope: Literal["point", "segment", "neighborhood", "tract", "hex", "city", "county"]
    lat: Optional[float] = None
    lon: Optional[float] = None


class System(BaseModel):  # affected infrastructure
    type: Literal["System"] = "System"
    id: str = Field(default_factory=lambda: _id("sys"))
    kind: Literal["road", "gauge", "power", "water", "comms", "medical", "building", "channel"]
    ref: Optional[str] = None  # e.g. road id / facility name


class DataSource(BaseModel):
    type: Literal["DataSource"] = "DataSource"
    id: str
    name: str
    source_class: SourceClass
    reliability_tier: ReliabilityTier


class Claim(BaseModel):
    type: Literal["Claim"] = "Claim"
    id: str = Field(default_factory=lambda: _id("c"))
    content: str
    info_type: str                      # "river-stage observation", "road-status", ...
    system: Optional[str] = None        # System.id
    location: str                       # Location.id
    timestamp: datetime
    source_id: str                      # DataSource.id
    raw_reliability: float              # source-class prior, 0..1
    status: ClaimStatus = ClaimStatus.active
    supersedes: Optional[str] = None    # Claim.id this replaces


class Belief(BaseModel):  # derived score on a claim
    type: Literal["Belief"] = "Belief"
    claim_id: str
    score: float                        # 0..1
    method: BeliefMethod
    confidence: float                   # 0..1 (0.45 typical for cold_start_delta)
    uncertainty: list[str] = []


class Phase(BaseModel):
    type: Literal["Phase"] = "Phase"
    id: str = Field(default_factory=lambda: _id("ph"))
    current: DisasterPhase
    changed_from: Optional[DisasterPhase] = None
    changed_at: datetime
    trigger: str                        # Claim.id or reason


class Intelligence(BaseModel):  # analyst-skill output
    type: Literal["Intelligence"] = "Intelligence"
    id: str = Field(default_factory=lambda: _id("i"))
    assessment: str                     # one-line bottom line
    confidence: float
    evidence: list[str]                 # Claim.ids used
    contradictions: list[str] = []
    uncertainties: list[str] = []
    location: str                       # Location.id
    valid_until: Optional[datetime] = None
    sources: list[str] = []             # DataSource.ids / URLs


LedgerRecord = Union[Location, System, DataSource, Claim, Belief, Phase, Intelligence]

_REGISTRY = {m.__name__: m for m in (Location, System, DataSource, Claim, Belief, Phase, Intelligence)}


# ---- ledger I/O --------------------------------------------------------------

def append(obj: BaseModel, path: str = "ledger.jsonl") -> None:
    """Validate-by-construction is already done; write one NDJSON line."""
    with open(path, "a") as f:
        f.write(obj.model_dump_json() + "\n")


def load(path: str = "ledger.jsonl") -> list[BaseModel]:
    """Read the ledger back into typed objects (validates each line)."""
    out: list[BaseModel] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            out.append(_REGISTRY[raw["type"]].model_validate(raw))
    return out
