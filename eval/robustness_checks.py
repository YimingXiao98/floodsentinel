"""Two post-hoc robustness checks on the frozen Study 3 (v4) county-events.

Neither was prespecified; both were requested by the simulated review of the manuscript (1 September 2026).

1. Mission-start offsets. The frozen study ran phase adaptation and the additive hybrid once per county-event, anchored at the window start, while every fixed clock was averaged over all of its 15-minute start offsets. Here both policies are run at every start offset in 15-minute steps over one base interval (6 h, 24 offsets), so their own alignment luck is averaged the same way. For each offset draw the frozen Q2 comparison is repeated against the frozen budget-matched offset median, and against the uniform 3 h clock's offset median.

2. Phase-free control. The same verification trigger (frozen v4 policy, additive anchoring) placed on a fixed 6 h base clock instead of the phase-adaptive cadence: 2 h while a qualifying warning holds a burst window open, 6 h otherwise, with the phase model still computing verdicts but never setting timing. Run at the same 24 start offsets. This isolates what the phase model contributes to timing once the trigger exists.

Reads the frozen v4 manifest and corpora; writes only under eval/results/robustness_checks/. Reuses the v3/v4 generators' run and summary helpers unchanged so every row is computed exactly as the study's schedule rows were.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.ground_truth import apply_threshold_compatibility  # noqa: E402
from eval.run_study import (  # noqa: E402
    FROZEN_REPLAY_ENVIRONMENT,
    _resolve_repo_path,
    _schedule_row,
    _slug,
    _temporary_environment,
    _write_json,
)
from eval.run_study_v3 import _init_worker, _run_one, _summary_for  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

MANIFEST = _ROOT / "eval" / "corpus" / "study_v4" / "manifest.json"
FROZEN_AGGREGATE = _ROOT / "eval" / "results" / "study_v4_additive" / "aggregate.json"
POST_HOC = _ROOT / "eval" / "results" / "post_hoc_comparators" / "summary.json"
OUT = _ROOT / "eval" / "results" / "robustness_checks"
BASE_INTERVAL_SECONDS = 21600
OFFSET_STEP_SECONDS = 900
EVAL_INTERVAL_SECONDS = 900


def _load_references() -> Dict[str, Dict[str, Any]]:
    agg = json.load(open(FROZEN_AGGREGATE))
    post = json.load(open(POST_HOC))["studies"]["study3_v4_additive"]["county_events"]
    post_by_slug = {r["county_event"]: r for r in post}
    refs: Dict[str, Dict[str, Any]] = {}
    for r in agg["criterion_inputs"]:
        refs[r["fips"] + "|" + r["event"]] = {
            "matched_median": r["matched_hybrid_offsets_unobserved_median"],
            "matched_min": r["matched_hybrid_offsets_unobserved_min"],
            "matched_median_episodes": r["matched_hybrid_offsets_episodes_observed_median"],
            "frozen_hybrid_unobserved": r["hybrid_unobserved_hours"],
            "frozen_hybrid_episodes": r["hybrid_episodes_observed"],
            "frozen_hybrid_acquisitions": r["hybrid_acquisitions"],
            "frozen_adaptive_unobserved": r["adaptive_unobserved_hours"],
        }
    return refs, post_by_slug


def _tasks_for(entry: Dict[str, Any], excluded_ids: List[str], trigger_spec: Dict[str, Any], poll_seconds: int, offsets: List[int]):
    base = {"entry": entry, "excluded_ids": excluded_ids}
    hybrid_kwargs = {"trigger": trigger_spec, "trigger_corpus_path": str(_resolve_repo_path(entry["trigger_corpus"])), "trigger_poll_interval_seconds": poll_seconds}
    tasks = []
    for off in offsets:
        tasks.append({**base, "name": f"adaptive@{off}", "run_kwargs": {"schedule_offset_seconds": off}})
        tasks.append({**base, "name": f"hybrid@{off}", "run_kwargs": {**hybrid_kwargs, "schedule_offset_seconds": off}})
        tasks.append({**base, "name": f"phase_free@{off}", "run_kwargs": {**hybrid_kwargs, "trigger": {**trigger_spec, "base_schedule": "fixed"}, "fixed_interval_seconds": BASE_INTERVAL_SECONDS, "schedule_offset_seconds": off}})
    return tasks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="only the first N county-events (smoke test)")
    ap.add_argument("--offsets", type=int, default=BASE_INTERVAL_SECONDS // OFFSET_STEP_SECONDS, help="number of 15-minute start offsets (default 24)")
    args = ap.parse_args(argv)

    manifest = json.load(open(MANIFEST))
    policy = manifest["frozen_trigger_policy"]
    trigger_spec = {"enabled": True, "qualifying": list(policy["qualifying"]), "burst_interval_seconds": int(policy["burst_interval_seconds"]), "burst_duration_seconds": int(policy["burst_duration_seconds"]), "anchor_mode": "additive"}
    poll_seconds = int(policy["poll_interval_seconds"])
    offsets = [k * OFFSET_STEP_SECONDS for k in range(args.offsets)]
    entries = [e for e in manifest["artifacts"] if e.get("analysis_role") == "episode_evaluation"]
    if args.limit:
        entries = entries[: args.limit]
    refs, post_by_slug = _load_references()

    OUT.mkdir(parents=True, exist_ok=True)
    per_county: List[Dict[str, Any]] = []
    t0 = time.time()
    for entry in entries:
        slug = f"{_slug(entry['county'])}_{entry['fips']}_{entry['analysis_start'][:10]}"
        excluded_ids = sorted({str(r.get("site_no")) for r in (entry.get("threshold_exclusions") or []) if r.get("site_no")})
        corpus, _ = apply_threshold_compatibility(load_corpus(_resolve_repo_path(entry["path"])), excluded_site_ids=excluded_ids)
        tasks = _tasks_for(entry, excluded_ids, trigger_spec, poll_seconds, offsets)
        tc = time.time()
        with _temporary_environment(FROZEN_REPLAY_ENVIRONMENT):
            with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker) as pool:
                runs = dict(pool.map(_run_one, tasks, chunksize=1))
        rows = {}
        for name, run in runs.items():
            summary = _summary_for(run, corpus=corpus, evaluation_interval_seconds=EVAL_INTERVAL_SECONDS)
            row = _schedule_row(entry, name, summary)
            rows[name] = {"unobserved": row["unobserved_flood_stage_duration_hours"], "episodes_observed": row["episodes_observed_by_schedule"], "acquisitions": row["acquisitions"], "folded_at": row["folded_at"]}
        ref = refs[entry["fips"] + "|" + entry["event"]]
        fixed3 = post_by_slug[slug]["fixed"]["3h"]["unobserved_median"]
        # offset-0 hybrid and adaptive must reproduce the frozen study exactly
        h0, a0 = rows["hybrid@0"], rows["adaptive@0"]
        sanity = {"hybrid_offset0_matches_frozen": abs(h0["unobserved"] - ref["frozen_hybrid_unobserved"]) < 1e-9 and h0["episodes_observed"] == ref["frozen_hybrid_episodes"] and h0["acquisitions"] == ref["frozen_hybrid_acquisitions"],
                  "adaptive_offset0_matches_frozen": abs(a0["unobserved"] - ref["frozen_adaptive_unobserved"]) < 1e-9}
        per_county.append({"county_event": slug, "county": entry["county"], "state": entry["state"], "event": entry["event"], "episodes_total": h0["episodes_observed"] + (post_by_slug[slug]["episodes_total"] - h0["episodes_observed"]), "references": {**ref, "fixed_3h_median": fixed3}, "offsets_seconds": offsets, "policies": {pol: [rows[f"{pol}@{off}"] for off in offsets] for pol in ("adaptive", "hybrid", "phase_free")}, "sanity": sanity, "seconds": round(time.time() - tc, 1)})
        print(f"{slug}: {len(tasks)} runs in {time.time()-tc:.0f}s; sanity {sanity}", flush=True)
        _write_json(OUT / "partial.json", per_county)

    # ---------- aggregation ----------
    n_off = len(offsets)
    def pooled(pol, k, key): return sum(c["policies"][pol][k][key] for c in per_county)
    per_offset = []
    for k in range(n_off):
        wins_matched = sum(c["policies"]["hybrid"][k]["unobserved"] < c["references"]["matched_median"] for c in per_county)
        losses_matched = sum(c["policies"]["hybrid"][k]["unobserved"] > c["references"]["matched_median"] for c in per_county)
        wins_3h = sum(c["policies"]["hybrid"][k]["unobserved"] < c["references"]["fixed_3h_median"] for c in per_county)
        losses_3h = sum(c["policies"]["hybrid"][k]["unobserved"] > c["references"]["fixed_3h_median"] for c in per_county)
        hyb_vs_pf_w = sum(c["policies"]["hybrid"][k]["unobserved"] < c["policies"]["phase_free"][k]["unobserved"] for c in per_county)
        hyb_vs_pf_l = sum(c["policies"]["hybrid"][k]["unobserved"] > c["policies"]["phase_free"][k]["unobserved"] for c in per_county)
        per_offset.append({"offset_seconds": offsets[k],
            "hybrid": {"unobserved": pooled("hybrid", k, "unobserved"), "episodes": pooled("hybrid", k, "episodes_observed"), "acquisitions": pooled("hybrid", k, "acquisitions")},
            "adaptive": {"unobserved": pooled("adaptive", k, "unobserved"), "episodes": pooled("adaptive", k, "episodes_observed"), "acquisitions": pooled("adaptive", k, "acquisitions")},
            "phase_free": {"unobserved": pooled("phase_free", k, "unobserved"), "episodes": pooled("phase_free", k, "episodes_observed"), "acquisitions": pooled("phase_free", k, "acquisitions")},
            "hybrid_below_matched_median": wins_matched, "hybrid_above_matched_median": losses_matched,
            "hybrid_below_fixed3h_median": wins_3h, "hybrid_above_fixed3h_median": losses_3h,
            "hybrid_below_phase_free": hyb_vs_pf_w, "hybrid_above_phase_free": hyb_vs_pf_l})
    def dist(vals): return {"min": min(vals), "median": statistics.median(vals), "max": max(vals)}
    summary = {
        "note": __doc__.strip(),
        "county_events": len(per_county), "offsets": n_off, "offset_step_seconds": OFFSET_STEP_SECONDS, "base_interval_seconds": BASE_INTERVAL_SECONDS,
        "sanity_all_offset0_match_frozen": all(c["sanity"]["hybrid_offset0_matches_frozen"] and c["sanity"]["adaptive_offset0_matches_frozen"] for c in per_county),
        "frozen_reference_pooled": {"matched_median_unobserved": sum(c["references"]["matched_median"] for c in per_county), "fixed_3h_median_unobserved": sum(c["references"]["fixed_3h_median"] for c in per_county), "matched_median_episodes": sum(c["references"]["matched_median_episodes"] for c in per_county)},
        "across_offsets": {
            pol: {"pooled_unobserved": dist([o[pol]["unobserved"] for o in per_offset]), "pooled_episodes": dist([o[pol]["episodes"] for o in per_offset]), "pooled_acquisitions": dist([o[pol]["acquisitions"] for o in per_offset])}
            for pol in ("hybrid", "adaptive", "phase_free")},
        "hybrid_wins_vs_matched_median_across_offsets": dist([o["hybrid_below_matched_median"] for o in per_offset]),
        "hybrid_wins_vs_fixed3h_median_across_offsets": dist([o["hybrid_below_fixed3h_median"] for o in per_offset]),
        "hybrid_wins_vs_phase_free_across_offsets": dist([o["hybrid_below_phase_free"] for o in per_offset]),
        "hybrid_losses_vs_phase_free_across_offsets": dist([o["hybrid_above_phase_free"] for o in per_offset]),
        "offsets_where_hybrid_wins_at_least_20_of_24_vs_matched": sum(o["hybrid_below_matched_median"] >= 20 for o in per_offset),
        "per_offset": per_offset,
        "per_county_event": per_county,
        "seconds_total": round(time.time() - t0, 1),
    }
    _write_json(OUT / "summary.json", summary)
    (OUT / "partial.json").unlink(missing_ok=True)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("per_offset", "per_county_event", "note")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
