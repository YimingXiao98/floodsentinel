"""Neo4j client for Disaster Sentinel runtime A-box."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from watcher.config import neo4j_database, neo4j_password, neo4j_uri, neo4j_user
from watcher.geo_util import normalize_county_name as _normalize_county_name
from watcher.geo_util import normalize_state as _normalize_state
from watcher.ontology.bridge import get_monitoring_summary, phase_to_iri
from watcher.timezone_util import state_to_timezone, utc_iso_to_local

DEFAULT_PHASE = "early_watch"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _driver():
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("neo4j package required: pip install neo4j") from exc
    password = neo4j_password()
    if not password:
        raise RuntimeError("NEO4J_PASSWORD or NEO4J_PASSWORD_FILE must be set")
    return GraphDatabase.driver(neo4j_uri(), auth=(neo4j_user(), password))


def check_neo4j_health() -> Dict[str, Any]:
    """Return whether Neo4j Bolt is reachable and authenticated."""
    uri = neo4j_uri()
    try:
        driver = _driver()
    except Exception as exc:
        return {"ok": False, "uri": uri, "error": "config_error", "message": str(exc)}
    try:
        with driver.session(database=neo4j_database()) as session:
            record = session.run("RETURN 1 AS ok").single()
            if not record or record["ok"] != 1:
                return {"ok": False, "uri": uri, "error": "neo4j_error", "message": "unexpected ping response"}
        return {"ok": True, "uri": uri, "database": neo4j_database()}
    except Exception as exc:
        return {"ok": False, "uri": uri, "error": "neo4j_unreachable", "message": str(exc)}
    finally:
        driver.close()


def _build_mission_envelope(
    mission: Dict[str, Any],
    aoi: Optional[Dict[str, Any]],
    belief: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    summary_raw = mission.get("monitoring_summary_snapshot")
    monitoring_summary: Dict[str, Any]
    if isinstance(summary_raw, str):
        try:
            monitoring_summary = json.loads(summary_raw)
        except json.JSONDecodeError:
            monitoring_summary = {}
    elif isinstance(summary_raw, dict):
        monitoring_summary = summary_raw
    else:
        monitoring_summary = {}

    fips = mission.get("fips") or (aoi.get("fips") if aoi else None)
    phase = mission.get("current_event_phase") or DEFAULT_PHASE
    if not monitoring_summary and fips:
        monitoring_summary = get_monitoring_summary(
            "flood",
            phase,
            county_fips=fips,
            county_name=mission.get("county"),
            aoi_id=mission.get("aoi_id"),
        )

    mission_id = mission.get("mission_id")
    mission_spec = {
        "mission_id": mission_id,
        "disaster_type": mission.get("disaster_type", "flood"),
        "location": mission.get("location"),
        "county": mission.get("county"),
        "state": mission.get("state"),
        "fips": fips,
        "aoi_id": mission.get("aoi_id"),
        "mode": mission.get("mode", "shadow_live"),
        "current_event_phase": phase,
        "event_phase_iri": mission.get("event_phase_iri") or phase_to_iri(phase),
        "heartbeat_interval_seconds": mission.get("heartbeat_interval_seconds", 800),
        "recovery_all_clear_streak": int(mission.get("recovery_all_clear_streak") or 0),
        "recovery_clear_since": mission.get("recovery_clear_since"),
        "active": bool(mission.get("active", True)),
        "monitoring_summary_snapshot": monitoring_summary,
    }

    belief_state: Dict[str, Any] = {}
    if belief:
        top_evidence = belief.get("top_evidence")
        if isinstance(top_evidence, str):
            try:
                top_evidence = json.loads(top_evidence)
            except json.JSONDecodeError:
                top_evidence = []
        unresolved = belief.get("unresolved_contradictions")
        if isinstance(unresolved, str):
            try:
                unresolved = json.loads(unresolved)
            except json.JSONDecodeError:
                unresolved = []
        belief_state = {
            "state_id": belief.get("state_id"),
            "event_phase": belief.get("event_phase"),
            "event_phase_iri": belief.get("event_phase_iri"),
            "alert_level": belief.get("alert_level", "none"),
            "top_evidence": top_evidence or [],
            "unresolved_contradictions": unresolved or [],
            "as_of_time": belief.get("as_of_time"),
        }

    return {
        "mission_id": mission_id,
        "mission_spec": mission_spec,
        "belief_state": belief_state,
        "monitoring_summary": monitoring_summary,
        "aoi_id": mission.get("aoi_id"),
        "fips": fips,
        "bbox": {},
    }


def list_active_missions() -> List[Dict[str, Any]]:
    """Return all active MissionSpec rows for heartbeat scheduling."""
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            rows = session.run(
                """
                MATCH (m:MissionSpec)
                WHERE coalesce(m.active, true) = true
                  AND coalesce(m.current_event_phase, '') <> 'folded'
                RETURN m.mission_id AS mission_id,
                       m.county AS county,
                       m.state AS state,
                       m.fips AS fips,
                       m.current_event_phase AS current_event_phase,
                       m.heartbeat_interval_seconds AS heartbeat_interval_seconds,
                       m.recovery_all_clear_streak AS recovery_all_clear_streak,
                       m.recovery_clear_since AS recovery_clear_since,
                       m.verification_trigger_state AS verification_trigger_state,
                       m.last_heartbeat_at AS last_heartbeat_at,
                       m.last_coordinator_report_at AS last_coordinator_report_at,
                       m.updated_at AS updated_at
                ORDER BY m.updated_at DESC
                """
            )
            return [dict(row) for row in rows]
    finally:
        driver.close()


def find_active_mission_id(
    *,
    fips: Optional[str] = None,
    county: Optional[str] = None,
    state: Optional[str] = None,
) -> Optional[str]:
    """Return the latest active MissionSpec id for a county/FIPS, or None."""
    county_norm = _normalize_county_name(county) if county else None
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            row = session.run(
                """
                MATCH (m:MissionSpec)
                WHERE coalesce(m.active, true) = true
                  AND ($fips IS NULL OR m.fips = $fips)
                  AND ($county IS NULL OR toLower(m.county) = toLower($county))
                  AND ($state IS NULL OR toLower(m.state) = toLower($state))
                RETURN m.mission_id AS mission_id
                ORDER BY m.updated_at DESC
                LIMIT 1
                """,
                fips=fips or None,
                county=county_norm,
                state=_normalize_state(state) if state else None,
            ).single()
    finally:
        driver.close()
    if not row or not row["mission_id"]:
        return None
    return row["mission_id"]


def get_mission_context_by_county(
    *,
    fips: Optional[str] = None,
    county: Optional[str] = None,
    state: Optional[str] = None,
) -> Dict[str, Any]:
    """Load mission context by county/FIPS — latest active MissionSpec in Neo4j."""
    mission_id = find_active_mission_id(fips=fips, county=county, state=state)
    if not mission_id:
        lookup = fips or f"{county}, {state}" if county and state else county or fips or "unknown"
        return {
            "error": "not_found",
            "message": f"No active MissionSpec for county lookup: {lookup}",
            "hint": "Run /skill mission-bootstrap for this county first",
            "fips": fips,
            "county": county,
            "state": state,
        }
    return get_mission_context(mission_id)


def get_mission_context(mission_id: str) -> Dict[str, Any]:
    """Load MissionSpec, latest BeliefState, and monitoring summary for a mission."""
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            row = session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                OPTIONAL MATCH (m)-[:MONITORS]->(a:AreaOfInterest)
                OPTIONAL MATCH (m)-[:HAS_STATE]->(b:BeliefState)
                WITH m, a, b
                ORDER BY b.as_of_time DESC
                WITH m, a, collect(b)[0] AS belief
                RETURN m AS mission, a AS aoi, belief AS belief
                """,
                mission_id=mission_id,
            ).single()
    finally:
        driver.close()

    if not row or not row["mission"]:
        return {
            "error": "not_found",
            "message": f"No MissionSpec for mission_id={mission_id}",
            "mission_id": mission_id,
            "hint": "Run /skill mission-bootstrap for this county first",
        }

    mission = dict(row["mission"])
    aoi = dict(row["aoi"]) if row["aoi"] else None
    belief = dict(row["belief"]) if row["belief"] else None
    return _build_mission_envelope(mission, aoi, belief)


