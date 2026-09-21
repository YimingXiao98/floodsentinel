"""Mechanical, policy-outcome-blind held-out selection for the v4 study.

Reuses the v3 machinery in :mod:`eval.select_holdout` with the amendments
frozen in ``eval/HYBRID_STUDY_PROTOCOL_V4.md``: territory FIPS coverage, all
fourteen development counties and eleven development events excluded, six
candidates tried per event, up to three counties admitted per event across
passes 1--3, a 24-county-event stop target, and a coverage-only cap of four.
Outputs corpora under ``eval/corpus/study_v4/`` and the selection record at
``eval/corpus/study_v4/holdout_selection.json``. Nothing here observes any
policy's behavior.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import eval.select_holdout as base  # noqa: E402

# --- v4 amendments (protocol section: held-out selection) -------------------

base._FIPS_TO_USPS.update(
    {"72": "PR", "78": "VI", "66": "GU", "60": "AS", "69": "MP"}
)

#: Development counties: five v2 + nine v3 (by FIPS), excluded outright.
DEV_COUNTY_FIPS_V4 = frozenset(
    {
        "48201", "12031", "12057", "12105", "12086",  # v2
        "34035", "37119", "12097", "37037", "45051",  # v3
        "22033", "37183", "01003", "48339",
    }
)

#: Development events: Harvey, Irma, and the nine v3 events (full NCEI names
#: with parenthetical dates so substring matching cannot collide, e.g.
#: "Hurricane Ida (August 2021)" never matches "Hurricane Idalia").
DEV_EVENT_NAMES_V4 = (
    "Hurricane Harvey",
    "Hurricane Irma",
    "Hurricane Ida (August 2021)",
    "Hurricane Helene (September 2024)",
    "Hurricane Milton (October 2024)",
    "Hurricane Michael (October 2018)",
    "Hurricane Florence (September 2018)",
    "Louisiana Flooding (August 2016)",
    "Hurricane Matthew (October 2016)",
    "Hurricane Sally (September 2020)",
    "Hurricane Beryl (July 2024)",
)

CANDIDATES_PER_EVENT_V4 = 6
MAX_PASSES_V4 = 3
STOP_MAX_COUNTY_EVENTS_V4 = 24
MIN_POWERED_COUNTY_EVENTS = 12
MAX_COVERAGE_ONLY_V4 = 4

base.DEV_COUNTY_FIPS = set(DEV_COUNTY_FIPS_V4)
base.DEV_EVENT_NAMES = DEV_EVENT_NAMES_V4
base.CORPUS_DIR = _ROOT / "eval" / "corpus" / "study_v4"
base.CACHE_DIR = _ROOT / "eval" / "corpus" / "study_v4" / "cache"
SELECTION_OUT = _ROOT / "eval" / "corpus" / "study_v4" / "holdout_selection.json"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(SELECTION_OUT))
    args = parser.parse_args(argv)

    frame, frame_excluded = base.load_event_frame()
    considered: List[Dict[str, Any]] = []
    included: List[Dict[str, Any]] = []
    coverage_only: List[Dict[str, Any]] = []
    included_events: Dict[str, int] = {}
    tried_by_event: Dict[str, List[str]] = {}

    def stop_reached() -> bool:
        return len(included) >= STOP_MAX_COUNTY_EVENTS_V4

    for pass_index in range(1, MAX_PASSES_V4 + 1):
        for event in frame:
            if stop_reached():
                break
            # Each pass may admit at most one new county per event, so an
            # event can contribute at most MAX_PASSES_V4 counties in total.
            if included_events.get(event["name"], 0) >= pass_index:
                continue
            candidates = base.candidate_counties(event)
            for candidate in candidates:
                with_stage, total = base.registry_gauge_counts(
                    candidate["county"], candidate["state"]
                )
                candidate["registry_gauges_with_flood_stage"] = with_stage
                candidate["registry_gauges_in_service"] = total
            ranked = sorted(
                candidates,
                key=lambda c: (
                    -c["registry_gauges_with_flood_stage"],
                    -c["registry_gauges_in_service"],
                    c["state"],
                    c["county"],
                ),
            )
            tried = tried_by_event.setdefault(event["name"], [])
            attempts = 0
            for candidate in ranked:
                if attempts >= CANDIDATES_PER_EVENT_V4:
                    break
                if candidate["registry_gauges_with_flood_stage"] < 1:
                    break
                if candidate["fips"] in tried:
                    continue
                tried.append(candidate["fips"])
                attempts += 1
                print(
                    f"[pass {pass_index}] {event['name']} -> trying "
                    f"{candidate['county']}, {candidate['state']} "
                    f"({candidate['fips']}; "
                    f"{candidate['registry_gauges_with_flood_stage']} gauges)",
                    file=sys.stderr,
                )
                try:
                    result = base.evaluate_candidate(event, candidate)
                except Exception as exc:
                    result = {
                        "event": event["name"],
                        "candidate": candidate,
                        "decision": "excluded",
                        "exclusion_reason": f"build_or_qc_error: {exc}",
                    }
                result["selection_pass"] = pass_index
                considered.append(result)
                if result["decision"] == "episode_evaluation":
                    included.append(result)
                    included_events[event["name"]] = (
                        included_events.get(event["name"], 0) + 1
                    )
                    break
                if (
                    result["decision"] == "coverage_only_candidate"
                    and len(coverage_only) < MAX_COVERAGE_ONLY_V4
                ):
                    coverage_only.append(result)
            if stop_reached():
                break
        if stop_reached():
            break

    selection = {
        "schema": "sentinel-v4-holdout-selection/1",
        "protocol": "eval/HYBRID_STUDY_PROTOCOL_V4.md",
        "policy_outcome_blind": True,
        "ncei_events_csv": str(base.NCEI_CSV.relative_to(_ROOT)),
        "ncei_events_csv_sha256": base._sha256(base.NCEI_CSV),
        "registry_csv": str(base.REGISTRY_CSV.relative_to(_ROOT)),
        "registry_csv_sha256": base._sha256(base.REGISTRY_CSV),
        "rules": {
            "event_order": "descending CPI-adjusted cost",
            "max_impact_period_days": base.MAX_IMPACT_PERIOD_DAYS,
            "window_days": [base.WINDOW_BACK_DAYS, base.WINDOW_FORWARD_DAYS],
            "candidates_per_event": CANDIDATES_PER_EVENT_V4,
            "max_passes": MAX_PASSES_V4,
            "max_counties_per_event": MAX_PASSES_V4,
            "stop_max_county_events": STOP_MAX_COUNTY_EVENTS_V4,
            "min_powered_county_events": MIN_POWERED_COUNTY_EVENTS,
            "max_coverage_only": MAX_COVERAGE_ONLY_V4,
            "min_acknowledged_truth_coverage": base.MIN_ACK_COVERAGE,
            "datum_screen_flood_fraction": base.DATUM_SCREEN_FLOOD_FRACTION,
            "datum_screen_min_observations": 20,
            "tidal_datum_names_excluded": sorted(base.TIDAL_DATUM_NAMES),
            "development_counties_excluded": sorted(DEV_COUNTY_FIPS_V4),
            "development_events_excluded": list(DEV_EVENT_NAMES_V4),
            "territories_reachable": ["PR", "VI", "GU", "AS", "MP"],
        },
        "event_frame": frame,
        "event_frame_excluded": frame_excluded,
        "included_episode_evaluation": included,
        "included_coverage_only": coverage_only,
        "all_candidates_considered": considered,
        "pooled_hydrologic_episodes_1h": sum(
            r["hydrologic_episodes_1h"] for r in included
        ),
        "n_events_included": len(included_events),
        "n_county_events_included": len(included),
        "powered": len(included) >= MIN_POWERED_COUNTY_EVENTS,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(selection, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "out": str(out_path),
                "n_county_events": len(included),
                "n_events": len(included_events),
                "pooled_episodes": selection["pooled_hydrologic_episodes_1h"],
                "coverage_only": len(coverage_only),
                "n_considered": len(considered),
                "powered": selection["powered"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
