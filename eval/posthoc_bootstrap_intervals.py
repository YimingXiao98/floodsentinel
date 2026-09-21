"""Post-hoc paired bootstrap intervals for the pooled unobserved-duration and episode differences.

Nothing here was prespecified. The frozen study reports summarize each policy comparison as a pooled sum over county-events plus an exact sign test; this script adds a percentile bootstrap interval for the same pooled difference, resampling county-events with replacement, so that the size of each difference can be read with a sampling-uncertainty band. It reads only frozen artifacts and the existing post-hoc summaries, reproduces the published check values from the per-county rows before doing anything else, and fails closed if any of them disagree. The frozen directories are never written.

Resampling unit: the county-event (24 in Study 3, 9 in Study 2). For each comparison A minus B the statistic is the pooled difference, sum over the resampled county-events of A's unobserved hours minus B's unobserved hours, the same thing for episodes observed where both policies have a per-county-event count, and the plain paired mean difference (pooled difference divided by n). The mean interval is the pooled interval divided by n; both are printed because the paper quotes pooled hours. Percentile 95% intervals, 10,000 resamples, seed 20260918, one shared index matrix per study so every comparison within a study is evaluated on the same resamples.

Robustness-check comparisons (phase-free control against the additive hybrid and against phase adaptation) use the per-county-event, per-offset rows of eval/results/robustness_checks/summary.json. The reported medians (56.00, 52.63, 103.88 pooled hours) are medians across the 24 start offsets of each policy's pooled value; with 24 offsets the median is the mean of two middle draws and the middle draws differ by policy, so no single "median offset" exists. The primary statistic here therefore averages each county-event's difference over all 24 offsets (offsets as strata) and bootstraps county-events; the offset-0 draw (identical to the frozen study run) and the per-offset range of intervals are reported beside it.
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "eval", "results", "posthoc_bootstrap")
SEED = 20260918
DRAWS = 10_000
ALPHA = 0.05

STUDY3_DIR = os.path.join(ROOT, "eval", "results", "study_v4_additive")
STUDY2_DIR = os.path.join(ROOT, "eval", "results", "study_v3_hybrid_rev2")
POST_HOC = os.path.join(ROOT, "eval", "results", "post_hoc_comparators", "summary.json")
ROBUST = os.path.join(ROOT, "eval", "results", "robustness_checks", "summary.json")

UNOBS = "unobserved_flood_stage_duration_hours"
EPIS = "episodes_observed_by_schedule"


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT)


def _load_county_rows(base: str):
    """Per-county-event policy values read from the frozen per-county result files (episode-evaluation only)."""
    rows = {}
    for d in sorted(glob.glob(os.path.join(base, "counties", "*"))):
        f = os.path.join(d, "result.json")
        if not os.path.exists(f):
            continue
        j = json.load(open(f))
        entry = j["corpus_manifest_entry"]
        if entry["analysis_role"] != "episode_evaluation":
            continue
        sr = j["schedule_rows"]
        hyb = next(r for r in sr if r["policy"] == "hybrid")
        ada = next(r for r in sr if r["policy"] == "adaptive")
        fixed3 = [r for r in sr if r.get("fixed_interval_hours") == 3.0 and r.get("schedule_kind") != "matched_budget"]
        matched = [r for r in j["matched_budget_rows"] if r.get("matched_target_policy") == "hybrid"]
        if not fixed3 or not matched:
            raise SystemExit(f"missing fixed 3 h or matched-budget rows in {f}")
        rows[os.path.basename(d)] = {
            "county": entry["county"],
            "state": entry["state"],
            "fips": entry["fips"],
            "event": entry["event"],
            "episodes_total": hyb[EPIS] + hyb["episodes_not_observed_by_schedule"],
            "hybrid": {"unobserved": hyb[UNOBS], "episodes": hyb[EPIS]},
            "adaptive": {"unobserved": ada[UNOBS], "episodes": ada[EPIS]},
            "matched_median": {
                "unobserved": st.median(r[UNOBS] for r in matched),
                "episodes": st.median(r[EPIS] for r in matched),
                "offsets": len(matched),
            },
            "fixed_3h_median": {
                "unobserved": st.median(r[UNOBS] for r in fixed3),
                "episodes": st.median(r[EPIS] for r in fixed3),
                "offsets": len(fixed3),
            },
        }
    return rows


def _check(label: str, got: float, want: float, checks: list) -> None:
    ok = abs(float(got) - float(want)) < 1e-9
    checks.append({"check": label, "reproduced": got, "expected": want, "ok": ok})
    if not ok:
        raise SystemExit(f"check failed: {label}: reproduced {got} expected {want}")


def _cross_check(rows: dict, aggregate_path: str, post_hoc_key: str, checks: list) -> None:
    """Every per-county-event value must agree with the frozen aggregate and the post-hoc comparator summary."""
    agg = json.load(open(aggregate_path))
    by_key = {(c["fips"], c["event"]): c for c in agg["criterion_inputs"]}
    if len(by_key) != len(rows):
        raise SystemExit(f"{aggregate_path}: {len(by_key)} criterion inputs vs {len(rows)} county-event result files")
    ph = json.load(open(POST_HOC))["studies"][post_hoc_key]["county_events"]
    ph = {c["county_event"]: c for c in ph}
    for slug, r in rows.items():
        c = by_key[(r["fips"], r["event"])]
        for mine, theirs in (
            (r["hybrid"]["unobserved"], c["hybrid_unobserved_hours"]),
            (r["adaptive"]["unobserved"], c["adaptive_unobserved_hours"]),
            (r["matched_median"]["unobserved"], c["matched_hybrid_offsets_unobserved_median"]),
            (r["hybrid"]["episodes"], c["hybrid_episodes_observed"]),
            (r["adaptive"]["episodes"], c["adaptive_episodes_observed"]),
            (r["episodes_total"], c["episodes_total"]),
        ):
            if abs(mine - theirs) > 1e-9:
                raise SystemExit(f"{slug}: result.json value {mine} disagrees with aggregate {theirs}")
        if "matched_hybrid_offsets_episodes_observed_median" in c:
            if abs(r["matched_median"]["episodes"] - c["matched_hybrid_offsets_episodes_observed_median"]) > 1e-9:
                raise SystemExit(f"{slug}: matched-median episodes disagree with aggregate")
        p = ph[slug]
        if abs(p["fixed"]["3h"]["unobserved_median"] - r["fixed_3h_median"]["unobserved"]) > 1e-9:
            raise SystemExit(f"{slug}: fixed 3 h median disagrees with post_hoc_comparators")
        if abs(p["fixed"]["3h"]["episodes_observed_median"] - r["fixed_3h_median"]["episodes"]) > 1e-9:
            raise SystemExit(f"{slug}: fixed 3 h episode median disagrees with post_hoc_comparators")
    checks.append({"check": f"{_rel(aggregate_path)} and post_hoc_comparators agree with per-county result.json rows for {len(rows)} county-events", "ok": True})


def _percentile_interval(samples: np.ndarray):
    lo, hi = np.percentile(samples, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    return float(lo), float(hi)


def _bootstrap(d: np.ndarray, idx: np.ndarray) -> dict:
    """d: per-county-event differences (A minus B). idx: DRAWS x n resample index matrix."""
    n = len(d)
    pooled = d[idx].sum(axis=1)
    lo, hi = _percentile_interval(pooled)
    point = float(d.sum())
    return {
        "n_county_events": n,
        "pooled_difference": {"point": point, "ci95": [lo, hi], "includes_zero": lo <= 0.0 <= hi},
        "mean_paired_difference": {"point": point / n, "ci95": [lo / n, hi / n], "includes_zero": lo <= 0.0 <= hi},
        "county_events_negative": int((d < 0).sum()),
        "county_events_zero": int((d == 0).sum()),
        "county_events_positive": int((d > 0).sum()),
        "bootstrap_share_below_zero": float((pooled < 0).mean()),
    }


def _compare(rows: dict, order: list, a: str, b: str, idx: np.ndarray, label: str, a_name: str, b_name: str) -> dict:
    au = np.array([rows[s][a]["unobserved"] for s in order], dtype=float)
    bu = np.array([rows[s][b]["unobserved"] for s in order], dtype=float)
    ae = np.array([rows[s][a]["episodes"] for s in order], dtype=float)
    be = np.array([rows[s][b]["episodes"] for s in order], dtype=float)
    return {
        "label": label,
        "a": a_name,
        "b": b_name,
        "pooled_a_unobserved_hours": float(au.sum()),
        "pooled_b_unobserved_hours": float(bu.sum()),
        "pooled_a_episodes_observed": float(ae.sum()),
        "pooled_b_episodes_observed": float(be.sum()),
        "unobserved_hours": _bootstrap(au - bu, idx),
        "episodes_observed": _bootstrap(ae - be, idx),
    }


def _robustness(idx: np.ndarray, checks: list) -> dict:
    r = json.load(open(ROBUST))
    pce = r["per_county_event"]
    if len(pce) != 24:
        raise SystemExit("robustness summary does not have 24 county-events")
    offsets = pce[0]["offsets_seconds"]
    k = len(offsets)
    pol = {}
    for p in ("hybrid", "phase_free", "adaptive"):
        pol[p] = np.array([[o["unobserved"] for o in c["policies"][p]] for c in pce], dtype=float)  # n x k
        pol[p + "_ep"] = np.array([[o["episodes_observed"] for o in c["policies"][p]] for c in pce], dtype=float)
        if pol[p].shape != (24, k):
            raise SystemExit(f"robustness {p}: unexpected shape {pol[p].shape}")
    # reproduce the reported per-offset pooled medians
    med = r["derived"]["pooled_medians_across_offsets"]
    for p in ("hybrid", "phase_free", "adaptive"):
        pooled_per_offset = pol[p].sum(axis=0)
        _check(f"robustness median across offsets of pooled {p} unobserved hours", float(np.median(pooled_per_offset)), med[p]["pooled_unobserved"], checks)
        _check(f"robustness median across offsets of pooled {p} episodes observed", float(np.median(pol[p + "_ep"].sum(axis=0))), med[p]["pooled_episodes"], checks)
    per_offset_0 = r["per_offset"][0]
    if per_offset_0["offset_seconds"] != 0:
        raise SystemExit("robustness per_offset[0] is not offset 0")
    _check("robustness offset-0 pooled hybrid unobserved hours equals frozen Study 3 hybrid", float(pol["hybrid"][:, 0].sum()), 60.25, checks)
    _check("robustness offset-0 pooled adaptive unobserved hours equals frozen Study 3 adaptive", float(pol["adaptive"][:, 0].sum()), 109.5, checks)
    _check("robustness offset-0 pooled phase-free unobserved hours", float(pol["phase_free"][:, 0].sum()), per_offset_0["phase_free"]["unobserved"], checks)
    hmpf = pol["hybrid"].sum(axis=0) - pol["phase_free"].sum(axis=0)
    _check("robustness median over offsets of pooled hybrid minus phase-free unobserved hours", float(np.median(hmpf)), r["derived"]["hybrid_minus_phase_free_pooled_unobserved_per_offset"]["median"], checks)

    out = {
        "unit": "county-event; each county-event's difference is first averaged over the 24 mission-start offsets (offsets treated as strata), then county-events are resampled",
        "offsets": k,
        "why_no_single_median_offset": "the reported 56.00 / 52.63 / 103.88 h are medians across 24 offsets of each policy's pooled value; with an even number of offsets the median is the mean of two middle draws, and the middle draws differ across policies, so no single offset realizes all three medians",
        "comparisons": {},
    }
    for label, a, b in (("phase_free_minus_hybrid", "phase_free", "hybrid"), ("phase_free_minus_adaptive", "phase_free", "adaptive")):
        d_avg = (pol[a] - pol[b]).mean(axis=1)
        d_ep_avg = (pol[a + "_ep"] - pol[b + "_ep"]).mean(axis=1)
        per_offset = []
        for j in range(k):
            bj = _bootstrap(pol[a][:, j] - pol[b][:, j], idx)
            per_offset.append({"offset_seconds": offsets[j], "pooled_point": bj["pooled_difference"]["point"], "ci95": bj["pooled_difference"]["ci95"], "includes_zero": bj["pooled_difference"]["includes_zero"]})
        d0 = _bootstrap(pol[a][:, 0] - pol[b][:, 0], idx)
        out["comparisons"][label] = {
            "a": a,
            "b": b,
            "offset_averaged": {
                "pooled_a_unobserved_hours_mean_over_offsets": float(pol[a].sum(axis=0).mean()),
                "pooled_b_unobserved_hours_mean_over_offsets": float(pol[b].sum(axis=0).mean()),
                "unobserved_hours": _bootstrap(d_avg, idx),
                "episodes_observed": _bootstrap(d_ep_avg, idx),
            },
            "offset_0_draw_identical_to_frozen_study_run": {
                "pooled_a_unobserved_hours": float(pol[a][:, 0].sum()),
                "pooled_b_unobserved_hours": float(pol[b][:, 0].sum()),
                "unobserved_hours": d0,
            },
            "per_offset_pooled_unobserved_hours": per_offset,
            "per_offset_ci_lower_range": [min(p["ci95"][0] for p in per_offset), max(p["ci95"][0] for p in per_offset)],
            "per_offset_ci_upper_range": [min(p["ci95"][1] for p in per_offset), max(p["ci95"][1] for p in per_offset)],
            "offsets_whose_interval_includes_zero": sum(p["includes_zero"] for p in per_offset),
        }
    return out


def _fmt(x: float) -> str:
    """Two decimals; three only for exact 15-minute grid values that need them (82.625), never a negative zero."""
    x = float(x)
    if abs(x) < 5e-3:
        return "0.00"
    on_grid = abs(x * 8 - round(x * 8)) < 1e-9
    if on_grid and abs(x * 100 - round(x * 100)) > 1e-9:
        return f"{x:.3f}"
    return f"{x:.2f}"


def _row(name: str, b: dict, unit: str) -> str:
    pd, md = b["pooled_difference"], b["mean_paired_difference"]
    inc = "yes" if pd["includes_zero"] else "no"
    return f"| {name} | {unit} | {_fmt(pd['point'])} | [{_fmt(pd['ci95'][0])}, {_fmt(pd['ci95'][1])}] | {inc} | {_fmt(md['point'])} | [{_fmt(md['ci95'][0])}, {_fmt(md['ci95'][1])}] | {b['county_events_negative']} / {b['county_events_zero']} / {b['county_events_positive']} |"


HEADER = "| Comparison (A minus B) | Quantity | Pooled difference | 95% percentile CI | Includes zero | Mean per county-event | 95% CI (mean) | County-events A<B / A=B / A>B |\n|---|---|---|---|---|---|---|---|"


def main() -> int:
    checks = []
    rng = np.random.default_rng(SEED)

    # Study 3 (v4 additive), n = 24
    s3 = _load_county_rows(STUDY3_DIR)
    order3 = sorted(s3)
    if len(order3) != 24:
        raise SystemExit(f"Study 3: expected 24 episode-evaluation county-events, found {len(order3)}")
    _cross_check(s3, os.path.join(STUDY3_DIR, "aggregate.json"), "study3_v4_additive", checks)
    _check("Study 3 pooled hybrid unobserved hours", sum(s3[s]["hybrid"]["unobserved"] for s in order3), 60.25, checks)
    _check("Study 3 pooled adaptive unobserved hours", sum(s3[s]["adaptive"]["unobserved"] for s in order3), 109.5, checks)
    _check("Study 3 pooled matched-budget offset median unobserved hours", sum(s3[s]["matched_median"]["unobserved"] for s in order3), 82.625, checks)
    _check("Study 3 pooled uniform 3 h clock offset median unobserved hours", sum(s3[s]["fixed_3h_median"]["unobserved"] for s in order3), 60.625, checks)
    _check("Study 3 pooled hybrid episodes observed", sum(s3[s]["hybrid"]["episodes"] for s in order3), 33, checks)
    _check("Study 3 pooled adaptive episodes observed", sum(s3[s]["adaptive"]["episodes"] for s in order3), 32, checks)
    _check("Study 3 pooled matched-budget offset median episodes observed", sum(s3[s]["matched_median"]["episodes"] for s in order3), 34, checks)
    _check("Study 3 pooled uniform 3 h clock offset median episodes observed", sum(s3[s]["fixed_3h_median"]["episodes"] for s in order3), 34, checks)
    _check("Study 3 reference episodes", sum(s3[s]["episodes_total"] for s in order3), 46, checks)
    idx3 = rng.integers(0, 24, size=(DRAWS, 24))
    study3 = {
        "n_county_events": 24,
        "source": [_rel(os.path.join(STUDY3_DIR, "counties", "*", "result.json")), _rel(os.path.join(STUDY3_DIR, "aggregate.json")), _rel(POST_HOC)],
        "county_event_order": order3,
        "comparisons": {
            "hybrid_minus_matched_median": _compare(s3, order3, "hybrid", "matched_median", idx3, "additive hybrid minus budget-matched offset median", "additive hybrid", "budget-matched fixed clock, offset median"),
            "hybrid_minus_fixed_3h_median": _compare(s3, order3, "hybrid", "fixed_3h_median", idx3, "additive hybrid minus uniform 3 h clock offset median", "additive hybrid", "uniform 3 h clock, offset median"),
            "hybrid_minus_adaptive": _compare(s3, order3, "hybrid", "adaptive", idx3, "additive hybrid minus phase adaptation", "additive hybrid", "phase adaptation"),
        },
    }

    # Study 2 (v3 rev2), n = 9
    s2 = _load_county_rows(STUDY2_DIR)
    order2 = sorted(s2)
    if len(order2) != 9:
        raise SystemExit(f"Study 2: expected 9 episode-evaluation county-events, found {len(order2)}")
    _cross_check(s2, os.path.join(STUDY2_DIR, "aggregate.json"), "study2_v3_rev2", checks)
    _check("Study 2 pooled hybrid unobserved hours", sum(s2[s]["hybrid"]["unobserved"] for s in order2), 9.0, checks)
    _check("Study 2 pooled adaptive unobserved hours", sum(s2[s]["adaptive"]["unobserved"] for s in order2), 23.5, checks)
    _check("Study 2 pooled matched-budget offset median unobserved hours", sum(s2[s]["matched_median"]["unobserved"] for s in order2), 21.875, checks)
    _check("Study 2 pooled uniform 3 h clock offset median unobserved hours", sum(s2[s]["fixed_3h_median"]["unobserved"] for s in order2), 14.25, checks)
    _check("Study 2 pooled hybrid episodes observed", sum(s2[s]["hybrid"]["episodes"] for s in order2), 9, checks)
    _check("Study 2 pooled adaptive episodes observed", sum(s2[s]["adaptive"]["episodes"] for s in order2), 8, checks)
    _check("Study 2 reference episodes", sum(s2[s]["episodes_total"] for s in order2), 10, checks)
    idx2 = rng.integers(0, 9, size=(DRAWS, 9))
    study2 = {
        "n_county_events": 9,
        "descriptive_only": "n = 9 county-events; percentile intervals from nine units are reported as descriptive bands, not as a test",
        "source": [_rel(os.path.join(STUDY2_DIR, "counties", "*", "result.json")), _rel(os.path.join(STUDY2_DIR, "aggregate.json")), _rel(POST_HOC)],
        "county_event_order": order2,
        "comparisons": {
            "hybrid_minus_adaptive": _compare(s2, order2, "hybrid", "adaptive", idx2, "hybrid minus phase adaptation", "re-anchoring hybrid", "phase adaptation"),
            "hybrid_minus_matched_median": _compare(s2, order2, "hybrid", "matched_median", idx2, "hybrid minus budget-matched offset median", "re-anchoring hybrid", "budget-matched fixed clock, offset median"),
            "hybrid_minus_fixed_3h_median": _compare(s2, order2, "hybrid", "fixed_3h_median", idx2, "hybrid minus uniform 3 h clock offset median", "re-anchoring hybrid", "uniform 3 h clock, offset median"),
        },
    }

    # Robustness checks (phase-free control), n = 24, offsets as strata; reuse the Study 3 index matrix
    robust = _robustness(idx3, checks)
    robust["source"] = [_rel(ROBUST)]

    report = {
        "schema": "sentinel-posthoc-bootstrap/1",
        "status": "post hoc; nothing prespecified; descriptive sampling-uncertainty bands for differences already reported in the frozen studies",
        "method": {
            "resampling_unit": "county-event",
            "draws": DRAWS,
            "seed": SEED,
            "generator": "numpy.random.default_rng(seed).integers; one index matrix per study, shared by all comparisons within that study; the robustness comparisons reuse the Study 3 matrix",
            "interval": "percentile 95% (2.5th and 97.5th percentiles of the resampled statistic, numpy.percentile with linear interpolation)",
            "statistics": ["pooled difference: sum over resampled county-events of A minus B", "mean paired difference: pooled difference divided by n (interval scales identically)", "episodes observed: same construction on per-county-event episode counts"],
            "caveats": ["county-events are treated as exchangeable; the four Study 3 county-events with recurring episodes (Marion, Sarasota, Marshall, Seminole) dominate the pooled differences, so intervals are wide and asymmetric", "matched-budget and uniform-clock comparators enter as per-county-event offset medians, exactly as in the frozen reports; the offset spread within a county-event is not resampled", "Study 2 has nine units and its intervals are descriptive"],
        },
        "checks": checks,
        "study3_v4_additive": study3,
        "study2_v3_rev2": study2,
        "robustness_phase_free": robust,
    }
    os.makedirs(OUT, exist_ok=True)
    json.dump(report, open(os.path.join(OUT, "summary.json"), "w"), indent=1, sort_keys=True)

    L = []
    L.append("# Post-hoc paired bootstrap intervals for the pooled policy differences")
    L.append("")
    L.append(f"Everything in this file is post hoc. Nothing was prespecified. The intervals add a sampling-uncertainty band to differences that the frozen Study 2 and Study 3 reports already state as pooled sums; they do not change any frozen verdict. Method: county-events are the resampling unit; {DRAWS:,} resamples with replacement, seed {SEED}, percentile 95% intervals. For each comparison A minus B the pooled difference is the sum over the resampled county-events of A's unobserved flood-stage hours minus B's, with the same construction for episodes observed; the mean paired difference is the pooled difference divided by n, so its interval is the pooled interval rescaled. A negative value favors A (less unobserved time; for episodes, a positive value favors A). One shared index matrix per study makes every comparison within a study use the same resamples. Generated by `eval/posthoc_bootstrap_intervals.py`, which reads `{_rel(STUDY3_DIR)}`, `{_rel(STUDY2_DIR)}`, `{_rel(POST_HOC)}` and `{_rel(ROBUST)}` and writes only into `{_rel(OUT)}`.")
    L.append("")
    L.append("## Check values reproduced from the per-county rows before bootstrapping")
    L.append("")
    L.append("| Check | Reproduced | Expected |")
    L.append("|---|---|---|")
    for c in checks:
        if "expected" in c:
            L.append(f"| {c['check']} | {c['reproduced']:g} | {c['expected']:g} |")
        else:
            L.append(f"| {c['check']} | agree | agree |")
    L.append("")
    L.append("## Study 3 (additive hybrid, 24 episode-evaluation county-events), post hoc")
    L.append("")
    L.append(HEADER)
    for key, lab in (("hybrid_minus_matched_median", "Hybrid minus budget-matched offset median"), ("hybrid_minus_fixed_3h_median", "Hybrid minus uniform 3 h clock offset median"), ("hybrid_minus_adaptive", "Hybrid minus phase adaptation")):
        c = study3["comparisons"][key]
        L.append(_row(f"{lab} (pooled {_fmt(c['pooled_a_unobserved_hours'])} vs {_fmt(c['pooled_b_unobserved_hours'])} h)", c["unobserved_hours"], "unobserved hours"))
        L.append(_row(f"{lab} (pooled {c['pooled_a_episodes_observed']:g} vs {c['pooled_b_episodes_observed']:g} episodes)", c["episodes_observed"], "episodes observed"))
    L.append("")
    L.append(_prose_study3(study3["comparisons"], s3, order3))
    L.append("")
    L.append("## Study 2 (re-anchoring hybrid, 9 episode-evaluation county-events), post hoc and descriptive")
    L.append("")
    L.append("With nine county-events the percentile bootstrap has few distinct resamples and the intervals below are descriptive bands only. The uniform 3 h clock row is additional to the two comparisons the study reports.")
    L.append("")
    L.append(HEADER)
    for key, lab in (("hybrid_minus_adaptive", "Hybrid minus phase adaptation"), ("hybrid_minus_matched_median", "Hybrid minus budget-matched offset median"), ("hybrid_minus_fixed_3h_median", "Hybrid minus uniform 3 h clock offset median")):
        c = study2["comparisons"][key]
        L.append(_row(f"{lab} (pooled {_fmt(c['pooled_a_unobserved_hours'])} vs {_fmt(c['pooled_b_unobserved_hours'])} h)", c["unobserved_hours"], "unobserved hours"))
        L.append(_row(f"{lab} (pooled {c['pooled_a_episodes_observed']:g} vs {c['pooled_b_episodes_observed']:g} episodes)", c["episodes_observed"], "episodes observed"))
    L.append("")
    L.append(_prose_study2(study2["comparisons"], s2, order2))
    L.append("")
    L.append("## Phase-free control (Study 3 county-events at 24 mission-start offsets), post hoc")
    L.append("")
    L.append(f"What was done: {robust['why_no_single_median_offset']}. Instead of picking one offset, each county-event's difference (phase-free minus hybrid, and phase-free minus phase adaptation) is averaged over all 24 offsets, offsets acting as strata, and county-events are then resampled with the Study 3 index matrix. The offset-0 draw, which is the frozen Study 3 run itself, and the range of the 24 per-offset intervals are given beside the offset-averaged result. Per-offset pooled values: hybrid {robust['comparisons']['phase_free_minus_hybrid']['offset_averaged']['pooled_b_unobserved_hours_mean_over_offsets']:.2f} h, phase-free {robust['comparisons']['phase_free_minus_hybrid']['offset_averaged']['pooled_a_unobserved_hours_mean_over_offsets']:.2f} h and phase adaptation {robust['comparisons']['phase_free_minus_adaptive']['offset_averaged']['pooled_b_unobserved_hours_mean_over_offsets']:.2f} h when averaged over offsets (the reported medians across offsets are 52.63, 56.00 and 103.88 h).")
    L.append("")
    L.append(HEADER)
    for key, lab in (("phase_free_minus_hybrid", "Phase-free control minus additive hybrid"), ("phase_free_minus_adaptive", "Phase-free control minus phase adaptation")):
        c = robust["comparisons"][key]
        oa = c["offset_averaged"]
        L.append(_row(f"{lab}, offset-averaged", oa["unobserved_hours"], "unobserved hours"))
        L.append(_row(f"{lab}, offset-averaged", oa["episodes_observed"], "episodes observed"))
        o0 = c["offset_0_draw_identical_to_frozen_study_run"]
        L.append(_row(f"{lab}, offset 0 only (pooled {_fmt(o0['pooled_a_unobserved_hours'])} vs {_fmt(o0['pooled_b_unobserved_hours'])} h)", o0["unobserved_hours"], "unobserved hours"))
    L.append("")
    L.append(_prose_robust(robust["comparisons"]))
    L.append("")
    L.append("## Reading the intervals")
    L.append("")
    top = sorted(order3, key=lambda s: -s3[s]["hybrid"]["unobserved"])[:3]
    top_share = sum(s3[s]["hybrid"]["unobserved"] for s in top) / sum(s3[s]["hybrid"]["unobserved"] for s in order3)
    L.append(f"The intervals are wide relative to the point estimates because the pooled hours are concentrated: in Study 3 the three county-events with the most hybrid unobserved time ({', '.join(s3[s]['county'] for s in top)}) hold {top_share:.0%} of the hybrid's 60.25 h, so a resample that draws them several times or not at all moves the sum by tens of hours. The bootstrap treats county-events as exchangeable draws from a population of comparable county-events; the selection was mechanical but not random, so the bands describe sampling variability among these cases rather than a population inference. Comparator clocks enter as per-county-event offset medians, exactly as in the frozen reports, so the within-county-event spread over clock offsets is not resampled. None of this alters the frozen verdicts: Study 2 failed P2 and Study 3 failed Q1 under their prespecified rules regardless of these bands.")
    open(os.path.join(OUT, "summary.md"), "w").write("\n".join(L) + "\n")
    print(f"wrote {os.path.join(OUT, 'summary.json')} and summary.md")
    return 0


def _ci_text(b: dict) -> str:
    pd = b["pooled_difference"]
    z = "includes zero" if pd["includes_zero"] else "excludes zero"
    return f"{_fmt(pd['point'])} h, 95% CI [{_fmt(pd['ci95'][0])}, {_fmt(pd['ci95'][1])}] ({z})"


def _ci_text_ep(b: dict) -> str:
    pd = b["pooled_difference"]
    z = "includes zero" if pd["includes_zero"] else "excludes zero"
    return f"{_fmt(pd['point'])} episodes, 95% CI [{_fmt(pd['ci95'][0])}, {_fmt(pd['ci95'][1])}] ({z})"


def _names(rows: dict, order: list, pred) -> list:
    return [rows[s]["county"] for s in order if pred(rows[s])]


def _prose_study3(c: dict, rows: dict, order: list) -> str:
    m, f, a = c["hybrid_minus_matched_median"], c["hybrid_minus_fixed_3h_median"], c["hybrid_minus_adaptive"]
    losses = _names(rows, order, lambda r: r["hybrid"]["unobserved"] > r["matched_median"]["unobserved"])
    multi = [s for s in order if rows[s]["episodes_total"] > 1]
    share_h = sum(rows[s]["hybrid"]["unobserved"] for s in multi) / sum(rows[s]["hybrid"]["unobserved"] for s in order)
    share_m = sum(rows[s]["matched_median"]["unobserved"] for s in multi) / sum(rows[s]["matched_median"]["unobserved"] for s in order)
    return (
        f"Against the budget-matched offset median the pooled difference is {_ci_text(m['unobserved_hours'])}; the episode difference is {_ci_text_ep(m['episodes_observed'])}. "
        f"Against the uniform 3 h clock offset median the pooled difference is {_ci_text(f['unobserved_hours'])}, and the episode difference is {_ci_text_ep(f['episodes_observed'])}. "
        f"Against phase adaptation the pooled difference is {_ci_text(a['unobserved_hours'])}, with an episode difference of {_ci_text_ep(a['episodes_observed'])}. "
        f"The frozen Q2 sign test ({m['unobserved_hours']['county_events_negative']} of 24 county-events below the matched median) and the pooled-hours interval measure different things: the sign test counts county-events, the interval weighs hours. The {len(multi)} county-events with more than one reference episode carry {share_h:.0%} of the hybrid's pooled hours and {share_m:.0%} of the matched median's, and the hybrid's {len(losses)} losses to the matched median are {', '.join(losses)}, so resamples that draw the recurring-episode county-events several times pull the pooled difference toward zero."
    )


def _prose_study2(c: dict, rows: dict, order: list) -> str:
    a, m, f = c["hybrid_minus_adaptive"], c["hybrid_minus_matched_median"], c["hybrid_minus_fixed_3h_median"]
    lost = _names(rows, order, lambda r: r["hybrid"]["unobserved"] > r["adaptive"]["unobserved"])
    return (
        f"Descriptively, the hybrid minus phase adaptation pooled difference is {_ci_text(a['unobserved_hours'])}, with episodes {_ci_text_ep(a['episodes_observed'])}; minus the budget-matched offset median it is {_ci_text(m['unobserved_hours'])}, episodes {_ci_text_ep(m['episodes_observed'])}; minus the uniform 3 h clock offset median it is {_ci_text(f['unobserved_hours'])}, episodes {_ci_text_ep(f['episodes_observed'])}. "
        f"These bands come from nine units; the county-event{'s' if len(lost) != 1 else ''} where the re-anchoring hybrid had more unobserved time than phase adaptation ({', '.join(lost)}) set{'s' if len(lost) == 1 else ''} the upper end of the first interval according to how often {'it is' if len(lost) == 1 else 'they are'} drawn."
    )


def _prose_robust(c: dict) -> str:
    h, a = c["phase_free_minus_hybrid"], c["phase_free_minus_adaptive"]
    ho, ao = h["offset_averaged"], a["offset_averaged"]
    return (
        f"Offset-averaged, phase-free minus hybrid is {_ci_text(ho['unobserved_hours'])} in pooled unobserved hours and {_ci_text_ep(ho['episodes_observed'])}; phase-free minus phase adaptation is {_ci_text(ao['unobserved_hours'])} and {_ci_text_ep(ao['episodes_observed'])}. "
        f"Across the 24 individual offsets, the phase-free minus hybrid interval includes zero at {h['offsets_whose_interval_includes_zero']} of 24 offsets (lower bounds range {_fmt(h['per_offset_ci_lower_range'][0])} to {_fmt(h['per_offset_ci_lower_range'][1])} h, upper bounds {_fmt(h['per_offset_ci_upper_range'][0])} to {_fmt(h['per_offset_ci_upper_range'][1])} h); the phase-free minus phase adaptation interval includes zero at {a['offsets_whose_interval_includes_zero']} of 24 offsets (lower bounds {_fmt(a['per_offset_ci_lower_range'][0])} to {_fmt(a['per_offset_ci_lower_range'][1])} h, upper bounds {_fmt(a['per_offset_ci_upper_range'][0])} to {_fmt(a['per_offset_ci_upper_range'][1])} h). "
        f"At offset 0, the frozen Study 3 start, the phase-free control and the hybrid have the same pooled unobserved hours ({_fmt(h['offset_0_draw_identical_to_frozen_study_run']['pooled_a_unobserved_hours'])} h each): the pooled difference is zero with {h['offset_0_draw_identical_to_frozen_study_run']['unobserved_hours']['county_events_negative']} county-event below and {h['offset_0_draw_identical_to_frozen_study_run']['unobserved_hours']['county_events_positive']} above, the rest tied."
    )


if __name__ == "__main__":
    raise SystemExit(main())
