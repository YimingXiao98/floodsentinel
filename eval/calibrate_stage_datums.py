"""Calibrate USGS gage-height series to NOAA/NWPS operational stage.

The frozen Duval replay corpus joins USGS parameter 00065 (gage height above a
station-specific zero) to NOAA/NWPS flood thresholds.  Those quantities have
the same unit but, at the Duval tidal sites, not the same vertical reference.

This utility has three deliberately separate operations:

``build``
    Fetch official NOAA historical crest records and official USGS IV records,
    retain only exact-time pairs outside the study window, and freeze the
    station-specific offsets in a calibration artifact.

``verify``
    Recompute every offset and site median from a frozen artifact without
    network access and enforce the prespecified offsets.

``apply``
    Apply the verified transforms to a corpus copy.  Each source value is kept
    as ``stage_raw`` and ``stage`` becomes the datum-aligned value used by the
    replay.  Applying the operation twice is idempotent.

NOAA's historical crest ``occurredTime`` values for these gauges are encoded
with a trailing ``Z`` but follow the station wall clock.  The NWPS payload also
identifies the station zone as ``EST5EDT``.  We therefore interpret the clock
fields in ``America/New_York`` before matching them to timezone-aware USGS
observations.  The frozen artifact records both the reported and aligned time
for every pair, making this otherwise easy-to-miss convention auditable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


SCHEMA = "sentinel-stage-datum-calibration/1"
NOAA_GAUGE_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{lid}"
USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
USER_AGENT = "OpenClaw-disaster-sentinel-datum-calibration/1.0"

CALIBRATION_QUERY_START = "2017-07-01"
CALIBRATION_QUERY_END = "2017-11-01"
CALIBRATION_START_UTC = "2017-07-01T04:00:00Z"
CALIBRATION_END_UTC = "2017-11-01T04:00:00Z"
STUDY_START_UTC = "2017-09-08T04:00:00Z"
STUDY_END_UTC = "2017-09-21T04:00:00Z"
STATION_TIME_ZONE = "America/New_York"

# Frozen before the replacement study results are generated.  Values are the
# medians of exact-time, out-of-window pairs in the calibration artifact.
SITE_CONFIG: Mapping[str, Mapping[str, Any]] = {
    "02246459": {"lid": "JAKF1", "expected_offset_ft": 12.15, "expected_anchors": 50},
    "02246825": {"lid": "CCJF1", "expected_offset_ft": 1.84, "expected_anchors": 37},
    "02246160": {"lid": "JULF1", "expected_offset_ft": 0.85, "expected_anchors": 22},
    "02231291": {"lid": "NATF1", "expected_offset_ft": 13.49, "expected_anchors": 3},
    "02246318": {"lid": "ORKF1", "expected_offset_ft": 1.07, "expected_anchors": 1},
    "02246518": {"lid": "PCUF1", "expected_offset_ft": 1.08, "expected_anchors": 45},
    "02246500": {"lid": "SJLF1", "expected_offset_ft": 11.71, "expected_anchors": 50},
    "02246621": {"lid": "TRJF1", "expected_offset_ft": 1.25, "expected_anchors": 14},
}


def _parse_utc(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def nwps_crest_wall_time_to_utc(value: str) -> datetime:
    """Interpret an NWPS historical crest clock field in the station zone."""
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    wall_time = datetime.fromisoformat(raw)
    if wall_time.tzinfo is not None:
        wall_time = wall_time.replace(tzinfo=None)
    return wall_time.replace(tzinfo=ZoneInfo(STATION_TIME_ZONE)).astimezone(
        timezone.utc
    )


def _fetch_json(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def _usgs_url(site_no: str) -> str:
    query = urllib.parse.urlencode(
        {
            "format": "json",
            "sites": site_no,
            "startDT": CALIBRATION_QUERY_START,
            "endDT": CALIBRATION_QUERY_END,
            "parameterCd": "00065,63160",
            "siteStatus": "all",
        }
    )
    return f"{USGS_IV_URL}?{query}"


def _series_by_parameter(payload: Mapping[str, Any]) -> Dict[str, Dict[datetime, Dict[str, Any]]]:
    out: Dict[str, Dict[datetime, Dict[str, Any]]] = {}
    for series in ((payload.get("value") or {}).get("timeSeries") or []):
        code = str(
            ((((series.get("variable") or {}).get("variableCode") or [{}])[0]).get("value"))
            or ""
        )
        if not code:
            continue
        points = out.setdefault(code, {})
        for block in series.get("values") or []:
            for point in block.get("value") or []:
                raw = point.get("value")
                if raw in (None, "", "-999999"):
                    continue
                when = _parse_utc(point["dateTime"])
                points[when] = {
                    "value": float(raw),
                    "qualifiers": list(point.get("qualifiers") or []),
                }
    return out


def _summary(values: Sequence[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "minimum": None, "maximum": None, "mad": None}
    median = statistics.median(values)
    return {
        "n": len(values),
        "median": round(median, 4),
        "minimum": round(min(values), 4),
        "maximum": round(max(values), 4),
        "mad": round(statistics.median(abs(value - median) for value in values), 4),
    }


def _outside_study(when: datetime) -> bool:
    return not (_parse_utc(STUDY_START_UTC) <= when < _parse_utc(STUDY_END_UTC))


def _in_calibration_window(when: datetime) -> bool:
    return _parse_utc(CALIBRATION_START_UTC) <= when < _parse_utc(
        CALIBRATION_END_UTC
    )


def match_anchors(
    *,
    site_no: str,
    lid: str,
    nwps_payload: Mapping[str, Any],
    usgs_payload: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return exact-time calibration pairs and held-out event validation."""
    series = _series_by_parameter(usgs_payload)
    gage_height = series.get("00065") or {}
    elevation = series.get("63160") or {}
    anchors: List[Dict[str, Any]] = []
    validation_residuals: List[float] = []
    expected = float(SITE_CONFIG[site_no]["expected_offset_ft"])

    for crest in (((nwps_payload.get("flood") or {}).get("crests") or {}).get("historic") or []):
        if crest.get("olddatum") or crest.get("stage") in (None, -9999):
            continue
        reported = str(crest.get("occurredTime") or "")
        if not reported:
            continue
        aligned = nwps_crest_wall_time_to_utc(reported)
        observation = gage_height.get(aligned)
        if not observation:
            continue
        raw_stage = float(observation["value"])
        official_stage = float(crest["stage"])
        offset = raw_stage - official_stage

        if _in_calibration_window(aligned) and _outside_study(aligned):
            elevation_observation = elevation.get(aligned)
            anchors.append(
                {
                    "nwps_occurred_time_reported": reported,
                    "matched_time_utc": _utc_iso(aligned),
                    "calibration_segment": (
                        "pre_event"
                        if aligned < _parse_utc(STUDY_START_UTC)
                        else "post_event"
                    ),
                    "nwps_stage_ft": round(official_stage, 4),
                    "usgs_00065_ft": round(raw_stage, 4),
                    "usgs_00065_qualifiers": observation["qualifiers"],
                    "offset_ft": round(offset, 4),
                    "usgs_63160_ft": (
                        round(float(elevation_observation["value"]), 4)
                        if elevation_observation
                        else None
                    ),
                    "usgs_63160_qualifiers": (
                        elevation_observation["qualifiers"]
                        if elevation_observation
                        else None
                    ),
                    "usgs_63160_minus_nwps_ft": (
                        round(float(elevation_observation["value"]) - official_stage, 4)
                        if elevation_observation
                        else None
                    ),
                }
            )
        elif not _outside_study(aligned):
            validation_residuals.append((raw_stage - expected) - official_stage)

    anchors.sort(key=lambda row: row["matched_time_utc"])
    validation = _summary(validation_residuals)
    validation["maximum_absolute_residual_ft"] = (
        round(max(abs(value) for value in validation_residuals), 4)
        if validation_residuals
        else None
    )
    return anchors, validation


