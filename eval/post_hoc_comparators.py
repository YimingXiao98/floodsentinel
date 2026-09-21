"""Post-hoc, descriptive comparators read from the frozen Study 2 (v3 rev2) and Study 3 (v4) artifacts.

Nothing here was prespecified. The frozen per-county result files already contain fixed 1/2/3/6/12 h clocks at
every 15-minute offset (the Study 1 comparator set), which the held-out reports never summarized, plus the
onset-anticipation flags the trigger accounting records per episode. This script only reads those files and
writes a summary; the frozen directories are not modified.
"""
from __future__ import annotations
import glob, json, os, statistics as st
from math import comb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "eval", "results", "post_hoc_comparators")
STUDIES = {
    "study2_v3_rev2": os.path.join(ROOT, "eval", "results", "study_v3_hybrid_rev2", "counties"),
    "study3_v4_additive": os.path.join(ROOT, "eval", "results", "study_v4_additive", "counties"),
}
FIXED = (1.0, 2.0, 3.0, 6.0, 12.0)


def sign_test_p(wins: int, losses: int) -> float:
    n = wins + losses
    return sum(comb(n, k) for k in range(wins, n + 1)) / 2 ** n


def load(base):
    for d in sorted(glob.glob(base + "/*")):
        f = os.path.join(d, "result.json")
        if not os.path.exists(f):
            continue
        j = json.load(open(f))
        if j["schedule_rows"][0].get("analysis_role") != "episode_evaluation":
            continue
        yield os.path.basename(d), j


