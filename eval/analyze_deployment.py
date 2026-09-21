"""Summarize concatenated-JSON heartbeat deployment logs.

The production cron log is not JSONL: each invocation writes one pretty-printed
JSON object immediately after the previous object.  This module parses that
format incrementally, retaining at most one poll object in memory, and emits a
small deterministic summary suitable for publication artifacts.

The input log is never modified.  Output is written only when ``--output`` is
provided; otherwise the summary is printed to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional


SCHEMA = "disaster-sentinel/deployment-summary/1"
DEFAULT_LOG = Path("logs/sentinel-heartbeat-cron.log")

_WHITESPACE = frozenset(b" \t\r\n")
_OPEN_TO_CLOSE = {ord("{"): ord("}"), ord("["): ord("]")}
_CLOSE = frozenset(_OPEN_TO_CLOSE.values())
_CODE_FIELD_NAMES = (
    "code_version",
    "code_commit",
    "git_commit",
    "git_sha",
    "commit_sha",
    "revision",
    "build_id",
    "build_version",
    "runtime_version",
    "version",
)

_MISSION_COUNT_KEYS = (
    "mission_opportunities",
    "due",
    "ran",
    "cadence_skips",
    "mission_errors",
    "folded",
    "report_deliveries",
    "report_messages_sent",
    "materiality_assessed",
    "material_updates",
    "materiality_skips",
    "delivery_failures",
    "delivery_not_recorded",
    "fold_deliveries",
    "source_fetch_failures",
    "coordinator_fetch_failures",
    "county_api_fetch_errors",
    "extraction_failures",
    "no_gauges",
)


class DeploymentLogParseError(ValueError):
    """Raised when a concatenated-JSON log is incomplete or malformed."""


def iter_concatenated_json(
    stream: BinaryIO,
    *,
    chunk_size: int = 64 * 1024,
    digest: Optional[Any] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield top-level objects from a concatenated JSON byte stream.

    A brace-aware scanner identifies complete objects without loading the whole
    deployment log.  Braces inside JSON strings and escape sequences are
    handled explicitly.  Only top-level objects are accepted because that is
    the cron-log contract.
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    parts: List[bytes] = []
    stack: List[int] = []
    collecting = False
    in_string = False
    escaped = False
    record_start = 0
    absolute_offset = 0

    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        if digest is not None:
            digest.update(chunk)

        # A record that began in an earlier chunk continues at byte zero of
        # this chunk; a new record sets its own start below.
        segment_start: Optional[int] = 0 if collecting else None
        for index, byte in enumerate(chunk):
            position = absolute_offset + index

            if not collecting:
                if byte in _WHITESPACE:
                    continue
                if byte != ord("{"):
                    raise DeploymentLogParseError(
                        f"expected a JSON object at byte {position}, got 0x{byte:02x}"
                    )
                collecting = True
                in_string = False
                escaped = False
                stack = [ord("}")]
                parts = []
                record_start = position
                segment_start = index
                continue

            if in_string:
                if escaped:
                    escaped = False
                elif byte == ord("\\"):
                    escaped = True
                elif byte == ord('"'):
                    in_string = False
                continue

            if byte == ord('"'):
                in_string = True
            elif byte in _OPEN_TO_CLOSE:
                stack.append(_OPEN_TO_CLOSE[byte])
            elif byte in _CLOSE:
                if not stack or byte != stack[-1]:
                    raise DeploymentLogParseError(
                        f"mismatched JSON delimiter at byte {position}"
                    )
                stack.pop()
                if not stack:
                    if byte != ord("}"):
                        raise DeploymentLogParseError(
                            f"top-level JSON value at byte {record_start} is not an object"
                        )
                    assert segment_start is not None
                    parts.append(chunk[segment_start : index + 1])
                    raw = b"".join(parts)
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise DeploymentLogParseError(
                            f"invalid JSON at byte {record_start + exc.pos}: {exc.msg}"
                        ) from exc
                    if not isinstance(value, dict):
                        raise DeploymentLogParseError(
                            f"top-level JSON value at byte {record_start} is not an object"
                        )
                    yield value
                    collecting = False
                    in_string = False
                    escaped = False
                    stack = []
                    parts = []
                    segment_start = None

        if collecting and segment_start is not None:
            parts.append(chunk[segment_start:])
        absolute_offset += len(chunk)

    if collecting:
        detail = "inside a string" if in_string else "with unclosed delimiters"
        raise DeploymentLogParseError(
            f"truncated JSON object beginning at byte {record_start} ({detail})"
        )


def _objects(value: Any) -> List[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _integer(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    return fallback


def _iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _canonical_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _clean_scalar(value: Any) -> Optional[Any]:
    if isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes):
        return value
    return None


def _code_fields(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Return explicit code/build identifiers, avoiding unrelated nested versions."""

    found: Dict[str, Any] = {}
    containers = [("", value)]
    for name in ("code", "build", "runtime", "deployment"):
        nested = value.get(name)
        if isinstance(nested, Mapping):
            containers.append((f"{name}.", nested))
    for prefix, container in containers:
        for key in _CODE_FIELD_NAMES:
            cleaned = _clean_scalar(container.get(key))
            if cleaned is not None:
                found[f"{prefix}{key}"] = cleaned
    return dict(sorted(found.items()))


