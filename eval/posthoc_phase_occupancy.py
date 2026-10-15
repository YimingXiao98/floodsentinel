"""Post-hoc phase occupancy of the Study 3 (v4 additive) replays.

Reads the frozen per-county results in ``eval/results/study_v4_additive/counties/*/result.json`` and, for the hybrid and phase-adaptive policies over the 24 episode-evaluation county-events, counts how many missions reached each runtime phase, the hours spent in each, and the share of window time at the 2 h cadence (imminent plus active incident). The source fields are ``schedule_rows[].phase_duration_hours``, ``phase_changes``, ``folded_at`` and ``analysis_role``. Nothing is replayed and the frozen artifacts are not modified.

Writes ``eval/results/posthoc_phase_occupancy/summary.json`` and ``summary.md``.
"""

from __future__ import annotations

import glob
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results" / "study_v4_additive" / "counties"
OUT = ROOT / "eval" / "results" / "posthoc_phase_occupancy"
POLICIES = ("hybrid", "adaptive")
FAST_PHASES = ("imminent", "active_incident")


def summarize(policy: str) -> dict:
    reached: Counter = Counter()
    hours: dict = defaultdict(float)
    window_hours = fast_hours = 0.0
    changes: list = []
    folded = n = 0
    for path in sorted(glob.glob(str(RESULTS / "*" / "result.json"))):
        data = json.loads(Path(path).read_text())
        for row in data["schedule_rows"]:
            if row["policy"] != policy or row["analysis_role"] != "episode_evaluation":
                continue
            n += 1
            durations = row["phase_duration_hours"]
            window_hours += sum(durations.values())
            for phase, value in durations.items():
                hours[phase] += value
                if value > 0:
                    reached[phase] += 1
            fast_hours += sum(durations.get(p, 0.0) for p in FAST_PHASES)
            changes.append(row["phase_changes"])
            folded += row["folded_at"] is not None
    return {
        "county_events": n,
        "missions_reaching_phase": dict(sorted(reached.items())),
        "hours_by_phase": dict(sorted(hours.items())),
        "window_hours": window_hours,
        "two_hour_cadence_hours": fast_hours,
        "two_hour_cadence_share": fast_hours / window_hours,
        "folded_missions": folded,
        "phase_changes_median": statistics.median(changes),
        "phase_changes_range": [min(changes), max(changes)],
    }


def main() -> int:
    summary = {"schema": "sentinel-posthoc-phase-occupancy/1", "source": str(RESULTS.relative_to(ROOT)), "policies": {p: summarize(p) for p in POLICIES}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = ["# Study 3 phase occupancy (post hoc)", "", f"Source: `{summary['source']}` (`schedule_rows`), episode-evaluation county-events only.", ""]
    for policy, s in summary["policies"].items():
        reach = ", ".join(f"{k} {v}" for k, v in s["missions_reaching_phase"].items())
        lines.append(f"- {policy}: {s['county_events']} county-events; missions reaching each phase: {reach}; folded {s['folded_missions']}; 2 h cadence {s['two_hour_cadence_hours']:.2f} of {s['window_hours']:.0f} window hours ({100 * s['two_hour_cadence_share']:.1f}%); median phase changes {s['phase_changes_median']} (range {s['phase_changes_range'][0]} to {s['phase_changes_range'][1]}).")
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
