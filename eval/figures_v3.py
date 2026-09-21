"""Study 2 figure: held-out verification-trigger outcomes (v3 rev2 artifacts).

Regenerates deterministically from the schema-checked aggregate of
``eval/results/study_v3_hybrid_rev2``. One panel: per county-event, the
unobserved truth-positive duration of the hybrid and the phase-adaptive
baseline against the full matched-budget clock-offset distribution.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.figures import GRID, INK, MUTED, _style  # noqa: E402

AGGREGATE = _ROOT / "eval" / "results" / "study_v3_hybrid_rev2" / "aggregate.json"
OUT_PDF = _ROOT / "paper" / "figures" / "fig_verification_trigger.pdf"
OUT_PNG = _ROOT / "paper" / "figures" / "fig_verification_trigger.png"

HYBRID = "#D55E00"
ADAPTIVE = "#0072B2"
OFFSET_BAND = "#c8c8c8"

EVENT_SHORT = {
    "Hurricane Ida (August 2021)": "Ida '21",
    "Hurricane Helene (September 2024)": "Helene '24",
    "Hurricane Milton (October 2024)": "Milton '24",
    "Hurricane Michael (October 2018)": "Michael '18",
    "Hurricane Florence (September 2018)": "Florence '18",
    "Louisiana Flooding (August 2016)": "LA flood '16",
    "Hurricane Matthew (October 2016)": "Matthew '16",
    "Hurricane Sally (September 2020)": "Sally '20",
    "Hurricane Beryl (July 2024)": "Beryl '24",
}


def main() -> int:
    aggregate = json.loads(AGGREGATE.read_text(encoding="utf-8"))
    if aggregate.get("schema") != "sentinel-study-v3-aggregate/1":
        raise ValueError(f"unexpected aggregate schema: {aggregate.get('schema')!r}")
    rows = aggregate["criterion_inputs"]

    _style()
    fig, ax = plt.subplots(figsize=(6.5, 2.9))
    ys = range(len(rows))
    for y, row in zip(ys, rows):
        lo = row["matched_hybrid_offsets_unobserved_min"]
        hi = row["matched_hybrid_offsets_unobserved_max"]
        med = row["matched_hybrid_offsets_unobserved_median"]
        ax.plot([lo, hi], [y, y], color=OFFSET_BAND, linewidth=5, solid_capstyle="butt",
                zorder=1)
        ax.plot([med], [y], marker="|", markersize=9, color=MUTED, zorder=2)
        ax.plot([row["adaptive_unobserved_hours"]], [y], marker="D", markersize=4.5,
                color=ADAPTIVE, linestyle="none", zorder=3)
        ax.plot([row["hybrid_unobserved_hours"]], [y], marker="o", markersize=5,
                markerfacecolor=HYBRID, markeredgecolor=INK, markeredgewidth=0.4,
                linestyle="none", zorder=4)
    ax.set_yticks(list(ys))
    ax.set_yticklabels(
        [f"{row['county']} ({EVENT_SHORT.get(row['event'], row['event'])})"
         for row in rows]
    )
    ax.invert_yaxis()
    ax.set_xlabel("Unobserved truth-positive duration (h)")
    ax.set_xlim(left=-0.15)
    ax.grid(axis="x", zorder=0)
    handles = [
        Line2D([], [], color=OFFSET_BAND, linewidth=5, label="Matched-budget offsets (min to max)"),
        Line2D([], [], color=MUTED, marker="|", markersize=9, linestyle="none", label="Offset median"),
        Line2D([], [], color=ADAPTIVE, marker="D", markersize=4.5, linestyle="none", label="Phase-adaptive"),
        Line2D([], [], marker="o", markersize=5, markerfacecolor=HYBRID,
               markeredgecolor=INK, markeredgewidth=0.4, linestyle="none",
               label="Warning-triggered hybrid"),
    ]
    ax.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0.0, 1.02),
        frameon=False,
        ncol=2,
        borderaxespad=0.0,
    )
    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PDF)
    fig.savefig(OUT_PNG, dpi=600)
    print(f"wrote {OUT_PDF} and {OUT_PNG}")
    return 0




V4_AGGREGATE = _ROOT / "eval" / "results" / "study_v4_additive" / "aggregate.json"
V4_OUT_PDF = _ROOT / "paper" / "figures" / "fig_verification_trigger_v4.pdf"
V4_OUT_PNG = _ROOT / "paper" / "figures" / "fig_verification_trigger_v4.png"


def main_v4() -> int:
    aggregate = json.loads(V4_AGGREGATE.read_text(encoding="utf-8"))
    if aggregate.get("schema") != "sentinel-study-v4-aggregate/1":
        raise ValueError(f"unexpected aggregate schema: {aggregate.get('schema')!r}")
    rows = sorted(
        aggregate["criterion_inputs"],
        key=lambda r: r["matched_hybrid_offsets_unobserved_median"],
        reverse=True,
    )
    _style()
    fig, ax = plt.subplots(figsize=(6.5, 5.6))
    ys = range(len(rows))
    for y, row in zip(ys, rows):
        lo = row["matched_hybrid_offsets_unobserved_min"]
        hi = row["matched_hybrid_offsets_unobserved_max"]
        med = row["matched_hybrid_offsets_unobserved_median"]
        ax.plot([lo, hi], [y, y], color=OFFSET_BAND, linewidth=4.5,
                solid_capstyle="butt", zorder=1)
        ax.plot([med], [y], marker="|", markersize=8, color=MUTED, zorder=2)
        ax.plot([row["adaptive_unobserved_hours"]], [y], marker="D", markersize=4,
                color=ADAPTIVE, linestyle="none", zorder=3)
        ax.plot([row["hybrid_unobserved_hours"]], [y], marker="o", markersize=4.5,
                markerfacecolor=HYBRID, markeredgecolor=INK, markeredgewidth=0.4,
                linestyle="none", zorder=4)
    labels = []
    for row in rows:
        event = row["event"]
        short = event.replace("Hurricane ", "").replace("Tropical Storm ", "TS ")
        short = short.split(" (")[0]
        year = event.split("(")[-1].rstrip(")").split()[-1][-2:]
        labels.append(f"{row['county']} ({short} '{year})")
    ax.set_yticks(list(ys))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("Unobserved truth-positive duration (h)")
    ax.set_xlim(left=-0.2)
    ax.grid(axis="x", zorder=0)
    handles = [
        Line2D([], [], color=OFFSET_BAND, linewidth=4.5,
               label="Matched-budget offsets (min to max)"),
        Line2D([], [], color=MUTED, marker="|", markersize=8, linestyle="none",
               label="Offset median"),
        Line2D([], [], color=ADAPTIVE, marker="D", markersize=4, linestyle="none",
               label="Phase-adaptive"),
        Line2D([], [], marker="o", markersize=4.5, markerfacecolor=HYBRID,
               markeredgecolor=INK, markeredgewidth=0.4, linestyle="none",
               label="Additive warning-triggered hybrid"),
    ]
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.0, 1.01),
              frameon=False, ncol=2, borderaxespad=0.0)
    V4_OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(V4_OUT_PDF)
    fig.savefig(V4_OUT_PNG, dpi=600)
    print(f"wrote {V4_OUT_PDF} and {V4_OUT_PNG}")
    return 0


if __name__ == "__main__":
    import sys as _sys

    if "--v4" in _sys.argv:
        raise SystemExit(main_v4())
    raise SystemExit(main())
