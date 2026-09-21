"""Orchestrate claim extraction from source snapshots."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from watcher.claims.confidence import credibility_for_claim
from watcher.claims.extract_nws import extract_nws_claims, extract_nws_claims_from_alert_dicts
from watcher.claims.extract_usgs import extract_usgs_claims
from watcher.claims.extract_county_flood import extract_county_flood_claims
from watcher.claims.validate import filter_claims_by_phase
from watcher.neo4j_client import get_latest_claim_id_for_source


def _apply_supersession(
    claims: List[Dict[str, Any]],
    *,
    snapshot: Dict[str, Any],
    mission_id: Optional[str],
) -> None:
    if snapshot.get("change_status") != "changed" or not mission_id:
        return
    for claim in claims:
        prior_id = get_latest_claim_id_for_source(
            claim.get("source_id", ""),
            claim.get("claim_type", ""),
            mission_id=mission_id,
        )
        if prior_id and prior_id != claim.get("claim_id"):
            claim["supersedes_claim_id"] = prior_id


def _snapshots_for_extraction(
    snapshots: List[Dict[str, Any]],
    *,
    only_changed: bool = True,
) -> List[Dict[str, Any]]:
    if not only_changed:
        return snapshots
    return [s for s in snapshots if s.get("change_status") in ("new", "changed")]


def extract_claims_from_snapshots(
    snapshots: List[Dict[str, Any]],
    *,
    monitoring_summary: Dict[str, Any],
    county: str,
    state: str,
    location: str,
    source_records: Optional[List[Dict[str, Any]]] = None,
    additional_claims: Optional[List[Dict[str, Any]]] = None,
    only_changed: bool = True,
    mission_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract, score, and validate snapshot and supplemental claims together.

    ``additional_claims`` lets non-snapshot evidence, currently curated news,
    enter the same phase admission batch as official-source claims.  Keeping
    this merge here prevents the live coordinator from appending evidence after
    validation while the replay validates the combined batch.
    """
    source_records = source_records or []
    records_by_id = {r.get("source_id"): r for r in source_records if r.get("source_id")}

    targets = _snapshots_for_extraction(snapshots, only_changed=only_changed)
    if not targets and snapshots:
        targets = snapshots

    raw_claims: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for snap in targets:
        sid = snap.get("source_id", "")
        rec = records_by_id.get(sid, {})
        ontology_type = rec.get("ontology_source_type", "")
        url = (rec.get("source_url") or snap.get("snapshot_url") or "").lower()

        if rec.get("resolution_strategy") == "county_flood_data_skill":
            if rec.get("ontology_source_type") == "NWSSource":
                snap_claims = extract_nws_claims_from_alert_dicts(
                    snap,
                    county=county,
                    state=state,
                    location=location,
                    source_record=rec,
                )
                if snap_claims:
                    _apply_supersession(snap_claims, snapshot=snap, mission_id=mission_id)
                    raw_claims.extend(snap_claims)
                continue
            cf_claims, fail = extract_county_flood_claims(
                snap,
                county=county,
                state=state,
                location=location,
                source_record=rec,
            )
            _apply_supersession(cf_claims, snapshot=snap, mission_id=mission_id)
            raw_claims.extend(cf_claims)
            if fail:
                failures.append(fail)
            continue

        snap_claims: List[Dict[str, Any]] = []
        if ontology_type == "NWSSource" or "weather.gov" in url:
            snap_claims = extract_nws_claims(
                snap,
                county=county,
                state=state,
                location=location,
                source_record=rec,
            )
        elif ontology_type == "USGSGaugeSource" or "usgs" in url or "waterdata" in url or "waterservices" in url:
            usgs_claims, fail = extract_usgs_claims(
                snap,
                county=county,
                state=state,
                location=location,
                source_record=rec,
            )
            snap_claims = usgs_claims
            if fail:
                failures.append(fail)

        if snap_claims:
            _apply_supersession(snap_claims, snapshot=snap, mission_id=mission_id)
            raw_claims.extend(snap_claims)

    raw_claims.extend(additional_claims or [])
    accepted, rejected = filter_claims_by_phase(raw_claims, monitoring_summary)
    assessments = [credibility_for_claim(c) for c in accepted]

    return {
        "evidence_claims": accepted,
        "rejected_claims": rejected,
        "extraction_failures": failures,
        "credibility_assessments": assessments,
        "claims_extracted": len(raw_claims),
        "claims_accepted": len(accepted),
    }
