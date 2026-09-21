"""Disaster Sentinel runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path


def neo4j_uri() -> str:
    return os.environ.get("NEO4J_URI", "bolt://localhost:7687")


def neo4j_user() -> str:
    return os.environ.get("NEO4J_USER", "neo4j")


def neo4j_password() -> str:
    pw = os.environ.get("NEO4J_PASSWORD", "")
    if pw:
        return pw
    pw_file = os.environ.get("NEO4J_PASSWORD_FILE", "/mnt/secondary/neo4j/.password")
    try:
        return Path(pw_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def neo4j_database() -> str:
    return os.environ.get("NEO4J_DATABASE", "neo4j")


def ontology_source() -> str:
    return os.environ.get("ONTOLOGY_SOURCE", "stub")


def sentinel_ontology_ledger() -> str:
    return os.environ.get("SENTINEL_ONTOLOGY_LEDGER", "off")


def sentinel_ledger_dir() -> str:
    return os.environ.get("SENTINEL_LEDGER_DIR", "state/ledger")


def sentinel_structured_analyst() -> str:
    return os.environ.get("SENTINEL_STRUCTURED_ANALYST", "off")


def sentinel_output_ontology() -> str:
    """v1 (default) | off — OutputReport → multi-message Slack rendering."""
    return os.environ.get("SENTINEL_OUTPUT_ONTOLOGY", "v1")


#: Ontology ablation for evaluation. ``full`` is the shipped system; the other
#: settings disable an ontology layer so its contribution can be measured.
#:   full      role layer on,  belief layer on   (default)
#:   no_belief role layer on,  belief layer off  (no credibility admission gate)
#:   no_role   role layer off, belief layer on   (no phase logic, fixed cadence)
#:   none      both off                          (naive baseline)
ABLATION_MODES = ("full", "no_belief", "no_role", "none")


def sentinel_ablation() -> str:
    """Active ablation mode; unrecognised values fall back to ``full``."""
    mode = os.environ.get("SENTINEL_ABLATION", "full").strip().lower()
    return mode if mode in ABLATION_MODES else "full"


def belief_layer_enabled() -> bool:
    """False when reliability tiering and the confidence composite are ablated."""
    return sentinel_ablation() in ("full", "no_role")


def role_layer_enabled() -> bool:
    """False when event-phase transitions and phase cadence are ablated."""
    return sentinel_ablation() in ("full", "no_belief")


def county_flood_min_gauges() -> int:
    """Fresh, scorable official gauges required to assert an all-clear.

    The county-flood skill uses the same environment variable for its regional
    verdict.  Keeping the parser here avoids a reverse import from the runtime
    into skill-local code while ensuring the report and phase policies share
    one configured threshold.
    """
    raw = os.environ.get("COUNTY_FLOOD_MIN_GAUGES", "").strip()
    if not raw:
        return 5
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


#: Confidence displayed for every claim when the belief layer is off, so that
#: no source is preferred over another and ranking is uninformative. Admission
#: bypasses confidence and tier gates in this mode; this value is never an
#: admission threshold input.
#:
#: The value is arbitrary by nature: a system without confidence scoring has no
#: principled way to choose it. That matters for evaluation, because calibration
#: error against a constant predictor is simply the distance from that constant
#: to the observed accuracy, so the ablated configuration's calibration is a
#: function of this choice rather than of the ablation. It is overridable so the
#: evaluation can sweep it and report the range instead of a single figure.
_DEFAULT_ABLATED_UNIFORM_CONFIDENCE = 0.75


def ablated_uniform_confidence() -> float:
    raw = os.environ.get("SENTINEL_ABLATED_CONFIDENCE", "").strip()
    if not raw:
        return _DEFAULT_ABLATED_UNIFORM_CONFIDENCE
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return _DEFAULT_ABLATED_UNIFORM_CONFIDENCE


#: Backwards-compatible alias for the default.
ABLATED_UNIFORM_CONFIDENCE = _DEFAULT_ABLATED_UNIFORM_CONFIDENCE

#: How old a reading may be and still describe the present, in hours.
#:
#: A gauge that stops reporting keeps returning its last value, so without this
#: window a frozen reading counts as live evidence for as long as the sensor
#: stays down. Set to 0 to disable the window.
_DEFAULT_READING_MAX_AGE_HOURS = 6.0


def sentinel_reading_max_age_hours() -> float:
    raw = os.environ.get("SENTINEL_READING_MAX_AGE_HOURS", "").strip()
    if not raw:
        return _DEFAULT_READING_MAX_AGE_HOURS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_READING_MAX_AGE_HOURS


#: Whether gauges outside the county but in the same watershed may be used.
#:
#: A county with no scorable gauge of its own currently reports insufficient
#: evidence. A gauge upstream in the same subbasin is genuinely informative
#: about that county, unlike one in a neighbouring county across a divide, so
#: shared drainage rather than shared border is what admits it. Such a gauge is
#: weaker evidence about *this* county and is scored accordingly, and it may
#: never on its own escalate an event or satisfy an all-clear.
#:
#: Off by default: it changes which sources a mission fetches, so it is opt-in
#: until reviewed against a live deployment.
def watershed_neighbours_enabled() -> bool:
    return os.environ.get("SENTINEL_WATERSHED_NEIGHBOURS", "").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


#: Location credibility for a reading from the same watershed but another
#: county. Below the in-county value of 0.90 because the reading describes water
#: that is hydrologically connected to the monitored area rather than in it, and
#: above the 0.55 given to a location that does not resolve at all.
_DEFAULT_WATERSHED_LOCATION_PRECISION = 0.65


def watershed_location_precision() -> float:
    raw = os.environ.get("SENTINEL_WATERSHED_LOCATION_PRECISION", "").strip()
    if not raw:
        return _DEFAULT_WATERSHED_LOCATION_PRECISION
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return _DEFAULT_WATERSHED_LOCATION_PRECISION


#: Default strength of the reliability-tier discount at the admission gate.
DEFAULT_TIER_WEIGHT = 1.0


def sentinel_tier_weight() -> float:
    """How strongly the reliability tier discounts a claim at the admission gate.

    The T0-T3 tier was intended to gate decisions but reached them only
    indirectly. The per-extractor ``source`` sub-score carries a coarse binary
    signal (0.90 for tier 0, 0.75 otherwise), so official and unofficial
    evidence already differ slightly; the finer T0/T1/T2/T3 gradient lived only
    in ``source_reliability_prior`` and was read by the formatters and the
    ledger. This setting applies that gradient at admission.

    The discount changes the admission comparison only; the reported confidence
    is left untouched. Because ``source`` already varies with tier, reliability
    is reinforced here rather than introduced, so the admission floors in the
    monitoring rules may warrant retuning alongside this value.

    ``1.0`` (the default) applies the full tier prior. ``0.0`` restores the
    previous behaviour, in which the tier gated nothing.
    """
    raw = os.environ.get("SENTINEL_TIER_WEIGHT", "").strip()
    if not raw:
        return DEFAULT_TIER_WEIGHT
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return DEFAULT_TIER_WEIGHT


#: Whether an official warning stream may request verification acquisitions.
#:
#: The trigger's whole authority is scheduling: it may request an immediate
#: official-data acquisition and a bounded temporary cadence, and nothing else.
#: It cannot declare flooding, change the alert level, establish an all-clear,
#: fold recovery, or substitute for official hydrologic evidence.
#:
#: Off by default: it adds a warning-stream consultation to every heartbeat
#: poll and changes acquisition timing, so it is opt-in until validated.
def verification_trigger_enabled() -> bool:
    return os.environ.get("SENTINEL_VERIFICATION_TRIGGER", "").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


def sentinel_slack_log_file() -> str:
    """Append-only text log of every Slack outbound payload."""
    return os.environ.get(
        "SENTINEL_SLACK_LOG_FILE",
        "/mnt/secondary/home/vijaymuruganprimary/.openclaw/workspace/logs/slack_delivery.log",
    )
