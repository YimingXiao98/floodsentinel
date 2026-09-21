"""LEGACY, SUPERSEDED Harris-only sensitivity script.

Do not use this module to regenerate publication results. Its C1/C2 conclusions
and no-role cadence comparison were withdrawn during the evaluation audit.
The frozen multi-county engineering sensitivities now live in
``eval/run_study.py`` and preserve phase reasoning in schedule baselines.

Historically, the headline results rested on parameters chosen by judgement: how long a reading
stays usable, how many gauges a county verdict requires, how strongly the
reliability tier discounts a claim, and what size of movement counts as
reportable. A result that holds only at one setting is not a result.

For each parameter this sweeps a range and asks whether the *conclusion*
survives, not whether the number moves:

  C1  the role layer decides whether the system reacts
      (full escalates and misses nothing; no_role never escalates)
  C2  the belief layer decides whether confidence carries information
      (calibration error is lower with the layer than without)
  C3  phase-aware monitoring makes fewer acquisitions than the no-role
      ablation (diagnostic only).  Primary cadence comparisons live in
      eval/baselines.py and preserve phase reasoning while overriding schedule.

Execution is gated behind ``--run-withdrawn-legacy-analysis`` to prevent an
accidental overwrite of current study artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.ground_truth import load as truth_load  # noqa: E402
from eval.metrics import evaluate  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

CORPUS = "eval/corpus/harris_harvey_2017.json"
TRUTH = "eval/truth/harris_harvey_2017.json"
START, END = "2017-08-25T00:00:00Z", "2017-09-01T00:00:00Z"
COUNTY, STATE, FIPS = "Harris", "TX", "48201"

#: Parameter, environment variable (or None for a CLI flag), and values swept.
#: The middle value of each range is the default used in the main results.
SWEEPS = [
    ("staleness_hours", None, [3.0, 6.0, 12.0, 24.0]),
    ("min_gauges", "COUNTY_FLOOD_MIN_GAUGES", [1, 3, 5, 8]),
    ("tier_weight", "SENTINEL_TIER_WEIGHT", [0.0, 0.5, 1.0]),
    ("flow_threshold_pct", "REPORT_GAUGE_FLOW_THRESHOLD_PCT", [10.0, 20.0, 50.0]),
    ("stage_threshold_ft", "REPORT_GAUGE_STAGE_THRESHOLD_FT", [0.05, 0.1, 0.3]),
]

MODES = ("full", "no_role", "no_belief")


def _run(mode: str, env_extra: Dict[str, str], staleness: float) -> Optional[Dict[str, Any]]:
    out = Path(f"/tmp/sens_{mode}.json")
    env = {**os.environ, **env_extra, "SENTINEL_ABLATION": mode}
    cmd = [
        sys.executable, "eval/run_replay.py",
        "--ablation", mode, "--county", COUNTY, "--state", STATE, "--fips", FIPS,
        "--corpus", CORPUS, "--start", START, "--end", END,
        "--max-staleness-hours", str(staleness), "--out", str(out),
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=_ROOT)
    if result.returncode != 0:
        print(f"    run failed ({mode}): {result.stderr.strip()[:200]}", flush=True)
        return None
    return json.loads(out.read_text())


def sweep() -> Dict[str, Any]:
    corpus = load_corpus(_ROOT / CORPUS)
    events = truth_load(_ROOT / TRUTH)["events"]
    findings: List[Dict[str, Any]] = []

    for name, env_var, values in SWEEPS:
        for value in values:
            env_extra = {env_var: str(value)} if env_var else {}
            staleness = value if name == "staleness_hours" else 6.0
            row: Dict[str, Any] = {"parameter": name, "value": value}
            for mode in MODES:
                run = _run(mode, env_extra, staleness)
                if run is None:
                    row[mode] = None
                    continue
                m = evaluate(run, corpus=corpus, events=events)
                row[mode] = {
                    "escalates": m["rq2_timeliness"]["first_system_escalation"] is not None,
                    "missed_rate": m["rq2_timeliness"]["missed_escalation_rate"],
                    "acquisitions": m["rq2_timeliness"]["acquisitions"],
                    "hypothetical_logical_report_delivery_opportunities_if_materiality_gated": m[
                        "rq2_timeliness"
                    ][
                        "hypothetical_logical_report_delivery_opportunities_if_materiality_gated"
                    ],
                    "ece": m["rq3_calibration"]["ece"],
                    "brier": m["rq3_calibration"]["brier"],
                }
            full, no_role, no_belief = row.get("full"), row.get("no_role"), row.get("no_belief")
            row["C1_role_decides_reaction"] = bool(
                full and no_role and full["escalates"] and not no_role["escalates"]
            )
            row["C2_belief_improves_calibration"] = bool(
                full and no_belief and full["ece"] < no_belief["ece"]
            )
            row["C3_fewer_acquisitions_than_no_role"] = bool(
                full
                and no_role
                and full["acquisitions"] < no_role["acquisitions"]
            )
            findings.append(row)
            print(
                f"  {name}={value}: C1={row['C1_role_decides_reaction']} "
                f"C2={row['C2_belief_improves_calibration']} "
                f"C3={row['C3_fewer_acquisitions_than_no_role']}",
                flush=True,
            )

    holds = {
        c: sum(1 for f in findings if f[c]) for c in
        (
            "C1_role_decides_reaction",
            "C2_belief_improves_calibration",
            "C3_fewer_acquisitions_than_no_role",
        )
    }
    return {
        "schema": "sentinel-sensitivity/1",
        "status": "legacy_superseded_withdrawn_do_not_cite",
        "county": COUNTY,
        "window": {"start": START, "end": END},
        "n_settings": len(findings),
        "conclusions_hold": holds,
        "findings": findings,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-withdrawn-legacy-analysis", action="store_true")
    args = parser.parse_args(argv)
    if not args.run_withdrawn_legacy_analysis:
        print(
            "Refusing to run superseded Harris-only sensitivity; use "
            "eval/run_study.py for the frozen multi-county sensitivities.",
            file=sys.stderr,
        )
        return 2
    result = sweep()
    target = _ROOT / "eval/results/sensitivity.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2))
    print(f"\n{result['n_settings']} settings; conclusions hold: {result['conclusions_hold']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