def one(name, j):
    sr = j["schedule_rows"]
    hyb = next(r for r in sr if r["policy"] == "hybrid")
    ada = next(r for r in sr if r["policy"] == "adaptive")
    fixed = {}
    for fi in FIXED:
        rs = [r for r in sr if r.get("fixed_interval_hours") == fi and r.get("schedule_kind") != "matched_budget"]
        un = [r["unobserved_flood_stage_duration_hours"] for r in rs]
        fixed[f"{fi:g}h"] = {
            "offsets": len(rs),
            "unobserved_median": st.median(un), "unobserved_min": min(un), "unobserved_max": max(un),
            "episodes_observed_median": st.median(r["episodes_observed_by_schedule"] for r in rs),
            "acquisitions_median": st.median(r["acquisitions"] for r in rs),
        }
    ta = j["trigger_accounting"]["hybrid"]
    return {
        "county_event": name,
        "episodes_total": hyb["episodes_observed_by_schedule"] + hyb["episodes_not_observed_by_schedule"],
        "episodes_with_onset_anticipating_warning": ta.get("episodes_with_onset_anticipating_trigger"),
        "hybrid": {k: hyb[k] for k in ("acquisitions", "episodes_observed_by_schedule", "unobserved_flood_stage_duration_hours")},
        "adaptive": {k: ada[k] for k in ("acquisitions", "episodes_observed_by_schedule", "unobserved_flood_stage_duration_hours")},
        "fixed": fixed,
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {"note": __doc__.strip(), "studies": {}}
    lines = ["# Post-hoc comparators from the frozen artifacts", "", "Descriptive only; nothing here was prespecified. Source: `eval/post_hoc_comparators.py` over the per-county `result.json` files of the frozen Study 2 and Study 3 directories.", ""]
    for study, base in STUDIES.items():
        rows = [one(n, j) for n, j in load(base)]
        pooled = {"county_events": len(rows), "episodes": sum(r["episodes_total"] for r in rows),
                  "hybrid": {k: sum(r["hybrid"][k] for r in rows) for k in rows[0]["hybrid"]},
                  "adaptive": {k: sum(r["adaptive"][k] for r in rows) for k in rows[0]["adaptive"]},
                  "fixed": {}, "hybrid_vs_fixed_offset_median": {}}
        for fk in rows[0]["fixed"]:
            pooled["fixed"][fk] = {m: sum(r["fixed"][fk][m] for r in rows) for m in ("unobserved_median", "unobserved_min", "unobserved_max", "episodes_observed_median", "acquisitions_median")}
            w = sum(r["hybrid"]["unobserved_flood_stage_duration_hours"] < r["fixed"][fk]["unobserved_median"] for r in rows)
            l = sum(r["hybrid"]["unobserved_flood_stage_duration_hours"] > r["fixed"][fk]["unobserved_median"] for r in rows)
            pooled["hybrid_vs_fixed_offset_median"][fk] = {"below": w, "above": l, "ties": len(rows) - w - l, "one_sided_sign_p": sign_test_p(w, l)}
        # anticipation strata (county-event level)
        strata = {}
        for r in rows:
            n, a = r["episodes_total"], r["episodes_with_onset_anticipating_warning"]
            key = "none_anticipated" if a == 0 else ("all_anticipated" if a == n else "some_anticipated")
            s = strata.setdefault(key, {"county_events": 0, "episodes": 0, "hybrid_unobserved": 0.0, "adaptive_unobserved": 0.0, "fixed_3h_median_unobserved": 0.0, "hybrid_observed": 0, "adaptive_observed": 0, "fixed_3h_median_observed": 0.0, "members": []})
            s["county_events"] += 1; s["episodes"] += n
            s["hybrid_unobserved"] += r["hybrid"]["unobserved_flood_stage_duration_hours"]
            s["adaptive_unobserved"] += r["adaptive"]["unobserved_flood_stage_duration_hours"]
            s["fixed_3h_median_unobserved"] += r["fixed"]["3h"]["unobserved_median"]
            s["hybrid_observed"] += r["hybrid"]["episodes_observed_by_schedule"]
            s["adaptive_observed"] += r["adaptive"]["episodes_observed_by_schedule"]
            s["fixed_3h_median_observed"] += r["fixed"]["3h"]["episodes_observed_median"]
            s["members"].append(r["county_event"])
        report["studies"][study] = {"pooled": pooled, "anticipation_strata": strata, "county_events": rows}
        lines += [f"## {study}: {pooled['county_events']} county-events, {pooled['episodes']} episodes", "",
                  "| policy | pooled unobserved h (offset median; min to max) | episodes observed | acquisitions |", "|---|---|---|---|",
                  f"| hybrid | {pooled['hybrid']['unobserved_flood_stage_duration_hours']:.2f} | {pooled['hybrid']['episodes_observed_by_schedule']} | {pooled['hybrid']['acquisitions']} |",
                  f"| adaptive | {pooled['adaptive']['unobserved_flood_stage_duration_hours']:.2f} | {pooled['adaptive']['episodes_observed_by_schedule']} | {pooled['adaptive']['acquisitions']} |"]
        for fk, v in pooled["fixed"].items():
            c = pooled["hybrid_vs_fixed_offset_median"][fk]
            lines.append(f"| fixed {fk} | {v['unobserved_median']:.2f} ({v['unobserved_min']:.2f} to {v['unobserved_max']:.2f}) | {v['episodes_observed_median']:.1f} | {v['acquisitions_median']:.0f} | hybrid below median in {c['below']}, above in {c['above']}, ties {c['ties']}; one-sided sign p = {c['one_sided_sign_p']:.3f}")
        lines += ["", "Anticipation strata (county-event level; a county-event is 'all' when every episode had an onset-anticipating warning):", "",
                  "| stratum | county-events | episodes | hybrid unobs h | fixed 3 h median unobs h | adaptive unobs h | episodes observed hybrid / fixed 3 h / adaptive |", "|---|---|---|---|---|---|---|"]
        for k in ("all_anticipated", "some_anticipated", "none_anticipated"):
            if k in strata:
                s = strata[k]
                lines.append(f"| {k} | {s['county_events']} | {s['episodes']} | {s['hybrid_unobserved']:.2f} | {s['fixed_3h_median_unobserved']:.2f} | {s['adaptive_unobserved']:.2f} | {s['hybrid_observed']} / {s['fixed_3h_median_observed']:.1f} / {s['adaptive_observed']} |")
        lines.append("")
    json.dump(report, open(os.path.join(OUT, "summary.json"), "w"), indent=1, sort_keys=True)
    open(os.path.join(OUT, "summary.md"), "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