def verify_bootstrap(mission_id: str) -> Dict[str, Any]:
    """Confirm MissionSpec graph nodes exist for a bootstrapped mission."""
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            row = session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                OPTIONAL MATCH (m)-[:MONITORS]->(a:AreaOfInterest)
                OPTIONAL MATCH (m)-[:MONITORS]->(c:County)
                OPTIONAL MATCH (m)-[:HAS_STATE]->(b:BeliefState)
                RETURN m.mission_id AS mission_id,
                       m.fips AS fips,
                       m.current_event_phase AS current_event_phase,
                       m.heartbeat_interval_seconds AS heartbeat_interval_seconds,
                       a.aoi_id AS aoi_id,
                       c.fips AS county_fips,
                       b.event_phase AS belief_event_phase,
                       count(DISTINCT a) AS aoi_count,
                       count(DISTINCT c) AS county_count,
                       count(DISTINCT b) AS belief_count
                """,
                mission_id=mission_id,
            ).single()
    finally:
        driver.close()

    if not row or not row["mission_id"]:
        return {
            "ok": False,
            "mission_id": mission_id,
            "error": "not_found",
            "message": f"No MissionSpec for mission_id={mission_id}",
        }

    checks = {
        "mission_spec": row["mission_id"] is not None,
        "area_of_interest": (row["aoi_count"] or 0) == 1 and row["aoi_id"] is not None,
        "county": (row["county_count"] or 0) == 1 and row["county_fips"] is not None,
        "belief_state": (row["belief_count"] or 0) == 1 and row["belief_event_phase"] is not None,
        "phase_early_watch": row["current_event_phase"] == DEFAULT_PHASE,
        "heartbeat_21600": row["heartbeat_interval_seconds"] == 21600,
    }
    ok = all(checks.values())
    return {
        "ok": ok,
        "mission_id": mission_id,
        "fips": row["fips"],
        "aoi_id": row["aoi_id"],
        "county_fips": row["county_fips"],
        "current_event_phase": row["current_event_phase"],
        "belief_event_phase": row["belief_event_phase"],
        "heartbeat_interval_seconds": row["heartbeat_interval_seconds"],
        "checks": checks,
    }


def get_latest_source_snapshots(mission_id: str) -> Dict[str, Dict[str, Any]]:
    """Return latest stored snapshot per source_id for a mission."""
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            rows = session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})-[:TRACKS]->(s:SourceRecord)-[:HAS_SNAPSHOT]->(snap:SourceSnapshot)
                WITH s, snap ORDER BY snap.fetched_at_utc DESC
                WITH s, collect(snap)[0] AS latest
                RETURN s.source_id AS source_id,
                       latest.snapshot_id AS snapshot_id,
                       latest.content_fingerprint AS content_fingerprint,
                       latest.fetched_at_utc AS fetched_at_utc,
                       latest.fetched_at_local AS fetched_at_local,
                       latest.change_status AS change_status
                """,
                mission_id=mission_id,
            )
            out: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                if row["source_id"]:
                    out[row["source_id"]] = {
                        "snapshot_id": row["snapshot_id"],
                        "content_fingerprint": row["content_fingerprint"],
                        "fetched_at_utc": row["fetched_at_utc"],
                        "fetched_at_local": row["fetched_at_local"],
                        "change_status": row["change_status"],
                    }
            return out
    finally:
        driver.close()


