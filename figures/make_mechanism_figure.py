#!/usr/bin/env python3
"""Render the three-case mechanism figure (reference, warning windows, acquisitions).

One row per county-event, three stacked panels sharing a UTC time axis: (a) the hydrologic reference at the 15-minute study grid and the 1 h maximum reference age, with threshold-compatible gauges' stage minus flood stage as thin lines; (b) the official warning windows the verification trigger consumes, each anchored at max(product transmission, published hazard begin) and open for the frozen 24 h burst duration; (c) acquisition instants for the hybrid, phase adaptation, and the equal-budget fixed clock at one representative offset, with the fold instant marked when a mission folded.

Acquisition instants are not stored in the study artifacts, so each policy is replayed here through ``eval.run_replay.run`` under the frozen replay environment. Every regenerated run is checked against the summary the frozen study recorded for the same policy (acquisition count, termination, acquisitions by reason, burst spans); the script fails closed on any discrepancy. The frozen result directories are read only.

Cases:

    Montgomery, TX / Hurricane Beryl (2024)   eval/results/study_v3_hybrid_rev2 (re-anchoring hybrid)
    Sarasota, FL / Hurricane Idalia (2023)    eval/results/study_v4_additive   (additive hybrid)
    New Castle, DE / Hurricane Isaias (2020)  eval/results/study_v4_additive   (additive hybrid)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "sentinel-paper-mpl-cache")
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402
from PIL import Image, ImageStat  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.baselines import matched_budget_interval_seconds  # noqa: E402
from eval.ground_truth import (  # noqa: E402
    apply_threshold_compatibility,
    hydrologic_truth_at,
    parse_iso,
)
from eval.run_replay import run as replay_run  # noqa: E402
from eval.run_study import (  # noqa: E402
    FROZEN_REPLAY_ENVIRONMENT,
    PRIMARY_POLICY_STALENESS_HOURS,
    PRIMARY_TRUTH_MAX_AGE_HOURS,
    _temporary_environment,
)
from eval.vtec_trigger import load_trigger_corpus, trigger_events_sorted  # noqa: E402
from watcher.replay import load_corpus  # noqa: E402

PDF_NAME = "fig_mechanism_cases.pdf"
PNG_NAME = "fig_mechanism_cases.png"
PNG_DPI = 200
FIGSIZE = (7.4, 8.9)
GRID_SECONDS = 900
GAUGE_FLOOR_FT = -6.0

# Colorblind-safe accents (Okabe-Ito), shared with make_results_figure.py where the same role exists. Panel position and tick geometry duplicate color coding.
INK = "#202124"
MUTED = "#5F6368"
GRID = "#D9DDE1"
PANEL_EDGE = "#B7BDC3"
WHITE = "#FFFFFF"
ADAPTIVE = "#0072B2"
MATCHED = "#D55E00"
HYBRID = "#009E73"
WARNING = "#E69F00"
FLOOD_FILL = "#BFD7EA"
GAUGE_LINE = "#7A828A"


@dataclass(frozen=True)
class CaseSpec:
    label: str
    study_dir: Path
    manifest_path: Path
    result_dir_glob: str
    hybrid_kind: str


CASES: tuple[CaseSpec, ...] = (
    CaseSpec(
        label="Montgomery County, TX, Hurricane Beryl (2024); Study 2, re-anchoring hybrid",
        study_dir=REPO_ROOT / "eval" / "results" / "study_v3_hybrid_rev2",
        manifest_path=REPO_ROOT / "eval" / "corpus" / "study_v3" / "manifest.json",
        result_dir_glob="montgomery_48339_*",
        hybrid_kind="reanchor",
    ),
    CaseSpec(
        label="Sarasota County, FL, Hurricane Idalia (2023); Study 3, additive hybrid",
        study_dir=REPO_ROOT / "eval" / "results" / "study_v4_additive",
        manifest_path=REPO_ROOT / "eval" / "corpus" / "study_v4" / "manifest.json",
        result_dir_glob="sarasota_12115_*",
        hybrid_kind="additive",
    ),
    CaseSpec(
        label="New Castle County, DE, Hurricane Isaias (2020); Study 3, additive hybrid",
        study_dir=REPO_ROOT / "eval" / "results" / "study_v4_additive",
        manifest_path=REPO_ROOT / "eval" / "corpus" / "study_v4" / "manifest.json",
        result_dir_glob="new_castle_10003_*",
        hybrid_kind="additive",
    ),
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    _require(path.is_file(), f"required frozen artifact is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"expected a JSON object in {path}")
    return value


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else REPO_ROOT / path


# ---------------------------------------------------------------------------
# Data reconstruction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRun:
    name: str
    display: str
    color: str
    ticks: tuple[Mapping[str, Any], ...]
    folded_at: Optional[datetime]
    stored_row: Mapping[str, Any]

    @property
    def instants(self) -> list[datetime]:
        return [parse_iso(str(tick["t"])) for tick in self.ticks]

    def first_flood_stage_observation(self) -> Optional[datetime]:
        for tick in self.ticks:
            if tick.get("flood_stage_observed"):
                return parse_iso(str(tick["t"]))
        return None


@dataclass(frozen=True)
class CaseData:
    spec: CaseSpec
    entry: Mapping[str, Any]
    result_path: Path
    start: datetime
    end: datetime
    grid: tuple[datetime, ...]
    reference: tuple[Optional[bool], ...]
    positive_intervals: tuple[tuple[datetime, datetime], ...]
    unknown_intervals: tuple[tuple[datetime, datetime], ...]
    stored_episodes: tuple[tuple[datetime, datetime], ...]
    gauge_series: tuple[tuple[str, list[datetime], list[float]], ...]
    warning_windows: tuple[Mapping[str, Any], ...]
    policies: tuple[PolicyRun, ...]
    matched_interval_seconds: int
    matched_offset_hours: float
    matched_offset_rule: str


def _stored_row(result: Mapping[str, Any], policy: str) -> Mapping[str, Any]:
    rows = [r for r in result["schedule_rows"] if r["policy"] == policy]
    rows += [r for r in result["matched_budget_rows"] if r["policy"] == policy]
    _require(len(rows) == 1, f"expected exactly one stored row for {policy}, found {len(rows)}")
    return rows[0]


def _representative_matched_offset(result: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    """The equal-budget clock offset that stands in for the offset median.

    The study's comparator is the median over every 15-minute offset of the clock matched to the hybrid's realized acquisition count. A single trace must be drawn, so the offset whose unobserved positive duration is nearest that median is chosen; ties go to the offset whose episodes-observed count is nearest the offset median of that count, then to the earliest offset. Outcome relative to the hybrid is never part of the rule.
    """
    rows = [r for r in result["matched_budget_rows"] if r["matched_target_policy"] == "hybrid"]
    _require(bool(rows), "no matched-budget rows for the hybrid")
    unobs_median = statistics.median(r["unobserved_flood_stage_duration_hours"] for r in rows)
    eps_median = statistics.median(r["episodes_observed_by_schedule"] for r in rows)
    chosen = min(
        rows,
        key=lambda r: (
            abs(r["unobserved_flood_stage_duration_hours"] - unobs_median),
            abs(r["episodes_observed_by_schedule"] - eps_median),
            float(r["schedule_offset_hours"]),
        ),
    )
    rule = (
        f"offset median unobserved {unobs_median:.4g} h over {len(rows)} offsets; chosen offset "
        f"{chosen['schedule_offset_hours']:g} h with {chosen['unobserved_flood_stage_duration_hours']:.4g} h "
        f"unobserved and {chosen['episodes_observed_by_schedule']} episodes observed "
        f"(offset median of episodes observed {eps_median:g})"
    )
    return chosen, rule


def _grid(start: datetime, end: datetime) -> list[datetime]:
    out = []
    t = start
    while t < end:
        out.append(t)
        t += timedelta(seconds=GRID_SECONDS)
    return out


def _intervals(
    grid: Sequence[datetime], values: Sequence[Optional[bool]], target: Optional[bool]
) -> list[tuple[datetime, datetime]]:
    """Half-open [grid_i, grid_i + step) runs where the label equals ``target``."""
    out: list[tuple[datetime, datetime]] = []
    step = timedelta(seconds=GRID_SECONDS)
    run_start: Optional[datetime] = None
    for t, value in zip(grid, values):
        hit = value is target if target is None else value is True
        if hit and run_start is None:
            run_start = t
        elif not hit and run_start is not None:
            out.append((run_start, t))
            run_start = None
    if run_start is not None:
        out.append((run_start, grid[-1] + step))
    return out


def _warning_windows(
    trigger_corpus_path: Path, qualifying: Sequence[str], burst_duration_seconds: int
) -> list[dict[str, Any]]:
    """Burst windows implied by the frozen trigger corpus under the protocol rule."""
    events = trigger_events_sorted(load_trigger_corpus(trigger_corpus_path))
    qualifying_set = {token.upper() for token in qualifying}
    out = []
    for event in events:
        token = f"{event['phenomena']}.{event['significance']}".upper()
        if token not in qualifying_set:
            continue
        available = parse_iso(str(event["available_at"]))
        begins = parse_iso(str(event["begins_at"])) if event.get("begins_at") else available
        anchor = max(available, begins)
        out.append(
            {
                "event_key": event["event_key"],
                "token": token,
                "available_at": available,
                "start": anchor,
                "end": anchor + timedelta(seconds=burst_duration_seconds),
            }
        )
    return out


def _check_windows_against_result(
    windows: Sequence[Mapping[str, Any]], result: Mapping[str, Any]
) -> None:
    seen = (result["policies"]["hybrid"].get("verification_trigger") or {}).get(
        "events_first_seen"
    ) or []
    stored = {
        row["event_key"]: (parse_iso(row["burst_window_start"]), parse_iso(row["burst_window_end"]))
        for row in seen
    }
    derived = {w["event_key"]: (w["start"], w["end"]) for w in windows}
    for key, span in stored.items():
        _require(key in derived, f"stored burst window {key} has no derived counterpart")
        _require(derived[key] == span, f"burst window {key} differs from the stored run: {derived[key]} vs {span}")


def _run_policy(
    *,
    entry: Mapping[str, Any],
    excluded_ids: Sequence[str],
    run_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    with _temporary_environment(FROZEN_REPLAY_ENVIRONMENT):
        return replay_run(
            corpus_path=str(_resolve(entry["path"])),
            county=entry["county"],
            state=entry["state"],
            fips=entry["fips"],
            start=entry["analysis_start"],
            end=entry["analysis_end"],
            max_staleness_hours=PRIMARY_POLICY_STALENESS_HOURS,
            threshold_excluded_site_ids=list(excluded_ids),
            **run_kwargs,
        )


def _verify_run(
    name: str,
    run: Mapping[str, Any],
    stored: Mapping[str, Any],
    stored_policy: Optional[Mapping[str, Any]],
) -> None:
    """Fail closed unless the regenerated run matches the frozen study's summary.

    Matched-budget runs have a summary row but no ``policies`` entry, so for them only the row is available.
    """
    ticks = run.get("ticks") or []
    _require(
        len(ticks) == int(stored["acquisitions"]),
        f"{name}: regenerated {len(ticks)} acquisitions, frozen study recorded {stored['acquisitions']}",
    )
    term = run.get("termination") or {}
    if stored_policy is not None:
        _require(
            term == stored_policy.get("termination"),
            f"{name}: termination {term} differs from the frozen study {stored_policy.get('termination')}",
        )
    folded = stored.get("folded_at")
    if folded:
        _require(ticks and ticks[-1].get("folded"), f"{name}: frozen study folded at {folded} but regenerated run did not fold")
        _require(ticks[-1]["t"] == folded, f"{name}: fold instant {ticks[-1]['t']} differs from the frozen study {folded}")
    else:
        _require(not (ticks and ticks[-1].get("folded")), f"{name}: regenerated run folded but the frozen study did not")
    accounting = run.get("verification_trigger")
    stored_accounting = (stored_policy or {}).get("verification_trigger")
    if stored_accounting:
        _require(accounting is not None, f"{name}: frozen study has trigger accounting but the regenerated run does not")
        for key in ("acquisitions_by_reason", "burst_spans", "immediate_fires", "trigger_stream_consultations"):
            _require(
                accounting.get(key) == stored_accounting.get(key),
                f"{name}: {key} differs from the frozen study: {accounting.get(key)} vs {stored_accounting.get(key)}",
            )
    observed = [t for t in ticks if t.get("flood_stage_observed")]
    lags = [lag for lag in (stored.get("schedule_flood_stage_observation_lag_hours") or []) if lag is not None]
    if lags and not observed:
        raise RuntimeError(f"{name}: frozen study recorded observed episodes but no regenerated acquisition observed flood stage")


def load_case(spec: CaseSpec) -> CaseData:
    result_dirs = sorted((spec.study_dir / "counties").glob(spec.result_dir_glob))
    _require(len(result_dirs) == 1, f"expected one result directory for {spec.result_dir_glob}, found {result_dirs}")
    result_path = result_dirs[0] / "result.json"
    result = _load_json(result_path)
    manifest = _load_json(spec.manifest_path)
    policy = manifest["frozen_trigger_policy"]
    entry = result["corpus_manifest_entry"]
    corpus_path = _resolve(entry["path"])
    trigger_path = _resolve(entry["trigger_corpus"])
    _require(_sha256(corpus_path) == entry["sha256"], f"gauge corpus checksum mismatch: {corpus_path}")
    _require(_sha256(trigger_path) == entry["trigger_corpus_sha256"], f"trigger corpus checksum mismatch: {trigger_path}")

    excluded_ids = sorted(
        {str(row["site_no"]) for row in (entry.get("threshold_exclusions") or []) if row.get("site_no")}
    )
    corpus, _ = apply_threshold_compatibility(load_corpus(corpus_path), excluded_site_ids=excluded_ids)
    start = parse_iso(entry["analysis_start"])
    end = parse_iso(entry["analysis_end"])
    grid = _grid(start, end)
    reference = [
        hydrologic_truth_at(corpus, t, max_age_hours=PRIMARY_TRUTH_MAX_AGE_HOURS)["is_flooding"] for t in grid
    ]
    positive = _intervals(grid, reference, True)
    unknown = _intervals(grid, reference, None)

    stored_episode_rows = result["trigger_accounting"]["hybrid"]["episode_trigger_rows"]
    stored_episodes = tuple((parse_iso(r["start"]), parse_iso(r["end"])) for r in stored_episode_rows)

    gauge_series = []
    for site in corpus.get("sites") or []:
        if site.get("flood_stage") is None:
            continue
        obs = [o for o in (site.get("observations") or []) if o.get("stage") is not None]
        if not obs:
            continue
        times = [parse_iso(o["t"]) for o in obs]
        values = [float(o["stage"]) - float(site["flood_stage"]) for o in obs]
        gauge_series.append((str(site["site_no"]), times, values))

    windows = _warning_windows(trigger_path, policy["qualifying"], int(policy["burst_duration_seconds"]))
    _check_windows_against_result(windows, result)

    trigger_spec = {
        "enabled": True,
        "qualifying": list(policy["qualifying"]),
        "burst_interval_seconds": int(policy["burst_interval_seconds"]),
        "burst_duration_seconds": int(policy["burst_duration_seconds"]),
        "anchor_mode": spec.hybrid_kind,
    }
    hybrid_kwargs = {
        "trigger": trigger_spec,
        "trigger_corpus_path": str(trigger_path),
        "trigger_poll_interval_seconds": int(policy["poll_interval_seconds"]),
    }
    stored_hybrid_policy = result["policies"]["hybrid"]
    stored_mode = ((stored_hybrid_policy.get("verification_trigger") or {}).get("policy") or {}).get("anchor_mode")
    if stored_mode is not None:
        _require(stored_mode == spec.hybrid_kind, f"stored anchor mode {stored_mode} differs from {spec.hybrid_kind}")

    hybrid_row = _stored_row(result, "hybrid")
    adaptive_row = _stored_row(result, "adaptive")
    matched_row, matched_rule = _representative_matched_offset(result)
    matched_interval = matched_budget_interval_seconds(
        start=entry["analysis_start"], end=entry["analysis_end"], target_acquisitions=int(hybrid_row["acquisitions"])
    )
    _require(
        round(matched_interval / 3600.0, 4) == round(float(matched_row["fixed_interval_hours"]), 4),
        f"matched interval {matched_interval} s differs from the stored {matched_row['fixed_interval_hours']} h",
    )
    matched_offset_seconds = int(round(float(matched_row["schedule_offset_hours"]) * 3600))

    runs = {
        "hybrid": _run_policy(entry=entry, excluded_ids=excluded_ids, run_kwargs=hybrid_kwargs),
        "adaptive": _run_policy(entry=entry, excluded_ids=excluded_ids, run_kwargs={}),
        "matched": _run_policy(
            entry=entry,
            excluded_ids=excluded_ids,
            run_kwargs={"fixed_interval_seconds": matched_interval, "schedule_offset_seconds": matched_offset_seconds},
        ),
    }
    _verify_run("hybrid", runs["hybrid"], hybrid_row, stored_hybrid_policy)
    _verify_run("adaptive", runs["adaptive"], adaptive_row, result["policies"]["adaptive"])
    _verify_run("matched", runs["matched"], matched_row, None)

    def _policy(name: str, display: str, color: str, row: Mapping[str, Any]) -> PolicyRun:
        ticks = tuple(runs[name]["ticks"])
        folded = parse_iso(ticks[-1]["t"]) if ticks and ticks[-1].get("folded") else None
        return PolicyRun(name=name, display=display, color=color, ticks=ticks, folded_at=folded, stored_row=row)

    hybrid_display = "Hybrid (re-anchoring, Study 2)" if spec.hybrid_kind == "reanchor" else "Hybrid (additive, Study 3)"
    policies = (
        _policy("hybrid", hybrid_display, HYBRID, hybrid_row),
        _policy("adaptive", "Phase adaptation", ADAPTIVE, adaptive_row),
        _policy(
            "matched",
            f"Equal-budget clock, {matched_interval / 3600:.2f} h, offset {matched_row['schedule_offset_hours']:g} h",
            MATCHED,
            matched_row,
        ),
    )
    return CaseData(
        spec=spec,
        entry=entry,
        result_path=result_path,
        start=start,
        end=end,
        grid=tuple(grid),
        reference=tuple(reference),
        positive_intervals=tuple(positive),
        unknown_intervals=tuple(unknown),
        stored_episodes=stored_episodes,
        gauge_series=tuple(gauge_series),
        warning_windows=tuple(windows),
        policies=policies,
        matched_interval_seconds=matched_interval,
        matched_offset_hours=float(matched_row["schedule_offset_hours"]),
        matched_offset_rule=matched_rule,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt(value: Optional[datetime]) -> str:
    return _utc_iso(value) if value is not None else "none"


def _episode_observations(case: CaseData, policy: PolicyRun) -> list[tuple[str, str, Optional[str]]]:
    """First flood-stage-positive acquisition at or after each reference episode onset and before its end."""
    out = []
    instants = [(parse_iso(str(t["t"])), bool(t.get("flood_stage_observed"))) for t in policy.ticks]
    for onset, finish in case.stored_episodes:
        first = next((t for t, seen in instants if seen and onset <= t < finish), None)
        out.append((_utc_iso(onset), _utc_iso(finish), _fmt(first)))
    return out


def report(case: CaseData) -> str:
    lines = [f"== {case.spec.label}", f"   result: {case.result_path.relative_to(REPO_ROOT)}"]
    lines.append(f"   window [{_utc_iso(case.start)}, {_utc_iso(case.end)}), grid points {len(case.grid)}")
    lines.append(
        f"   reference: {len(case.positive_intervals)} positive interval(s) from the grid; stored episode rows {len(case.stored_episodes)}; "
        f"unknown intervals {len(case.unknown_intervals)}"
    )
    for span in case.positive_intervals:
        lines.append(f"     positive  {_utc_iso(span[0])} .. {_utc_iso(span[1])}")
    for span in case.stored_episodes:
        lines.append(f"     stored ep {_utc_iso(span[0])} .. {_utc_iso(span[1])}")
    lines.append(f"   warning windows ({len(case.warning_windows)}):")
    for w in case.warning_windows:
        lines.append(f"     {w['event_key']:<22} available {_utc_iso(w['available_at'])} window {_utc_iso(w['start'])} .. {_utc_iso(w['end'])}")
    lines.append(f"   matched clock: {case.matched_offset_rule}")
    for policy in case.policies:
        row = policy.stored_row
        reasons = (row.get("acquisitions_by_reason") or {})
        lines.append(
            f"   {policy.display}: {len(policy.ticks)} acquisitions (stored {row['acquisitions']}); fold {_fmt(policy.folded_at)} "
            f"(stored {row.get('folded_at')}); first flood-stage-positive acquisition {_fmt(policy.first_flood_stage_observation())}; "
            f"episodes observed (stored) {row['episodes_observed_by_schedule']}/{row['observed_flood_episodes']}; "
            f"unobserved positive h (stored) {row['unobserved_flood_stage_duration_hours']}; by reason {reasons or 'n/a'}"
        )
        for onset, finish, first in _episode_observations(case, policy):
            lines.append(f"       episode {onset} .. {finish}: first positive acquisition {first}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------


def _style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 7.2,
            "axes.titlesize": 8.2,
            "axes.labelsize": 7.4,
            "xtick.labelsize": 6.7,
            "ytick.labelsize": 6.7,
            "legend.fontsize": 6.4,
            "axes.linewidth": 0.7,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "hatch.linewidth": 0.45,
        }
    )


def _decorate(ax) -> None:
    ax.set_facecolor(WHITE)
    for spine in ax.spines.values():
        spine.set_color(PANEL_EDGE)
    ax.tick_params(axis="both", colors=MUTED, width=0.6, length=2.5)


def _draw_reference(ax, case: CaseData) -> None:
    for span in case.unknown_intervals:
        ax.axvspan(span[0], span[1], facecolor="#E9EBED", edgecolor="#A6ACB2", linewidth=0.0, hatch="///", zorder=0.5)
    for span in case.positive_intervals:
        ax.axvspan(span[0], span[1], facecolor=FLOOD_FILL, edgecolor="none", zorder=1)
    top = GAUGE_FLOOR_FT
    for _, times, values in case.gauge_series:
        ax.plot(times, values, color=GAUGE_LINE, linewidth=0.5, alpha=0.85, zorder=2)
        top = max(top, max(values))
    ax.axhline(0.0, color=INK, linewidth=0.6, linestyle=(0, (3, 2)), zorder=3)
    # Gauges far below flood stage carry no information for this panel, so the axis is clipped at a fixed floor; the crossing of zero is what matters.
    ax.set_ylim(GAUGE_FLOOR_FT - 0.4, max(1.0, top) + 0.6)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, prune="lower"))
    ax.set_ylabel("Stage minus flood\nstage (ft), floor \u22126")
    ax.grid(axis="y", color=GRID, linewidth=0.5, alpha=0.8, zorder=0)


def _draw_warnings(ax, case: CaseData) -> None:
    windows = sorted(case.warning_windows, key=lambda w: (w["start"], w["event_key"]))
    lanes: list[datetime] = []
    for w in windows:
        lane = next((i for i, busy_until in enumerate(lanes) if w["start"] >= busy_until), None)
        if lane is None:
            lanes.append(w["end"])
            lane = len(lanes) - 1
        else:
            lanes[lane] = w["end"]
        width = mdates.date2num(w["end"]) - mdates.date2num(w["start"])
        ax.barh(lane, width, left=mdates.date2num(w["start"]), height=0.72, color=WARNING, edgecolor="none", zorder=2)
        ax.plot([w["available_at"], w["available_at"]], [lane - 0.42, lane + 0.42], color=INK, linewidth=0.8, solid_capstyle="butt", zorder=3)
    n = max(1, len(lanes))
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_yticks([])
    ax.set_ylabel("Warning\nwindows")
    ax.grid(False)


def _lane_label(case: CaseData, policy: PolicyRun) -> str:
    count = len(policy.ticks)
    if policy.name == "hybrid":
        return f"Hybrid ({count})"
    if policy.name == "adaptive":
        return f"Adaptation ({count})"
    return (
        f"Clock ({count})\n"
        f"{case.matched_interval_seconds / 3600:.2f} h, offset {case.matched_offset_hours:g} h"
    )


def _draw_acquisitions(ax, case: CaseData) -> None:
    lanes = list(case.policies)
    for lane_index, policy in enumerate(lanes):
        y = len(lanes) - 1 - lane_index
        for tick in policy.ticks:
            t = parse_iso(str(tick["t"]))
            verification = str(tick.get("acquisition_due_reason") or "").startswith("verification")
            half = 0.22 if verification else 0.36
            ax.plot([t, t], [y - half, y + half], color=policy.color, linewidth=0.7, alpha=0.95, solid_capstyle="butt", zorder=2)
            if tick.get("flood_stage_observed"):
                ax.plot([t], [y + 0.47], marker="o", markersize=1.9, color=policy.color, markeredgecolor=policy.color, linestyle="none", zorder=3)
        if policy.folded_at is not None:
            ax.plot([policy.folded_at], [y], marker="x", markersize=5.5, markeredgewidth=1.1, color=INK, linestyle="none", zorder=4)
    ax.set_ylim(-0.7, len(lanes) - 0.3)
    ax.set_yticks(list(range(len(lanes))))
    ax.set_yticklabels([_lane_label(case, p) for p in reversed(lanes)], fontsize=6.4)
    ax.set_ylabel("")
    ax.grid(False)


def build_figure(cases: Sequence[CaseData]):
    _style()
    fig = plt.figure(figsize=FIGSIZE)
    fig.patch.set_facecolor(WHITE)
    outer = fig.add_gridspec(len(cases), 1, left=0.155, right=0.985, top=0.905, bottom=0.05, hspace=0.32)
    first_axes = None
    for row, case in enumerate(cases):
        inner = outer[row].subgridspec(3, 1, height_ratios=[1.45, 0.75, 1.15], hspace=0.08)
        ax_ref = fig.add_subplot(inner[0])
        ax_warn = fig.add_subplot(inner[1], sharex=ax_ref)
        ax_acq = fig.add_subplot(inner[2], sharex=ax_ref)
        for ax in (ax_ref, ax_warn, ax_acq):
            _decorate(ax)
            ax.set_xlim(case.start, case.end)
        _draw_reference(ax_ref, case)
        _draw_warnings(ax_warn, case)
        _draw_acquisitions(ax_acq, case)
        for ax in (ax_ref, ax_warn):
            plt.setp(ax.get_xticklabels(), visible=False)
            ax.tick_params(axis="x", length=0)
        locator = mdates.DayLocator(interval=1)
        ax_acq.xaxis.set_major_locator(locator)
        ax_acq.xaxis.set_major_formatter(mdates.DateFormatter("%d %b", tz=timezone.utc))
        ax_acq.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        ax_acq.tick_params(axis="x", which="minor", length=1.5)
        ax_acq.set_xlabel(f"UTC, {case.start.year}")
        ax_ref.set_title(f"({'abc'[row]}) {case.spec.label}", loc="left", fontsize=8.0, color=INK, fontweight="bold", pad=3)
        if first_axes is None:
            first_axes = ax_ref

    handles = [
        Patch(facecolor=FLOOD_FILL, edgecolor="none", label="Reference flood-positive"),
        Patch(facecolor="#E9EBED", edgecolor="#A6ACB2", hatch="///", linewidth=0.0, label="Reference unknown"),
        Line2D([], [], color=GAUGE_LINE, linewidth=0.7, label="Gauge stage minus flood stage"),
        Patch(facecolor=WARNING, edgecolor="none", label="Warning window (24 h from anchor)"),
        Line2D([], [], marker="|", color=INK, linestyle="none", markersize=6, markeredgewidth=0.9, label="Warning transmission time"),
        Line2D([], [], color=HYBRID, linewidth=1.2, label="Hybrid acquisition (short tick: verification)"),
        Line2D([], [], color=ADAPTIVE, linewidth=1.2, label="Phase-adaptation acquisition"),
        Line2D([], [], color=MATCHED, linewidth=1.2, label="Equal-budget clock acquisition"),
        Line2D([], [], marker="o", color=INK, linestyle="none", markersize=2.8, label="Acquisition observed flood stage"),
        Line2D([], [], marker="x", color=INK, linestyle="none", markersize=5.5, markeredgewidth=1.1, label="Mission folded"),
    ]
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=3,
        frameon=False,
        handlelength=1.4,
        columnspacing=1.1,
        handletextpad=0.5,
    )
    return fig


def write_outputs(output_dir: Path, cases: Sequence[CaseData]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / PDF_NAME
    png_path = output_dir / PNG_NAME
    fig = build_figure(cases)
    metadata = {
        "Title": "Mechanism cases: reference, warning windows, acquisitions",
        "Subject": "Montgomery/Beryl, Sarasota/Idalia, New Castle/Isaias replay timelines",
        "Creator": "paper/figures/make_mechanism_figure.py",
        "Producer": "Matplotlib",
        "CreationDate": None,
        "ModDate": None,
    }
    fig.savefig(pdf_path, format="pdf", metadata=metadata)
    fig.savefig(png_path, format="png", dpi=PNG_DPI, metadata={"Software": "paper/figures/make_mechanism_figure.py"})
    plt.close(fig)
    return pdf_path, png_path


def verify_outputs(pdf_path: Path, png_path: Path) -> None:
    if not pdf_path.is_file() or not pdf_path.read_bytes().startswith(b"%PDF"):
        raise RuntimeError(f"missing or invalid vector PDF: {pdf_path}")
    if not png_path.is_file() or not png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"missing or invalid PNG: {png_path}")
    with Image.open(png_path) as image:
        expected = (round(FIGSIZE[0] * PNG_DPI), round(FIGSIZE[1] * PNG_DPI))
        if image.size != expected:
            raise RuntimeError(f"PNG dimensions {image.size} != expected {expected}")
        extrema = ImageStat.Stat(image.convert("RGB")).extrema
        if all(low == high for low, high in extrema):
            raise RuntimeError("PNG is blank or single-colored")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE, help="directory for the PDF and PNG (default: script directory)")
    parser.add_argument("--report", action="store_true", help="print the reconstructed numbers for each case")
    parser.add_argument("--no-render", action="store_true", help="reconstruct and verify only")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    cases = [load_case(spec) for spec in CASES]
    if args.report:
        for case in cases:
            print(report(case))
    if not args.no_render:
        pdf_path, png_path = write_outputs(args.output_dir, cases)
        verify_outputs(pdf_path, png_path)
        print(f"PDF sha256: {_sha256(pdf_path)} ({pdf_path.stat().st_size:,} B)")
        print(f"PNG sha256: {_sha256(png_path)} ({png_path.stat().st_size:,} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