def build_calibration_artifact(*, generated_at: Optional[str] = None) -> Dict[str, Any]:
    sites: List[Dict[str, Any]] = []
    for site_no, config in SITE_CONFIG.items():
        lid = str(config["lid"])
        noaa_url = NOAA_GAUGE_URL.format(lid=lid)
        usgs_url = _usgs_url(site_no)
        nwps_payload = _fetch_json(noaa_url)
        usgs_payload = _fetch_json(usgs_url)
        anchors, validation = match_anchors(
            site_no=site_no,
            lid=lid,
            nwps_payload=nwps_payload,
            usgs_payload=usgs_payload,
        )
        offsets = [float(anchor["offset_ft"]) for anchor in anchors]
        elevation_offsets = [
            float(anchor["usgs_63160_minus_nwps_ft"])
            for anchor in anchors
            if anchor.get("usgs_63160_minus_nwps_ft") is not None
        ]
        sites.append(
            {
                "site_no": site_no,
                "nwps_lid": lid,
                "name": nwps_payload.get("name"),
                "input_parameter": {
                    "code": "00065",
                    "description": "USGS gage height, feet",
                },
                "output_reference": "NOAA/NWPS operational stage used by flood categories",
                "transform": "stage_ft = stage_raw_ft - offset_ft",
                "prespecified_offset_ft": float(config["expected_offset_ft"]),
                "expected_calibration_anchors": int(config["expected_anchors"]),
                "source_urls": {"noaa_nwps": noaa_url, "usgs_iv": usgs_url},
                "nwps_time_zone": nwps_payload.get("timeZone"),
                "nwps_hydronotes": nwps_payload.get("hydronotes") or [],
                "nwps_vertical_datums_at_retrieval": (
                    ((nwps_payload.get("datums") or {}).get("vertical") or {}).get("value")
                    or []
                ),
                "offset_summary_ft": _summary(offsets),
                "parameter_63160_minus_nwps_summary_ft": _summary(elevation_offsets),
                "study_window_validation_residual_ft": validation,
                "calibration_anchors": anchors,
            }
        )

    artifact = {
        "schema": SCHEMA,
        "generated_at": (
            _utc_iso(_parse_utc(generated_at))
            if generated_at
            else _utc_iso(datetime.now(timezone.utc))
        ),
        "purpose": (
            "Align USGS 00065 gage heights with the NOAA/NWPS stage datum of "
            "the joined flood thresholds without using the Irma study window "
            "as calibration data."
        ),
        "study_window_utc_half_open": [STUDY_START_UTC, STUDY_END_UTC],
        "calibration_window_utc_half_open": [
            CALIBRATION_START_UTC,
            CALIBRATION_END_UTC,
        ],
        "calibration_exclusion": (
            "Every anchor satisfies matched_time < study_start or "
            "matched_time >= study_end."
        ),
        "source_query_local_dates": [
            CALIBRATION_QUERY_START,
            CALIBRATION_QUERY_END,
        ],
        "source_filtering": {
            "noaa_nwps": (
                "retrieve the complete official historical-crest list, then "
                "retain exact-time USGS matches inside the calibration window"
            ),
            "usgs_iv": (
                "request parameter codes 00065 and 63160 for the listed local "
                "calendar dates, then retain observations inside the UTC "
                "calibration window"
            ),
            "excluded_crest_flag": "flood.crests.historic[].olddatum == true",
        },
        "time_interpretation": {
            "nwps_field": "flood.crests.historic[].occurredTime",
            "reported_encoding": "timestamp string with trailing Z",
            "interpretation": (
                "station wall clock in America/New_York, as identified by the "
                "NWPS gauge timeZone EST5EDT; convert wall clock to UTC before "
                "exact-time matching"
            ),
            "usgs_field": "value.timeSeries[].values[].value[].dateTime",
            "usgs_interpretation": "honor the explicit ISO-8601 UTC offset",
            "match_tolerance_seconds": 0,
        },
        "official_sources": {
            "noaa_nwps_api": "https://api.water.noaa.gov/nwps/v1/docs/",
            "usgs_iv_api": "https://waterservices.usgs.gov/docs/instantaneous-values/instantaneous-values-details/",
        },
        "sites": sites,
    }
    validate_calibration_artifact(artifact)
    return artifact


