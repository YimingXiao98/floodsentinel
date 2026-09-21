"""Part A of the v5 ontology protocol: credibility-model measurement.

Regenerates the 24 v4 additive-hybrid replays deterministically, grades every
extracted claim by the existing rq3 semantics, and reports calibration and
discrimination of the belief layer's credibility composite. Measurement only:
no pass/fail gate; the single prespecified inferential statement is the AUC
cluster-bootstrap interval (resampling county-events, 10,000 draws, seed
20260830). See ``eval/ONTOLOGY_STUDY_PROTOCOL.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.ground_truth import apply_threshold_compatibility  # noqa: E402
from eval.metrics import _claim_outcomes, annotate_run, rq3_calibration  # noqa: E402
from eval.run_replay import run as replay_run  # noqa: E402
from eval.run_study import FROZEN_REPLAY_ENVIRONMENT, _sha256, _write_json  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

PROTOCOL = _ROOT / "eval" / "ONTOLOGY_STUDY_PROTOCOL.md"
V4_MANIFEST = _ROOT / "eval" / "corpus" / "study_v4" / "manifest.json"
BOOTSTRAP_SEED = 20260830
BOOTSTRAP_DRAWS = 10_000


def _init_worker() -> None:
    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)


def _county_outcomes(entry: Dict[str, Any]) -> Dict[str, Any]:
    manifest = json.loads(V4_MANIFEST.read_text(encoding="utf-8"))
    policy = manifest["frozen_trigger_policy"]
    excluded = sorted(
        {
            str(row.get("site_no"))
            for row in (entry.get("threshold_exclusions") or [])
            if row.get("site_no")
        }
    )
    corpus, _ = apply_threshold_compatibility(
        load_corpus(_ROOT / entry["path"]), excluded_site_ids=excluded
    )
    run = replay_run(
        corpus_path=str(_ROOT / entry["path"]),
        county=entry["county"],
        state=entry["state"],
        fips=entry["fips"],
        start=entry["analysis_start"],
        end=entry["analysis_end"],
        max_staleness_hours=6.0,
        threshold_excluded_site_ids=excluded,
        trigger={
            "enabled": True,
            "qualifying": policy["qualifying"],
            "burst_interval_seconds": policy["burst_interval_seconds"],
            "burst_duration_seconds": policy["burst_duration_seconds"],
            "anchor_mode": policy["anchor_mode"],
        },
        trigger_corpus_path=str(_ROOT / entry["trigger_corpus"]),
        trigger_poll_interval_seconds=policy["poll_interval_seconds"],
    )
    ticks = annotate_run(run, corpus=corpus, events=[])
    outcomes = _claim_outcomes(ticks, corpus)
    calibration = rq3_calibration(ticks, corpus=corpus)
    return {
        "county": entry["county"],
        "fips": entry["fips"],
        "event": entry.get("event"),
        "outcomes": outcomes,
        "calibration": calibration,
    }


def _auc(outcomes: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Rank-based AUC: P(correct claim outranks incorrect), ties at 0.5."""
    pos = sorted(o["confidence"] for o in outcomes if o["correct"])
    neg = sorted(o["confidence"] for o in outcomes if not o["correct"])
    if not pos or not neg:
        return None
    import bisect

    wins = 0.0
    for value in pos:
        lo = bisect.bisect_left(neg, value)
        hi = bisect.bisect_right(neg, value)
        wins += lo + 0.5 * (hi - lo)
    return wins / (len(pos) * len(neg))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="eval/results/study_v5_ontology/credibility_measurement.json",
    )
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    manifest = json.loads(V4_MANIFEST.read_text(encoding="utf-8"))
    entries = [
        e
        for e in manifest["artifacts"]
        if e.get("analysis_role") == "episode_evaluation"
    ]
    os.environ.update(FROZEN_REPLAY_ENVIRONMENT)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker) as pool:
        county_rows = list(pool.map(_county_outcomes, entries, chunksize=1))

    pooled = [o for row in county_rows for o in row["outcomes"]]
    auc = _auc(pooled)
    rng = random.Random(BOOTSTRAP_SEED)
    draws: List[float] = []
    for _ in range(BOOTSTRAP_DRAWS):
        sample = [
            o
            for row in rng.choices(county_rows, k=len(county_rows))
            for o in row["outcomes"]
        ]
        value = _auc(sample)
        if value is not None:
            draws.append(value)
    draws.sort()
    ci = (
        [round(draws[int(0.025 * len(draws))], 4), round(draws[int(0.975 * len(draws)) - 1], 4)]
        if draws
        else None
    )

    by_tier: Dict[str, List[Dict[str, Any]]] = {}
    for o in pooled:
        by_tier.setdefault(o["tier"], []).append(o)

    def _summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(rows)
        correct = sum(o["correct"] for o in rows)
        return {
            "n_claims": n,
            "accuracy": round(correct / n, 4) if n else None,
            "auc": (round(_auc(rows), 4) if _auc(rows) is not None else None),
        }

    # Pooled calibration is computed directly from outcomes to avoid
    # re-annotating: Brier and ECE over the pooled (confidence, correct) rows.
    brier = (
        round(
            sum((o["confidence"] - o["correct"]) ** 2 for o in pooled) / len(pooled), 4
        )
        if pooled
        else None
    )
    bins = []
    ece = 0.0
    for index in range(10):
        low, high = index / 10, (index + 1) / 10
        members = [
            o
            for o in pooled
            if (low <= o["confidence"] < high)
            or (index == 9 and o["confidence"] == 1.0)
        ]
        if not members:
            continue
        mean_conf = sum(o["confidence"] for o in members) / len(members)
        accuracy = sum(o["correct"] for o in members) / len(members)
        ece += (len(members) / len(pooled)) * abs(accuracy - mean_conf)
        bins.append(
            {
                "range": [round(low, 2), round(high, 2)],
                "n": len(members),
                "mean_confidence": round(mean_conf, 4),
                "accuracy": round(accuracy, 4),
            }
        )

    result = {
        "schema": "sentinel-v5-credibility-measurement/1",
        "protocol": "eval/ONTOLOGY_STUDY_PROTOCOL.md",
        "protocol_sha256": _sha256(PROTOCOL),
        "v4_manifest_sha256": _sha256(V4_MANIFEST),
        "role": (
            "measurement of the credibility composite's scores; no claim "
            "about operational decisions"
        ),
        "n_county_events": len(county_rows),
        "n_claims_graded": len(pooled),
        "n_correct": sum(o["correct"] for o in pooled),
        "pooled_accuracy": (
            round(sum(o["correct"] for o in pooled) / len(pooled), 4)
            if pooled
            else None
        ),
        "brier": brier,
        "ece": round(ece, 4) if pooled else None,
        "distinct_confidences": len({round(o["confidence"], 4) for o in pooled}),
        "auc": round(auc, 4) if auc is not None else None,
        "auc_ci95_cluster_bootstrap": ci,
        "bootstrap": {"seed": BOOTSTRAP_SEED, "draws": BOOTSTRAP_DRAWS,
                      "cluster": "county_event"},
        "prespecified_statement": (
            "the composite discriminates iff the AUC bootstrap interval "
            "excludes 0.5"
        ),
        "discriminates": (
            bool(ci and (ci[0] > 0.5 or ci[1] < 0.5)) if ci else None
        ),
        "reliability_bins": bins,
        "by_tier": {tier: _summary(rows) for tier, rows in sorted(by_tier.items())},
        "per_county_calibration": [
            {
                "county": row["county"],
                "event": row["event"],
                "n_claims": row["calibration"]["n_claims"],
                "brier": row["calibration"]["brier"],
                "ece": row["calibration"]["ece"],
            }
            for row in county_rows
        ],
    }
    out = _ROOT / args.out
    _write_json(out, result)
    print(
        json.dumps(
            {
                "out": str(out),
                "sha256": _sha256(out),
                "n_claims": result["n_claims_graded"],
                "accuracy": result["pooled_accuracy"],
                "auc": result["auc"],
                "auc_ci95": result["auc_ci95_cluster_bootstrap"],
                "discriminates": result["discriminates"],
                "brier": result["brier"],
                "ece": result["ece"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
