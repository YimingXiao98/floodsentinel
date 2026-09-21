#!/usr/bin/env python3
"""Render the study-area map: every county-event in the three replay studies.

The figure shows the contiguous United States with Puerto Rico as an inset. Each county that appears in a study is shaded and marked at its centroid; color and marker shape identify the study, a filled marker identifies an episode-evaluation county-event, and a hollow marker a coverage-only county-event. A county with two distinct events carries a "x2" label. A county-event that two studies share is drawn with concentric hollow rings in both study colors and counts as one event.

The only numerical inputs are the frozen study artifacts::

    eval/results/study_v2/artifact_manifest.json           (county_results)
    eval/corpus/study/manifest.json                        (cross-check of Study 1)
    eval/results/study_v3_hybrid_rev2/counties/*/result.json (corpus_manifest_entry)
    eval/results/study_v4_additive/counties/*/result.json    (corpus_manifest_entry)

Geometry comes from the U.S. Census Bureau 2023 cartographic boundary files at 1:20,000,000 (``cb_2023_us_state_20m`` and ``cb_2023_us_county_20m``, NAD83 geographic coordinates). They are downloaded on first use into ``--geo-dir`` and read with pyshp. Coordinates are projected with a manual Albers equal-area conic (USA Contiguous parameters for the main panel; Puerto Rico parameters for the inset).

The generator fails closed if an artifact is missing, a county-event count differs from the frozen study sizes (5, 11, 28), a county lies outside the contiguous states or Puerto Rico, or a FIPS code is absent from the boundary file.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "sentinel-paper-mpl-cache"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402
import matplotlib.patheffects as patheffects  # noqa: E402

try:
    import shapefile  # pyshp
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyshp is required: pip install pyshp") from exc


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
RESULTS = REPO_ROOT / "eval" / "results"
STUDY1_MANIFEST = RESULTS / "study_v2" / "artifact_manifest.json"
STUDY1_CORPUS_MANIFEST = REPO_ROOT / "eval" / "corpus" / "study" / "manifest.json"
STUDY2_DIR = RESULTS / "study_v3_hybrid_rev2" / "counties"
STUDY3_DIR = RESULTS / "study_v4_additive" / "counties"

PDF_NAME = "fig_county_events_map.pdf"
PNG_NAME = "fig_county_events_map.png"
PNG_DPI = 200
FIGSIZE = (7.4, 4.55)

EXPECTED_COUNTS = {1: 5, 2: 11, 3: 28}
EPISODE = "episode_evaluation"
COVERAGE = "coverage_only"

CENSUS_BASE = "https://www2.census.gov/geo/tiger/GENZ2023/shp/"
STATE_LAYER = "cb_2023_us_state_20m"
COUNTY_LAYER = "cb_2023_us_county_20m"
NON_CONUS_STATEFP = {"02": "Alaska", "15": "Hawaii", "72": "Puerto Rico"}
PUERTO_RICO = "72"

# Colorblind-safe accents (Okabe-Ito). Marker shape duplicates the color coding.
INK = "#202124"
MUTED = "#5F6368"
WHITE = "#FFFFFF"
LAND = "#F4F5F6"
STATE_EDGE = "#9AA1A8"
STUDY_COLOR = {1: "#0072B2", 2: "#D55E00", 3: "#009E73"}
STUDY_MARKER = {1: "D", 2: "s", 3: "o"}
STUDY_LABEL = {1: "Study 1", 2: "Study 2", 3: "Study 3"}


# --------------------------------------------------------------------------- study data


@dataclass(frozen=True)
class CountyEvent:
    study: int
    fips: str
    county: str
    state: str
    event: str
    role: str
    source: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"make_county_map: {message}")


def _load_json(path: Path) -> dict:
    _require(path.is_file(), f"missing artifact {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_study1() -> list[CountyEvent]:
    manifest = _load_json(STUDY1_MANIFEST)
    corpus = _load_json(STUDY1_CORPUS_MANIFEST)
    by_fips = {entry["fips"]: entry for entry in corpus["artifacts"]}
    events: list[CountyEvent] = []
    for row in manifest["county_results"]:
        entry = by_fips.get(row["fips"])
        _require(entry is not None, f"Study 1 FIPS {row['fips']} is not in the corpus manifest")
        _require(
            entry["county"] == row["county"] and entry["state"] == row["state"] and entry["analysis_role"] == row["analysis_role"],
            f"Study 1 county {row['fips']} disagrees between artifact_manifest.json and corpus manifest",
        )
        events.append(
            CountyEvent(
                study=1,
                fips=row["fips"],
                county=row["county"],
                state=row["state"],
                event=entry["event"],
                role=row["analysis_role"],
                source=str(STUDY1_MANIFEST.relative_to(REPO_ROOT)),
            )
        )
    return events


def load_result_dir(study: int, directory: Path) -> list[CountyEvent]:
    _require(directory.is_dir(), f"missing county results directory {directory}")
    events: list[CountyEvent] = []
    for result_path in sorted(directory.glob("*/result.json")):
        entry = _load_json(result_path)["corpus_manifest_entry"]
        events.append(
            CountyEvent(
                study=study,
                fips=str(entry["fips"]).zfill(5),
                county=entry["county"],
                state=entry["state"],
                event=entry["event"],
                role=entry["analysis_role"],
                source=str(result_path.relative_to(REPO_ROOT)),
            )
        )
    return events


def load_all() -> list[CountyEvent]:
    events = load_study1() + load_result_dir(2, STUDY2_DIR) + load_result_dir(3, STUDY3_DIR)
    for study, expected in EXPECTED_COUNTS.items():
        count = sum(1 for e in events if e.study == study)
        _require(count == expected, f"Study {study} has {count} county-events; expected {expected}")
    for e in events:
        _require(e.role in (EPISODE, COVERAGE), f"unexpected analysis_role {e.role!r} for {e.fips}")
        statefp = e.fips[:2]
        _require(
            statefp not in NON_CONUS_STATEFP or statefp == PUERTO_RICO,
            f"county {e.fips} lies in {NON_CONUS_STATEFP.get(statefp, statefp)}, which this figure does not draw",
        )
    return events


# --------------------------------------------------------------------------- geometry


def ensure_layer(geo_dir: Path, layer: str) -> Path:
    shp = geo_dir / layer / f"{layer}.shp"
    if shp.is_file():
        return shp
    geo_dir.mkdir(parents=True, exist_ok=True)
    url = f"{CENSUS_BASE}{layer}.zip"
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(geo_dir / layer)
    _require(shp.is_file(), f"download of {layer} did not produce {shp}")
    return shp


def albers(lon: float, lat: float, lon0: float, lat0: float, phi1: float, phi2: float) -> tuple[float, float]:
    """Albers equal-area conic on a sphere (radius 6,371 km), returning kilometers."""

    radius = 6371.0
    lam, phi = math.radians(lon), math.radians(lat)
    lam0, phi0 = math.radians(lon0), math.radians(lat0)
    p1, p2 = math.radians(phi1), math.radians(phi2)
    n = 0.5 * (math.sin(p1) + math.sin(p2))
    c = math.cos(p1) ** 2 + 2.0 * n * math.sin(p1)
    rho0 = radius * math.sqrt(c - 2.0 * n * math.sin(phi0)) / n
    rho = radius * math.sqrt(c - 2.0 * n * math.sin(phi)) / n
    theta = n * (lam - lam0)
    return rho * math.sin(theta), rho0 - rho * math.cos(theta)


CONUS_PROJ = dict(lon0=-96.0, lat0=37.5, phi1=29.5, phi2=45.5)
PR_PROJ = dict(lon0=-66.5, lat0=18.2, phi1=17.5, phi2=18.9)


def rings_of(shape: shapefile.Shape) -> list[list[tuple[float, float]]]:
    points = shape.points
    parts = list(shape.parts) + [len(points)]
    return [points[parts[i] : parts[i + 1]] for i in range(len(parts) - 1)]


def project_rings(rings: Iterable[Sequence[tuple[float, float]]], proj: dict) -> list[list[tuple[float, float]]]:
    return [[albers(x, y, **proj) for x, y in ring] for ring in rings]


def compound_path(rings: list[list[tuple[float, float]]]) -> MplPath:
    vertices: list[tuple[float, float]] = []
    codes: list[int] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        vertices.extend(ring)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(ring) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(vertices, codes)


def centroid(rings: list[list[tuple[float, float]]]) -> tuple[float, float]:
    """Area-weighted centroid of the largest ring (the county's main body)."""

    best_area, best_cx, best_cy = 0.0, 0.0, 0.0
    for ring in rings:
        area = cx = cy = 0.0
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
            cross = x0 * y1 - x1 * y0
            area += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        if abs(area) > abs(best_area) and area != 0.0:
            best_area, best_cx, best_cy = area, cx / (3.0 * area), cy / (3.0 * area)
    return best_cx, best_cy


# --------------------------------------------------------------------------- drawing


def style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 7.2,
            "axes.titlesize": 8.2,
            "axes.labelsize": 8.0,
            "xtick.labelsize": 6.7,
            "ytick.labelsize": 6.7,
            "legend.fontsize": 6.4,
            "axes.linewidth": 0.7,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def draw_states(ax, reader: shapefile.Reader, keep, proj: dict) -> None:
    for rec, shape in zip(reader.iterRecords(fields=["STATEFP"]), reader.iterShapes()):
        if not keep(rec["STATEFP"]):
            continue
        path = compound_path(project_rings(rings_of(shape), proj))
        ax.add_patch(PathPatch(path, facecolor=LAND, edgecolor=STATE_EDGE, linewidth=0.45, zorder=1))


def county_geometry(reader: shapefile.Reader, wanted: set[str]) -> dict[str, list[list[tuple[float, float]]]]:
    found: dict[str, list[list[tuple[float, float]]]] = {}
    for rec, shape in zip(reader.iterRecords(fields=["GEOID"]), reader.iterShapes()):
        geoid = rec["GEOID"]
        if geoid in wanted:
            found[geoid] = rings_of(shape)
    missing = sorted(wanted - found.keys())
    _require(not missing, f"FIPS codes absent from {COUNTY_LAYER}: {missing}")
    return found


def draw_counties(ax, events: list[CountyEvent], geometry: dict, proj: dict, marker_size: float, label_offset_pt: float) -> None:
    by_fips: dict[str, list[CountyEvent]] = defaultdict(list)
    for e in events:
        by_fips[e.fips].append(e)
    for fips, group in by_fips.items():
        rings = project_rings(geometry[fips], proj)
        studies = sorted({e.study for e in group})
        roles = {e.role for e in group}
        primary = studies[0]
        fill = STUDY_COLOR[primary]
        ax.add_patch(PathPatch(compound_path(rings), facecolor=fill, edgecolor=fill, linewidth=0.5, alpha=0.55, zorder=2))
        cx, cy = centroid(rings)
        # Role: filled marker for episode evaluation, hollow for coverage only. A county with both
        # roles across its events (none in the current studies) would be drawn filled and flagged.
        hollow = roles == {COVERAGE}
        if len(studies) == 1:
            _marker(ax, cx, cy, primary, hollow, marker_size)
        else:
            # Shared county-event: concentric rings, outer for the earlier study.
            _require(hollow, f"county {fips} is shared by studies {studies} with mixed roles; unsupported")
            for k, study in enumerate(studies):
                _marker(ax, cx, cy, study, True, marker_size * (1.0 - 0.42 * k), zorder=6 + k)
        # Distinct events, not rows: a county-event shared by two studies is one event.
        n_events = len({e.event for e in group})
        if n_events > 1:
            text = ax.annotate(
                f"x{n_events}",
                (cx, cy),
                xytext=(label_offset_pt, -label_offset_pt),
                textcoords="offset points",
                fontsize=6.2,
                fontweight="bold",
                color=STUDY_COLOR[primary],
                ha="left",
                va="top",
                zorder=9,
            )
            text.set_path_effects([patheffects.withStroke(linewidth=1.6, foreground=WHITE)])


def _marker(ax, x: float, y: float, study: int, hollow: bool, size: float, zorder: int = 6) -> None:
    color = STUDY_COLOR[study]
    ax.plot(
        [x],
        [y],
        marker=STUDY_MARKER[study],
        linestyle="none",
        markersize=size,
        markerfacecolor=WHITE if hollow else color,
        markeredgecolor=color if hollow else WHITE,
        markeredgewidth=1.0 if hollow else 0.6,
        zorder=zorder,
        clip_on=False,
    )


def set_extent(ax, rings_iter, pad_frac: float = 0.02) -> None:
    xs = [x for rings in rings_iter for ring in rings for x, _ in ring]
    ys = [y for rings in rings_iter for ring in rings for _, y in ring]
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    ax.set_xlim(min(xs) - pad_frac * dx, max(xs) + pad_frac * dx)
    ax.set_ylim(min(ys) - pad_frac * dy, max(ys) + pad_frac * dy)
    ax.set_aspect("equal")
    ax.set_axis_off()


def build_figure(events: list[CountyEvent], geo_dir: Path):
    style()
    states = shapefile.Reader(str(ensure_layer(geo_dir, STATE_LAYER)))
    counties = shapefile.Reader(str(ensure_layer(geo_dir, COUNTY_LAYER)))
    geometry = county_geometry(counties, {e.fips for e in events})

    conus_events = [e for e in events if e.fips[:2] != PUERTO_RICO]
    pr_events = [e for e in events if e.fips[:2] == PUERTO_RICO]

    fig = plt.figure(figsize=FIGSIZE)
    fig.patch.set_facecolor(WHITE)
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_facecolor(WHITE)

    conus_rings = []
    for rec, shape in zip(states.iterRecords(fields=["STATEFP"]), states.iterShapes()):
        if rec["STATEFP"] not in NON_CONUS_STATEFP:
            conus_rings.append(project_rings(rings_of(shape), CONUS_PROJ))
    draw_states(ax, states, lambda fp: fp not in NON_CONUS_STATEFP, CONUS_PROJ)
    draw_counties(ax, conus_events, geometry, CONUS_PROJ, marker_size=5.2, label_offset_pt=2.6)
    set_extent(ax, conus_rings)

    # Puerto Rico inset, placed over the empty ocean south-west of Texas.
    inset = fig.add_axes([0.20, 0.06, 0.20, 0.13])
    inset.set_facecolor(WHITE)
    pr_rings = []
    for rec, shape in zip(states.iterRecords(fields=["STATEFP"]), states.iterShapes()):
        if rec["STATEFP"] == PUERTO_RICO:
            pr_rings.append(project_rings(rings_of(shape), PR_PROJ))
    draw_states(inset, states, lambda fp: fp == PUERTO_RICO, PR_PROJ)
    draw_counties(inset, pr_events, geometry, PR_PROJ, marker_size=5.2, label_offset_pt=2.6)
    set_extent(inset, pr_rings, pad_frac=0.10)
    for spine in inset.spines.values():
        spine.set_visible(True)
        spine.set_color(STATE_EDGE)
        spine.set_linewidth(0.6)
    inset.set_axis_on()
    inset.set_xticks([])
    inset.set_yticks([])
    inset.text(0.02, 0.96, "Puerto Rico", transform=inset.transAxes, fontsize=6.4, color=MUTED, ha="left", va="top")

    handles = [
        Line2D([], [], marker=STUDY_MARKER[s], linestyle="none", markersize=5.2, markerfacecolor=STUDY_COLOR[s], markeredgecolor=WHITE, markeredgewidth=0.6, label=STUDY_LABEL[s])
        for s in (1, 2, 3)
    ]
    handles += [
        Line2D([], [], marker="o", linestyle="none", markersize=5.2, markerfacecolor=MUTED, markeredgecolor=WHITE, markeredgewidth=0.6, label="Episode evaluation"),
        Line2D([], [], marker="o", linestyle="none", markersize=5.2, markerfacecolor=WHITE, markeredgecolor=MUTED, markeredgewidth=1.0, label="Coverage only"),
        Line2D([], [], linestyle="none", marker="$\\mathregular{x2}$", markersize=9, markeredgewidth=0.0, markerfacecolor=MUTED, markeredgecolor=MUTED, label="Two events in the county"),
    ]
    legend = ax.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0.005, 0.02),
        ncol=1,
        frameon=True,
        framealpha=1.0,
        edgecolor=STATE_EDGE,
        fancybox=False,
        borderpad=0.6,
        handletextpad=0.5,
        labelspacing=0.45,
    )
    legend.get_frame().set_linewidth(0.6)
    return fig


