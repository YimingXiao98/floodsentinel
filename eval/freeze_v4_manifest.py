"""Freeze the v4 held-out study input manifest.

Reads the mechanical v4 selection artifact, the v4 development-grid artifact
(for the frozen additive trigger configuration), and the frozen v4 protocol,
and emits ``eval/corpus/study_v4/manifest.json`` binding every input by
SHA-256. Run exactly once, after the protocol text is final and before any v4
held-out policy or comparator outcome is examined.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PROTOCOL = _ROOT / "eval" / "HYBRID_STUDY_PROTOCOL_V4.md"
SELECTION = _ROOT / "eval" / "corpus" / "study_v4" / "holdout_selection.json"
DEV_GRID = _ROOT / "eval" / "results" / "study_v4_dev" / "dev_grid.json"
OUT = _ROOT / "eval" / "corpus" / "study_v4" / "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    dev_grid = json.loads(DEV_GRID.read_text(encoding="utf-8"))
    selected_id = dev_grid["selected_config_id"]
    config = next(
        c for c in dev_grid["config_summary"] if c["config_id"] == selected_id
    )

    artifacts: List[Dict[str, Any]] = []
    for role, rows in (
        ("episode_evaluation", selection["included_episode_evaluation"]),
        ("coverage_only", selection["included_coverage_only"]),
    ):
        for row in rows:
            entry = dict(row["manifest_entry"])
            corpus_path = _ROOT / entry["path"]
            slug = corpus_path.name.replace(".json", "")
            trigger_path = (
                _ROOT / "eval" / "corpus" / "study_v4" / f"{slug}_vtec_triggers.json"
            )
            if not trigger_path.is_file():
                raise FileNotFoundError(trigger_path)
            corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
            entry.update(
                {
                    "analysis_role": role,
                    "event_ncei_name": row["event"],
                    "sha256": _sha256(corpus_path),
                    "trigger_corpus": str(trigger_path.relative_to(_ROOT)),
                    "trigger_corpus_sha256": _sha256(trigger_path),
                    "sites_selected": len(corpus.get("sites") or []),
                    "sites_with_flood_stage": sum(
                        1
                        for site in corpus.get("sites") or []
                        if site.get("flood_stage") is not None
                    ),
                    "observations": sum(
                        len(site.get("observations") or [])
                        for site in corpus.get("sites") or []
                    ),
                }
            )
            if role == "coverage_only":
                entry["analysis_role_rationale"] = (
                    "passed QC with zero hydrologic episodes in the frozen "
                    "window; retained for coverage and trigger-burden "
                    "reporting only, per the frozen selection rules"
                )
            artifacts.append(entry)

    manifest = {
        "schema": "sentinel-study-corpus-manifest-v4/1",
        "builder": "eval/select_holdout_v4.py + eval/vtec_trigger.py",
        "source": "USGS NWIS instantaneous values; IEM VTEC archive",
        "analysis_window_convention": (
            "half-open UTC interval [analysis_start, analysis_end); "
            "312 elapsed hours"
        ),
        "protocol": str(PROTOCOL.relative_to(_ROOT)),
        "protocol_sha256": _sha256(PROTOCOL),
        "selection": str(SELECTION.relative_to(_ROOT)),
        "selection_sha256": _sha256(SELECTION),
        "dev_grid": str(DEV_GRID.relative_to(_ROOT)),
        "dev_grid_sha256": _sha256(DEV_GRID),
        "frozen_trigger_policy": {
            "config_id": selected_id,
            "anchor_mode": "additive",
            "qualifying": list(config["qualifying"]),
            "burst_interval_seconds": int(config["burst_interval_seconds"]),
            "burst_duration_seconds": int(config["burst_duration_seconds"]),
            "poll_interval_seconds": 900,
            "selection_rule": dev_grid["selection_criterion"],
        },
        "frame_attribution_disclosure": (
            "the Marshall, MN county-event is admitted under the NCEI 'Fort "
            "Lauderdale Flash Flood (April 2023)' frame label via the date-"
            "window declaration rule; meteorologically it belongs to the "
            "concurrent April 2023 upper-Midwest flooding, which no other "
            "included county-event shares"
        ),
        "artifacts": artifacts,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out_path),
                "sha256": _sha256(out_path),
                "n_artifacts": len(artifacts),
                "frozen_config": selected_id,
                "protocol_sha256": manifest["protocol_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