def validate_calibration_artifact(artifact: Mapping[str, Any]) -> Dict[str, Any]:
    if artifact.get("schema") != SCHEMA:
        raise ValueError(f"unsupported calibration schema: {artifact.get('schema')!r}")
    if list(artifact.get("study_window_utc_half_open") or []) != [
        STUDY_START_UTC,
        STUDY_END_UTC,
    ]:
        raise ValueError("calibration artifact declares an unexpected study window")
    if list(artifact.get("calibration_window_utc_half_open") or []) != [
        CALIBRATION_START_UTC,
        CALIBRATION_END_UTC,
    ]:
        raise ValueError("calibration artifact declares an unexpected calibration window")
    sites = artifact.get("sites") or []
    by_site = {str(site.get("site_no") or ""): site for site in sites}
    if len(by_site) != len(sites):
        raise ValueError("calibration artifact contains duplicate site numbers")
    if set(by_site) != set(SITE_CONFIG):
        missing = sorted(set(SITE_CONFIG) - set(by_site))
        extra = sorted(set(by_site) - set(SITE_CONFIG))
        raise ValueError(f"calibration site mismatch; missing={missing}, extra={extra}")

    total_anchors = 0
    summary: Dict[str, Any] = {}
    for site_no, config in SITE_CONFIG.items():
        site = by_site[site_no]
        expected_lid = str(config["lid"])
        if str(site.get("nwps_lid") or "") != expected_lid:
            raise ValueError(
                f"{site_no} NWPS LID {site.get('nwps_lid')!r} != {expected_lid}"
            )
        expected = float(config["expected_offset_ft"])
        if abs(float(site.get("prespecified_offset_ft")) - expected) > 0.000001:
            raise ValueError(f"{site_no} has an unexpected prespecified offset")
        anchors = list(site.get("calibration_anchors") or [])
        expected_anchor_count = int(config["expected_anchors"])
        if len(anchors) != expected_anchor_count:
            raise ValueError(
                f"{site_no} has {len(anchors)} anchors; "
                f"frozen artifact requires {expected_anchor_count}"
            )
        offsets: List[float] = []
        matched_times = set()
        for anchor in anchors:
            matched = _parse_utc(anchor["matched_time_utc"])
            if not _in_calibration_window(matched):
                raise ValueError(
                    f"{site_no} calibration anchor lies outside calibration window"
                )
            if not _outside_study(matched):
                raise ValueError(f"{site_no} calibration anchor lies in study window")
            matched_iso = _utc_iso(matched)
            if matched_iso in matched_times:
                raise ValueError(f"{site_no} has a duplicate calibration anchor time")
            matched_times.add(matched_iso)
            expected_segment = (
                "pre_event"
                if matched < _parse_utc(STUDY_START_UTC)
                else "post_event"
            )
            if anchor.get("calibration_segment") != expected_segment:
                raise ValueError(f"{site_no} has an incorrect calibration segment")
            recomputed = float(anchor["usgs_00065_ft"]) - float(
                anchor["nwps_stage_ft"]
            )
            recorded = float(anchor["offset_ft"])
            if abs(recomputed - recorded) > 0.00011:
                raise ValueError(f"{site_no} anchor offset is internally inconsistent")
            offsets.append(recorded)
        median = round(statistics.median(offsets), 2)
        if abs(median - expected) > 0.005:
            raise ValueError(
                f"{site_no} median offset {median} != prespecified {expected}"
            )
        recomputed_summary = _summary(offsets)
        recorded_summary = site.get("offset_summary_ft") or {}
        for field, recomputed_value in recomputed_summary.items():
            recorded_value = recorded_summary.get(field)
            if recorded_value is None or abs(
                float(recorded_value) - float(recomputed_value)
            ) > 0.00011:
                raise ValueError(
                    f"{site_no} recorded offset summary field {field} is inconsistent"
                )
        total_anchors += len(anchors)
        summary[site_no] = {"anchors": len(anchors), "offset_ft": expected}
    return {"sites": len(summary), "anchors": total_anchors, "offsets": summary}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_calibration(
    corpus: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    calibration_path: str,
    calibration_sha256: str,
) -> Dict[str, Any]:
    validation = validate_calibration_artifact(artifact)
    prepared = deepcopy(corpus)
    if str(prepared.get("fips") or "") != "12031":
        raise ValueError("the frozen datum calibration applies only to Duval FIPS 12031")
    corpus_sites = [str(site.get("site_no") or "") for site in prepared.get("sites") or []]
    if len(set(corpus_sites)) != len(corpus_sites):
        raise ValueError("Duval corpus contains duplicate site numbers")
    if set(corpus_sites) != set(SITE_CONFIG):
        missing = sorted(set(SITE_CONFIG) - set(corpus_sites))
        extra = sorted(set(corpus_sites) - set(SITE_CONFIG))
        raise ValueError(f"Duval corpus site mismatch; missing={missing}, extra={extra}")
    artifact_sites = {
        str(site["site_no"]): site for site in (artifact.get("sites") or [])
    }
    transformed_observations = 0
    transformed_sites = 0
    for site in prepared.get("sites") or []:
        site_no = str(site.get("site_no") or "")
        calibration = artifact_sites.get(site_no)
        if calibration is None:
            raise ValueError(f"Duval corpus site {site_no} has no datum calibration")
        offset = float(calibration["prespecified_offset_ft"])
        for observation in site.get("observations") or []:
            raw = observation.get("stage_raw", observation.get("stage"))
            if raw is None:
                continue
            observation["stage_raw"] = float(raw)
            observation["stage"] = round(float(raw) - offset, 3)
            transformed_observations += 1
        site["stage_parameter_code"] = "00065"
        site["stage_raw_unit"] = "ft"
        site["stage_datum"] = "NOAA/NWPS operational stage"
        site["threshold_compatible"] = True
        site["evaluation_threshold_excluded"] = False
        site["stage_transform"] = {
            "type": "subtract_constant",
            "formula": "stage_ft = stage_raw_ft - offset_ft",
            "offset_ft": offset,
            "calibration_artifact": calibration_path,
            "calibration_artifact_sha256": calibration_sha256,
            "calibration_anchors": int(
                calibration["offset_summary_ft"]["n"]
            ),
            "calibration_window_excludes_study_window": True,
        }
        transformed_sites += 1

    if transformed_sites != len(SITE_CONFIG):
        raise ValueError(
            f"expected {len(SITE_CONFIG)} transformed sites, found {transformed_sites}"
        )
    prepared["datum_calibration"] = {
        "status": "applied",
        "input_parameter_code": "00065",
        "output_reference": "NOAA/NWPS operational stage",
        "calibration_artifact": calibration_path,
        "calibration_artifact_sha256": calibration_sha256,
        "transformed_sites": transformed_sites,
        "transformed_stage_observations": transformed_observations,
        "calibration_anchors": validation["anchors"],
        "study_window_used_for_calibration": False,
    }
    return prepared