# --------------------------------------------------------------------------- entry point


def print_inventory(events: list[CountyEvent]) -> None:
    for study in (1, 2, 3):
        rows = [e for e in events if e.study == study]
        n_ep = sum(1 for e in rows if e.role == EPISODE)
        n_cov = len(rows) - n_ep
        print(f"Study {study}: {len(rows)} county-events ({n_ep} episode evaluation, {n_cov} coverage only)")
        for e in rows:
            print(f"  {e.fips}  {e.county}, {e.state}  {e.event}  [{e.role}]  <- {e.source}")
    repeats = defaultdict(list)
    for e in events:
        repeats[e.fips].append(e)
    for fips, group in sorted(repeats.items()):
        if len(group) > 1:
            print(f"repeated county {fips} {group[0].county}, {group[0].state}: " + "; ".join(f"S{e.study} {e.event}" for e in group))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--geo-dir", type=Path, default=Path(tempfile.gettempdir()) / "sentinel-census-geodata", help="directory holding (or receiving) the Census cb_2023_*_20m shapefiles")
    parser.add_argument("--out-dir", type=Path, default=HERE, help="directory for the PDF and PNG")
    args = parser.parse_args()

    events = load_all()
    print_inventory(events)
    fig = build_figure(events, args.geo_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    pdf = args.out_dir / PDF_NAME
    png = args.out_dir / PNG_NAME
    fig.savefig(pdf, format="pdf", bbox_inches="tight", pad_inches=0.02, facecolor=WHITE)
    fig.savefig(png, format="png", dpi=PNG_DPI, bbox_inches="tight", pad_inches=0.02, facecolor=WHITE)
    print(f"wrote {pdf}\nwrote {png}")


if __name__ == "__main__":
    main()