def get_prior_county_flood_gauges(mission_id: str, fips: str) -> List[Dict[str, Any]]:
    """Load merged gauge readings from latest county-flood snapshots for delta comparison."""
    fips_norm = str(fips).zfill(5)
    prefix = f"src_county_flood_"
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            rows = session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})-[:TRACKS]->(s:SourceRecord)-[:HAS_SNAPSHOT]->(snap:SourceSnapshot)
                WHERE s.source_id STARTS WITH $prefix AND s.source_id CONTAINS $fips
                WITH s, snap ORDER BY snap.fetched_at_utc DESC
                WITH s, collect(snap)[0] AS latest
                RETURN latest.raw_text AS raw_text,
                       latest.fetched_at_utc AS fetched_at_utc
                """,
                mission_id=mission_id,
                prefix=prefix,
                fips=fips_norm,
            )
            merged: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                raw = row.get("raw_text") or row.get("raw_excerpt")
                fetched_at = row.get("fetched_at_utc")
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if payload.get("schema") != "county-flood-data/1":
                    continue
                for gauge in payload.get("gauges") or []:
                    if not isinstance(gauge, dict):
                        continue
                    site = str(gauge.get("site_no") or "").strip()
                    shef = str(gauge.get("shef_id") or "").strip().upper()
                    if site and shef and site != shef:
                        key = f"site:{site}"
                    elif site:
                        key = f"site:{site}"
                    elif shef:
                        key = f"shef:{shef}"
                    else:
                        continue
                    entry = dict(gauge)
                    entry["_fetched_at"] = fetched_at
                    merged[key] = entry
            return list(merged.values())
    finally:
        driver.close()


def list_source_readings(
    *,
    mission_id: Optional[str] = None,
    fips: Optional[str] = None,
    county: Optional[str] = None,
    state: Optional[str] = None,
    source_id: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """List persisted source readings from Neo4j for a mission or county."""
    if not mission_id:
        mission_id = find_active_mission_id(fips=fips, county=county, state=state)
    if not mission_id:
        return {
            "error": "not_found",
            "message": "No active mission for source readings lookup",
            "hint": "Run /skill mission-bootstrap first",
        }

    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            rows = session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})-[:TRACKS]->(s:SourceRecord)-[:HAS_SNAPSHOT]->(snap:SourceSnapshot)
                WHERE $source_id IS NULL OR s.source_id = $source_id
                RETURN s.source_id AS source_id,
                       s.source_name AS source_name,
                       s.source_url AS source_url,
                       s.county_fips AS county_fips,
                       s.timezone AS timezone,
                       s.latest_fingerprint AS latest_fingerprint,
                       s.latest_fetched_at_utc AS latest_fetched_at_utc,
                       s.latest_fetched_at_local AS latest_fetched_at_local,
                       s.updated_at AS source_updated_at,
                       snap.snapshot_id AS snapshot_id,
                       snap.fetched_at_utc AS fetched_at_utc,
                       snap.fetched_at_local AS fetched_at_local,
                       snap.content_fingerprint AS content_fingerprint,
                       snap.change_status AS change_status,
                       snap.raw_excerpt AS raw_excerpt,
                       snap.byte_length AS byte_length
                ORDER BY snap.fetched_at_utc DESC
                LIMIT $limit
                """,
                mission_id=mission_id,
                source_id=source_id,
                limit=limit,
            )
            readings = [dict(row) for row in rows]
    finally:
        driver.close()

    ctx = get_mission_context(mission_id)
    return {
        "mission_id": mission_id,
        "county_fips": ctx.get("fips"),
        "county": ctx.get("mission_spec", {}).get("county"),
        "state": ctx.get("mission_spec", {}).get("state"),
        "readings": readings,
        "count": len(readings),
    }