def _write_json(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="fetch and freeze calibration pairs")
    build_parser.add_argument("--out", required=True)
    build_parser.add_argument(
        "--generated-at",
        help=(
            "optional pinned UTC build timestamp; record it in reproduction "
            "commands when byte-for-byte checksums are required"
        ),
    )

    verify_parser = subparsers.add_parser("verify", help="verify a frozen artifact")
    verify_parser.add_argument("--calibration", required=True)

    apply_parser = subparsers.add_parser("apply", help="apply transforms to a corpus copy")
    apply_parser.add_argument("--corpus", required=True)
    apply_parser.add_argument("--calibration", required=True)
    apply_parser.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    if args.command == "build":
        target = Path(args.out)
        artifact = build_calibration_artifact(generated_at=args.generated_at)
        _write_json(artifact, target)
        print(
            json.dumps(
                {
                    "path": str(target),
                    "sha256": _sha256(target),
                    **validate_calibration_artifact(artifact),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    calibration_path = Path(args.calibration)
    artifact = json.loads(calibration_path.read_text(encoding="utf-8"))
    validation = validate_calibration_artifact(artifact)
    if args.command == "verify":
        print(json.dumps({"path": str(calibration_path), **validation}, indent=2, sort_keys=True))
        return 0

    corpus_path = Path(args.corpus)
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    transformed = apply_calibration(
        corpus,
        artifact,
        calibration_path=str(calibration_path),
        calibration_sha256=_sha256(calibration_path),
    )
    target = Path(args.out)
    _write_json(transformed, target)
    print(
        json.dumps(
            {
                "path": str(target),
                "sha256": _sha256(target),
                **transformed["datum_calibration"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