def _identity(value: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    spec = value.get("mission_spec") if isinstance(value.get("mission_spec"), Mapping) else {}
    payload = (
        value.get("county_flood_payload")
        if isinstance(value.get("county_flood_payload"), Mapping)
        else {}
    )
    mapping = (
        value.get("county_api_mapping")
        if isinstance(value.get("county_api_mapping"), Mapping)
        else {}
    )
    return {
        "county": value.get("county_under_monitor")
        or value.get("county")
        or spec.get("county")
        or payload.get("county")
        or mapping.get("county_name"),
        "state": value.get("state_under_monitor")
        or value.get("state")
        or spec.get("state")
        or payload.get("state"),
        "fips": value.get("fips")
        or spec.get("fips")
        or payload.get("fips")
        or mapping.get("county_fips"),
    }


def _heartbeat_interval(value: Mapping[str, Any]) -> Optional[int]:
    """Return the effective post-cycle interval recorded for a mission."""

    summary = value.get("monitoring_summary")
    if isinstance(summary, Mapping):
        interval = summary.get("heartbeat_interval_seconds")
        if isinstance(interval, int) and not isinstance(interval, bool):
            return interval
    interval = value.get("heartbeat_interval_seconds")
    if isinstance(interval, int) and not isinstance(interval, bool):
        return interval
    spec = value.get("mission_spec")
    if isinstance(spec, Mapping):
        interval = spec.get("heartbeat_interval_seconds")
        if isinstance(interval, int) and not isinstance(interval, bool):
            return interval
    return None


def _reason(item: Any) -> str:
    if isinstance(item, Mapping):
        value = item.get("failure_reason") or item.get("error") or item.get("reason")
    else:
        value = item
    text = str(value).strip() if value not in (None, "") else "unknown"
    return text or "unknown"


def _source(item: Any, fallback: str = "unknown") -> str:
    if isinstance(item, Mapping):
        value = item.get("source") or item.get("source_id") or fallback
        return str(value).strip() or fallback
    return fallback


def _message_count(delivery: Mapping[str, Any]) -> int:
    declared = delivery.get("messages_sent")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared >= 0:
        return declared
    results = delivery.get("results")
    if isinstance(results, list) and results:
        return sum(
            1
            for item in results
            if not isinstance(item, Mapping) or item.get("ok") is not False
        )
    return 1


def _new_mission_stats() -> Dict[str, Any]:
    return {
        "identity": {"county": None, "state": None, "fips": None},
        "counts": Counter({key: 0 for key in _MISSION_COUNT_KEYS}),
    }


class DeploymentAccumulator:
    """Incrementally accumulate publication-relevant deployment statistics."""

    def __init__(self) -> None:
        self.json_objects = 0
        self.polls = Counter(total=0, ok=0, not_ok=0)
        self.non_poll_errors: Counter[str] = Counter()
        self.first_at: Optional[datetime] = None
        self.last_at: Optional[datetime] = None
        self.invalid_run_at = 0
        self.mission_activity = Counter(
            mission_opportunities=0,
            due=0,
            ran=0,
            cadence_skips=0,
            mission_errors=0,
            folded=0,
        )
        self.delivery = Counter(
            report_deliveries=0,
            report_messages_sent=0,
            materiality_assessed=0,
            material_updates=0,
            materiality_skips=0,
            delivery_failures=0,
            delivery_not_recorded=0,
            fold_deliveries=0,
        )
        self.delivery_skip_reasons: Counter[str] = Counter()
        self.fetch = Counter(total=0, coordinator_failed_fetches=0, county_api_fetch_errors=0)
        self.fetch_by_source: Counter[str] = Counter()
        self.fetch_by_reason: Counter[str] = Counter()
        self.extraction = Counter(total=0, no_gauges=0)
        self.extraction_by_source: Counter[str] = Counter()
        self.extraction_by_reason: Counter[str] = Counter()
        self.missions: MutableMapping[str, Dict[str, Any]] = defaultdict(_new_mission_stats)
        self.unattributed = Counter(opportunities=0, due=0, ran=0, skips=0, errors=0)
        self.accounting_mismatch_polls = 0
        self.observed_code_fields: set[str] = set()
        self.global_epochs: List[Dict[str, Any]] = []
        self.mission_epochs: MutableMapping[str, List[Dict[str, Any]]] = defaultdict(list)

    def add(self, record: Mapping[str, Any]) -> None:
        self.json_objects += 1
        if "missions_total" not in record:
            error = record.get("error")
            self.non_poll_errors[str(error or "non_poll_object")] += 1
            return

        self.polls["total"] += 1
        if record.get("ok") is True:
            self.polls["ok"] += 1
        else:
            self.polls["not_ok"] += 1

        run_at_raw = record.get("run_at")
        run_at = _iso_datetime(run_at_raw)
        if run_at is None:
            self.invalid_run_at += 1
        else:
            self.first_at = min(self.first_at, run_at) if self.first_at else run_at
            self.last_at = max(self.last_at, run_at) if self.last_at else run_at

        results = _objects(record.get("results"))
        skipped = _objects(record.get("skipped"))
        errors = _objects(record.get("errors"))
        folded = _objects(record.get("missions_folded"))

        reported_total = _integer(record.get("missions_total"), len(results) + len(skipped) + len(errors))
        reported_ran = _integer(record.get("missions_ran"), len(results))
        reported_skipped = _integer(record.get("missions_skipped"), len(skipped))
        reported_errors = _integer(record.get("missions_errored"), len(errors))

        self.mission_activity["mission_opportunities"] += reported_total
        self.mission_activity["ran"] += reported_ran
        self.mission_activity["cadence_skips"] += reported_skipped
        self.mission_activity["mission_errors"] += reported_errors
        self.mission_activity["due"] += reported_ran + reported_errors
        self.mission_activity["folded"] += len(folded)

        observed_total = len(results) + len(skipped) + len(errors)
        if (
            reported_total != observed_total
            or reported_ran != len(results)
            or reported_skipped != len(skipped)
            or reported_errors != len(errors)
        ):
            self.accounting_mismatch_polls += 1
        self.unattributed["opportunities"] += max(0, reported_total - observed_total)
        self.unattributed["ran"] += max(0, reported_ran - len(results))
        self.unattributed["skips"] += max(0, reported_skipped - len(skipped))
        self.unattributed["errors"] += max(0, reported_errors - len(errors))
        self.unattributed["due"] += max(
            0, reported_ran + reported_errors - len(results) - len(errors)
        )

        record_code = _code_fields(record)
        self.observed_code_fields.update(record_code)
        self._add_global_epoch(
            run_at_raw if run_at is not None else None,
            record.get("cron_interval_minutes"),
            record_code,
        )

        for item in results:
            mission = self._touch_mission(item, run_at_raw, record_code)
            if mission is not None:
                counts = mission["counts"]
                counts["mission_opportunities"] += 1
                counts["due"] += 1
                counts["ran"] += 1
                if item.get("mission_folded") is True:
                    counts["folded"] += 1
            self._add_result(item, mission)

        for item in skipped:
            mission = self._touch_mission(item, run_at_raw, record_code)
            if mission is not None:
                counts = mission["counts"]
                counts["mission_opportunities"] += 1
                counts["cadence_skips"] += 1

        for item in errors:
            mission = self._touch_mission(item, run_at_raw, record_code)
            if mission is not None:
                counts = mission["counts"]
                counts["mission_opportunities"] += 1
                counts["due"] += 1
                counts["mission_errors"] += 1

    def _touch_mission(
        self,
        item: Mapping[str, Any],
        run_at: Any,
        record_code: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        mission_id = item.get("mission_id")
        if not isinstance(mission_id, str) or not mission_id:
            return None
        mission = self.missions[mission_id]
        identity = _identity(item)
        for key, value in identity.items():
            if value not in (None, ""):
                mission["identity"][key] = str(value)

        item_code = dict(record_code)
        item_code.update(_code_fields(item))
        self.observed_code_fields.update(item_code)
        self._add_mission_epoch(
            mission_id,
            run_at if _iso_datetime(run_at) is not None else None,
            _heartbeat_interval(item),
            item_code,
        )
        return mission

    def _add_result(
        self,
        result: Mapping[str, Any],
        mission: Optional[Dict[str, Any]],
    ) -> None:
        counts = mission["counts"] if mission is not None else Counter()

        material = result.get("material_update")
        if isinstance(material, bool):
            self.delivery["materiality_assessed"] += 1
            counts["materiality_assessed"] += 1
            if material:
                self.delivery["material_updates"] += 1
                counts["material_updates"] += 1

        delivery = result.get("slack_delivery")
        materiality_skip = material is False
        if isinstance(delivery, Mapping):
            if delivery.get("ok") is True:
                messages = _message_count(delivery)
                self.delivery["report_deliveries"] += 1
                self.delivery["report_messages_sent"] += messages
                counts["report_deliveries"] += 1
                counts["report_messages_sent"] += messages
            elif delivery.get("skipped") is True:
                reason = str(delivery.get("reason") or "unknown")
                self.delivery_skip_reasons[reason] += 1
                if reason == "no_material_change":
                    materiality_skip = True
            elif delivery.get("ok") is False:
                self.delivery["delivery_failures"] += 1
                counts["delivery_failures"] += 1
            else:
                self.delivery["delivery_not_recorded"] += 1
                counts["delivery_not_recorded"] += 1
        else:
            self.delivery["delivery_not_recorded"] += 1
            counts["delivery_not_recorded"] += 1

        if materiality_skip:
            self.delivery["materiality_skips"] += 1
            counts["materiality_skips"] += 1

        fold_delivery = result.get("fold_slack_delivery")
        if isinstance(fold_delivery, Mapping) and fold_delivery.get("ok") is True:
            self.delivery["fold_deliveries"] += 1
            counts["fold_deliveries"] += 1

        for layer, source, reason in self._source_failures(result):
            self.fetch["total"] += 1
            self.fetch[layer] += 1
            self.fetch_by_source[source] += 1
            self.fetch_by_reason[reason] += 1
            counts["source_fetch_failures"] += 1
            if layer == "coordinator_failed_fetches":
                counts["coordinator_fetch_failures"] += 1
            else:
                counts["county_api_fetch_errors"] += 1

        for failure in _objects(result.get("extraction_failures")):
            reason = _reason(failure)
            source = _source(failure)
            self.extraction["total"] += 1
            self.extraction_by_reason[reason] += 1
            self.extraction_by_source[source] += 1
            counts["extraction_failures"] += 1
            if reason == "no_gauges":
                self.extraction["no_gauges"] += 1
                counts["no_gauges"] += 1

    @staticmethod
    def _source_failures(
        result: Mapping[str, Any],
    ) -> Iterator[tuple[str, str, str]]:
        for failure in _objects(result.get("failed_fetches")):
            yield "coordinator_failed_fetches", _source(failure), _reason(failure)

        payload = result.get("county_flood_payload")
        if not isinstance(payload, Mapping):
            return
        sources = payload.get("sources")
        if not isinstance(sources, Mapping):
            return
        for provider, metadata in sources.items():
            if not isinstance(metadata, Mapping):
                continue
            failures = metadata.get("fetch_errors")
            if not isinstance(failures, list):
                continue
            for failure in failures:
                yield (
                    "county_api_fetch_errors",
                    _source(failure, str(provider)),
                    _reason(failure),
                )

    def _add_global_epoch(
        self,
        run_at: Any,
        cron_interval: Any,
        code: Mapping[str, Any],
    ) -> None:
        if not isinstance(run_at, str):
            return
        cron_value = cron_interval if isinstance(cron_interval, int) else None
        signature = (cron_value, tuple(sorted(code.items())))
        if self.global_epochs and self.global_epochs[-1]["_signature"] == signature:
            epoch = self.global_epochs[-1]
            epoch["end_at"] = run_at
            epoch["polls"] += 1
            return
        self.global_epochs.append(
            {
                "_signature": signature,
                "start_at": run_at,
                "end_at": run_at,
                "polls": 1,
                "cron_interval_minutes": cron_value,
                "code": dict(code),
            }
        )

    def _add_mission_epoch(
        self,
        mission_id: str,
        run_at: Any,
        heartbeat_interval: Optional[int],
        code: Mapping[str, Any],
    ) -> None:
        if not isinstance(run_at, str):
            return
        signature = (heartbeat_interval, tuple(sorted(code.items())))
        epochs = self.mission_epochs[mission_id]
        if epochs and epochs[-1]["_signature"] == signature:
            epoch = epochs[-1]
            epoch["end_at"] = run_at
            epoch["opportunities"] += 1
            return
        epochs.append(
            {
                "_signature": signature,
                "mission_id": mission_id,
                "start_at": run_at,
                "end_at": run_at,
                "opportunities": 1,
                "heartbeat_interval_seconds": heartbeat_interval,
                "code": dict(code),
            }
        )

    def finalize(self, input_metadata: Mapping[str, Any]) -> Dict[str, Any]:
        counties: Dict[str, Dict[str, Any]] = {}
        for mission_id, mission in sorted(self.missions.items()):
            identity = mission["identity"]
            key = identity.get("fips") or "|".join(
                filter(None, (identity.get("county"), identity.get("state")))
            )
            key = key or f"mission:{mission_id}"
            if key not in counties:
                counties[key] = {
                    "county": identity.get("county"),
                    "state": identity.get("state"),
                    "fips": identity.get("fips"),
                    "mission_ids": [],
                    **{name: 0 for name in _MISSION_COUNT_KEYS},
                }
            county = counties[key]
            county["mission_ids"].append(mission_id)
            for name in _MISSION_COUNT_KEYS:
                county[name] += mission["counts"][name]

        county_rows = sorted(
            counties.values(),
            key=lambda row: (
                row.get("fips") or "",
                row.get("state") or "",
                row.get("county") or "",
            ),
        )
        for row in county_rows:
            row["mission_ids"].sort()

        mission_epochs: List[Dict[str, Any]] = []
        for mission_id, epochs in sorted(self.mission_epochs.items()):
            identity = self.missions[mission_id]["identity"]
            for stored in epochs:
                epoch = {key: value for key, value in stored.items() if key != "_signature"}
                epoch.update(identity)
                mission_epochs.append(epoch)

        global_epochs = [
            {key: value for key, value in epoch.items() if key != "_signature"}
            for epoch in self.global_epochs
        ]

        time_window: Dict[str, Any] = {
            "start_at": _canonical_iso(self.first_at) if self.first_at else None,
            "end_at": _canonical_iso(self.last_at) if self.last_at else None,
            "duration_seconds": None,
            "duration_days": None,
        }
        if self.first_at is not None and self.last_at is not None:
            seconds = (self.last_at - self.first_at).total_seconds()
            time_window["duration_seconds"] = round(seconds, 6)
            time_window["duration_days"] = round(seconds / 86400.0, 3)

        return {
            "schema": SCHEMA,
            "input": dict(input_metadata),
            "polls": {
                "json_objects": self.json_objects,
                "heartbeat_polls": self.polls["total"],
                "ok": self.polls["ok"],
                "not_ok": self.polls["not_ok"],
                "non_poll_objects": self.json_objects - self.polls["total"],
                "non_poll_errors_by_type": dict(sorted(self.non_poll_errors.items())),
                "time_window": time_window,
            },
            "mission_activity": dict(self.mission_activity),
            "delivery": {
                **dict(self.delivery),
                "other_skips_by_reason": {
                    reason: count
                    for reason, count in sorted(self.delivery_skip_reasons.items())
                    if reason != "no_material_change"
                },
            },
            "source_fetch_failures": {
                **dict(self.fetch),
                "by_source": dict(sorted(self.fetch_by_source.items())),
                "by_reason": dict(sorted(self.fetch_by_reason.items())),
            },
            "extraction_failures": {
                **dict(self.extraction),
                "by_source": dict(sorted(self.extraction_by_source.items())),
                "by_reason": dict(sorted(self.extraction_by_reason.items())),
            },
            "counties": county_rows,
            "configuration_epochs": {
                "observed_code_fields": sorted(self.observed_code_fields),
                "global": global_epochs,
                "mission_heartbeat": mission_epochs,
            },
            "data_quality": {
                "poll_accounting_mismatches": self.accounting_mismatch_polls,
                "invalid_or_missing_poll_times": self.invalid_run_at,
                "unattributed_reported_counts": dict(self.unattributed),
            },
        }


def summarize_deployment_log(path: Path | str) -> Dict[str, Any]:
    """Parse *path* and return a deterministic deployment summary."""

    log_path = Path(path)
    digest = hashlib.sha256()
    accumulator = DeploymentAccumulator()
    with log_path.open("rb") as stream:
        for record in iter_concatenated_json(stream, digest=digest):
            accumulator.add(record)
        bytes_read = stream.tell()
    return accumulator.finalize(
        {
            "path": str(log_path),
            "bytes": bytes_read,
            "sha256": digest.hexdigest(),
        }
    )


def _json_text(summary: Mapping[str, Any]) -> str:
    return json.dumps(summary, indent=2, sort_keys=True) + "\n"


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "log",
        type=Path,
        nargs="?",
        default=DEFAULT_LOG,
        help=f"concatenated-JSON cron log (default: {DEFAULT_LOG})",
    )
    parser.add_argument("--output", type=Path, help="write summary JSON instead of stdout")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.output is not None and args.output.resolve() == args.log.resolve():
        parser.error("refusing to overwrite the deployment log")

    try:
        summary = summarize_deployment_log(args.log)
    except (OSError, DeploymentLogParseError) as exc:
        parser.exit(1, f"deployment summary failed: {exc}\n")

    text = _json_text(summary)
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
