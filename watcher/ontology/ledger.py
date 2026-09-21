"""
ledger.py -- per-mission JSONL ledger path resolution + write helper.

Wraps belief_schema.append()/load() so callers never hardcode paths, and so
the ledger directory is controlled by a single env var (SENTINEL_LEDGER_DIR).
"""
from __future__ import annotations

import os

from .belief_schema import append as _append
from .belief_schema import load as _load

DEFAULT_LEDGER_ROOT = "state/ledger"


def _ledger_root() -> str:
    return os.environ.get("SENTINEL_LEDGER_DIR", DEFAULT_LEDGER_ROOT)


def ledger_path(mission_id: str) -> str:
    root = _ledger_root()
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, f"{mission_id}.jsonl")


def write(mission_id: str, obj) -> None:
    """Append one typed belief_schema object to this mission's ledger."""
    _append(obj, path=ledger_path(mission_id))


def read_all(mission_id: str) -> list:
    """Load and validate this mission's full ledger."""
    return _load(path=ledger_path(mission_id))
