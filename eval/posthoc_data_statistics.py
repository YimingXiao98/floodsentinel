"""Data-statistics table for the manuscript, every number read from a frozen artifact and cited by path and key.

Reads only frozen artifacts (corpus manifests, VTEC trigger corpora, study aggregates and preflight QC, the belief/news null audit, the v5 extension results, the deployment summary) and writes eval/results/posthoc_bootstrap/data_statistics.md. Counts that the manuscript already states are asserted here, so the script fails closed if an artifact no longer reproduces them. Where a quantity is not present in any artifact the row says so instead of estimating.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "eval", "results", "posthoc_bootstrap", "data_statistics.md")

STUDIES = {
    "Study 1": {
        "manifest": "eval/corpus/study/manifest.json",
        "aggregate": "eval/results/study_v2/aggregate.json",
        "preflight": "eval/results/study_v2/preflight_qc.json",
        "tables": None,
        "expected": {"ep": 4, "cov": 1, "episodes": 6},
    },
    "Study 2": {
        "manifest": "eval/corpus/study_v3/manifest.json",
        "aggregate": "eval/results/study_v3_hybrid_rev2/aggregate.json",
        "preflight": "eval/results/study_v3_hybrid_rev2/preflight_qc.json",
        "tables": "eval/results/study_v3_hybrid_rev2/tables.json",
        "expected": {"ep": 9, "cov": 2, "episodes": 10, "consultations": 9024},
    },
    "Study 3": {
        "manifest": "eval/corpus/study_v4/manifest.json",
        "aggregate": "eval/results/study_v4_additive/aggregate.json",
        "preflight": "eval/results/study_v4_additive/preflight_qc.json",
        "tables": "eval/results/study_v4_additive/tables.json",
        "expected": {"ep": 24, "cov": 4, "episodes": 46, "consultations": 27195},
    },
}
EP, COV = "episode_evaluation", "coverage_only"


def J(rel: str):
    return json.load(open(os.path.join(ROOT, rel)))


def expect(label: str, got, want) -> None:
    if got != want:
        raise SystemExit(f"{label}: artifact gives {got}, manuscript states {want}")


def n(x) -> str:
    return f"{x:,}" if isinstance(x, int) else f"{x:g}"


def study_rows(study: str, cfg: dict) -> list:
    rows = []
    m = J(cfg["manifest"])
    arts = m["artifacts"]
    ep = [a for a in arts if a["analysis_role"] == EP]
    cov = [a for a in arts if a["analysis_role"] == COV]
    if len(ep) + len(cov) != len(arts):
        raise SystemExit(f"{study}: unexpected analysis_role values")
    expect(f"{study} episode-evaluation county-events", len(ep), cfg["expected"]["ep"])
    expect(f"{study} coverage-only county-events", len(cov), cfg["expected"]["cov"])
    agg = J(cfg["aggregate"])
    pre = J(cfg["preflight"])
    src_m = cfg["manifest"]
    rows.append((study, "County-events, episode evaluation", n(len(ep)), f"`{src_m}` artifacts[].analysis_role == episode_evaluation; `{cfg['aggregate']}` " + ("n_episode_evaluation_counties" if study == "Study 1" else "n_episode_evaluation_counties" if study == "Study 2" else "n_episode_evaluation_county_events")))
    rows.append((study, "County-events, coverage only", n(len(cov)), f"`{src_m}` artifacts[].analysis_role == coverage_only" + ("; `eval/results/study_v2/aggregate.json` n_coverage_only_counties" if study == "Study 1" else f"; `{cfg['aggregate']}` n_counties minus the episode-evaluation count")))
    ev_ep = sorted(set(a["event"] for a in ep))
    ev_all = sorted(set(a["event"] for a in arts))
    rows.append((study, "Distinct events (episode-evaluation county-events)", n(len(ev_ep)) + (f" ({len(ev_all)} including coverage-only)" if len(ev_all) != len(ev_ep) else " (same including coverage-only)"), f"`{src_m}` distinct artifacts[].event"))
    st_ep = sorted(set(a["state"] for a in ep))
    st_all = sorted(set(a["state"] for a in arts))
    pr = "PR" in st_ep
    states_txt = f"{len(st_ep) - (1 if pr else 0)} states" + (" + PR" if pr else "") + f" ({', '.join(st_ep)})"
    if st_all != st_ep:
        states_txt += f"; including coverage-only: {', '.join(st_all)}"
    else:
        states_txt += "; coverage-only county-events add no state"
    rows.append((study, "States (+PR), episode-evaluation county-events", states_txt, f"`{src_m}` distinct artifacts[].state"))
    if study == "Study 1":
        episodes = agg["observed_flood_episodes"]
        src_ep = f"`{cfg['aggregate']}` observed_flood_episodes"
    else:
        episodes = sum(c["episodes_total"] for c in agg["criterion_inputs"])
        src_ep = f"`{cfg['aggregate']}` sum of criterion_inputs[].episodes_total"
    expect(f"{study} reference episodes", episodes, cfg["expected"]["episodes"])
    rows.append((study, "Reference episodes (episode-evaluation county-events)", n(episodes), src_ep))
    for label, key in (("Gauge sites selected", "sites_selected"), ("Sites with a compatible flood threshold", "sites_with_flood_stage"), ("Gauge observations", "observations")):
        a, b = sum(x[key] for x in ep), sum(x[key] for x in cov)
        extra = ""
        if key == "observations":
            raw = 0
            for x in arts:
                c = J(x["path"])
                raw += sum(len(s.get("observations", [])) for s in c["sites"])
            if raw != a + b:
                raise SystemExit(f"{study}: manifest observations {a + b} differ from corpus files {raw}")
            extra = "; equals the summed length of sites[].observations in the corpus files"
        rows.append((study, f"{label}, episode evaluation / coverage only", f"{n(a)} / {n(b)} (total {n(a + b)})", f"`{src_m}` artifacts[].{key} (also copied as corpus_manifest_entry.{key} in each county result.json){extra}"))
    ra = sum(c["reference_sites_after_exclusion"] for c in pre["counties"] if c["analysis_role"] == EP)
    rb = sum(c["reference_sites_after_exclusion"] for c in pre["counties"] if c["analysis_role"] == COV)
    rows.append((study, "Reference sites after symmetric quality exclusions, episode evaluation / coverage only", f"{n(ra)} / {n(rb)}", f"`{cfg['preflight']}` counties[].reference_sites_after_exclusion (the count the manuscript's case tables call reference sites; `eval/run_study.py` _preflight counts sites that keep a flood_stage after apply_threshold_compatibility and the recorded datum or threshold exclusions and that have at least one observation in the frozen corpus, so it is smaller than sites_with_flood_stage)"))
    hours = set()
    for a in arts:
        s = datetime.fromisoformat(a["analysis_start"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(a["analysis_end"].replace("Z", "+00:00"))
        hours.add((e - s).total_seconds() / 3600)
    elapsed = set(c["analysis_elapsed_hours"] for c in pre["counties"])
    if hours != {312.0} or elapsed != {312.0}:
        raise SystemExit(f"{study}: analysis windows are not all 312 h: {hours} {elapsed}")
    rows.append((study, "Analysis window length per county-event", "312 elapsed hours (13 days), half-open UTC interval", f"`{src_m}` analysis_window_convention and artifacts[].analysis_start/analysis_end; `{cfg['preflight']}` counties[].analysis_elapsed_hours"))
    if "trigger_corpus" in arts[0]:
        q = set(m["frozen_trigger_policy"]["qualifying"])
        counts = {}
        for role, sel in ((EP, ep), (COV, cov)):
            ne = nq = nraw = nnf = 0
            for a in sel:
                t = J(a["trigger_corpus"])
                if t["n_events"] != len(t["events"]):
                    raise SystemExit(f"{a['trigger_corpus']}: n_events disagrees with len(events)")
                ne += len(t["events"])
                nq += sum(1 for e in t["events"] if f"{e['phenomena']}.{e['significance']}" in q)
                nraw += sum(t["per_ugc_raw_event_counts"].values())
                nnf += t["anomalies"]["non_flood_phenomena"]
            counts[role] = (ne, nq, nraw, nnf)
        vt = "`" + os.path.dirname(cfg["manifest"]) + "/*_vtec_triggers.json` events[] (n_events); a row is one VTEC event identity (year, WFO, phenomena, significance, ETN) after `eval/vtec_trigger.py` merges the county UGC with the county's named forecast zones, drops non-flood phenomena, and keeps the earliest transmitted product's id and transmission time as available_at; per_ugc_raw_event_counts (" + f"{n(counts[EP][2])} / {n(counts[COV][2])}" + ") counts the archive's per-UGC listings before that merge, of which " + f"{n(counts[EP][3])} / {n(counts[COV][3])}" + " were non-flood phenomena"
        rows.append((study, "VTEC products in the trigger corpora (event rows), episode evaluation / coverage only", f"{n(counts[EP][0])} / {n(counts[COV][0])} (total {n(counts[EP][0] + counts[COV][0])})", vt))
        rows.append((study, "Qualifying products (phenomena.significance in FF.W, FA.W, FL.W), episode evaluation / coverage only", f"{n(counts[EP][1])} / {n(counts[COV][1])} (total {n(counts[EP][1] + counts[COV][1])})", f"same files, events[].phenomena and events[].significance against `{src_m}` frozen_trigger_policy.qualifying"))
        tb = J(cfg["tables"])
        hyb = [r for r in tb["trigger_accounting_rows"] if r["policy"] == "hybrid"]
        ce = sum(r["trigger_stream_consultations"] for r in hyb if r["analysis_role"] == EP)
        cc = sum(r["trigger_stream_consultations"] for r in hyb if r["analysis_role"] == COV)
        expect(f"{study} warning consultations", ce, cfg["expected"]["consultations"])
        rows.append((study, "Warning-stream consultations by the hybrid, episode evaluation / coverage only", f"{n(ce)} / {n(cc)} (total {n(ce + cc)})", f"`{cfg['tables']}` trigger_accounting_rows[policy == hybrid].trigger_stream_consultations (one consultation per 15-minute poll: 1,248 for every county-event whose mission did not fold, fewer only where schedule_rows[policy == hybrid].folded_at is set)"))
    else:
        rows.append((study, "VTEC products in the trigger corpora", "not applicable: Study 1 injected no warning products and its manifest has no trigger_corpus field", f"`{src_m}` artifacts[] (no trigger_corpus key)"))
        rows.append((study, "Warning-stream consultations", "not applicable (no trigger stream in Study 1)", f"`{src_m}`; `eval/results/study_v2/counties/*/result.json` nws_system_input is null"))
    return rows


def news_rows() -> list:
    rows = []
    b = J("eval/results/belief_news_null.json")
    usable = b["provenance"]["inputs"]["news_corpus"]["usable_title_articles"]
    expect("Harris usable GDELT records", usable, 128984)
    swept = b["design"]["news_source_coverage"]["slices_swept"]
    missing = b["design"]["news_source_coverage"]["slices_missing"]
    expect("Harris slices swept", swept, 1440)
    expect("Harris slices present", swept - missing, 1419)
    rows.append(("Harris news ablation", "Usable GDELT GKG records (title-bearing, deduplicated by URL)", n(usable), "`eval/results/belief_news_null.json` provenance.inputs.news_corpus.usable_title_articles"))
    rows.append(("Harris news ablation", "15-minute GKG slices present in the corpus window", f"{n(swept - missing)} of {n(swept)} (slice_coverage_fraction {b['design']['news_source_coverage']['slice_coverage_fraction']})", "`eval/results/belief_news_null.json` design.news_source_coverage.slices_swept and slices_missing; 1,419 is swept minus missing"))
    hr = b["headline_result"]
    rows.append(("Harris news ablation", "Detector-selected positive article events / unique URLs", f"{n(hr['detector_selected_positive_article_events'])} / {n(hr['detector_selected_positive_unique_urls'])}", "`eval/results/belief_news_null.json` headline_result.detector_selected_positive_article_events, detector_selected_positive_unique_urls"))
    return rows


def v5_rows() -> list:
    rows = []
    c = J("eval/results/study_v5_ontology/credibility_measurement.json")
    expect("v5 graded claims", c["n_claims_graded"], 7353)
    rows.append(("v5 extension", "Official gauge claims graded for credibility", n(c["n_claims_graded"]) + f" across {c['n_county_events']} county-events", "`eval/results/study_v5_ontology/credibility_measurement.json` n_claims_graded, n_county_events (by_tier.T0.n_claims gives the same 7,353, all T0)"))
    t = J("eval/results/study_v5_ontology/t2_gap_study.json")
    inputs = t["inputs"]
    if len(inputs) != 6:
        raise SystemExit("v5 t2_gap_study does not list six county-events")
    total = sum(i["n_news_articles"] for i in inputs)
    raw = 0
    for i in inputs:
        g = J(i["news_corpus"])
        raw += len(g["articles"])
        if len(g["articles"]) != i["n_news_articles"]:
            raise SystemExit(f"{i['news_corpus']}: articles {len(g['articles'])} vs n_news_articles {i['n_news_articles']}")
    per = "; ".join(f"{i['county']} {n(i['n_news_articles'])}" for i in inputs)
    rows.append(("v5 extension", "GDELT news records available in the six news-verification county-events", f"{n(total)} ({per})", "`eval/results/study_v5_ontology/t2_gap_study.json` inputs[].n_news_articles; equals len(articles) in each `eval/corpus/study_v5/*_gdelt.json`"))
    rows.append(("v5 extension", "Detector-admitted news trigger events (T2) in those six county-events", n(sum(i["n_t2_events"] for i in inputs)) + " (" + "; ".join(f"{i['county']} {i['n_t2_events']}" for i in inputs) + ")", "`eval/results/study_v5_ontology/t2_gap_study.json` inputs[].n_t2_events (n_events in each `eval/corpus/study_v5/*_t2_triggers.json`)"))
    po = t["pooled"]["official"]
    rows.append(("v5 extension", "Reference episodes in the six county-events, unwarned / warned", f"{n(po['unwarned']['episodes'])} / {n(po['warned']['episodes'])}", "`eval/results/study_v5_ontology/t2_gap_study.json` pooled.official.unwarned.episodes, pooled.official.warned.episodes"))
    return rows


def deployment_rows() -> list:
    d = J("eval/results/deployment_summary.json")
    polls = d["polls"]["heartbeat_polls"]
    opp = d["mission_activity"]["mission_opportunities"]
    due = d["mission_activity"]["due"]
    rep = d["delivery"]["report_deliveries"]
    parts = d["delivery"]["report_messages_sent"]
    for label, got, want in (("polls", polls, 2272), ("opportunities", opp, 4566), ("due", due, 689), ("logical reports", rep, 602), ("Slack parts", parts, 1054)):
        expect(f"deployment {label}", got, want)
    src = "`eval/results/deployment_summary.json` "
    return [
        ("Deployment", "Heartbeat polls", n(polls), src + "polls.heartbeat_polls"),
        ("Deployment", "Log window", f"{d['polls']['time_window']['duration_days']} days ({d['polls']['time_window']['start_at']} to {d['polls']['time_window']['end_at']})", src + "polls.time_window.duration_days, start_at, end_at"),
        ("Deployment", "Counties monitored", n(len(d["counties"])) + " (" + ", ".join(sorted(c["county"] for c in d["counties"])) + ")", src + "counties[].county"),
        ("Deployment", "County-mission opportunities", n(opp), src + "mission_activity.mission_opportunities"),
        ("Deployment", "Due cycles", n(due) + f" ({n(d['mission_activity']['cadence_skips'])} skipped by cadence)", src + "mission_activity.due, mission_activity.cadence_skips"),
        ("Deployment", "Logical reports delivered", n(rep), src + "delivery.report_deliveries"),
        ("Deployment", "Slack message parts sent", n(parts), src + "delivery.report_messages_sent"),
    ]


def main() -> int:
    rows = []
    for study, cfg in STUDIES.items():
        rows.extend(study_rows(study, cfg))
    rows.extend(news_rows())
    rows.extend(v5_rows())
    rows.extend(deployment_rows())
    L = [
        "# Data statistics for the manuscript",
        "",
        "Every value below is read from a frozen artifact by `eval/posthoc_data_statistics.py`; the source column names the artifact path and the key, and the script fails if a value the manuscript already states no longer reproduces. Study 1 is the five-county Harvey/Irma replay (`eval/results/study_v2/`), Study 2 the nine-county held-out hybrid round (`eval/results/study_v3_hybrid_rev2/`), and Study 3 the 24-county additive round (`eval/results/study_v4_additive/`). Episode-evaluation and coverage-only county-events are shown separately because the manuscript's pooled outcomes use only the former. Nothing here is estimated; where an artifact does not record a quantity, the row says so.",
        "",
        "| Study | Quantity | Value | Source |",
        "|---|---|---|---|",
    ]
    for s, q, v, src in rows:
        L.append(f"| {s} | {q} | {v} | {src} |")
    L.append("")
    L.append("Not recorded in artifacts: the number of distinct raw VTEC product transmissions behind each event row (the corpora keep one product per event identity), and the number of GDELT records screened before the usable-title filter for the v5 county-events (their corpora hold only the retained articles).")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w").write("\n".join(L) + "\n")
    print(f"wrote {OUT} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