def persist_watcher_readings(
    mission_id: str,
    *,
    fips: str,
    county: str,
    state: str,
    source_records: List[Dict[str, Any]],
    source_snapshots: List[Dict[str, Any]],
    failed_fetches: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Persist or update SourceRecord nodes and append timestamped SourceSnapshot nodes.

    SourceRecord is MERGE-updated (latest fingerprint/time). Snapshots are append-only
    history. Snapshots with change_status=unchanged (fingerprint match or sub-threshold
    stage delta) are skipped so Neo4j priors only advance on real changes / cold start.
    """
    tz_name = state_to_timezone(state)
    now = _utc_now()
    failed_fetches = failed_fetches or []
    persisted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    updated_sources = 0
    new_snapshots = 0
    skipped_snapshots = 0

    record_by_id = {r["source_id"]: r for r in source_records if r.get("source_id")}
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            for snap in source_snapshots:
                source_id = snap.get("source_id")
                if not source_id:
                    continue
                change_status = snap.get("change_status", "new")
                if change_status == "unchanged":
                    skipped_snapshots += 1
                    skipped.append(
                        {
                            "source_id": source_id,
                            "snapshot_id": snap.get("snapshot_id"),
                            "change_status": change_status,
                            "skip_reason": snap.get("skip_reason") or "unchanged",
                        }
                    )
                    continue

                rec = record_by_id.get(source_id, {})
                fetched_at_utc = snap.get("fetched_at") or now
                fetched_at_local = utc_iso_to_local(fetched_at_utc, tz_name)
                fingerprint = snap.get("content_fingerprint", "")
                snapshot_id = snap.get("snapshot_id") or f"snap_{source_id}_{_slug(fetched_at_utc)}"
                api_resolution = rec.get("api_resolution")
                api_resolution_json = json.dumps(api_resolution) if api_resolution else None

                session.run(
                    """
                    MATCH (m:MissionSpec {mission_id: $mission_id})
                    MERGE (s:SourceRecord {source_id: $source_id})
                    SET s.source_name = $source_name,
                        s.source_url = $source_url,
                        s.source_type = $source_type,
                        s.source_type_iri = $source_type_iri,
                        s.county_fips = $county_fips,
                        s.county = $county,
                        s.state = $state,
                        s.timezone = $timezone,
                        s.trust_tier_level = $trust_tier_level,
                        s.health_status = coalesce($health_status, s.health_status, 'verified'),
                        s.resolution_strategy = $resolution_strategy,
                        s.api_resolution_json = $api_resolution_json,
                        s.latest_fingerprint = $fingerprint,
                        s.latest_fetched_at_utc = $fetched_at_utc,
                        s.latest_fetched_at_local = $fetched_at_local,
                        s.latest_change_status = $change_status,
                        s.created_at = coalesce(s.created_at, $now),
                        s.updated_at = $now
                    MERGE (m)-[:TRACKS]->(s)
                    CREATE (snap:SourceSnapshot {snapshot_id: $snapshot_id})
                    SET snap.source_id = $source_id,
                        snap.mission_id = $mission_id,
                        snap.county_fips = $county_fips,
                        snap.timezone = $timezone,
                        snap.fetched_at_utc = $fetched_at_utc,
                        snap.fetched_at_local = $fetched_at_local,
                        snap.published_at = $published_at,
                        snap.content_fingerprint = $fingerprint,
                        snap.raw_excerpt = $raw_excerpt,
                        snap.raw_text = $raw_text,
                        snap.snapshot_url = $snapshot_url,
                        snap.change_status = $change_status,
                        snap.content_type = $content_type,
                        snap.byte_length = $byte_length,
                        snap.created_at = $now
                    MERGE (s)-[:HAS_SNAPSHOT]->(snap)
                    """,
                    mission_id=mission_id,
                    source_id=source_id,
                    source_name=rec.get("source_name") or snap.get("source_name", source_id),
                    source_url=rec.get("source_url") or snap.get("snapshot_url", ""),
                    source_type=rec.get("source_type", "official"),
                    source_type_iri=rec.get("source_type_iri"),
                    county_fips=fips,
                    county=county,
                    state=state,
                    timezone=tz_name,
                    trust_tier_level=(rec.get("trust_tier") or {}).get("level", 0),
                    health_status=rec.get("health_status"),
                    resolution_strategy=rec.get("resolution_strategy") or rec.get("discovery_hint"),
                    api_resolution_json=api_resolution_json,
                    fingerprint=fingerprint,
                    fetched_at_utc=fetched_at_utc,
                    fetched_at_local=fetched_at_local,
                    change_status=change_status,
                    now=now,
                    snapshot_id=snapshot_id,
                    published_at=snap.get("published_at"),
                    raw_excerpt=(snap.get("raw_excerpt") or "")[:2000],
                    raw_text=snap.get("raw_text") or "",
                    snapshot_url=snap.get("snapshot_url"),
                    content_type=snap.get("content_type"),
                    byte_length=snap.get("byte_length"),
                )
                persisted.append(
                    {
                        "source_id": source_id,
                        "snapshot_id": snapshot_id,
                        "change_status": change_status,
                        "fetched_at_utc": fetched_at_utc,
                        "fetched_at_local": fetched_at_local,
                        "timezone": tz_name,
                        "content_fingerprint": fingerprint,
                    }
                )
                updated_sources += 1
                new_snapshots += 1

            for fail in failed_fetches:
                sid = fail.get("source_id")
                if not sid:
                    continue
                rec = record_by_id.get(sid, {})
                session.run(
                    """
                    MATCH (m:MissionSpec {mission_id: $mission_id})
                    MERGE (s:SourceRecord {source_id: $source_id})
                    SET s.source_url = coalesce($source_url, s.source_url),
                        s.county_fips = $county_fips,
                        s.county = $county,
                        s.state = $state,
                        s.timezone = $timezone,
                        s.health_status = 'degraded',
                        s.last_failure_reason = $failure_reason,
                        s.last_failure_at = $now,
                        s.updated_at = $now
                    MERGE (m)-[:TRACKS]->(s)
                    """,
                    mission_id=mission_id,
                    source_id=sid,
                    source_url=fail.get("source_url") or rec.get("source_url"),
                    county_fips=fips,
                    county=county,
                    state=state,
                    timezone=tz_name,
                    failure_reason=fail.get("failure_reason"),
                    now=now,
                )
    finally:
        driver.close()

    return {
        "ok": True,
        "mission_id": mission_id,
        "county_fips": fips,
        "timezone": tz_name,
        "sources_updated": updated_sources,
        "snapshots_created": new_snapshots,
        "snapshots_skipped": skipped_snapshots,
        "persisted": persisted,
        "skipped": skipped,
    }


def init_schema() -> None:
    statements = [
        "CREATE CONSTRAINT mission_spec_id IF NOT EXISTS FOR (m:MissionSpec) REQUIRE m.mission_id IS UNIQUE",
        "CREATE CONSTRAINT state_id IF NOT EXISTS FOR (b:BeliefState) REQUIRE b.state_id IS UNIQUE",
        "CREATE CONSTRAINT aoi_id IF NOT EXISTS FOR (a:AreaOfInterest) REQUIRE a.aoi_id IS UNIQUE",
        "CREATE CONSTRAINT county_fips_unique IF NOT EXISTS FOR (c:County) REQUIRE c.fips IS UNIQUE",
        "CREATE CONSTRAINT source_record_id IF NOT EXISTS FOR (s:SourceRecord) REQUIRE s.source_id IS UNIQUE",
        "CREATE CONSTRAINT source_snapshot_id IF NOT EXISTS FOR (snap:SourceSnapshot) REQUIRE snap.snapshot_id IS UNIQUE",
        "CREATE CONSTRAINT evidence_claim_id IF NOT EXISTS FOR (c:EvidenceClaim) REQUIRE c.claim_id IS UNIQUE",
    ]
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            for stmt in statements:
                try:
                    session.run(stmt)
                except Exception:
                    pass  # constraint/index may already exist under a different name
    finally:
        driver.close()


def bootstrap_mission(
    county: str,
    state: str,
    fips: str,
    mode: str = "shadow_live",
    county_name: Optional[str] = None,
) -> Dict[str, Any]:
    county_name = _normalize_county_name(county_name or county)
    state = _normalize_state(state)

    existing_id = find_active_mission_id(fips=fips, county=county_name, state=state)
    if existing_id:
        ctx = get_mission_context(existing_id)
        return {
            "mission_id": existing_id,
            "mission_spec": ctx.get("mission_spec") or {},
            "monitoring_summary": ctx.get("monitoring_summary") or {},
            "aoi_id": ctx.get("aoi_id"),
            "bbox": {},
            "already_active": True,
            "reused_active_mission": True,
            "neo4j_verification": {"ok": True, "mission_id": existing_id, "skipped": True},
        }

    aoi_id = f"aoi_{fips}"
    summary = get_monitoring_summary("flood", DEFAULT_PHASE, county_fips=fips, county_name=county_name, aoi_id=aoi_id)
    mission_id = f"evt_{_slug(county_name)}_flood_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    state_id = f"bs_{uuid.uuid4().hex[:12]}"
    now = _utc_now()
    phase = DEFAULT_PHASE
    phase_iri = phase_to_iri(phase)
    heartbeat = summary.get("heartbeat_interval_seconds", 800)
    location = f"{county_name} County, {state}" if state else county_name

    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            session.run(
                """
                MERGE (c:County {fips: $fips})
                SET c.name = $county_name, c.state = $state, c.state_code = $state_code, c.updated_at = $now
                MERGE (a:AreaOfInterest {aoi_id: $aoi_id})
                SET a.fips = $fips, a.county_name = $county_name, a.state = $state, a.label = $location, a.updated_at = $now
                MERGE (m:MissionSpec {mission_id: $mission_id})
                SET m.disaster_type = 'flood',
                    m.location = $location,
                    m.county = $county_name,
                    m.state = $state,
                    m.fips = $fips,
                    m.aoi_id = $aoi_id,
                    m.mode = $mode,
                    m.current_event_phase = $phase,
                    m.event_phase_iri = $phase_iri,
                    m.heartbeat_interval_seconds = $heartbeat,
                    m.monitoring_summary_phase = $phase,
                    m.monitoring_summary_snapshot = $summary_json,
                    m.recovery_all_clear_streak = 0,
                    m.recovery_clear_since = null,
                    m.active = true,
                    m.created_at = coalesce(m.created_at, $now),
                    m.updated_at = $now
                MERGE (b:BeliefState {state_id: $state_id})
                SET b.mission_id = $mission_id,
                    b.event_phase = $phase,
                    b.event_phase_iri = $phase_iri,
                    b.alert_level = 'none',
                    b.top_evidence = [],
                    b.unresolved_contradictions = [],
                    b.as_of_time = $now,
                    b.created_at = $now
                MERGE (m)-[:MONITORS]->(a)
                MERGE (m)-[:MONITORS]->(c)
                MERGE (m)-[:HAS_STATE]->(b)
                """,
                fips=fips,
                county_name=county_name,
                state=state,
                state_code=state[:2].upper() if len(state) == 2 else state,
                location=location,
                mission_id=mission_id,
                aoi_id=aoi_id,
                mode=mode,
                phase=phase,
                phase_iri=phase_iri,
                heartbeat=heartbeat,
                summary_json=json.dumps(summary),
                state_id=state_id,
                now=now,
            )
    finally:
        driver.close()

    mission_spec = {
        "mission_id": mission_id,
        "disaster_type": "flood",
        "location": location,
        "county": county_name,
        "state": state,
        "fips": fips,
        "aoi_id": aoi_id,
        "mode": mode,
        "current_event_phase": phase,
        "event_phase_iri": phase_iri,
        "heartbeat_interval_seconds": heartbeat,
        "recovery_all_clear_streak": 0,
        "recovery_clear_since": None,
        "active": True,
        "monitoring_summary_snapshot": summary,
    }
    result = {
        "mission_id": mission_id,
        "mission_spec": mission_spec,
        "monitoring_summary": summary,
        "initial_belief_state": {
            "state_id": state_id,
            "event_phase": phase,
            "event_phase_iri": phase_iri,
            "alert_level": "none",
            "top_evidence": [],
            "unresolved_contradictions": [],
        },
        "aoi_id": aoi_id,
        "bbox": {},
        "already_active": False,
        "reused_active_mission": False,
    }
    result["neo4j_verification"] = verify_bootstrap(mission_id)
    return result


def get_last_coordinator_report(mission_id: str) -> Optional[Dict[str, Any]]:
    """Load prior coordinator report snapshot from MissionSpec."""
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            row = session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                RETURN m.last_coordinator_report_json AS report_json
                """,
                mission_id=mission_id,
            ).single()
    finally:
        driver.close()
    if not row or not row["report_json"]:
        return None
    raw = row["report_json"]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, dict):
        return raw
    return None


