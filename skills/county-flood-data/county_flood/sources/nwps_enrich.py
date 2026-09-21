"""Batch-fetch NWPS gauge payloads for USGS enrichment."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

NWPS_API_BASE = "https://api.water.noaa.gov/nwps/v1/gauges"
DEFAULT_WORKERS = 6
REQUEST_TIMEOUT = 30

_FETCH_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OpenClaw-county-flood-data/1.0",
}


def _request_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_FETCH_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_nwps_payloads(shef_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Return mapping shef_id -> NWPS JSON payload."""
    ids = [sid.strip() for sid in shef_ids if sid and sid.strip()]
    if not ids:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    workers = min(DEFAULT_WORKERS, max(1, len(ids)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_request_json, f"{NWPS_API_BASE}/{sid}"): sid for sid in ids
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                out[sid.upper()] = future.result()
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                continue
            except Exception:
                continue
    return out
