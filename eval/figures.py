"""Publication figures for the evaluation.

Palette is Okabe-Ito based and validated for colour-vision deficiency and for
contrast against a light print surface. Series identity is carried by colour
*and* line style or direct label, never by colour alone. One axis per figure; no
dual scales.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.ground_truth import first_flood_warning_time, parse_iso  # noqa: E402

#: Validated categorical palette (all six checks pass, light surface).
PALETTE = {
    "full": "#0072B2",
    "no_belief": "#D55E00",
    "no_role": "#009E73",
    "none": "#5D3A9B",
}
LINESTYLE = {"full": "-", "no_belief": "--", "no_role": "-.", "none": ":"}
LABEL = {
    "full": "Full (role + belief)",
    "no_belief": "No belief layer",
    "no_role": "No role layer",
    "none": "Neither (naive)",
}

INK = "#1a1a1a"
MUTED = "#6b6b6b"
GRID = "#d9d9d9"
TRUTH_BAND = "#c8c8c8"

PHASE_LEVEL = {
    "early_watch": 0,
    "elevated_watch": 1,
    "imminent": 2,
    "active_incident": 3,
    "recovery": 1,
    "folded": 0,
}
PHASE_TICKS = ["early\nwatch", "elevated\nwatch", "imminent", "active\nincident"]


def _style() -> None:
    plt.rcParams.update(
        {
            "font.size": 8,
            "axes.labelsize": 8,
            "axes.titlesize": 9,
            "legend.fontsize": 7,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "axes.edgecolor": MUTED,
            "axes.labelcolor": INK,
            "text.color": INK,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": GRID,
            "grid.linewidth": 0.5,
            "figure.dpi": 300,
            "savefig.bbox": "tight",
        }
    )


def _hours_from(ticks: Sequence[str], origin) -> List[float]:
    return [(parse_iso(t) - origin).total_seconds() / 3600.0 for t in ticks]


# --------------------------------------------------------------------------


def fig_phase_trajectory(
    runs: Dict[str, Dict[str, Any]],
    *,
    annotated: Dict[str, List[Dict[str, Any]]],
    events: Sequence[Dict[str, Any]],
    out_path: str,
) -> str:
    """RQ2: when each configuration escalates, against hydrologic truth."""
    _style()
    fig, ax = plt.subplots(figsize=(7.0, 2.9))

    any_ticks = next(iter(annotated.values()))
    origin = parse_iso(any_ticks[0]["t"])

    # Truth band: intervals where a gauge stood above flood stage.
    truth_ticks = annotated.get("no_role") or any_ticks
    hours = _hours_from([t["t"] for t in truth_ticks], origin)
    flooding = [bool(t.get("truth_hydrologic_flooding")) for t in truth_ticks]
    start = None
    for i, flag in enumerate(flooding):
        if flag and start is None:
            start = hours[i]
        elif not flag and start is not None:
            ax.axvspan(start, hours[i], color=TRUTH_BAND, alpha=0.55, lw=0, zorder=0)
            start = None
    if start is not None:
        ax.axvspan(start, hours[-1], color=TRUTH_BAND, alpha=0.55, lw=0, zorder=0)

    official = first_flood_warning_time(events)
    if official is not None:
        ax.axvline(
            (official - origin).total_seconds() / 3600.0,
            color=INK,
            lw=1.0,
            ls=(0, (4, 2)),
            zorder=2,
        )

    for mode in ("full", "no_belief", "no_role", "none"):
        ticks = annotated.get(mode)
        if not ticks:
            continue
        xs = _hours_from([t["t"] for t in ticks], origin)
        ys = [PHASE_LEVEL.get(t["phase_after"], 0) for t in ticks]
        ax.step(
            xs,
            ys,
            where="post",
            color=PALETTE[mode],
            ls=LINESTYLE[mode],
            lw=1.6,
            label=LABEL[mode],
            zorder=3,
        )

    top = max(
        (PHASE_LEVEL.get(t["phase_after"], 0) for ts in annotated.values() for t in ts),
        default=len(PHASE_TICKS) - 1,
    )
    ax.set_yticks(range(top + 1))
    ax.set_yticklabels(PHASE_TICKS[: top + 1])
    ax.set_ylim(-0.3, top + 0.45)
    ax.set_xlabel("hours since replay start (2017-08-25 00:00 UTC)")
    ax.grid(axis="y", zorder=0)
    ax.set_axisbelow(True)

    handles, labels = ax.get_legend_handles_labels()
    handles.append(matplotlib.patches.Patch(facecolor=TRUTH_BAND, alpha=0.55, lw=0))
    labels.append("gauge above flood stage (truth)")
    handles.append(Line2D([0], [0], color=INK, lw=1.0, ls=(0, (4, 2))))
    labels.append("first NWS flood warning")
    ax.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.30),
        frameon=False,
        ncol=3,
    )

    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def fig_calibration(
    calibrations: Dict[str, Dict[str, Any]], *, out_path: str
) -> str:
    """RQ3: calibration error, and whether confidence discriminates at all.

    Scored claims cluster in a single confidence band, so a reliability diagram
    would be one point per configuration and would hide the result. The two
    panels instead report calibration error directly, and the accuracy gap
    between the lower and upper half of the confidence distribution: a layer
    that ranks evidence usefully should show a positive gap, a uniform score
    cannot.
    """
    _style()
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6))
    modes = [m for m in ("full", "no_belief", "no_role", "none") if m in calibrations]

    ax = axes[0]
    x = range(len(modes))
    width = 0.38
    brier = [calibrations[m]["brier"] for m in modes]
    ece = [calibrations[m]["ece"] for m in modes]
    b1 = ax.bar([i - width / 2 for i in x], brier, width, color=PALETTE["full"], label="Brier", zorder=3)
    b2 = ax.bar([i + width / 2 for i in x], ece, width, color=PALETTE["no_role"], label="ECE", zorder=3)
    for bars, vals in ((b1, brier), (b2, ece)):
        for bar, val in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.004,
                    f"{val:.3f}", ha="center", fontsize=6.5, color=INK)
    ax.set_xticks(list(x))
    ax.set_xticklabels([LABEL[m].replace(" (role + belief)", "").replace(" layer", "") for m in modes],
                       rotation=12, ha="right")
    ax.set_ylabel("error (lower is better)")
    ax.set_ylim(0, max(max(brier), max(ece)) * 1.28)
    ax.grid(axis="y", zorder=0); ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left")
    ax.set_title("Calibration error", loc="left")

    ax = axes[1]
    # A configuration with one confidence value has no meaningful split, so its
    # bars are omitted and labelled rather than drawn at zero, which would read
    # as "always wrong" instead of "not applicable".
    lows = [calibrations[m]["accuracy_low_confidence_half"] for m in modes]
    highs = [calibrations[m]["accuracy_high_confidence_half"] for m in modes]
    b1 = ax.bar([i - width / 2 for i in x], [v or 0 for v in lows], width,
                color=PALETTE["no_belief"], label="lower-confidence half", zorder=3)
    b2 = ax.bar([i + width / 2 for i in x], [v or 0 for v in highs], width,
                color=PALETTE["none"], label="upper-confidence half", zorder=3)
    for bars, vals in ((b1, lows), (b2, highs)):
        for bar, val in zip(bars, vals):
            if val is None:
                bar.set_visible(False)
                continue
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.012,
                    f"{val:.2f}", ha="center", fontsize=6.5, color=INK)
    for i, mode in enumerate(modes):
        if lows[i] is None:
            ax.text(i, 0.06, "single confidence\nvalue: undefined", ha="center",
                    fontsize=6, color=MUTED, style="italic")
    ax.set_xticks(list(x))
    ax.set_xticklabels([LABEL[m].replace(" (role + belief)", "").replace(" layer", "") for m in modes],
                       rotation=12, ha="right")
    ax.set_ylabel("claim accuracy")
    ax.set_ylim(0, 1.52)
    ax.grid(axis="y", zorder=0); ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", ncol=2)
    ax.set_title("Does confidence discriminate?", loc="left")

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def fig_abstention(
    per_county: Dict[str, Dict[str, Any]], *, out_path: str
) -> str:
    """RQ4: behaviour under sparse evidence, gauge-rich versus gauge-poor."""
    _style()
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.9))

    # Ordered by coverage so the degradation reads left to right.
    counties = sorted(
        per_county, key=lambda c: per_county[c]["mean_gauges_scored"] or 0, reverse=True
    )
    x = range(len(counties))

    # Gauge coverage and the share of low-evidence intervals are different
    # quantities, so they get their own axes rather than one shared scale.
    ax = axes[0]
    scored = [per_county[c]["mean_gauges_scored"] or 0 for c in counties]
    bars = ax.bar(list(x), scored, 0.5, color=PALETTE["full"], zorder=3)
    for bar, val in zip(bars, scored):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.15,
                f"{val:.1f}", ha="center", fontsize=7, color=INK)
    ax.set_xticks(list(x))
    ax.set_xticklabels(counties, fontsize=6.5, rotation=18, ha="right")
    ax.set_ylabel("mean scorable gauges per interval")
    ax.set_ylim(0, max(scored) * 1.35 + 0.5)
    ax.grid(axis="y", zorder=0)
    ax.set_axisbelow(True)
    ax.set_title("Source coverage", loc="left")

    ax = axes[1]
    detected = [
        100.0 * per_county[c].get("detected_ticks", 0) / max(per_county[c]["n_ticks"], 1)
        for c in counties
    ]
    low = [
        100.0 * per_county[c]["low_evidence_ticks"] / max(per_county[c]["n_ticks"], 1)
        for c in counties
    ]
    width = 0.38
    b1 = ax.bar([i - width / 2 for i in x], detected, width, color=PALETTE["full"],
                label="flooding detected", zorder=3)
    b2 = ax.bar([i + width / 2 for i in x], low, width, color=PALETTE["no_role"],
                label="reported insufficient data", zorder=3)
    for bars_, vals in ((b1, detected), (b2, low)):
        for bar, val in zip(bars_, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 2.0,
                    f"{val:.0f}%", ha="center", fontsize=7, color=INK)
    ax.set_xticks(list(x))
    ax.set_xticklabels(counties, fontsize=6.5, rotation=18, ha="right")
    ax.set_ylabel("share of monitored intervals")
    ax.set_ylim(0, 132)
    ax.grid(axis="y", zorder=0)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", fontsize=6.5)
    ax.set_title("Behaviour as coverage degrades", loc="left")

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def fig_reporting_burden(per_county: Dict[str, Dict[str, int]], *, out_path: str) -> str:
    """Reports issued per county, phase-aware against fixed interval.

    This is the result that survives every check, so it gets a figure. Counts
    rather than rates: the two schedules observe at different frequencies, and a
    rate would conceal that.
    """
    _style()
    fig, ax = plt.subplots(figsize=(7.0, 2.9))
    counties = sorted(per_county, key=lambda c: per_county[c]["fixed"] - per_county[c]["phase"], reverse=True)
    x = range(len(counties))
    width = 0.38
    phase = [per_county[c]["phase"] for c in counties]
    fixed = [per_county[c]["fixed"] for c in counties]

    b1 = ax.bar([i - width / 2 for i in x], fixed, width, color=PALETTE["no_role"],
                label="fixed interval", zorder=3)
    b2 = ax.bar([i + width / 2 for i in x], phase, width, color=PALETTE["full"],
                label="phase-aware", zorder=3)
    for bars, vals in ((b1, fixed), (b2, phase)):
        for bar, val in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 4,
                    f"{val}", ha="center", fontsize=7, color=INK)
    for i, c in enumerate(counties):
        cut = 100 * (1 - per_county[c]["phase"] / per_county[c]["fixed"])
        ax.text(i, max(fixed) * 1.10, f"-{cut:.0f}%", ha="center", fontsize=7.5,
                color=INK, weight="bold")

    ax.set_xticks(list(x))
    ax.set_xticklabels(counties, fontsize=7, rotation=12, ha="right")
    ax.set_ylabel("reports issued over the event")
    ax.set_ylim(0, max(fixed) * 1.22)
    ax.grid(axis="y", zorder=0)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2)

    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def fig_ordering(low: float, high: float, *, out_path: str) -> str:
    """Whether confidence separates correct claims from incorrect ones.

    The ablated configuration carries one score, so the split does not exist
    for it at any value. That is marked rather than drawn as a zero bar.
    """
    _style()
    fig, ax = plt.subplots(figsize=(3.6, 2.8))
    ax.bar([0, 1], [low, high], 0.5,
           color=[PALETTE["no_belief"], PALETTE["full"]], zorder=3)
    for xpos, v in ((0, low), (1, high)):
        ax.text(xpos, v + 0.02, f"{v:.2f}", ha="center", fontsize=8, color=INK)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["lower-confidence\nhalf", "upper-confidence\nhalf"], fontsize=7)
    ax.set_ylabel("share of claims that were correct")
    ax.set_ylim(0, 1.18)
    ax.grid(axis="y", zorder=0); ax.set_axisbelow(True)
    ax.set_title("With the belief layer", loc="left")
    fig.savefig(out_path)
    plt.close(fig)
    return out_path