def save_coordinator_report(mission_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
    """Persist coordinator report snapshot for next-run delta."""
    now = _utc_now()
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                SET m.last_coordinator_report_json = $report_json,
                    m.last_coordinator_report_at = $now,
                    m.last_heartbeat_at = $now,
                    m.updated_at = $now
                """,
                mission_id=mission_id,
                report_json=json.dumps(report),
                now=now,
            )
    finally:
        driver.close()
    return {"ok": True, "mission_id": mission_id, "saved_at": now}


def persist_evidence_claims(
    mission_id: str,
    claims: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """MERGE EvidenceClaim nodes and link from SourceSnapshot via YIELDED."""
    now = _utc_now()
    persisted = 0
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            for claim in claims:
                claim_id = claim.get("claim_id")
                if not claim_id:
                    continue
                snapshot_id = claim.get("snapshot_id")
                session.run(
                    """
                    MATCH (m:MissionSpec {mission_id: $mission_id})
                    MERGE (c:EvidenceClaim {claim_id: $claim_id})
                    SET c.mission_id = $mission_id,
                        c.source_id = $source_id,
                        c.claim_type = $claim_type,
                        c.claim_type_iri = $claim_type_iri,
                        c.severity = $severity,
                        c.overall_confidence = $overall_confidence,
                        c.confidence_components = $confidence_json,
                        c.excerpt = $excerpt,
                        c.evidence_url = $evidence_url,
                        c.location_text = $location_text,
                        c.observed_at = $observed_at,
                        c.published_at = $published_at,
                        c.snapshot_id = $snapshot_id,
                        c.updated_at = $now,
                        c.created_at = coalesce(c.created_at, $now)
                    WITH m, c
                    OPTIONAL MATCH (snap:SourceSnapshot {snapshot_id: $snapshot_id})
                    FOREACH (_ IN CASE WHEN snap IS NOT NULL THEN [1] ELSE [] END |
                        MERGE (snap)-[:YIELDED]->(c)
                    )
                    """,
                    mission_id=mission_id,
                    claim_id=claim_id,
                    source_id=claim.get("source_id"),
                    claim_type=claim.get("claim_type"),
                    claim_type_iri=claim.get("claim_type_iri"),
                    severity=claim.get("severity"),
                    overall_confidence=claim.get("overall_confidence"),
                    confidence_json=json.dumps(claim.get("confidence_components") or {}),
                    excerpt=(claim.get("excerpt") or "")[:2000],
                    evidence_url=claim.get("evidence_url"),
                    location_text=claim.get("location_text"),
                    observed_at=claim.get("observed_at"),
                    published_at=claim.get("published_at"),
                    snapshot_id=snapshot_id,
                    now=now,
                )
                persisted += 1
    finally:
        driver.close()
    return {"ok": True, "claims_persisted": persisted}


def get_latest_claim_id_for_source(
    source_id: str,
    claim_type: str,
    *,
    mission_id: Optional[str] = None,
) -> Optional[str]:
    """Most recent EvidenceClaim id for a source + claim type."""
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            if mission_id:
                result = session.run(
                    """
                    MATCH (m:MissionSpec {mission_id: $mission_id})-[:TRACKS]->(s:SourceRecord {source_id: $source_id})
                          -[:HAS_SNAPSHOT]->(:SourceSnapshot)-[:YIELDED]->(e:EvidenceClaim {claim_type: $claim_type})
                    RETURN e.claim_id AS claim_id
                    ORDER BY e.observed_at DESC
                    LIMIT 1
                    """,
                    mission_id=mission_id,
                    source_id=source_id,
                    claim_type=claim_type,
                )
            else:
                result = session.run(
                    """
                    MATCH (s:SourceRecord {source_id: $source_id})-[:HAS_SNAPSHOT]->(:SourceSnapshot)
                          -[:YIELDED]->(e:EvidenceClaim {claim_type: $claim_type})
                    RETURN e.claim_id AS claim_id
                    ORDER BY e.observed_at DESC
                    LIMIT 1
                    """,
                    source_id=source_id,
                    claim_type=claim_type,
                )
            record = result.single()
            return record["claim_id"] if record else None
    finally:
        driver.close()


def apply_belief_state_update(
    mission_id: str,
    *,
    event_phase: str,
    event_phase_iri: str,
    alert_level: str,
    top_evidence: List[str],
    state_diff: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append new BeliefState and link BASED_ON top evidence claims."""
    state_id = f"bs_{uuid.uuid4().hex[:12]}"
    now = _utc_now()
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                MERGE (b:BeliefState {state_id: $state_id})
                SET b.mission_id = $mission_id,
                    b.event_phase = $event_phase,
                    b.event_phase_iri = $event_phase_iri,
                    b.alert_level = $alert_level,
                    b.top_evidence = $top_evidence,
                    b.state_diff = $state_diff_json,
                    b.unresolved_contradictions = [],
                    b.as_of_time = $now,
                    b.created_at = $now
                MERGE (m)-[:HAS_STATE]->(b)
                WITH m, b
                UNWIND $top_evidence AS claim_id
                OPTIONAL MATCH (c:EvidenceClaim {claim_id: claim_id})
                FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END |
                    MERGE (b)-[:BASED_ON]->(c)
                )
                """,
                mission_id=mission_id,
                state_id=state_id,
                event_phase=event_phase,
                event_phase_iri=event_phase_iri,
                alert_level=alert_level,
                top_evidence=top_evidence,
                state_diff_json=json.dumps(state_diff or {}),
                now=now,
            )
    finally:
        driver.close()
    return {
        "ok": True,
        "state_id": state_id,
        "event_phase": event_phase,
        "alert_level": alert_level,
        "top_evidence": top_evidence,
    }


def update_mission_phase(
    mission_id: str,
    *,
    new_phase: str,
    fips: str,
    county_name: str,
    aoi_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Update MissionSpec phase and refresh monitoring_summary_snapshot."""
    phase_iri = phase_to_iri(new_phase)
    summary = get_monitoring_summary(
        "flood",
        new_phase,
        county_fips=fips,
        county_name=county_name,
        aoi_id=aoi_id,
    )
    now = _utc_now()
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                SET m.current_event_phase = $phase,
                    m.event_phase_iri = $phase_iri,
                    m.monitoring_summary_phase = $phase,
                    m.monitoring_summary_snapshot = $summary_json,
                    m.heartbeat_interval_seconds = $heartbeat,
                    m.updated_at = $now
                """,
                mission_id=mission_id,
                phase=new_phase,
                phase_iri=phase_iri,
                summary_json=json.dumps(summary),
                heartbeat=summary.get("heartbeat_interval_seconds", 800),
                now=now,
            )
    finally:
        driver.close()
    return {
        "ok": True,
        "mission_id": mission_id,
        "current_event_phase": new_phase,
        "monitoring_summary": summary,
    }


def set_recovery_all_clear_streak(
    mission_id: str, streak: int, *, clear_since: Optional[str] = None
) -> Dict[str, Any]:
    """Persist recovery clearance audit state used for elapsed-time auto-fold."""
    now = _utc_now()
    streak_i = max(0, int(streak))
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                SET m.recovery_all_clear_streak = $streak,
                    m.recovery_clear_since = $clear_since,
                    m.updated_at = $now
                """,
                mission_id=mission_id,
                streak=streak_i,
                clear_since=clear_since,
                now=now,
            )
    finally:
        driver.close()
    return {
        "ok": True,
        "mission_id": mission_id,
        "recovery_all_clear_streak": streak_i,
        "recovery_clear_since": clear_since,
    }


