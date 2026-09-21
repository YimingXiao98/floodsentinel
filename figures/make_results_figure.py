#!/usr/bin/env python3
"""Render the audited acquisition--episode-observation-delay trade-off.

This figure is deliberately narrow: it visualizes acquisition counts and
episode observation delays from the clean, deterministic study artifacts.  It
does not visualize same-series classification scores, message burden, or
human workload.  Its only numerical inputs are::

    eval/results/study_v2/aggregate.json
    eval/results/study_v2/tables.json

The generator fails closed if either artifact is absent, has an unexpected
schema, is internally inconsistent, or does not contain the complete offset
enumeration described by the study protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Avoid a home-directory cache and keep font discovery stable in runners.
os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "sentinel-paper-mpl-cache")
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402
from PIL import Image, ImageStat  # noqa: E402


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
STUDY_DIR = REPO_ROOT / "eval" / "results" / "study_v2"
AGGREGATE_PATH = STUDY_DIR / "aggregate.json"
TABLES_PATH = STUDY_DIR / "tables.json"

PDF_NAME = "fig_acquisition_delay_tradeoff.pdf"
PNG_NAME = "fig_acquisition_delay_tradeoff.png"
PNG_DPI = 450
FIGSIZE = (7.4, 7.0)

AGGREGATE_SCHEMA = "sentinel-study-aggregate/3"
TABLES_SCHEMA = "sentinel-study-tables/3"
EXPECTED_FIXED_HOURS = (1.0, 2.0, 3.0, 6.0, 12.0)
EXPECTED_OFFSET_STEP_HOURS = 0.25

# Colorblind-safe accents. Shape, fill, and line style duplicate color coding.
INK = "#202124"
MUTED = "#5F6368"
GRID = "#D9DDE1"
PANEL_EDGE = "#B7BDC3"
FIXED = "#7A828A"
FIXED_DARK = "#4F5962"
ADAPTIVE = "#0072B2"
MATCHED = "#D55E00"
WHITE = "#FFFFFF"
COMPLETE = "#E7F3FA"
INCOMPLETE = "#F6C6A7"


@dataclass(frozen=True)
class StudyData:
    """Validated table rows used by the figure."""

    aggregate: Mapping[str, Any]
    primary: tuple[Mapping[str, Any], ...]
    schedule_rows: tuple[Mapping[str, Any], ...]
    fixed_ranges: tuple[Mapping[str, Any], ...]
    matched_ranges: tuple[Mapping[str, Any], ...]
    matched_selected: tuple[Mapping[str, Any], ...]
    evaluable_fips: tuple[str, ...]
    coverage_only_names: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(
            f"required clean study artifact is missing: {path}; "
            "finish eval/run_study.py before rendering"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _number(value: Any, label: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"{label} must be finite")
    return result


def _integer(value: Any, label: str) -> int:
    number = _number(value, label)
    assert number is not None
    if number < 0 or not number.is_integer():
        raise RuntimeError(f"{label} must be a non-negative integer")
    return int(number)


def _list_of_objects(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise RuntimeError(f"{label} must be a list of objects")
    return value


def _lag_values(row: Mapping[str, Any], label: str) -> tuple[float, ...]:
    raw = row.get("schedule_flood_stage_observation_lag_hours")
    if not isinstance(raw, list):
        raise RuntimeError(f"{label}.schedule_flood_stage_observation_lag_hours must be a list")
    values = []
    for index, value in enumerate(raw):
        parsed = _number(value, f"{label}.lag[{index}]", allow_none=True)
        if parsed is not None:
            _require(parsed >= 0, f"{label}.lag[{index}] must be non-negative")
            values.append(parsed)
    return tuple(values)


def _validate_schedule_row(row: Mapping[str, Any], label: str) -> None:
    for field in ("county", "state", "fips", "policy", "analysis_role"):
        _require(isinstance(row.get(field), str) and bool(row[field]), f"{label}.{field} is required")
    acquisitions = _integer(row.get("acquisitions"), f"{label}.acquisitions")
    _require(acquisitions > 0, f"{label}.acquisitions must be positive")
    total = _integer(row.get("observed_flood_episodes"), f"{label}.observed_flood_episodes")
    observed = _integer(
        row.get("episodes_observed_by_schedule"),
        f"{label}.episodes_observed_by_schedule",
    )
    missed = _integer(
        row.get("episodes_not_observed_by_schedule"),
        f"{label}.episodes_not_observed_by_schedule",
    )
    _require(observed + missed == total, f"{label} episode counts do not partition the reference episodes")
    lags = _lag_values(row, label)
    _require(len(lags) == observed, f"{label} has {len(lags)} finite delays but reports {observed} observed episodes")


def _triplet(row: Mapping[str, Any], field: str, label: str) -> tuple[float, float, float]:
    value = row.get(field)
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}.{field} must be a range object")
    parts = tuple(_number(value.get(key), f"{label}.{field}.{key}") for key in ("min", "median", "max"))
    assert all(item is not None for item in parts)
    low, middle, high = (float(item) for item in parts)
    _require(0 <= low <= middle <= high, f"{label}.{field} is not an ordered non-negative range")
    return low, middle, high


def _require_null_triplet(row: Mapping[str, Any], field: str, label: str) -> None:
    value = row.get(field)
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}.{field} must be a range object")
    _require(
        all(value.get(key) is None for key in ("min", "median", "max")),
        f"{label}.{field} must be null for a coverage-only county",
    )


def _computed_triplet(values: Iterable[float]) -> tuple[float, float, float]:
    materialized = [float(value) for value in values]
    if not materialized:
        raise RuntimeError("cannot summarize an empty numeric sequence")
    return min(materialized), statistics.median(materialized), max(materialized)


def _same_triplet(
    actual: tuple[float, float, float],
    expected: tuple[float, float, float],
    label: str,
) -> None:
    if any(abs(left - right) > 0.00011 for left, right in zip(actual, expected)):
        raise RuntimeError(f"{label} range {actual} disagrees with schedule rows {expected}")


def load_study_data() -> StudyData:
    """Load, cross-check, and return the two clean study artifacts."""

    aggregate = _load_object(AGGREGATE_PATH)
    tables = _load_object(TABLES_PATH)
    _require(aggregate.get("schema") == AGGREGATE_SCHEMA, f"unexpected aggregate schema: {aggregate.get('schema')!r}")
    _require(tables.get("schema") == TABLES_SCHEMA, f"unexpected tables schema: {tables.get('schema')!r}")

    required_tables = {
        "county_primary",
        "schedule_by_county_and_offset",
        "fixed_cadence_offset_ranges",
        "matched_budget_offset_ranges",
        "matched_budget_selected",
    }
    missing = required_tables - set(tables)
    _require(not missing, f"tables artifact is missing sections: {sorted(missing)}")

    primary = _list_of_objects(tables["county_primary"], "county_primary")
    schedule_rows = _list_of_objects(
        tables["schedule_by_county_and_offset"], "schedule_by_county_and_offset"
    )
    fixed_ranges = _list_of_objects(
        tables["fixed_cadence_offset_ranges"], "fixed_cadence_offset_ranges"
    )
    matched_ranges = _list_of_objects(
        tables["matched_budget_offset_ranges"], "matched_budget_offset_ranges"
    )
    matched_selected = _list_of_objects(
        tables["matched_budget_selected"], "matched_budget_selected"
    )

    for index, row in enumerate(primary):
        _validate_schedule_row(row, f"county_primary[{index}]")
        _require(row["policy"] == "adaptive", f"county_primary[{index}] is not adaptive")
    for index, row in enumerate(schedule_rows):
        _validate_schedule_row(row, f"schedule_by_county_and_offset[{index}]")
    for index, row in enumerate(matched_selected):
        _validate_schedule_row(row, f"matched_budget_selected[{index}]")
        _require(
            row["policy"] == "matched_budget_selected",
            f"matched_budget_selected[{index}] has an unexpected policy",
        )

    primary_by_fips = {row["fips"]: row for row in primary}
    _require(len(primary_by_fips) == len(primary), "county_primary contains duplicate FIPS rows")
    n_counties = _integer(aggregate.get("n_counties"), "aggregate.n_counties")
    _require(n_counties == len(primary), "aggregate county count disagrees with county_primary")
    _require(n_counties == 5, f"this frozen five-county figure expected 5 counties, found {n_counties}")

    evaluable = tuple(
        row["fips"] for row in primary if row["analysis_role"] == "episode_evaluation"
    )
    coverage_only = tuple(
        row for row in primary if row["analysis_role"] == "coverage_only"
    )
    _require(len(evaluable) == 4, f"expected 4 episode-evaluation counties, found {len(evaluable)}")
    _require(len(coverage_only) == 1, f"expected 1 coverage-only county, found {len(coverage_only)}")
    _require(
        _integer(aggregate.get("n_episode_evaluation_counties"), "aggregate.n_episode_evaluation_counties")
        == len(evaluable),
        "aggregate episode-evaluation county count disagrees with table roles",
    )
    _require(
        _integer(aggregate.get("n_coverage_only_counties"), "aggregate.n_coverage_only_counties")
        == len(coverage_only),
        "aggregate coverage-only county count disagrees with table roles",
    )

    adaptive_acquisitions = sum(_integer(row["acquisitions"], "adaptive acquisitions") for row in primary)
    _require(
        adaptive_acquisitions
        == _integer(aggregate.get("adaptive_acquisitions"), "aggregate.adaptive_acquisitions"),
        "aggregate adaptive acquisition count disagrees with county rows",
    )
    evaluable_primary = [primary_by_fips[fips] for fips in evaluable]
    episode_total = sum(_integer(row["observed_flood_episodes"], "reference episodes") for row in evaluable_primary)
    episode_observed = sum(
        _integer(row["episodes_observed_by_schedule"], "adaptive observed episodes")
        for row in evaluable_primary
    )
    _require(episode_total > 0, "no evaluable reference episodes are present")
    _require(
        episode_total == _integer(aggregate.get("observed_flood_episodes"), "aggregate.observed_flood_episodes"),
        "aggregate reference episode count disagrees with county rows",
    )
    _require(
        episode_observed
        == _integer(
            aggregate.get("episodes_observed_by_adaptive_schedule"),
            "aggregate.episodes_observed_by_adaptive_schedule",
        ),
        "aggregate adaptive episode count disagrees with county rows",
    )

    schedule_by_fips: dict[str, list[Mapping[str, Any]]] = {fips: [] for fips in primary_by_fips}
    for row in schedule_rows:
        _require(row["fips"] in schedule_by_fips, f"schedule row has unknown FIPS {row['fips']}")
        schedule_by_fips[row["fips"]].append(row)

    fixed_range_keys: set[tuple[str, float]] = set()
    for index, range_row in enumerate(fixed_ranges):
        label = f"fixed_cadence_offset_ranges[{index}]"
        fips = range_row.get("fips")
        cadence_value = _number(range_row.get("fixed_interval_hours"), f"{label}.fixed_interval_hours")
        assert cadence_value is not None
        cadence = float(cadence_value)
        _require(fips in primary_by_fips, f"{label} has unknown FIPS {fips!r}")
        _require(cadence in EXPECTED_FIXED_HOURS, f"{label} has unexpected cadence {cadence}")
        key = (str(fips), cadence)
        _require(key not in fixed_range_keys, f"duplicate fixed range for {key}")
        fixed_range_keys.add(key)
        members = [
            row
            for row in schedule_by_fips[str(fips)]
            if str(row["policy"]).startswith("fixed_")
            and float(_number(row.get("fixed_interval_hours"), "fixed interval")) == cadence
        ]
        expected_offsets = int(round(cadence / EXPECTED_OFFSET_STEP_HOURS))
        _require(len(members) == expected_offsets, f"{label} expected {expected_offsets} offsets, found {len(members)}")
        _require(
            _integer(range_row.get("n_phase_offsets"), f"{label}.n_phase_offsets") == len(members),
            f"{label} offset count disagrees with schedule rows",
        )
        _same_triplet(
            _triplet(range_row, "acquisitions", label),
            _computed_triplet(float(row["acquisitions"]) for row in members),
            f"{label}.acquisitions",
        )
        member_lags = [lag for row in members for lag in _lag_values(row, "fixed member")]
        if primary_by_fips[str(fips)]["analysis_role"] == "episode_evaluation":
            _same_triplet(
                _triplet(range_row, "flood_stage_observation_lag_hours", label),
                _computed_triplet(member_lags),
                f"{label}.flood_stage_observation_lag_hours",
            )
        else:
            _require(not member_lags, f"{label} coverage-only rows unexpectedly contain delays")
            _require_null_triplet(range_row, "flood_stage_observation_lag_hours", label)
    expected_fixed_keys = {(fips, cadence) for fips in primary_by_fips for cadence in EXPECTED_FIXED_HOURS}
    _require(fixed_range_keys == expected_fixed_keys, "fixed cadence ranges are incomplete")

    matched_range_by_fips: dict[str, Mapping[str, Any]] = {}
    for index, range_row in enumerate(matched_ranges):
        label = f"matched_budget_offset_ranges[{index}]"
        fips = str(range_row.get("fips"))
        _require(fips in primary_by_fips, f"{label} has unknown FIPS {fips!r}")
        _require(fips not in matched_range_by_fips, f"duplicate equal-budget range for {fips}")
        members = [
            row for row in schedule_by_fips[fips] if str(row["policy"]).startswith("matched_budget_offset_")
        ]
        _require(bool(members), f"{label} has no offset rows")
        interval_value = _number(range_row.get("fixed_interval_hours"), f"{label}.fixed_interval_hours")
        assert interval_value is not None
        expected_offsets = int(math.ceil(float(interval_value) / EXPECTED_OFFSET_STEP_HOURS - 1e-10))
        _require(len(members) == expected_offsets, f"{label} expected {expected_offsets} offsets, found {len(members)}")
        _require(
            _integer(range_row.get("n_phase_offsets"), f"{label}.n_phase_offsets") == len(members),
            f"{label} offset count disagrees with schedule rows",
        )
        _same_triplet(
            _triplet(range_row, "realised_acquisitions", label),
            _computed_triplet(float(row["acquisitions"]) for row in members),
            f"{label}.realised_acquisitions",
        )
        member_lags = [lag for row in members for lag in _lag_values(row, "equal-budget member")]
        if primary_by_fips[fips]["analysis_role"] == "episode_evaluation":
            _same_triplet(
                _triplet(range_row, "flood_stage_observation_lag_hours", label),
                _computed_triplet(member_lags),
                f"{label}.flood_stage_observation_lag_hours",
            )
        else:
            _require(not member_lags, f"{label} coverage-only rows unexpectedly contain delays")
            _require_null_triplet(range_row, "flood_stage_observation_lag_hours", label)
        selected_members = [row for row in members if row.get("selected_nearest_realised_acquisition_count") is True]
        _require(len(selected_members) == 1, f"{label} must mark exactly one selected offset")
        matched_range_by_fips[fips] = range_row
    _require(set(matched_range_by_fips) == set(primary_by_fips), "equal-budget ranges are incomplete")

    selected_by_fips = {row["fips"]: row for row in matched_selected}
    _require(len(selected_by_fips) == len(matched_selected), "selected equal-budget rows contain duplicate FIPS")
    _require(set(selected_by_fips) == set(primary_by_fips), "selected equal-budget rows are incomplete")
    for fips, selected in selected_by_fips.items():
        marked = [
            row
            for row in schedule_by_fips[fips]
            if str(row["policy"]).startswith("matched_budget_offset_")
            and row.get("selected_nearest_realised_acquisition_count") is True
        ]
        _require(len(marked) == 1, f"{fips} does not have exactly one marked equal-budget offset")
        chosen = marked[0]
        _require(selected["acquisitions"] == chosen["acquisitions"], f"{fips} selected acquisition count disagrees")
        _require(_lag_values(selected, "selected") == _lag_values(chosen, "marked offset"), f"{fips} selected delays disagree")

    return StudyData(
        aggregate=aggregate,
        primary=tuple(primary),
        schedule_rows=tuple(schedule_rows),
        fixed_ranges=tuple(fixed_ranges),
        matched_ranges=tuple(matched_ranges),
        matched_selected=tuple(matched_selected),
        evaluable_fips=evaluable,
        coverage_only_names=tuple(str(row["county"]) for row in coverage_only),
    )


def _range_marker(
    ax: Axes,
    x_values: tuple[float, float, float],
    y_values: tuple[float, float, float],
    *,
    color: str,
    marker: str,
    marker_face: str,
    marker_size: float,
    zorder: float,
) -> None:
    x_low, x_mid, x_high = x_values
    y_low, y_mid, y_high = y_values
    ax.errorbar(
        [x_mid],
        [y_mid],
        xerr=[[x_mid - x_low], [x_high - x_mid]],
        yerr=[[y_mid - y_low], [y_high - y_mid]],
        fmt=marker,
        markersize=marker_size,
        markerfacecolor=marker_face,
        markeredgecolor=color,
        markeredgewidth=0.9,
        ecolor=color,
        elinewidth=1.05,
        capsize=2.0,
        capthick=0.9,
        zorder=zorder,
    )


def _row_median_marker(
    ax: Axes,
    row: Mapping[str, Any],
    *,
    color: str,
    marker: str,
    marker_face: str,
    marker_size: float,
    zorder: float,
) -> None:
    lags = _lag_values(row, str(row["policy"]))
    if not lags:
        return
    _range_marker(
        ax,
        (float(row["acquisitions"]),) * 3,
        _computed_triplet(lags),
        color=color,
        marker=marker,
        marker_face=marker_face,
        marker_size=marker_size,
        zorder=zorder,
    )


def _observation_counts(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """Return observed and possible episode-by-run outcomes."""

    observed = sum(int(row["episodes_observed_by_schedule"]) for row in rows)
    possible = sum(int(row["observed_flood_episodes"]) for row in rows)
    _require(possible > 0, "completeness group has no reference episodes")
    _require(0 <= observed <= possible, "invalid completeness group")
    return observed, possible


def _blend_hex(low: str, high: str, fraction: float) -> str:
    """Linearly blend two fixed colors without adding a plotting dependency."""

    fraction = min(1.0, max(0.0, float(fraction)))
    low_rgb = tuple(int(low[index : index + 2], 16) for index in (1, 3, 5))
    high_rgb = tuple(int(high[index : index + 2], 16) for index in (1, 3, 5))
    rgb = tuple(round(left + (right - left) * fraction) for left, right in zip(low_rgb, high_rgb))
    return "#" + "".join(f"{value:02X}" for value in rgb)


def _draw_completeness_matrix(
    ax: Axes,
    *,
    data: StudyData,
    primary_by_fips: Mapping[str, Mapping[str, Any]],
    selected_by_fips: Mapping[str, Mapping[str, Any]],
    fixed_by_fips: Mapping[str, Sequence[Mapping[str, Any]]],
    matched_by_fips: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    """Show every null delay outcome with exact, comparable denominators."""

    column_labels = (
        "Adaptive",
        "Fixed\n1 h",
        "Fixed\n2 h",
        "Fixed\n3 h",
        "Fixed\n6 h",
        "Fixed\n12 h",
        "Equal-budget\nall offsets",
        "Equal-budget\nselected",
    )
    cells: list[list[tuple[int, int]]] = []
    row_labels: list[str] = []
    for fips in data.evaluable_fips:
        primary = primary_by_fips[fips]
        selected = selected_by_fips[fips]
        fixed_rows = fixed_by_fips[fips]
        by_cadence = {
            cadence: [
                row
                for row in fixed_rows
                if float(row["fixed_interval_hours"]) == cadence
            ]
            for cadence in EXPECTED_FIXED_HOURS
        }
        cells.append(
            [
                _observation_counts([primary]),
                *(_observation_counts(by_cadence[cadence]) for cadence in EXPECTED_FIXED_HOURS),
                _observation_counts(matched_by_fips[fips]),
                _observation_counts([selected]),
            ]
        )
        row_labels.append(f"{primary['county']}, {primary['state']}")

    n_rows = len(cells)
    n_columns = len(column_labels)
    ax.set_xlim(-1.52, n_columns)
    # Reserve an internal header band.  Keeping the column labels inside this
    # axis prevents them from colliding with the shared acquisition-axis label
    # above the matrix.
    ax.set_ylim(n_rows, -0.85)
    ax.axis("off")
    for row_index, (label, row) in enumerate(zip(row_labels, cells)):
        ax.text(
            -0.08,
            row_index + 0.5,
            label,
            ha="right",
            va="center",
            fontsize=6.0,
            color=INK,
            fontweight="bold",
        )
        for column_index, (observed, possible) in enumerate(row):
            proportion = observed / possible
            missing_fraction = 1.0 - proportion
            facecolor = COMPLETE if missing_fraction == 0 else _blend_hex(WHITE, INCOMPLETE, 0.38 + 0.62 * missing_fraction)
            ax.add_patch(
                Rectangle(
                    (column_index, row_index),
                    1,
                    1,
                    facecolor=facecolor,
                    edgecolor=WHITE,
                    linewidth=1.5,
                )
            )
            text_color = INK if missing_fraction < 0.68 else "#6F281D"
            ax.text(
                column_index + 0.5,
                row_index + 0.5,
                f"{observed}/{possible}",
                ha="center",
                va="center",
                fontsize=5.8,
                color=text_color,
                fontweight="bold" if missing_fraction else "normal",
            )
    for column_index, label in enumerate(column_labels):
        ax.text(
            column_index + 0.5,
            -0.12,
            label,
            ha="center",
            va="bottom",
            fontsize=5.65,
            color=MUTED,
            linespacing=1.05,
        )
    ax.text(
        -1.50,
        -0.12,
        "Completeness\nobserved / possible\n(episode × run)",
        ha="left",
        va="bottom",
        fontsize=5.9,
        color=INK,
        fontweight="bold",
        linespacing=1.02,
    )


def build_figure(data: StudyData) -> Figure:
    """Build the four-panel trade-off figure from validated rows."""

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

    primary_by_fips = {row["fips"]: row for row in data.primary}
    selected_by_fips = {row["fips"]: row for row in data.matched_selected}
    fixed_by_fips: dict[str, list[Mapping[str, Any]]] = {fips: [] for fips in data.evaluable_fips}
    matched_by_fips: dict[str, list[Mapping[str, Any]]] = {fips: [] for fips in data.evaluable_fips}
    for row in data.schedule_rows:
        fips = row["fips"]
        if fips not in fixed_by_fips:
            continue
        if str(row["policy"]).startswith("fixed_"):
            fixed_by_fips[fips].append(row)
        elif str(row["policy"]).startswith("matched_budget_offset_"):
            matched_by_fips[fips].append(row)
    fixed_ranges_by_fips: dict[str, list[Mapping[str, Any]]] = {fips: [] for fips in data.evaluable_fips}
    for row in data.fixed_ranges:
        if row["fips"] in fixed_ranges_by_fips:
            fixed_ranges_by_fips[row["fips"]].append(row)

    all_x: list[float] = []
    all_y: list[float] = []
    for fips in data.evaluable_fips:
        rows = [primary_by_fips[fips], selected_by_fips[fips], *fixed_by_fips[fips], *matched_by_fips[fips]]
        all_x.extend(float(row["acquisitions"]) for row in rows)
        all_y.extend(lag for row in rows for lag in _lag_values(row, str(row["policy"])))
    _require(bool(all_y), "no observed episode delays are available for plotting")
    x_span = max(all_x) - min(all_x)
    x_limits = (max(0.0, min(all_x) - 0.055 * x_span), max(all_x) + 0.055 * x_span)
    y_upper = max(all_y)
    y_limits = (0.0, max(1.0, y_upper * 1.13 + 0.15))

    fig = plt.figure(figsize=FIGSIZE)
    fig.patch.set_facecolor(WHITE)
    panel_grid = fig.add_gridspec(
        2,
        2,
        left=0.090,
        right=0.985,
        bottom=0.365,
        top=0.830,
        wspace=0.16,
        hspace=0.34,
    )
    axes = (
        (fig.add_subplot(panel_grid[0, 0]), fig.add_subplot(panel_grid[0, 1])),
        (fig.add_subplot(panel_grid[1, 0]), fig.add_subplot(panel_grid[1, 1])),
    )
    for ax in (axes[0][1], axes[1][0], axes[1][1]):
        ax.sharex(axes[0][0])
        ax.sharey(axes[0][0])

    for ax, fips in zip((axis for row in axes for axis in row), data.evaluable_fips):
        primary = primary_by_fips[fips]
        fixed_rows = fixed_by_fips[fips]
        matched_rows = matched_by_fips[fips]
        selected = selected_by_fips[fips]

        ax.set_facecolor(WHITE)
        ax.grid(axis="y", color=GRID, linewidth=0.55, alpha=0.75, zorder=0)
        for spine in ax.spines.values():
            spine.set_color(PANEL_EDGE)
            spine.set_linewidth(0.65)

        # Every dot is an observed reference episode under one prespecified
        # offset. The quantitative matrix below preserves every null delay.
        for row in fixed_rows:
            lags = _lag_values(row, str(row["policy"]))
            if lags:
                ax.scatter(
                    [float(row["acquisitions"])] * len(lags),
                    lags,
                    s=8,
                    marker="o",
                    facecolors=FIXED,
                    edgecolors="none",
                    alpha=0.28,
                    zorder=2,
                )
        for row in matched_rows:
            lags = _lag_values(row, str(row["policy"]))
            if lags:
                ax.scatter(
                    [float(row["acquisitions"])] * len(lags),
                    lags,
                    s=17,
                    marker="o",
                    facecolors="none",
                    edgecolors=MATCHED,
                    linewidths=0.7,
                    alpha=0.75,
                    zorder=4,
                )

        for range_row in sorted(fixed_ranges_by_fips[fips], key=lambda row: float(row["fixed_interval_hours"])):
            _range_marker(
                ax,
                _triplet(range_row, "acquisitions", "fixed range"),
                _triplet(range_row, "flood_stage_observation_lag_hours", "fixed range"),
                color=FIXED_DARK,
                marker="o",
                marker_face=WHITE,
                marker_size=3.1,
                zorder=3,
            )
            x_mid = _triplet(range_row, "acquisitions", "fixed range")[1]
            y_high = _triplet(range_row, "flood_stage_observation_lag_hours", "fixed range")[2]
            ax.annotate(
                f"{float(range_row['fixed_interval_hours']):g} h",
                (x_mid, y_high),
                xytext=(0, 4),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=5.5,
                color=FIXED_DARK,
                zorder=7,
            )

        _row_median_marker(
            ax,
            primary,
            color=ADAPTIVE,
            marker="D",
            marker_face=ADAPTIVE,
            marker_size=5.2,
            zorder=7,
        )
        _row_median_marker(
            ax,
            selected,
            color=MATCHED,
            marker="s",
            marker_face=MATCHED,
            marker_size=5.0,
            zorder=7,
        )

        event = str(primary.get("event") or "event window")
        county = str(primary["county"])
        state = str(primary["state"])
        episodes = int(primary["observed_flood_episodes"])
        ax.set_title(
            f"{county} County, {state} · {event}\n{episodes} reference episode{'s' if episodes != 1 else ''}",
            loc="left",
            color=INK,
            fontweight="bold",
            pad=5,
        )
        ax.set_xlim(*x_limits)
        ax.set_ylim(*y_limits)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=6, min_n_ticks=4))
        ax.tick_params(axis="both", colors=MUTED, width=0.6, length=2.5)

    for ax in axes[0]:
        ax.tick_params(labelbottom=False)
    for ax in (axes[0][1], axes[1][1]):
        ax.tick_params(labelleft=False)

    fig.text(
        0.537,
        0.314,
        "Acquisitions during the 312-h county window",
        ha="center",
        va="center",
        fontsize=8.0,
        color=INK,
    )
    fig.text(
        0.020,
        0.595,
        "Onset-to-first flood-stage observation (h)",
        ha="center",
        va="center",
        rotation=90,
        fontsize=8.0,
        color=INK,
    )

    matrix_ax = fig.add_axes((0.090, 0.070, 0.895, 0.205))
    _draw_completeness_matrix(
        matrix_ax,
        data=data,
        primary_by_fips=primary_by_fips,
        selected_by_fips=selected_by_fips,
        fixed_by_fips=fixed_by_fips,
        matched_by_fips=matched_by_fips,
    )

    fig.suptitle(
        "Acquisition–episode-observation-delay trade-off",
        x=0.075,
        y=0.982,
        ha="left",
        va="top",
        fontsize=10.8,
        fontweight="bold",
        color=INK,
    )
    fig.text(
        0.075,
        0.949,
        "Phase-adaptive replay versus complete fixed-clock offsets and county-specific equal-budget fixed schedules",
        ha="left",
        va="top",
        fontsize=7.1,
        color=MUTED,
    )

    legend_handles = [
        Line2D([], [], marker="D", linestyle="none", markersize=5.2, markerfacecolor=ADAPTIVE, markeredgecolor=ADAPTIVE, label="Adaptive"),
        Line2D([], [], marker="o", linestyle="-", linewidth=1.0, markersize=3.3, markerfacecolor=WHITE, markeredgecolor=FIXED_DARK, color=FIXED_DARK, label="Fixed offsets; median and min–max"),
        Line2D([], [], marker="o", linestyle="none", markersize=4.1, markerfacecolor=WHITE, markeredgecolor=MATCHED, color=MATCHED, label="Equal-budget offsets"),
        Line2D([], [], marker="s", linestyle="none", markersize=5.0, markerfacecolor=MATCHED, markeredgecolor=MATCHED, label="Equal-budget selected"),
    ]
    fig.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.925),
        ncol=4,
        frameon=False,
        handlelength=1.25,
        columnspacing=1.25,
        handletextpad=0.45,
    )

    coverage_note = ", ".join(data.coverage_only_names)
    source_note = (
        "Delay panels include observed episode × run outcomes only; the matrix gives observed/all possible outcomes, so misses remain explicit. "
        "Bars span min–max and centers are medians. "
        "The filled orange square is selected only by nearest realized acquisition count (earliest-offset tie-break). "
        f"{coverage_note} is coverage-only and is omitted. Delays measure threshold-observation timing, not detector accuracy. "
        f"Sources: {TABLES_SCHEMA} and {AGGREGATE_SCHEMA}."
    )
    fig.text(
        0.075,
        0.006,
        source_note,
        ha="left",
        va="bottom",
        fontsize=5.25,
        color=MUTED,
        wrap=True,
    )
    return fig


def write_outputs(output_dir: Path, data: StudyData) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / PDF_NAME
    png_path = output_dir / PNG_NAME
    fig = build_figure(data)
    metadata: Mapping[str, object] = {
        "Title": "Acquisition-episode-observation-delay trade-off",
        "Subject": "Phase-adaptive, fixed-offset, and equal-budget fixed replay comparison",
        "Creator": "paper/figures/make_results_figure.py",
        "Producer": "Matplotlib",
        "CreationDate": None,
        "ModDate": None,
    }
    fig.savefig(pdf_path, format="pdf", metadata=metadata)
    fig.savefig(
        png_path,
        format="png",
        dpi=PNG_DPI,
        metadata={"Software": "paper/figures/make_results_figure.py"},
    )
    plt.close(fig)
    return pdf_path, png_path


def verify_outputs(pdf_path: Path, png_path: Path) -> None:
    if not pdf_path.is_file() or not pdf_path.read_bytes().startswith(b"%PDF"):
        raise RuntimeError(f"missing or invalid vector PDF: {pdf_path}")
    if pdf_path.stat().st_size < 15_000:
        raise RuntimeError(f"unexpectedly small vector PDF: {pdf_path}")
    if not png_path.is_file() or not png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"missing or invalid PNG: {png_path}")
    with Image.open(png_path) as image:
        expected = (round(FIGSIZE[0] * PNG_DPI), round(FIGSIZE[1] * PNG_DPI))
        if image.size != expected:
            raise RuntimeError(f"PNG dimensions {image.size} != expected {expected}")
        if image.mode not in {"RGB", "RGBA"}:
            raise RuntimeError(f"unexpected PNG mode: {image.mode}")
        extrema = ImageStat.Stat(image.convert("RGB")).extrema
        if all(low == high for low, high in extrema):
            raise RuntimeError("PNG is blank or single-colored")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=HERE,
        help="directory for the PDF and PNG (default: script directory)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the clean source artifacts and existing products without rendering",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    data = load_study_data()
    pdf_path = args.output_dir / PDF_NAME
    png_path = args.output_dir / PNG_NAME
    if not args.check_only:
        pdf_path, png_path = write_outputs(args.output_dir, data)
    verify_outputs(pdf_path, png_path)
    print(f"aggregate sha256: {_sha256(AGGREGATE_PATH)}")
    print(f"tables sha256:    {_sha256(TABLES_PATH)}")
    print(f"PDF sha256:       {_sha256(pdf_path)} ({pdf_path.stat().st_size:,} B)")
    print(f"PNG sha256:       {_sha256(png_path)} ({png_path.stat().st_size:,} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
