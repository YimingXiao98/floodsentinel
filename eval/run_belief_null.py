"""Generate the publication artifact for the belief/news architectural null.

The experiment is a two-by-two replay: the belief admission gate is enabled or
disabled, and the frozen Harris/Harvey news corpus is present or absent.  Every
cell uses :func:`eval.run_replay.run`; a non-mutating observer wraps the real
claim filter only long enough to count the exact claims that passed through it.

The expected null is architectural.  T2 news may be admitted when the belief
gate is disabled, but the phase engine independently excludes news/crowd claims
from phase transitions, alert level, and recovery.  Equality of operational
outputs therefore does *not* establish belief-layer efficacy.

Usage::

    python3 eval/run_belief_null.py \
        --out eval/results/belief_news_null.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.news_replay import ArchivedNewsFeed  # noqa: E402
from eval.run_replay import run as replay_run  # noqa: E402
from watcher.article_evidence_bridge import ARTICLE_CONFIDENCE_THRESHOLD  # noqa: E402


DEFAULT_MANIFEST = _ROOT / "eval" / "corpus" / "study" / "manifest.json"
DEFAULT_NEWS_CORPUS = _ROOT / "eval" / "corpus" / "harris_harvey_gdelt.json"
DEFAULT_NEWS_MANIFEST = (
    _ROOT / "eval" / "corpus" / "study" / "belief_news_manifest.json"
)
DEFAULT_OUTPUT = _ROOT / "eval" / "results" / "belief_news_null.json"
DEFAULT_PROTOCOL = _ROOT / "eval" / "STUDY_PROTOCOL.md"

POSITIVE_INDICATIONS = frozenset({"confirms_flood", "suggests_flood_risk"})
SCENARIOS: Tuple[Tuple[str, str, bool], ...] = (
    ("full_with_news", "full", True),
    ("no_belief_with_news", "no_belief", True),
    ("full_without_news", "full", False),
    ("no_belief_without_news", "no_belief", False),
)
OPERATIONAL_COMPONENTS = (
    "phase_timeline",
    "alert_timeline",
    "flood_verdict_timeline",
    "acquisition_cadence",
    "recovery_timeline",
    "termination",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(_ROOT))
    except ValueError:
        return str(resolved)


def _parse_utc(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _claim_tier(claim: Mapping[str, Any]) -> str:
    value = claim.get("value") or {}
    if isinstance(value, Mapping) and value.get("evidence_tier"):
        return str(value["evidence_tier"])
    return "T0"


@contextmanager
def _observe_claim_filter() -> Iterator[List[List[Dict[str, Any]]]]:
    """Observe, but do not alter, calls to the production admission function."""
    from watcher.claims import validate as validate_module

    original = validate_module.filter_claims_by_phase
    observations: List[List[Dict[str, Any]]] = []

    def observed_filter(
        claims: List[Dict[str, Any]], monitoring_summary: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        accepted, rejected = original(claims, monitoring_summary)
        accepted_objects = {id(claim) for claim in accepted}
        rejection_reasons: Dict[Tuple[Any, Any, Any], str] = {}
        for claim in rejected:
            key = (
                claim.get("claim_id"),
                claim.get("source_id"),
                claim.get("claim_type"),
            )
            rejection_reasons[key] = str(claim.get("rejection_reason") or "rejected")

        tick_observation: List[Dict[str, Any]] = []
        for claim in claims:
            valid, validation_reason = validate_module.validate_claim(claim)
            key = (
                claim.get("claim_id"),
                claim.get("source_id"),
                claim.get("claim_type"),
            )
            admitted = id(claim) in accepted_objects
            rejection_reason = None if admitted else rejection_reasons.get(key, "rejected")
            tick_observation.append(
                {
                    "claim_id": str(claim.get("claim_id") or ""),
                    "claim_type": str(claim.get("claim_type") or "unknown"),
                    "tier": _claim_tier(claim),
                    "structurally_valid": bool(valid),
                    "structural_validation_reason": str(validation_reason),
                    "admitted": admitted,
                    "rejection_reason": rejection_reason,
                }
            )
        observations.append(tick_observation)
        return accepted, rejected

    validate_module.filter_claims_by_phase = observed_filter
    try:
        yield observations
    finally:
        validate_module.filter_claims_by_phase = original


@contextmanager
def _reuse_immutable_news_feed(
    feed: ArchivedNewsFeed, expected_path: Path
) -> Iterator[None]:
    """Reuse one parsed, immutable feed across the two with-news replay cells."""
    from eval import news_replay as news_module

    original = news_module.load_feed
    expected = expected_path.resolve()

    def cached_loader(path: str | Path, **kwargs: Any) -> ArchivedNewsFeed:
        if Path(path).resolve() != expected:
            raise ValueError(f"unexpected news corpus requested: {path}")
        if kwargs.get("county", feed.county) != feed.county:
            raise ValueError("cached news feed county mismatch")
        if kwargs.get("state", feed.state) != feed.state:
            raise ValueError("cached news feed state mismatch")
        return feed

    news_module.load_feed = cached_loader
    try:
        yield
    finally:
        news_module.load_feed = original


def _claim_counts(observations: Sequence[Sequence[Mapping[str, Any]]]) -> Dict[str, Any]:
    """Count actual claim-filter events and unique claim identifiers."""
    buckets: Dict[Tuple[str, str], Counter[str]] = defaultdict(Counter)
    unique: Dict[Tuple[str, str], Dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    reasons: Counter[str] = Counter()

    for tick in observations:
        for claim in tick:
            key = (str(claim["tier"]), str(claim["claim_type"]))
            bucket = buckets[key]
            bucket["generated_events"] += 1
            unique[key]["generated"].add(str(claim.get("claim_id") or ""))
            if claim.get("structurally_valid"):
                bucket["structurally_valid_events"] += 1
                unique[key]["structurally_valid"].add(
                    str(claim.get("claim_id") or "")
                )
            else:
                bucket["structurally_invalid_events"] += 1
                unique[key]["structurally_invalid"].add(
                    str(claim.get("claim_id") or "")
                )
            if claim.get("admitted"):
                bucket["admitted_events"] += 1
                unique[key]["admitted"].add(str(claim.get("claim_id") or ""))
            else:
                bucket["rejected_events"] += 1
                unique[key]["rejected"].add(str(claim.get("claim_id") or ""))
                reason = str(claim.get("rejection_reason") or "rejected")
                reasons[reason] += 1
                if claim.get("structurally_valid"):
                    bucket["valid_but_not_admitted_events"] += 1

    rows: List[Dict[str, Any]] = []
    for key in sorted(buckets):
        tier, claim_type = key
        counts = buckets[key]
        ids = unique[key]
        rows.append(
            {
                "tier": tier,
                "claim_type": claim_type,
                **{
                    name: counts[name]
                    for name in (
                        "generated_events",
                        "structurally_valid_events",
                        "structurally_invalid_events",
                        "admitted_events",
                        "rejected_events",
                        "valid_but_not_admitted_events",
                    )
                },
                "unique_claim_ids": {
                    name: len(ids[name])
                    for name in (
                        "generated",
                        "structurally_valid",
                        "structurally_invalid",
                        "admitted",
                        "rejected",
                    )
                },
            }
        )

    def total(name: str) -> int:
        return sum(row[name] for row in rows)

    return {
        "counting_unit": (
            "claim-filter events; the same stable claim_id may recur at multiple "
            "acquisitions, so unique claim_id counts are reported separately"
        ),
        "totals": {
            name: total(name)
            for name in (
                "generated_events",
                "structurally_valid_events",
                "structurally_invalid_events",
                "admitted_events",
                "rejected_events",
                "valid_but_not_admitted_events",
            )
        },
        "by_tier_and_type": rows,
        "rejection_reasons": dict(sorted(reasons.items())),
    }


def _article_counts(ticks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    events: Counter[str] = Counter()
    urls: Dict[str, set[str]] = defaultdict(set)
    for tick in ticks:
        for article in tick.get("articles_admitted") or []:
            indication = str(article.get("indication") or "unknown")
            events[indication] += 1
            urls[indication].add(str(article.get("url") or ""))
    indications = sorted(set(events) | set(urls))
    positive_events = sum(events[name] for name in POSITIVE_INDICATIONS)
    positive_urls = set().union(
        *(urls[name] for name in POSITIVE_INDICATIONS)
    ) if POSITIVE_INDICATIONS else set()
    return {
        "selection_boundary": (
            "production detector page/relevance threshold before belief claim admission"
        ),
        "counting_unit": (
            "detector-selected article events; repeated selection of one URL at "
            "successive acquisitions is counted repeatedly, with unique URLs separate"
        ),
        "selected_events_by_indication": {
            name: events[name] for name in indications
        },
        "unique_urls_by_indication": {
            name: len(urls[name]) for name in indications
        },
        "selected_events_total": sum(events.values()),
        "unique_urls_total": len(set().union(*urls.values())) if urls else 0,
        "positive_selected_events": positive_events,
        "positive_unique_urls": len(positive_urls),
        "positive_indications": sorted(POSITIVE_INDICATIONS),
    }


def _seconds_between(left: str, right: str) -> int:
    def parse(value: str) -> datetime:
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(raw)

    return round((parse(right) - parse(left)).total_seconds())


def operational_projection(run: Mapping[str, Any]) -> Dict[str, Any]:
    """The exact operational fields whose equality is asserted in the artifact."""
    ticks = list(run.get("ticks") or [])
    timestamps = [str(tick.get("t")) for tick in ticks]
    return {
        "phase_timeline": [
            {
                "t": tick.get("t"),
                "phase_before": tick.get("phase_before"),
                "phase_after": tick.get("phase_after"),
                "phase_changed": bool(tick.get("phase_changed")),
            }
            for tick in ticks
        ],
        "alert_timeline": [
            {"t": tick.get("t"), "alert_level": tick.get("alert_level")}
            for tick in ticks
        ],
        "flood_verdict_timeline": [
            {
                "t": tick.get("t"),
                "regional_verdict": tick.get("regional_verdict"),
                "is_flooding": bool(tick.get("is_flooding")),
                "flood_stage_observed": bool(tick.get("flood_stage_observed")),
                "max_flood_state": tick.get("max_flood_state"),
            }
            for tick in ticks
        ],
        "acquisition_cadence": {
            "timestamps": timestamps,
            "interval_seconds": [
                _seconds_between(left, right)
                for left, right in zip(timestamps, timestamps[1:])
            ],
            "schedule": run.get("schedule"),
        },
        "recovery_timeline": [
            {
                "t": tick.get("t"),
                "recovery_all_clear_streak": int(
                    tick.get("recovery_all_clear_streak") or 0
                ),
                "recovery_clear_since": tick.get("recovery_clear_since"),
                "recovery_clear_elapsed_hours": float(
                    tick.get("recovery_clear_elapsed_hours") or 0.0
                ),
                "recovery_all_clear": bool(tick.get("recovery_all_clear")),
                "folded": bool(tick.get("folded")),
            }
            for tick in ticks
        ],
        "termination": run.get("termination"),
    }


def _transition_rows(
    ticks: Sequence[Mapping[str, Any]], field: str
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    previous: Any = object()
    for tick in ticks:
        value = tick.get(field)
        if value != previous:
            rows.append({"t": tick.get("t"), field: value})
            previous = value
    return rows


def _operational_summary(run: Mapping[str, Any], projection: Mapping[str, Any]) -> Dict[str, Any]:
    ticks = list(run.get("ticks") or [])
    intervals = projection["acquisition_cadence"]["interval_seconds"]
    return {
        "acquisitions": len(ticks),
        "first_acquisition": ticks[0].get("t") if ticks else None,
        "last_acquisition": ticks[-1].get("t") if ticks else None,
        "phase_transitions": [
            {
                "t": tick.get("t"),
                "from": tick.get("phase_before"),
                "to": tick.get("phase_after"),
            }
            for tick in ticks
            if tick.get("phase_changed")
        ],
        "alert_transitions": _transition_rows(ticks, "alert_level"),
        "regional_verdict_transitions": _transition_rows(ticks, "regional_verdict"),
        "cadence_interval_seconds_counts": {
            str(seconds): count
            for seconds, count in sorted(Counter(intervals).items())
        },
        "folded": bool(ticks and ticks[-1].get("folded")),
        "termination": run.get("termination"),
        "component_sha256": {
            name: _json_sha256(projection[name]) for name in OPERATIONAL_COMPONENTS
        },
        "combined_sha256": _json_sha256(projection),
    }


def _first_difference(left: Any, right: Any, path: str = "$") -> Optional[Dict[str, Any]]:
    if type(left) is not type(right):
        return {"path": path, "left": left, "right": right}
    if isinstance(left, Mapping):
        left_keys, right_keys = set(left), set(right)
        if left_keys != right_keys:
            return {
                "path": path,
                "left_keys_only": sorted(left_keys - right_keys),
                "right_keys_only": sorted(right_keys - left_keys),
            }
        for key in sorted(left):
            found = _first_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list):
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            found = _first_difference(
                left_item, right_item, f"{path}[{index}]"
            )
            if found:
                return found
        if len(left) != len(right):
            return {
                "path": path,
                "left_length": len(left),
                "right_length": len(right),
            }
        return None
    if left != right:
        return {"path": path, "left": left, "right": right}
    return None


def compare_operational_outputs(
    left_name: str,
    right_name: str,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> Dict[str, Any]:
    components: Dict[str, Dict[str, Any]] = {}
    for name in OPERATIONAL_COMPONENTS:
        left_value, right_value = left[name], right[name]
        difference = _first_difference(left_value, right_value)
        row: Dict[str, Any] = {
            "exact_equal": difference is None,
            "left_sha256": _json_sha256(left_value),
            "right_sha256": _json_sha256(right_value),
        }
        if difference is not None:
            row["first_difference"] = difference
        components[name] = row
    unequal = [name for name, row in components.items() if not row["exact_equal"]]
    return {
        "left": left_name,
        "right": right_name,
        "all_operational_outputs_exact_equal": not unequal,
        "unequal_components": unequal,
        "components": components,
    }


def _implementation_files() -> List[Path]:
    """A conservative superset of code/config that can affect this artifact."""
    # Do not hash unrelated evaluation analyses (metrics, figures, or the
    # multi-county study generator): changing a table formatter must not make
    # this replay artifact appear stale.  These are the evaluation modules on
    # the executed import path; watcher and county-flood packages remain broad
    # supersets because their internal import graph is deliberately modular.
    candidates = {
        _ROOT / "eval" / "__init__.py",
        _ROOT / "eval" / "ground_truth.py",
        _ROOT / "eval" / "news_archive.py",
        _ROOT / "eval" / "news_gdelt.py",
        _ROOT / "eval" / "news_replay.py",
        _ROOT / "eval" / "run_belief_null.py",
        _ROOT / "eval" / "run_replay.py",
    }
    candidates.update((_ROOT / "watcher").rglob("*.py"))
    candidates.update((_ROOT / "watcher" / "ontology").glob("stub_*.json"))
    candidates.update(
        (_ROOT / "skills" / "county-flood-data" / "county_flood").rglob("*.py")
    )
    candidates.update((_ROOT / "skills" / "flood-article-detector").glob("*.py"))
    excluded_parts = {"__pycache__", "tests", "results"}
    return sorted(
        (
            path
            for path in candidates
            if path.is_file() and not excluded_parts.intersection(path.parts)
        ),
        key=_portable_path,
    )


def _implementation_manifest() -> Dict[str, Any]:
    hashes = {
        _portable_path(path): _sha256(path) for path in _implementation_files()
    }
    aggregate = hashlib.sha256()
    for path, digest in hashes.items():
        aggregate.update(path.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
    return {
        "rule": (
            "sorted executed eval modules (run_belief_null, run_replay, "
            "news_replay, news_archive, news_gdelt, ground_truth), watcher/**/*.py, "
            "watcher/ontology/stub_*.json, skills/county-flood-data/"
            "county_flood/**/*.py, and skills/flood-article-detector/*.py; "
            "tests/caches/results excluded"
        ),
        "aggregate_sha256": aggregate.hexdigest(),
        "files": hashes,
    }


def _load_harris_entry(manifest_path: Path) -> Dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matches = [
        entry
        for entry in manifest.get("artifacts") or []
        if str(entry.get("fips")) == "48201"
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one Harris (48201) manifest entry, found {len(matches)}")
    entry = dict(matches[0])
    required = ("path", "county", "state", "fips", "analysis_start", "analysis_end", "sha256")
    missing = [key for key in required if not entry.get(key)]
    if missing:
        raise ValueError(f"Harris manifest entry missing {missing}")
    return entry


def _load_news_manifest_entry(manifest_path: Path) -> Dict[str, Any]:
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "sentinel-belief-news-corpus-manifest/1":
        raise ValueError("unsupported belief-news corpus manifest schema")
    entry = dict(manifest.get("artifact") or {})
    required = (
        "path",
        "sha256",
        "corpus_schema",
        "window",
        "required_lookback_hours",
        "maximum_missing_slice_fraction",
        "as_of_field",
        "slices_swept",
        "slices_missing",
        "slice_coverage_fraction",
    )
    missing = [key for key in required if entry.get(key) is None]
    if missing:
        raise ValueError(f"belief-news manifest missing {missing}")
    return entry


def _validate_news_corpus(
    *,
    news_path: Path,
    news_manifest_path: Path,
    harris_entry: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    manifest_entry = _load_news_manifest_entry(news_manifest_path)
    declared_path = Path(str(manifest_entry["path"]))
    if not declared_path.is_absolute():
        declared_path = _ROOT / declared_path
    if declared_path.resolve() != news_path.resolve():
        raise ValueError(
            "news corpus path differs from the checksum-bound belief-news manifest"
        )
    actual_hash = _sha256(news_path)
    if actual_hash != str(manifest_entry["sha256"]):
        raise ValueError(
            "news corpus checksum mismatch: "
            f"manifest={manifest_entry['sha256']} actual={actual_hash}"
        )

    corpus = json.loads(news_path.read_text(encoding="utf-8"))
    if corpus.get("schema") != manifest_entry["corpus_schema"]:
        raise ValueError("news corpus schema differs from its manifest")
    if corpus.get("county") != harris_entry["county"] or corpus.get(
        "state"
    ) != harris_entry["state"]:
        raise ValueError("news corpus county/state differs from the replay case")
    if corpus.get("source") != "gdelt_gkg":
        raise ValueError("belief-null publication corpus must use GDELT GKG")
    if corpus.get("as_of_field") != "GKG_V2_1_DATE":
        raise ValueError("news corpus must declare GKG V2.1DATE as its as-of field")
    if corpus.get("as_of_field") != manifest_entry["as_of_field"]:
        raise ValueError("news corpus as-of field differs from its manifest")

    corpus_window = corpus.get("window") or {}
    if corpus_window != manifest_entry["window"]:
        raise ValueError("news corpus window differs from its manifest")
    try:
        corpus_start = datetime.strptime(
            str(corpus_window["start"]), "%Y%m%d"
        ).replace(tzinfo=timezone.utc)
        corpus_end = datetime.strptime(
            str(corpus_window["end"]), "%Y%m%d"
        ).replace(tzinfo=timezone.utc)
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid news corpus UTC date window") from exc
    analysis_start = _parse_utc(str(harris_entry["analysis_start"]))
    analysis_end = _parse_utc(str(harris_entry["analysis_end"]))
    lookback_hours = float(manifest_entry["required_lookback_hours"])
    required_start = analysis_start - timedelta(hours=lookback_hours)
    if corpus_start > required_start or corpus_end < analysis_end:
        raise ValueError(
            "news corpus does not cover the replay window plus required lookback: "
            f"corpus=[{corpus_start.isoformat()},{corpus_end.isoformat()}) "
            f"required=[{required_start.isoformat()},{analysis_end.isoformat()})"
        )

    swept = int(corpus.get("slices_swept") or 0)
    missing_slices = list(corpus.get("missing_slices") or [])
    missing_count = int(corpus.get("slices_missing") or 0)
    expected_slices = round((corpus_end - corpus_start).total_seconds() / 900)
    if swept != expected_slices or missing_count != len(missing_slices):
        raise ValueError("news corpus slice-accounting metadata is inconsistent")
    if swept != int(manifest_entry["slices_swept"]) or missing_count != int(
        manifest_entry["slices_missing"]
    ):
        raise ValueError("news corpus slice counts differ from its manifest")
    if len(set(missing_slices)) != len(missing_slices):
        raise ValueError("news corpus missing-slice list contains duplicates")
    missing_fraction = missing_count / swept if swept else 1.0
    declared_fraction = float(
        manifest_entry["maximum_missing_slice_fraction"]
    )
    if missing_fraction > declared_fraction:
        raise ValueError(
            f"news missing-slice fraction {missing_fraction:.6f} exceeds "
            f"manifest maximum {declared_fraction:.6f}"
        )
    recorded_coverage = corpus.get("slice_coverage_fraction")
    recomputed_coverage = round(1.0 - missing_fraction, 6)
    if recorded_coverage != recomputed_coverage:
        raise ValueError("news corpus slice coverage does not recompute")
    if recorded_coverage != manifest_entry["slice_coverage_fraction"]:
        raise ValueError("news corpus slice coverage differs from its manifest")

    return corpus, {
        "corpus_sha256": actual_hash,
        "corpus_window": corpus_window,
        "required_window": {
            "start": required_start.isoformat().replace("+00:00", "Z"),
            "end": analysis_end.isoformat().replace("+00:00", "Z"),
            "lookback_hours": lookback_hours,
        },
        "slices_swept": swept,
        "slices_missing": missing_count,
        "missing_slices": missing_slices,
        "slice_coverage_fraction": recomputed_coverage,
        "maximum_missing_slice_fraction": declared_fraction,
        "as_of_field": corpus["as_of_field"],
        "as_of_semantics": corpus.get("as_of_semantics"),
    }


def _run_one(
    *,
    name: str,
    ablation: str,
    with_news: bool,
    entry: Mapping[str, Any],
    corpus_path: Path,
    news_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    environment = {
        "SENTINEL_ABLATION": ablation,
        "SENTINEL_TIER_WEIGHT": "1.0",
        "SENTINEL_ABLATED_CONFIDENCE": "0.75",
        "SENTINEL_READING_MAX_AGE_HOURS": "6.0",
        "COUNTY_FLOOD_MIN_GAUGES": "5",
        "SENTINEL_WATERSHED_NEIGHBOURS": "off",
        "ONTOLOGY_SOURCE": "stub",
    }
    with _temporary_environment(environment), _observe_claim_filter() as observed:
        run = replay_run(
            corpus_path=str(corpus_path),
            news_corpus_path=str(news_path) if with_news else None,
            county=str(entry["county"]),
            state=str(entry["state"]),
            fips=str(entry["fips"]),
            start=str(entry["analysis_start"]),
            end=str(entry["analysis_end"]),
            max_staleness_hours=6.0,
            recovery_min_clear_hours=48.0,
            threshold_excluded_site_ids=tuple(
                str(item.get("site_no") if isinstance(item, Mapping) else item)
                for item in (entry.get("threshold_exclusions") or [])
            ),
        )
    if len(observed) != int(run.get("n_ticks") or 0):
        raise RuntimeError(
            f"{name}: observed {len(observed)} claim-filter calls for "
            f"{run.get('n_ticks')} replay ticks"
        )
    projection = operational_projection(run)
    claims = _claim_counts(observed)
    articles = _article_counts(run.get("ticks") or [])
    return (
        {
            "ablation": ablation,
            "news_present": with_news,
            "replay_schema": run.get("schema"),
            "articles": articles,
            "claims": claims,
            "operational": _operational_summary(run, projection),
        },
        projection,
    )


def _t2_admitted_events(scenario: Mapping[str, Any]) -> int:
    return sum(
        int(row["admitted_events"])
        for row in scenario["claims"]["by_tier_and_type"]
        if row["tier"] == "T2"
    )


def _tier_events(scenario: Mapping[str, Any], tier: str, field: str) -> int:
    return sum(
        int(row[field])
        for row in scenario["claims"]["by_tier_and_type"]
        if row["tier"] == tier
    )


def generate(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    news_path: Path = DEFAULT_NEWS_CORPUS,
    news_manifest_path: Path = DEFAULT_NEWS_MANIFEST,
) -> Dict[str, Any]:
    manifest_path = manifest_path.resolve()
    news_path = news_path.resolve()
    news_manifest_path = news_manifest_path.resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    if not news_path.is_file():
        raise FileNotFoundError(
            f"frozen Harris/Harvey GDELT corpus is required: {news_path}"
        )
    entry = _load_harris_entry(manifest_path)
    corpus_path = (_ROOT / str(entry["path"])).resolve()
    if not corpus_path.is_file():
        raise FileNotFoundError(corpus_path)
    corpus_hash = _sha256(corpus_path)
    if corpus_hash != str(entry["sha256"]):
        raise ValueError(
            f"Harris corpus checksum mismatch: manifest={entry['sha256']} "
            f"actual={corpus_hash}"
        )

    news_corpus, news_coverage = _validate_news_corpus(
        news_path=news_path,
        news_manifest_path=news_manifest_path,
        harris_entry=entry,
    )

    # Loading the large source twice would add no experimental independence.
    # ArchivedNewsFeed is immutable after construction, so both with-news cells
    # share one parsed instance while executing the same replay lookup/scorer.
    shared_feed = ArchivedNewsFeed(
        news_corpus,
        county=str(entry["county"]),
        state=str(entry["state"]),
    )
    del news_corpus
    scenario_rows: Dict[str, Any] = {}
    projections: Dict[str, Dict[str, Any]] = {}
    with _reuse_immutable_news_feed(shared_feed, news_path):
        for name, ablation, with_news in SCENARIOS:
            scenario_rows[name], projections[name] = _run_one(
                name=name,
                ablation=ablation,
                with_news=with_news,
                entry=entry,
                corpus_path=corpus_path,
                news_path=news_path,
            )

    comparisons: Dict[str, Any] = {}
    for left, right in (
        ("full_with_news", "no_belief_with_news"),
        ("full_without_news", "no_belief_without_news"),
        ("full_with_news", "full_without_news"),
        ("no_belief_with_news", "no_belief_without_news"),
    ):
        key = f"{left}_vs_{right}"
        row = compare_operational_outputs(
            left, right, projections[left], projections[right]
        )
        row["t2_admitted_claim_event_delta_right_minus_left"] = (
            _t2_admitted_events(scenario_rows[right])
            - _t2_admitted_events(scenario_rows[left])
        )
        comparisons[key] = row

    positive_events = scenario_rows["full_with_news"]["articles"][
        "positive_selected_events"
    ]
    if positive_events <= 0:
        raise RuntimeError(
            "the frozen news corpus produced no positive detector-selected T2 news"
        )
    detector_selection_equal = (
        scenario_rows["full_with_news"]["articles"]
        == scenario_rows["no_belief_with_news"]["articles"]
    )
    if not detector_selection_equal:
        raise RuntimeError("belief ablation unexpectedly changed article selection")

    within_full = comparisons["full_with_news_vs_full_without_news"]
    within_no_belief = comparisons[
        "no_belief_with_news_vs_no_belief_without_news"
    ]
    belief_with_news = comparisons[
        "full_with_news_vs_no_belief_with_news"
    ]
    belief_without_news = comparisons[
        "full_without_news_vs_no_belief_without_news"
    ]
    news_operational_null = bool(
        within_full["all_operational_outputs_exact_equal"]
        and within_no_belief["all_operational_outputs_exact_equal"]
    )

    news_hash = news_coverage["corpus_sha256"]
    full_with_news = scenario_rows["full_with_news"]
    no_belief_with_news = scenario_rows["no_belief_with_news"]
    return {
        "schema": "sentinel-belief-news-architectural-null/2",
        "analysis_label": "analysis-contract architectural null audit",
        "headline_result": {
            "detector_selected_positive_article_events": full_with_news[
                "articles"
            ]["positive_selected_events"],
            "detector_selected_positive_unique_urls": full_with_news["articles"][
                "positive_unique_urls"
            ],
            "structurally_valid_t2_claim_events": _tier_events(
                full_with_news, "T2", "structurally_valid_events"
            ),
            "full_t2_admitted_claim_events": _tier_events(
                full_with_news, "T2", "admitted_events"
            ),
            "full_t2_rejected_claim_events": _tier_events(
                full_with_news, "T2", "rejected_events"
            ),
            "no_belief_t2_admitted_claim_events": _tier_events(
                no_belief_with_news, "T2", "admitted_events"
            ),
            "no_belief_t2_rejected_claim_events": _tier_events(
                no_belief_with_news, "T2", "rejected_events"
            ),
            "all_six_operational_components_exact_equal_in_all_four_comparisons": all(
                comparison["all_operational_outputs_exact_equal"]
                for comparison in comparisons.values()
            ),
        },
        "interpretation": {
            "news_operational_null_observed": news_operational_null,
            "belief_ablation_operational_equality_with_news": belief_with_news[
                "all_operational_outputs_exact_equal"
            ],
            "belief_ablation_operational_equality_without_news": belief_without_news[
                "all_operational_outputs_exact_equal"
            ],
            "expected_by_current_architecture": True,
            "mechanism": (
                "the regional flood verdict is computed from the official gauge "
                "payload, while the phase engine independently excludes T2/news/"
                "crowd claims from phase transitions, alert level, de-escalation, "
                "and recovery"
            ),
            "inference_boundary": (
                "Exact equality is expected under the downstream T2 veto. It is "
                "a null/negative architecture result and is not evidence that the "
                "belief layer improves decision quality, calibration, or efficacy."
            ),
            "outputs_not_claimed_equal": [
                "accepted/rejected claim inventory",
                "report body and raw news section",
                "report-delta materiality counterfactual",
            ],
        },
        "design": {
            "factorial_cells": [name for name, _, _ in SCENARIOS],
            "system_path": "eval.run_replay.run (current production extraction/admission/phase path)",
            "claim_instrumentation": (
                "non-mutating observer around watcher.claims.validate."
                "filter_claims_by_phase; the original function executes unchanged"
            ),
            "news_feed_reuse": (
                "one immutable parsed ArchivedNewsFeed instance is shared by the "
                "two with-news cells; lookup and scoring execute independently at "
                "every replay acquisition"
            ),
            "window": {
                "start": entry["analysis_start"],
                "end": entry["analysis_end"],
                "convention": "half-open [start,end)",
            },
            "parameters": {
                "max_staleness_hours": 6.0,
                "recovery_min_clear_hours": 48.0,
                "county_flood_min_gauges": 5,
                "tier_weight": 1.0,
                "ablated_uniform_confidence_display_only": 0.75,
                "news_lookback_hours": 24.0,
                "news_candidate_limit": 120,
                "news_detector_top_n": 5,
                "article_detector_min_page_score_strictly_greater_than": float(
                    ARTICLE_CONFIDENCE_THRESHOLD
                ),
                "nws_system_input": None,
            },
            "news_construct": {
                "as_of_field": news_coverage["as_of_field"],
                "as_of_semantics": news_coverage["as_of_semantics"],
                "corpus_selected_by": (
                    "GDELT location entities matching the frozen Harris/Houston "
                    "place terms; URLs deduplicated by retaining the first record"
                ),
                "usable_title_filter": (
                    "URL-derived title must contain at least four alphabetic words"
                ),
                "scored_content": (
                    "title repeated as title/snippet with the corpus county/state "
                    "supplied as a synthetic dateline; archived article body absent"
                ),
                "inference_boundary": (
                    "article and claim counts characterize this title-only proxy, "
                    "not general news-evidence quality; operational equality is "
                    "structurally expected from the downstream T2 veto"
                ),
            },
            "news_source_coverage": news_coverage,
            "operational_projection_components": list(OPERATIONAL_COMPONENTS),
        },
        "provenance": {
            "reproduction_command": (
                "python3 eval/run_belief_null.py --out "
                "eval/results/belief_news_null.json"
            ),
            "python": {
                "implementation": platform.python_implementation(),
                "version": platform.python_version(),
                "version_detail": sys.version,
            },
            "inputs": {
                "analysis_contract": {
                    "path": _portable_path(DEFAULT_PROTOCOL),
                    "sha256": _sha256(DEFAULT_PROTOCOL),
                    "bytes": DEFAULT_PROTOCOL.stat().st_size,
                },
                "study_manifest": {
                    "path": _portable_path(manifest_path),
                    "sha256": _sha256(manifest_path),
                    "bytes": manifest_path.stat().st_size,
                },
                "belief_news_manifest": {
                    "path": _portable_path(news_manifest_path),
                    "sha256": _sha256(news_manifest_path),
                    "bytes": news_manifest_path.stat().st_size,
                },
                "hydrologic_corpus": {
                    "path": _portable_path(corpus_path),
                    "sha256": corpus_hash,
                    "manifest_sha256": entry["sha256"],
                    "bytes": corpus_path.stat().st_size,
                },
                "news_corpus": {
                    "path": _portable_path(news_path),
                    "sha256": news_hash,
                    "bytes": news_path.stat().st_size,
                    "usable_title_articles": len(shared_feed),
                    "source_coverage": news_coverage,
                },
                "news_corpus_builder": {
                    "path": _portable_path(_ROOT / "eval" / "news_gdelt.py"),
                    "sha256": _sha256(_ROOT / "eval" / "news_gdelt.py"),
                },
            },
            "implementation": _implementation_manifest(),
        },
        "scenarios": scenario_rows,
        "comparisons": comparisons,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--news-corpus", default=str(DEFAULT_NEWS_CORPUS))
    parser.add_argument(
        "--news-manifest", default=str(DEFAULT_NEWS_MANIFEST)
    )
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    artifact = generate(
        manifest_path=Path(args.manifest),
        news_path=Path(args.news_corpus),
        news_manifest_path=Path(args.news_manifest),
    )
    target = Path(args.out)
    if not target.is_absolute():
        target = _ROOT / target
    _write_json(target, artifact)
    print(
        f"wrote {_portable_path(target)}; "
        f"news_operational_null={artifact['interpretation']['news_operational_null_observed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