def set_verification_trigger_state(
    mission_id: str, state: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Persist the mission's verification-trigger scheduling state.

    The state is scheduling audit data only (seen warning identities and armed
    burst windows). It carries no evidence and can never influence verdict,
    phase, alert, or recovery, which read exclusively from claims.
    """
    now = _utc_now()
    state_json = json.dumps(state, sort_keys=True) if state is not None else None
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                SET m.verification_trigger_state = $state_json,
                    m.updated_at = $now
                """,
                mission_id=mission_id,
                state_json=state_json,
                now=now,
            )
    finally:
        driver.close()
    return {"ok": True, "mission_id": mission_id}


def deactivate_mission(mission_id: str, *, reason: str = "folded") -> Dict[str, Any]:
    """Mark MissionSpec inactive (folded) so heartbeat skips it."""
    now = _utc_now()
    phase = "folded"
    phase_iri = phase_to_iri(phase)
    try:
        summary = get_monitoring_summary("flood", phase)
    except Exception:
        summary = {"phase": phase, "heartbeat_interval_seconds": 86400, "phase_transition_rules": []}
    driver = _driver()
    try:
        with driver.session(database=neo4j_database()) as session:
            session.run(
                """
                MATCH (m:MissionSpec {mission_id: $mission_id})
                SET m.active = false,
                    m.current_event_phase = $phase,
                    m.event_phase_iri = $phase_iri,
                    m.monitoring_summary_phase = $phase,
                    m.monitoring_summary_snapshot = $summary_json,
                    m.heartbeat_interval_seconds = $heartbeat,
                    m.recovery_all_clear_streak = 0,
                    m.recovery_clear_since = null,
                    m.verification_trigger_state = null,
                    m.ended_at = $now,
                    m.end_reason = $reason,
                    m.updated_at = $now
                """,
                mission_id=mission_id,
                phase=phase,
                phase_iri=phase_iri,
                summary_json=json.dumps(summary),
                heartbeat=int(summary.get("heartbeat_interval_seconds") or 86400),
                reason=reason,
                now=now,
            )
    finally:
        driver.close()
    return {
        "ok": True,
        "mission_id": mission_id,
        "active": False,
        "current_event_phase": phase,
        "ended_at": now,
        "end_reason": reason,
    }
