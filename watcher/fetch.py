"""HTTP fetch helpers for stub source monitoring."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def fetch_source_record(source: Dict[str, Any], timeout: int = 30) -> Tuple[Dict[str, Any], Dict[str, Any] | None]:
    """Fetch one source URL; return (snapshot, failure_or_none)."""
    source_id = source.get("source_id", "unknown")
    source_url = source.get("source_url", "")
    headers = dict(source.get("fetch_headers") or {})
    if "User-Agent" not in headers:
        headers["User-Agent"] = "DisasterSentinel/1.0 (flood monitoring)"

    fetched_at = _utc_now()
    try:
        req = urllib.request.Request(source_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return {}, {
            "source_id": source_id,
            "source_url": source_url,
            "failure_reason": f"http_{exc.code}: {exc.reason}",
        }
    except urllib.error.URLError as exc:
        return {}, {
            "source_id": source_id,
            "source_url": source_url,
            "failure_reason": f"url_error: {exc.reason}",
        }
    except Exception as exc:
        return {}, {
            "source_id": source_id,
            "source_url": source_url,
            "failure_reason": str(exc),
        }

    fingerprint = hashlib.sha256(raw_bytes).hexdigest()
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        text = repr(raw_bytes[:200])

    published_at = None
    if "json" in content_type.lower() or text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                for key in ("updated", "sent", "dateIssued", "generationtime"):
                    if payload.get(key):
                        published_at = str(payload[key])
                        break
        except json.JSONDecodeError:
            pass

    snapshot = {
        "snapshot_id": f"snap_{source_id}_{fetched_at.replace(':', '').replace('-', '')}",
        "source_id": source_id,
        "fetched_at": fetched_at,
        "published_at": published_at,
        "content_fingerprint": fingerprint,
        "raw_excerpt": text[:500],
        "raw_text": text[:2_000_000] if len(text) <= 2_000_000 else text[:2_000_000],
        "snapshot_url": source_url,
        "change_status": "new",
        "content_type": content_type,
        "byte_length": len(raw_bytes),
    }
    return snapshot, None


def apply_change_detection(
    snapshots: List[Dict[str, Any]],
    prior_by_source: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Set change_status by comparing content_fingerprint to Neo4j prior readings."""
    out: List[Dict[str, Any]] = []
    for snap in snapshots:
        sid = snap.get("source_id")
        prior = prior_by_source.get(sid or "")
        if not prior:
            snap["change_status"] = "new"
            snap["prior_fingerprint"] = None
        elif prior.get("content_fingerprint") == snap.get("content_fingerprint"):
            snap["change_status"] = "unchanged"
            snap["prior_fingerprint"] = prior.get("content_fingerprint")
            snap["prior_fetched_at_utc"] = prior.get("fetched_at_utc")
        else:
            snap["change_status"] = "changed"
            snap["prior_fingerprint"] = prior.get("content_fingerprint")
            snap["prior_fetched_at_utc"] = prior.get("fetched_at_utc")
        out.append(snap)
    return out


def fetch_source_records(
    sources: List[Dict[str, Any]],
    prior_by_source: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fetch all sources; return snapshots and failures."""
    snapshots: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for source in sources:
        snapshot, failure = fetch_source_record(source)
        if failure:
            failures.append(failure)
        else:
            snapshots.append(snapshot)
    if prior_by_source:
        snapshots = apply_change_detection(snapshots, prior_by_source)
    return {
        "source_snapshots": snapshots,
        "failed_fetches": failures,
        "fetched_at": _utc_now(),
        "sources_attempted": len(sources),
        "sources_succeeded": len(snapshots),
        "prior_sources_loaded": len(prior_by_source or {}),
    }
