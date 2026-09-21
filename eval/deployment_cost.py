"""Operational-cost summary of the production heartbeat log.

Reads ``logs/sentinel-heartbeat-cron.log`` (concatenated pretty-printed JSON, one object per cron poll) exactly once through ``eval.analyze_deployment.iter_concatenated_json`` and writes ``eval/results/deployment_cost/summary.json`` and ``summary.md``. The log is never modified.

The unit of analysis is the due acquisition cycle: one entry of a poll's ``results`` list, which ``analyze_deployment`` counts as ``due`` and ``ran`` (``errors`` is empty in this log, so due equals ran). For those cycles the script reports:

1. Timing. The log carries no elapsed, duration or latency field. It does carry timestamps stamped at known points of the sequential cycle (``run_at`` on the poll, ``fetched_at``, ``article_evidence.generated_at`` and ``run_at`` on the result, and the Slack-assigned ``message_id`` of each delivered part), so wall-time spans between those points can be reconstructed. They are reported as reconstructed spans, not as recorded durations.
2. Source requests. What each cycle records about source fetch attempts and their failures, reconciled with ``eval/results/deployment_summary.json``.
3. Slack delivery. Logical reports and transport parts, reconciled with the same file.
4. Cadence. Acquisitions per county per day at the observed cadence, and what fixed clocks would require over the same mission-active durations.
5. Absence checks. External API quota, HTTP status codes, byte counts and token or LLM usage.

Every number in ``summary.md`` names the log field it was computed from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.analyze_deployment import DEFAULT_LOG, iter_concatenated_json  # noqa: E402

SCHEMA = "disaster-sentinel/deployment-cost/1"
OUT_DIR = ROOT / "eval" / "results" / "deployment_cost"
DEPLOYMENT_SUMMARY = ROOT / "eval" / "results" / "deployment_summary.json"

#: Fixed clocks compared against the observed cadence (label -> seconds).
CLOCKS: Dict[str, int] = {"15 min": 900, "1 h": 3600, "3 h": 10800, "6 h": 21600}

#: Code constants that govern how many HTTP requests one logged source attempt implies. They are read from the
#: skill source for the reader's orientation only; nothing in the summary treats them as measurements.
USGS_IV_BATCH_SIZE = 50  # skills/county-flood-data/county_flood/sources/stages.py

_SLACK_TS = re.compile(r"^\d{10}\.\d{1,6}$")
_HTTP_CODE = re.compile(r"HTTP(?: Error)?\s*(\d{3})")
_DUE_REASON = re.compile(r"(\d+)s elapsed, interval (\d+)s")
_ABSENCE_PROBES: Dict[str, re.Pattern[str]] = {
    "token_or_llm_usage": re.compile(r"token|usage|prompt|completion|llm|model", re.I),
    "quota_or_rate_limit": re.compile(r"quota|rate_limit|ratelimit|retry|retries|throttl", re.I),
    "http_status": re.compile(r"status_code|http_status|statuscode|response_code", re.I),
    "byte_counts": re.compile(r"bytes|byte_length|content_length|size", re.I),
    "timing": re.compile(r"elapsed|duration|latency|_ms$|took|timing|started_at|finished_at|ended_at", re.I),
    "cost": re.compile(r"cost|price|billing|credit", re.I),
}


# --------------------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------------------


def _dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _slack_ts(message_id: Any) -> Optional[datetime]:
    """A Slack message id is the server-assigned ``ts`` (epoch seconds with a fractional part)."""
    if not isinstance(message_id, str) or not _SLACK_TS.match(message_id):
        return None
    return datetime.fromtimestamp(float(message_id), tz=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _seconds(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[float]:
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds()


def _stats(values: Sequence[float]) -> Dict[str, Any]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"n": 0}
    out: Dict[str, Any] = {
        "n": len(clean),
        "min": round(min(clean), 3),
        "median": round(statistics.median(clean), 3),
        "max": round(max(clean), 3),
        "sum": round(sum(clean), 3),
    }
    if len(clean) >= 2:
        q1, _, q3 = statistics.quantiles(clean, n=4, method="exclusive")
        out["q1"] = round(q1, 3)
        out["q3"] = round(q3, 3)
    else:
        out["q1"] = out["q3"] = out["median"]
    return out


def _classify_error(text: str) -> str:
    if text == "no_iv_data":
        return "no_iv_data"
    match = _HTTP_CODE.search(text)
    if match:
        return f"http_{match.group(1)}"
    if "timed out" in text.lower() or "timeout" in text.lower():
        return "timeout"
    return "other"


def _walk_keys(value: Any, prefix: str, sink: Dict[str, str]) -> None:
    """Record every key path once, with the JSON type first seen at that path."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            sink.setdefault(path, type(child).__name__)
            _walk_keys(child, path, sink)
    elif isinstance(value, list):
        for child in value:
            _walk_keys(child, prefix + "[]", sink)


# --------------------------------------------------------------------------------------------------------------
# Accumulator
# --------------------------------------------------------------------------------------------------------------


class CostAccumulator:
    def __init__(self) -> None:
        self.json_objects = 0
        self.polls = 0
        self.non_poll_objects = 0
        self.poll_times: List[datetime] = []
        self.results_per_poll: Counter = Counter()
        self.key_paths: Dict[str, str] = {}
        # Item 1
        self.spans: Dict[str, List[float]] = defaultdict(list)
        self.spans_first_in_poll: Dict[str, List[float]] = defaultdict(list)
        self.poll_wall: List[float] = []
        self.span_anomalies: Counter = Counter()
        self.slack_ts_unparsed = 0
        self.slack_parts_seen = 0
        # Item 2
        self.sources_attempted: Counter = Counter()
        self.sources_succeeded: Counter = Counter()
        self.per_cycle: Dict[str, List[float]] = defaultdict(list)
        self.payload_present = 0
        self.payload_absent = 0
        self.fetch_error_by_provider_class: Counter = Counter()
        self.fetch_error_by_provider: Counter = Counter()
        self.coordinator_failed_fetches: Counter = Counter()
        self.cycles_with_any_source_error = 0
        self.cycles_with_only_no_iv_data = 0
        self.extraction_failures_by_reason: Counter = Counter()
        self.extraction_failures_by_source: Counter = Counter()
        self.cycles_with_extraction_failure = 0
        self.gauges_by_source: Counter = Counter()
        self.gauge_error_field_non_null = 0
        self.news_llm_verdict: Counter = Counter()
        # Item 3
        self.slack_ok = 0
        self.slack_messages_sent = 0
        self.slack_results_ok = 0
        self.slack_results_not_ok = 0
        self.slack_parts_prepared_delivered = 0
        self.slack_parts_prepared_all = 0
        self.slack_chars_delivered = 0
        self.slack_skipped: Counter = Counter()
        self.slack_failed = 0
        self.slack_not_recorded = 0
        self.fold_deliveries = 0
        self.slack_parts_per_delivery: List[float] = []
        self.slack_message_id_mismatch = 0
        # Item 4
        self.missions: Dict[str, Dict[str, Any]] = {}
        self.interval_realized: Dict[int, List[float]] = defaultdict(list)
        self.due_reason_unparsed: Counter = Counter()
        self.result_interval: Counter = Counter()
        self.phase_by_interval: Counter = Counter()
        self.cache_hit: Counter = Counter()
        # Item 5
        self.byte_length_by_source: Dict[str, List[float]] = defaultdict(list)
        self.http_codes_in_errors: Counter = Counter()
        self.csv_truncated: Counter = Counter()
        self.article_llm_verdict_null = 0
        self.article_llm_verdict_non_null = 0
        self.cron_fields: Optional[Dict[str, Any]] = None

    # ----------------------------------------------------------------------------------------------------------

    def _mission(self, mission_id: str, item: Mapping[str, Any]) -> Dict[str, Any]:
        mission = self.missions.get(mission_id)
        if mission is None:
            mission = {
                "mission_id": mission_id,
                "county": None,
                "state": None,
                "fips": None,
                "first_poll_at": None,
                "last_poll_at": None,
                "opportunities": 0,
                "due": 0,
                "cadence_skips": 0,
                "heartbeat_intervals_seconds": Counter(),
            }
            self.missions[mission_id] = mission
        spec = item.get("mission_spec") if isinstance(item.get("mission_spec"), Mapping) else {}
        for key, candidates in (
            ("county", (item.get("county_under_monitor"), item.get("county"), spec.get("county"))),
            ("state", (item.get("state_under_monitor"), item.get("state"), spec.get("state"))),
            ("fips", (item.get("fips"), spec.get("fips"))),
        ):
            for value in candidates:
                if isinstance(value, str) and value:
                    mission[key] = value
                    break
        return mission

    def _touch(self, mission: Dict[str, Any], poll_at: datetime) -> None:
        mission["opportunities"] += 1
        if mission["first_poll_at"] is None or poll_at < mission["first_poll_at"]:
            mission["first_poll_at"] = poll_at
        if mission["last_poll_at"] is None or poll_at > mission["last_poll_at"]:
            mission["last_poll_at"] = poll_at

    # ----------------------------------------------------------------------------------------------------------

    def add(self, record: Mapping[str, Any]) -> None:
        self.json_objects += 1
        _walk_keys(record, "", self.key_paths)
        if "missions_total" not in record:
            self.non_poll_objects += 1
            return
        self.polls += 1
        if self.cron_fields is None and isinstance(record.get("cron"), Mapping):
            self.cron_fields = {k: type(v).__name__ for k, v in record["cron"].items()}

        poll_at = _dt(record.get("run_at"))
        if poll_at is not None:
            self.poll_times.append(poll_at)

        results = [r for r in record.get("results") or [] if isinstance(r, Mapping)]
        self.results_per_poll[len(results)] += 1

        for item in record.get("skipped") or []:
            if not isinstance(item, Mapping):
                continue
            mission_id = item.get("mission_id")
            if isinstance(mission_id, str) and mission_id and poll_at is not None:
                mission = self._mission(mission_id, item)
                self._touch(mission, poll_at)
                mission["cadence_skips"] += 1
                interval = item.get("heartbeat_interval_seconds")
                if isinstance(interval, int) and not isinstance(interval, bool):
                    mission["heartbeat_intervals_seconds"][interval] += 1

        for item in record.get("errors") or []:
            if not isinstance(item, Mapping):
                continue
            mission_id = item.get("mission_id")
            if isinstance(mission_id, str) and mission_id and poll_at is not None:
                mission = self._mission(mission_id, item)
                self._touch(mission, poll_at)
                mission["due"] += 1

        cycle_start = poll_at
        poll_end = poll_at
        for index, result in enumerate(results):
            end = self._add_result(result, poll_at, cycle_start, index)
            if end is not None:
                cycle_start = end
                if poll_end is None or end > poll_end:
                    poll_end = end
        if results and poll_at is not None and poll_end is not None:
            self.poll_wall.append(_seconds(poll_end, poll_at) or 0.0)

    # ----------------------------------------------------------------------------------------------------------

    def _add_result(
        self,
        result: Mapping[str, Any],
        poll_at: Optional[datetime],
        cycle_start: Optional[datetime],
        index: int,
    ) -> Optional[datetime]:
        mission_id = result.get("mission_id")
        mission = None
        if isinstance(mission_id, str) and mission_id and poll_at is not None:
            mission = self._mission(mission_id, result)
            self._touch(mission, poll_at)
            mission["due"] += 1
            spec = result.get("mission_spec") if isinstance(result.get("mission_spec"), Mapping) else {}
            interval = spec.get("heartbeat_interval_seconds")
            if isinstance(interval, int) and not isinstance(interval, bool):
                mission["heartbeat_intervals_seconds"][interval] += 1
                self.result_interval[interval] += 1
                self.phase_by_interval[f"{spec.get('current_event_phase')}:{interval}"] += 1

        mapping = result.get("county_api_mapping")
        self.cache_hit[str(mapping.get("cache_hit")) if isinstance(mapping, Mapping) else "no_mapping"] += 1

        # ---- Item 4: realized inter-acquisition gap recorded by the scheduler in cycle_due_reason
        reason = result.get("cycle_due_reason")
        match = _DUE_REASON.search(reason) if isinstance(reason, str) else None
        if match:
            self.interval_realized[int(match.group(2))].append(float(match.group(1)))
        else:
            self.due_reason_unparsed[str(reason)] += 1

        # ---- Item 1: reconstructed spans
        fetched_at = _dt(result.get("fetched_at"))
        article = result.get("article_evidence") if isinstance(result.get("article_evidence"), Mapping) else {}
        article_at = _dt(article.get("generated_at"))
        phase = result.get("phase_decision") if isinstance(result.get("phase_decision"), Mapping) else {}
        phase_at = _dt(phase.get("evaluated_at_utc"))
        run_at = _dt(result.get("run_at"))

        delivery = result.get("slack_delivery") if isinstance(result.get("slack_delivery"), Mapping) else None
        slack_times: List[datetime] = []
        if delivery is not None and delivery.get("ok") is True:
            for part in delivery.get("results") or []:
                if not isinstance(part, Mapping):
                    continue
                self.slack_parts_seen += 1
                ts = _slack_ts(part.get("message_id"))
                if ts is None:
                    self.slack_ts_unparsed += 1
                else:
                    slack_times.append(ts)
        fold = result.get("fold_slack_delivery")
        fold_ts = _slack_ts(fold.get("message_id")) if isinstance(fold, Mapping) and fold.get("ok") is True else None
        if fold_ts is not None:
            slack_times.append(fold_ts)

        last_slack = max(slack_times) if slack_times else None
        end = last_slack or run_at

        spans = {
            "official_fetch_stage": _seconds(fetched_at, cycle_start),
            "news_stage": _seconds(article_at, fetched_at),
            "post_evidence_stage": _seconds(run_at, article_at),
            "phase_to_run_at": _seconds(run_at, phase_at),
            "compute": _seconds(run_at, cycle_start),
            "slack_delivery": _seconds(last_slack, run_at) if last_slack else None,
            "end_to_end": _seconds(end, cycle_start),
        }
        for name, value in spans.items():
            if value is None:
                continue
            if value < 0:
                self.span_anomalies[name] += 1
                continue
            self.spans[name].append(value)
            if index == 0:
                self.spans_first_in_poll[name].append(value)

        # ---- Item 2: source attempts and failures
        attempted = result.get("sources_attempted")
        succeeded = result.get("sources_succeeded")
        self.sources_attempted[attempted if isinstance(attempted, int) else "missing"] += 1
        self.sources_succeeded[succeeded if isinstance(succeeded, int) else "missing"] += 1

        cycle_error_classes: Counter = Counter()
        for failure in result.get("failed_fetches") or []:
            if isinstance(failure, Mapping):
                text = str(failure.get("failure_reason") or "unknown")
                klass = _classify_error(text)
                self.coordinator_failed_fetches[klass] += 1
                cycle_error_classes[klass] += 1
                code = _HTTP_CODE.search(text)
                if code:
                    self.http_codes_in_errors[f"coordinator:{code.group(1)}"] += 1

        payload = result.get("county_flood_payload")
        if isinstance(payload, Mapping):
            self.payload_present += 1
            sources = payload.get("sources") if isinstance(payload.get("sources"), Mapping) else {}
            nwps = sources.get("nwps") if isinstance(sources.get("nwps"), Mapping) else {}
            usgs = sources.get("usgs") if isinstance(sources.get("usgs"), Mapping) else {}
            alerts = sources.get("nws_alerts") if isinstance(sources.get("nws_alerts"), Mapping) else {}
            summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}

            self.per_cycle["nwps.csv_match_count"].append(float(nwps.get("csv_match_count") or 0))
            self.per_cycle["usgs.sites_selected"].append(float(usgs.get("sites_selected") or 0))
            self.per_cycle["usgs.discovered_sites"].append(float(usgs.get("discovered_sites") or 0))
            self.per_cycle["usgs.registry_sites"].append(float(usgs.get("registry_sites") or 0))
            self.per_cycle["nws_alerts.alert_count"].append(float(alerts.get("alert_count") or 0))
            self.per_cycle["summary.gauges_queried"].append(float(summary.get("gauges_queried") or 0))
            self.per_cycle["summary.gauges_with_data"].append(float(summary.get("gauges_with_data") or 0))
            self.per_cycle["summary.gauges_scored"].append(float(summary.get("gauges_scored") or 0))
            self.per_cycle["summary.gauges_unscored"].append(float(summary.get("gauges_unscored") or 0))
            self.per_cycle["len(gauges)"].append(float(len(payload.get("gauges") or [])))
            usgs_batches = math.ceil(float(usgs.get("sites_selected") or 0) / USGS_IV_BATCH_SIZE)
            self.per_cycle["code_implied_official_http_get_lower_bound"].append(
                float(nwps.get("csv_match_count") or 0) + usgs_batches + 1.0
            )
            for flag_source, meta in (("nwps", nwps), ("usgs", usgs)):
                self.csv_truncated[f"{flag_source}:{meta.get('csv_truncated')}"] += 1

            for gauge in payload.get("gauges") or []:
                if isinstance(gauge, Mapping):
                    self.gauges_by_source[str(gauge.get("source"))] += 1
                    if gauge.get("error") is not None:
                        self.gauge_error_field_non_null += 1

            for provider, meta in sources.items():
                if not isinstance(meta, Mapping):
                    continue
                for failure in meta.get("fetch_errors") or []:
                    if not isinstance(failure, Mapping):
                        continue
                    text = str(failure.get("error") or "unknown")
                    klass = _classify_error(text)
                    self.fetch_error_by_provider_class[f"{provider}:{klass}"] += 1
                    self.fetch_error_by_provider[str(provider)] += 1
                    cycle_error_classes[klass] += 1
                    code = _HTTP_CODE.search(text)
                    if code:
                        self.http_codes_in_errors[f"{provider}:{code.group(1)}"] += 1
        else:
            self.payload_absent += 1

        if cycle_error_classes:
            self.cycles_with_any_source_error += 1
            if set(cycle_error_classes) == {"no_iv_data"}:
                self.cycles_with_only_no_iv_data += 1

        extraction = [f for f in result.get("extraction_failures") or [] if isinstance(f, Mapping)]
        if extraction:
            self.cycles_with_extraction_failure += 1
        for failure in extraction:
            self.extraction_failures_by_reason[str(failure.get("failure_reason") or "unknown")] += 1
            self.extraction_failures_by_source[str(failure.get("source_id") or "unknown")] += 1

        search = article.get("search_summary") if isinstance(article.get("search_summary"), Mapping) else {}
        for key in ("queries_run", "candidates_found", "candidates_fetched", "candidates_scored"):
            value = search.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.per_cycle[f"article_evidence.search_summary.{key}"].append(float(value))
        above = article.get("articles_above_threshold")
        if isinstance(above, int):
            self.per_cycle["article_evidence.articles_above_threshold"].append(float(above))
        for bucket in ("top_sources", "high_confidence_articles"):
            for art in article.get(bucket) or []:
                if isinstance(art, Mapping) and "llm_verdict" in art:
                    if art.get("llm_verdict") is None:
                        self.article_llm_verdict_null += 1
                    else:
                        self.article_llm_verdict_non_null += 1

        # ---- Item 3: Slack
        prepared = result.get("slack_messages") if isinstance(result.get("slack_messages"), list) else []
        self.slack_parts_prepared_all += len(prepared)
        if delivery is None:
            self.slack_not_recorded += 1
        elif delivery.get("ok") is True:
            self.slack_ok += 1
            sent = delivery.get("messages_sent")
            parts = [p for p in delivery.get("results") or [] if isinstance(p, Mapping)]
            sent_count = sent if isinstance(sent, int) and not isinstance(sent, bool) else len(parts)
            self.slack_messages_sent += sent_count
            self.slack_parts_per_delivery.append(float(sent_count))
            self.slack_results_ok += sum(1 for p in parts if p.get("ok") is True)
            self.slack_results_not_ok += sum(1 for p in parts if p.get("ok") is not True)
            self.slack_parts_prepared_delivered += len(prepared)
            self.slack_chars_delivered += sum(len(str(m)) for m in prepared)
            if parts and delivery.get("message_id") != parts[-1].get("message_id"):
                self.slack_message_id_mismatch += 1
        elif delivery.get("skipped") is True:
            self.slack_skipped[str(delivery.get("reason") or "unknown")] += 1
        elif delivery.get("ok") is False:
            self.slack_failed += 1
        else:
            self.slack_not_recorded += 1
        if isinstance(fold, Mapping) and fold.get("ok") is True:
            self.fold_deliveries += 1

        # ---- Item 5: persisted snapshot sizes
        for snap in result.get("source_snapshots") or []:
            if isinstance(snap, Mapping):
                length = snap.get("byte_length")
                if isinstance(length, (int, float)) and not isinstance(length, bool):
                    source_id = str(snap.get("source_id") or "")
                    family = re.sub(r"_\d{5}$", "", source_id)
                    self.byte_length_by_source[family].append(float(length))

        return end

    # ----------------------------------------------------------------------------------------------------------

    def finalize(self, input_metadata: Mapping[str, Any]) -> Dict[str, Any]:
        due_total = sum(m["due"] for m in self.missions.values())

        # ---- Item 4: cadence tables
        mission_rows: List[Dict[str, Any]] = []
        for mission_id, mission in sorted(self.missions.items()):
            first, last = mission["first_poll_at"], mission["last_poll_at"]
            duration = _seconds(last, first) or 0.0
            days = duration / 86400.0
            clocks = {label: (math.floor(duration / secs) + 1 if duration >= 0 else None) for label, secs in CLOCKS.items()}
            mission_rows.append(
                {
                    "mission_id": mission_id,
                    "county": mission["county"],
                    "state": mission["state"],
                    "fips": mission["fips"],
                    "first_poll_at": _iso(first),
                    "last_poll_at": _iso(last),
                    "active_duration_seconds": round(duration, 3),
                    "active_duration_days": round(days, 3),
                    "opportunities": mission["opportunities"],
                    "due_acquisitions": mission["due"],
                    "cadence_skips": mission["cadence_skips"],
                    "due_per_day": round(mission["due"] / days, 3) if days > 0 else None,
                    "heartbeat_intervals_seconds": dict(sorted(mission["heartbeat_intervals_seconds"].items())),
                    "clock_acquisitions_required": clocks,
                }
            )

        county_rows: Dict[str, Dict[str, Any]] = {}
        for row in mission_rows:
            key = row["fips"] or f"{row['county']}|{row['state']}"
            county = county_rows.setdefault(
                key,
                {
                    "county": row["county"],
                    "state": row["state"],
                    "fips": row["fips"],
                    "mission_ids": [],
                    "active_duration_days": 0.0,
                    "due_acquisitions": 0,
                    "clock_acquisitions_required": {label: 0 for label in CLOCKS},
                },
            )
            county["mission_ids"].append(row["mission_id"])
            county["active_duration_days"] += row["active_duration_days"]
            county["due_acquisitions"] += row["due_acquisitions"]
            for label in CLOCKS:
                county["clock_acquisitions_required"][label] += row["clock_acquisitions_required"][label] or 0
        for county in county_rows.values():
            days = county["active_duration_days"]
            county["active_duration_days"] = round(days, 3)
            county["due_per_day"] = round(county["due_acquisitions"] / days, 3) if days > 0 else None
            county["clock_per_day"] = {
                label: round(count / days, 3) if days > 0 else None
                for label, count in county["clock_acquisitions_required"].items()
            }
            county["observed_over_clock"] = {
                label: round(county["due_acquisitions"] / count, 4) if count else None
                for label, count in county["clock_acquisitions_required"].items()
            }
        county_list = sorted(county_rows.values(), key=lambda r: (r["fips"] or "", r["county"] or ""))

        total_days = sum(r["active_duration_days"] for r in mission_rows)
        totals = {
            "mission_active_days_summed": round(total_days, 3),
            "due_acquisitions": due_total,
            "due_per_mission_day": round(due_total / total_days, 3) if total_days > 0 else None,
            "clock_acquisitions_required": {
                label: sum(r["clock_acquisitions_required"][label] or 0 for r in mission_rows) for label in CLOCKS
            },
        }
        totals["observed_over_clock"] = {
            label: round(due_total / count, 4) if count else None
            for label, count in totals["clock_acquisitions_required"].items()
        }

        realized = {
            str(interval): {
                **_stats(values),
                "median_over_configured": round(statistics.median(values) / interval, 4) if values else None,
            }
            for interval, values in sorted(self.interval_realized.items())
        }

        poll_gaps: List[float] = []
        long_gaps: List[Dict[str, Any]] = []
        for earlier, later in zip(self.poll_times, self.poll_times[1:]):
            gap = (later - earlier).total_seconds()
            poll_gaps.append(gap)
            if gap > 1800:
                inside = [
                    m["mission_id"]
                    for m in self.missions.values()
                    if m["first_poll_at"] is not None
                    and m["last_poll_at"] is not None
                    and m["first_poll_at"] <= earlier
                    and m["last_poll_at"] >= later
                ]
                long_gaps.append(
                    {
                        "from_poll_run_at": _iso(earlier),
                        "to_poll_run_at": _iso(later),
                        "gap_seconds": round(gap, 3),
                        "mission_windows_spanning_gap": sorted(inside),
                    }
                )

        # ---- Item 5: key-path probes
        probes: Dict[str, Dict[str, str]] = {}
        for name, pattern in _ABSENCE_PROBES.items():
            hits = {path: kind for path, kind in self.key_paths.items() if pattern.search(path.rsplit(".", 1)[-1])}
            probes[name] = dict(sorted(hits.items()))

        return {
            "schema": SCHEMA,
            "input": dict(input_metadata),
            "parse": {
                "json_objects": self.json_objects,
                "heartbeat_polls": self.polls,
                "non_poll_objects": self.non_poll_objects,
                "first_poll_at": _iso(min(self.poll_times)) if self.poll_times else None,
                "last_poll_at": _iso(max(self.poll_times)) if self.poll_times else None,
                "results_per_poll": {str(k): v for k, v in sorted(self.results_per_poll.items())},
                "poll_gap_seconds": _stats(poll_gaps),
                "poll_gaps_over_1800_seconds": long_gaps,
                "due_cycles": due_total,
                "distinct_key_paths": len(self.key_paths),
            },
            "item1_timing": {
                "recorded_duration_fields": [],
                "timestamp_fields_used": {
                    "cycle_start": "poll run_at for the first result of a poll; otherwise the previous result's end (its last Slack message_id ts, or its run_at when nothing was delivered)",
                    "fetched_at": "results[].fetched_at (county_flood_payload.generated_at, stamped when the official payload is assembled after all three official sources return)",
                    "article_at": "results[].article_evidence.generated_at (stamped when the news detector returns)",
                    "phase_at": "results[].phase_decision.evaluated_at_utc",
                    "run_at": "results[].run_at (stamped at the end of _run_coordinator_cycle, after Neo4j persistence and the delta report, before Slack delivery)",
                    "slack_ts": "results[].slack_delivery.results[].message_id and results[].fold_slack_delivery.message_id (Slack-assigned ts, epoch seconds)",
                },
                "spans_seconds_all_due_cycles": {name: _stats(values) for name, values in sorted(self.spans.items())},
                "spans_seconds_first_result_in_poll": {
                    name: _stats(values) for name, values in sorted(self.spans_first_in_poll.items())
                },
                "poll_wall_seconds_polls_with_results": _stats(self.poll_wall),
                "negative_span_count_by_name": dict(sorted(self.span_anomalies.items())),
                "slack_message_ids_seen": self.slack_parts_seen,
                "slack_message_ids_not_parseable_as_ts": self.slack_ts_unparsed,
                "per_source_fetch_latency_fields": [],
            },
            "item2_source_requests": {
                "sources_attempted_per_cycle": {str(k): v for k, v in sorted(self.sources_attempted.items(), key=str)},
                "sources_succeeded_per_cycle": {str(k): v for k, v in sorted(self.sources_succeeded.items(), key=str)},
                "county_flood_payload_present": self.payload_present,
                "county_flood_payload_absent": self.payload_absent,
                "county_api_mapping_cache_hit": dict(sorted(self.cache_hit.items())),
                "per_cycle_fields": {name: _stats(values) for name, values in sorted(self.per_cycle.items())},
                "gauge_rows_by_source": dict(sorted(self.gauges_by_source.items())),
                "gauge_rows_with_non_null_error": self.gauge_error_field_non_null,
                "fetch_errors_by_provider": dict(sorted(self.fetch_error_by_provider.items())),
                "fetch_errors_by_provider_and_class": dict(sorted(self.fetch_error_by_provider_class.items())),
                "coordinator_failed_fetches_by_class": dict(sorted(self.coordinator_failed_fetches.items())),
                "source_fetch_error_total": sum(self.fetch_error_by_provider.values())
                + sum(self.coordinator_failed_fetches.values()),
                "cycles_with_any_source_error": self.cycles_with_any_source_error,
                "cycles_with_only_no_iv_data_errors": self.cycles_with_only_no_iv_data,
                "extraction_failures_by_reason": dict(sorted(self.extraction_failures_by_reason.items())),
                "extraction_failures_by_source": dict(sorted(self.extraction_failures_by_source.items())),
                "extraction_failure_total": sum(self.extraction_failures_by_reason.values()),
                "cycles_with_extraction_failure": self.cycles_with_extraction_failure,
                "code_constants": {"USGS_IV_BATCH_SIZE": USGS_IV_BATCH_SIZE},
            },
            "item3_slack": {
                "logical_reports_delivered": self.slack_ok,
                "transport_parts_sent": self.slack_messages_sent,
                "transport_parts_ok": self.slack_results_ok,
                "transport_parts_not_ok": self.slack_results_not_ok,
                "parts_per_delivered_report": _stats(self.slack_parts_per_delivery),
                "slack_messages_prepared_all_cycles": self.slack_parts_prepared_all,
                "slack_messages_prepared_delivered_cycles": self.slack_parts_prepared_delivered,
                "slack_messages_chars_delivered_cycles": self.slack_chars_delivered,
                "skipped_by_reason": dict(sorted(self.slack_skipped.items())),
                "delivery_failures": self.slack_failed,
                "delivery_not_recorded": self.slack_not_recorded,
                "fold_deliveries": self.fold_deliveries,
                "message_id_not_last_part": self.slack_message_id_mismatch,
            },
            "item4_cadence": {
                "clock_definitions_seconds": dict(CLOCKS),
                "clock_rule": "floor(active_duration_seconds / interval) + 1 acquisitions per mission",
                "heartbeat_interval_seconds_on_due_results": {str(k): v for k, v in sorted(self.result_interval.items())},
                "phase_by_interval_on_due_results": dict(sorted(self.phase_by_interval.items())),
                "realized_elapsed_seconds_by_configured_interval": realized,
                "cycle_due_reason_unparsed": dict(self.due_reason_unparsed),
                "missions": mission_rows,
                "counties": county_list,
                "totals": totals,
            },
            "item5_absence_checks": {
                "key_path_probes": probes,
                "http_status_codes_only_inside_error_strings": dict(sorted(self.http_codes_in_errors.items())),
                "article_llm_verdict_null": self.article_llm_verdict_null,
                "article_llm_verdict_non_null": self.article_llm_verdict_non_null,
                "source_snapshot_byte_length_by_source_family": {
                    name: _stats(values) for name, values in sorted(self.byte_length_by_source.items())
                },
                "csv_truncated_flags": dict(sorted(self.csv_truncated.items())),
                "cron_block_fields": self.cron_fields or {},
                "verification_trigger_key_present": any(p.endswith("verification_trigger") for p in self.key_paths),
                "ontology_ledger_key_present": any(p.endswith("ontology_ledger") for p in self.key_paths),
            },
        }


# --------------------------------------------------------------------------------------------------------------
# Reconciliation with the published deployment summary
# --------------------------------------------------------------------------------------------------------------


def reconcile(summary: Dict[str, Any], published_path: Path) -> Dict[str, Any]:
    if not published_path.exists():
        return {"published_path": str(published_path), "available": False}
    published = json.loads(published_path.read_text(encoding="utf-8"))
    checks: Dict[str, Dict[str, Any]] = {}

    def check(name: str, ours: Any, theirs: Any) -> None:
        checks[name] = {"this_script": ours, "deployment_summary": theirs, "match": ours == theirs}

    check("input.sha256", summary["input"]["sha256"], published.get("input", {}).get("sha256"))
    check("due_cycles", summary["parse"]["due_cycles"], published.get("mission_activity", {}).get("due"))
    check("heartbeat_polls", summary["parse"]["heartbeat_polls"], published.get("polls", {}).get("heartbeat_polls"))
    check(
        "source_fetch_error_total",
        summary["item2_source_requests"]["source_fetch_error_total"],
        published.get("source_fetch_failures", {}).get("total"),
    )
    check(
        "coordinator_failed_fetches",
        sum(summary["item2_source_requests"]["coordinator_failed_fetches_by_class"].values()),
        published.get("source_fetch_failures", {}).get("coordinator_failed_fetches"),
    )
    check(
        "county_api_fetch_errors",
        sum(summary["item2_source_requests"]["fetch_errors_by_provider"].values()),
        published.get("source_fetch_failures", {}).get("county_api_fetch_errors"),
    )
    check(
        "no_iv_data",
        summary["item2_source_requests"]["fetch_errors_by_provider_and_class"].get("usgs:no_iv_data"),
        published.get("source_fetch_failures", {}).get("by_reason", {}).get("no_iv_data"),
    )
    check(
        "extraction_failures_no_gauges",
        summary["item2_source_requests"]["extraction_failures_by_reason"].get("no_gauges"),
        published.get("extraction_failures", {}).get("no_gauges"),
    )
    check(
        "report_deliveries",
        summary["item3_slack"]["logical_reports_delivered"],
        published.get("delivery", {}).get("report_deliveries"),
    )
    check(
        "report_messages_sent",
        summary["item3_slack"]["transport_parts_sent"],
        published.get("delivery", {}).get("report_messages_sent"),
    )
    check(
        "materiality_skips",
        summary["item3_slack"]["skipped_by_reason"].get("no_material_change"),
        published.get("delivery", {}).get("materiality_skips"),
    )
    check("fold_deliveries", summary["item3_slack"]["fold_deliveries"], published.get("delivery", {}).get("fold_deliveries"))

    # Mission-active windows against configuration_epochs.mission_heartbeat
    epochs = published.get("configuration_epochs", {}).get("mission_heartbeat", [])
    windows: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for epoch in epochs:
        mission_id = epoch.get("mission_id")
        start, end = epoch.get("start_at"), epoch.get("end_at")
        if not mission_id:
            continue
        old = windows.get(mission_id)
        windows[mission_id] = (
            min(filter(None, (old[0] if old else None, start))) if start or old else None,
            max(filter(None, (old[1] if old else None, end))) if end or old else None,
        )
    mission_checks = []
    for row in summary["item4_cadence"]["missions"]:
        theirs = windows.get(row["mission_id"], (None, None))
        mission_checks.append(
            {
                "mission_id": row["mission_id"],
                "first_poll_at_match": row["first_poll_at"] == theirs[0],
                "last_poll_at_match": row["last_poll_at"] == theirs[1],
                "opportunities_match": row["opportunities"]
                == sum(e.get("opportunities", 0) for e in epochs if e.get("mission_id") == row["mission_id"]),
            }
        )
    return {
        "published_path": str(published_path.relative_to(ROOT)),
        "available": True,
        "checks": checks,
        "all_checks_match": all(c["match"] for c in checks.values()),
        "mission_window_checks": mission_checks,
        "all_mission_windows_match": all(
            c["first_poll_at_match"] and c["last_poll_at_match"] and c["opportunities_match"] for c in mission_checks
        ),
    }


# --------------------------------------------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _stat_row(label: str, field: str, stats: Mapping[str, Any], digits: int = 1) -> str:
    if not stats or not stats.get("n"):
        return f"| {label} | `{field}` | 0 | n/a | n/a | n/a | n/a |"
    return (
        f"| {label} | `{field}` | {_fmt(stats['n'])} | {_fmt(stats['median'], digits)} | "
        f"{_fmt(stats.get('q1'), digits)} to {_fmt(stats.get('q3'), digits)} | {_fmt(stats['max'], digits)} | {_fmt(stats['sum'], digits)} |"
    )


def render_markdown(summary: Dict[str, Any], recon: Dict[str, Any]) -> str:
    p = summary["parse"]
    t1 = summary["item1_timing"]
    t2 = summary["item2_source_requests"]
    t3 = summary["item3_slack"]
    t4 = summary["item4_cadence"]
    t5 = summary["item5_absence_checks"]
    lines: List[str] = []
    add = lines.append

    add("# Operational cost of the production deployment (read from the heartbeat cron log)")
    add("")
    add(
        f"Source: `{summary['input']['path']}` ({summary['input']['bytes']:,} bytes, sha256 `{summary['input']['sha256']}`), parsed once with `eval.analyze_deployment.iter_concatenated_json`. "
        f"The log holds {p['json_objects']:,} JSON objects: {p['heartbeat_polls']:,} heartbeat polls (objects with `missions_total`) from `run_at` {p['first_poll_at']} to {p['last_poll_at']}, and {p['non_poll_objects']} non-poll object. "
        f"The unit of analysis below is the due acquisition cycle, one entry of a poll's `results` list; there are {p['due_cycles']:,} of them, and `errors` is empty throughout, so due equals ran. "
        f"{p['results_per_poll'].get('0', 0):,} polls ran no cycle, {p['results_per_poll'].get('1', 0):,} ran one and {p['results_per_poll'].get('2', 0):,} ran two. "
        f"Consecutive poll `run_at` gaps: median {_fmt(p['poll_gap_seconds'].get('median'))} s, interquartile range {_fmt(p['poll_gap_seconds'].get('q1'))} to {_fmt(p['poll_gap_seconds'].get('q3'))} s, maximum {_fmt(p['poll_gap_seconds'].get('max'))} s. "
        + (
            "Gaps longer than 1,800 s: "
            + "; ".join(
                f"{g['from_poll_run_at']} to {g['to_poll_run_at']} ({_fmt(g['gap_seconds'] / 3600, 2)} h), which lies inside the active window of "
                + (", ".join(f"`{m}`" for m in g["mission_windows_spanning_gap"]) if g["mission_windows_spanning_gap"] else "no mission, so it inflates no mission's duration")
                for g in p["poll_gaps_over_1800_seconds"]
            )
            + "."
            if p["poll_gaps_over_1800_seconds"]
            else "No gap between consecutive polls exceeds 1,800 s."
        )
    )
    add("")
    add("Medians and interquartile ranges use `statistics.median` and `statistics.quantiles(n=4, method=\"exclusive\")`. Every number names the field it came from. Nothing here is converted to money.")
    add("")

    # ---------------------------------------------------------------- Item 1
    add("## 1. Per-cycle timing")
    add("")
    add(
        "The log records no elapsed, duration or latency field at any level: not on the poll, not on the result, not on any source, gauge, snapshot or Slack part. "
        "The key-path probe for timing-like names (`elapsed`, `duration`, `latency`, `_ms`, `took`, `timing`, `started_at`, `finished_at`, `ended_at`) matches only "
        + (", ".join(f"`{k}`" for k in t5["key_path_probes"]["timing"]) or "nothing")
        + ", none of which is a measured duration (`ended_at` is the mission deactivation time written once when the Escambia mission folded). Per-source fetch latencies do not exist in the log."
    )
    add("")
    add(
        "What the log does carry are timestamps stamped at fixed points of a cycle that runs strictly sequentially inside one cron process (`cmd_run_heartbeat` iterates missions in a plain loop; `_run_coordinator_cycle` fetches the official sources, then the news detector, then persists and diffs, and only then is Slack called). "
        "Wall-time spans between those stamps can therefore be reconstructed. They are reconstructed spans, not recorded durations, and they inherit two caveats: the start of a cycle that is not the first in its poll is taken as the previous cycle's end, which absorbs the between-mission bookkeeping (verification-trigger consultation, off in this deployment, and the Neo4j `get_mission_context` read); and the Slack stamp is the server-assigned `ts` on the accepted post, so the delivery span includes the `openclaw message send` subprocess and the 0.3 s inter-part sleep (`MULTI_MESSAGE_SEND_DELAY_SEC`) as well as network time."
    )
    add("")
    add("Stamps used (all UTC): " + "; ".join(f"**{k}**: {v}" for k, v in t1["timestamp_fields_used"].items()) + ".")
    add("")
    add(f"| Span | Definition | n | Median (s) | IQR (s) | Max (s) | Sum (s) |")
    add("|---|---|---|---|---|---|---|")
    defs = {
        "official_fetch_stage": "fetched_at minus cycle_start",
        "news_stage": "article_evidence.generated_at minus fetched_at",
        "post_evidence_stage": "run_at minus article_evidence.generated_at",
        "compute": "run_at minus cycle_start",
        "slack_delivery": "last Slack message_id ts minus run_at (delivered cycles)",
        "end_to_end": "last Slack ts (or run_at when nothing was delivered) minus cycle_start",
    }
    for name, definition in defs.items():
        add(_stat_row(name, definition, t1["spans_seconds_all_due_cycles"].get(name, {})))
    add("")
    add(
        f"Restricted to the first result of each poll, where `cycle_start` is exactly the poll `run_at`; the `official_fetch_stage` span then still includes the Neo4j reads that precede the fetch (`get_mission_context`, `_phase_aware_context`, `get_last_coordinator_report`, prior gauge readings) (n = {t1['spans_seconds_first_result_in_poll'].get('compute', {}).get('n', 0)}):"
    )
    add("")
    add(f"| Span | Definition | n | Median (s) | IQR (s) | Max (s) | Sum (s) |")
    add("|---|---|---|---|---|---|---|")
    for name, definition in defs.items():
        add(_stat_row(name, definition, t1["spans_seconds_first_result_in_poll"].get(name, {})))
    add("")
    pw = t1["poll_wall_seconds_polls_with_results"]
    add(
        f"Whole-poll wall time for polls that ran at least one cycle (last Slack ts or result `run_at` in the poll minus poll `run_at`; n = {pw.get('n', 0)}): median {_fmt(pw.get('median'))} s, interquartile range {_fmt(pw.get('q1'))} to {_fmt(pw.get('q3'))} s, maximum {_fmt(pw.get('max'))} s. "
        f"Of {t1['slack_message_ids_seen']:,} Slack `message_id` values on delivered parts, {t1['slack_message_ids_not_parseable_as_ts']} failed to parse as a Slack `ts`. "
        f"Negative spans (a later stamp preceding an earlier one) were excluded and counted: {json.dumps(t1['negative_span_count_by_name']) if t1['negative_span_count_by_name'] else 'none'}."
    )
    add("")

    # ---------------------------------------------------------------- Item 2
    add("## 2. Source requests per due cycle")
    add("")
    sa = t2["sources_attempted_per_cycle"]
    ss = t2["sources_succeeded_per_cycle"]
    add(
        f"`sources_attempted` is {', '.join(f'{k} in {v:,} cycles' for k, v in sa.items())}; `sources_succeeded` is {', '.join(f'{k} in {v:,} cycles' for k, v in ss.items())}. "
        "These count the three official source records the coordinator asks the county-flood-data skill for (`source_records[]` with `source_id` `src_county_flood_nwps_*`, `src_county_flood_usgs_*`, `src_county_flood_nws_alerts_*`), not HTTP requests; `county_flood_source_mode` is `both` in every cycle. "
        f"The skill returned a payload in {t2['county_flood_payload_present']:,} cycles and none in {t2['county_flood_payload_absent']:,} (`county_flood_payload` null, `sources_succeeded` 0, one `failed_fetches[]` entry). "
        "The news detector is not counted in `sources_attempted`; it appears as `article_evidence` in every cycle."
    )
    add("")
    add("Per-cycle quantities the payload records (cycles with a payload):")
    add("")
    add("| Quantity | Field | n | Median | IQR | Max | Total |")
    add("|---|---|---|---|---|---|---|")
    for label, field in (
        ("NWPS gauges selected (one `GET /nwps/v1/gauges/{shef_id}` each in `nwps._fetch_one`)", "county_flood_payload.sources.nwps.csv_match_count"),
        ("USGS sites selected (batched into one NWIS IV `GET` per 50 sites in `fetch_usgs_iv`)", "county_flood_payload.sources.usgs.sites_selected"),
        ("USGS sites found by discovery", "county_flood_payload.sources.usgs.discovered_sites"),
        ("NWS alerts returned (one `GET` to `api.weather.gov/alerts/active` per cycle)", "county_flood_payload.sources.nws_alerts.alert_count"),
        ("Gauge rows in the payload", "len(county_flood_payload.gauges)"),
        ("Gauges queried (summary)", "county_flood_payload.summary.gauges_queried"),
        ("Gauges with data", "county_flood_payload.summary.gauges_with_data"),
        ("Gauges scored against a flood stage", "county_flood_payload.summary.gauges_scored"),
        ("Gauges unscored", "county_flood_payload.summary.gauges_unscored"),
    ):
        key = field.replace("county_flood_payload.sources.", "").replace("county_flood_payload.", "").replace("len(county_flood_payload.gauges)", "len(gauges)")
        add(_stat_row(label, field, t2["per_cycle_fields"].get(key, {}), digits=0))
    add("")
    lb = t2["per_cycle_fields"].get("code_implied_official_http_get_lower_bound", {})
    add(
        f"The log does not record HTTP requests. Reading the skill source, one payload implies at least `csv_match_count` NWPS requests plus ceil(`sites_selected`/50) NWIS IV requests plus one NWS alerts request; USGS site discovery, county-to-API mapping (`county_api_mapping.cache_hit` by value: {json.dumps(t2['county_api_mapping_cache_hit'])}) and NWPS threshold enrichment add requests the log does not count. "
        f"That code-implied lower bound is median {_fmt(lb.get('median'), 0)} official GETs per cycle (interquartile range {_fmt(lb.get('q1'), 0)} to {_fmt(lb.get('q3'), 0)}, maximum {_fmt(lb.get('max'), 0)}, sum {_fmt(lb.get('sum'), 0)} over {lb.get('n', 0):,} cycles). It is an inference from code, not a measurement."
    )
    add("")
    add("News (`article_evidence.search_summary`): the detector issues one Google News RSS `GET` per cycle (`search_google_news_rss`) and then one `curl` page fetch per candidate the secondary web search surfaces. `queries_run` is `max(1, len(build_article_queries(...)))` in `discover_and_fetch`, a constant 5 that does not count requests issued.")
    add("")
    add("| Quantity | Field | n | Median | IQR | Max | Total |")
    add("|---|---|---|---|---|---|---|")
    for label, field in (
        ("Queries declared (constant by construction)", "article_evidence.search_summary.queries_run"),
        ("Candidates found", "article_evidence.search_summary.candidates_found"),
        ("Candidates fetched (RSS items kept plus pages fetched)", "article_evidence.search_summary.candidates_fetched"),
        ("Candidates scored", "article_evidence.search_summary.candidates_scored"),
        ("Articles above the 0.75 threshold", "article_evidence.articles_above_threshold"),
    ):
        add(_stat_row(label, field, t2["per_cycle_fields"].get(field, {}), digits=0))
    add("")
    add("### Fetch errors")
    add("")
    add(
        f"Total source fetch errors: {t2['source_fetch_error_total']:,} = {sum(t2['fetch_errors_by_provider'].values()):,} entries in `county_flood_payload.sources.<provider>.fetch_errors[]` plus {sum(t2['coordinator_failed_fetches_by_class'].values()):,} coordinator-level `failed_fetches[]` entries (whole-skill failures in cycles with no payload). "
        f"They fall in {t2['cycles_with_any_source_error']:,} of the {p['due_cycles']:,} due cycles; in {t2['cycles_with_only_no_iv_data_errors']:,} of those the only error class is `no_iv_data`."
    )
    add("")
    add("| Provider and class | Field | Count | Meaning |")
    add("|---|---|---|---|")
    meanings = {
        "usgs:no_iv_data": "a selected site absent from a successful bulk NWIS IV response (`fetch_usgs_county_gauges` appends `{site_no, error: 'no_iv_data'}` per missing site); not a failed request",
        "usgs:http_400": "the bulk NWIS IV request itself failed with HTTP 400 (`{source: 'usgs_iv', error}`); all selected sites unread",
        "usgs:http_503": "the bulk NWIS IV request itself failed with HTTP 503 (`{source: 'usgs_iv', error}`); all selected sites unread",
        "usgs:timeout": "the bulk NWIS IV request timed out",
        "nws_alerts:http_503": "the NWS alerts request failed with HTTP 503",
        "nwps:timeout": "one per-gauge NWPS request timed out (`{shef_id, error}`)",
    }
    for key, count in t2["fetch_errors_by_provider_and_class"].items():
        add(f"| `{key}` | `county_flood_payload.sources.{key.split(':')[0]}.fetch_errors[].error` | {count:,} | {meanings.get(key, 'see error string')} |")
    for key, count in t2["coordinator_failed_fetches_by_class"].items():
        add(f"| `coordinator:{key}` | `failed_fetches[].failure_reason` | {count:,} | the county-flood-data skill raised before returning a payload (`county_flood_bridge` records one entry with `sources_succeeded` 0) |")
    add("")
    add(
        f"Extraction failures: {t2['extraction_failure_total']:,} entries in `extraction_failures[]`, all `failure_reason` `no_gauges` ({json.dumps(t2['extraction_failures_by_source'])} by `source_id`), in {t2['cycles_with_extraction_failure']:,} cycles. "
        f"Every `county_flood_payload.gauges[].error` is null ({t2['gauge_rows_with_non_null_error']} non-null of {sum(t2['gauge_rows_by_source'].values()):,} rows; rows by `source`: {json.dumps(t2['gauge_rows_by_source'])})."
    )
    add("")

    # ---------------------------------------------------------------- Item 3
    add("## 3. Slack delivery per due cycle")
    add("")
    ppd = t3["parts_per_delivered_report"]
    add(
        f"Logical reports delivered: {t3['logical_reports_delivered']:,} cycles with `slack_delivery.ok` true. Transport parts: {t3['transport_parts_sent']:,} (`slack_delivery.messages_sent`), of which {t3['transport_parts_ok']:,} `slack_delivery.results[].ok` true and {t3['transport_parts_not_ok']} otherwise. "
        f"Parts per delivered report (`messages_sent`): median {_fmt(ppd.get('median'), 0)}, interquartile range {_fmt(ppd.get('q1'), 0)} to {_fmt(ppd.get('q3'), 0)}, maximum {_fmt(ppd.get('max'), 0)}. "
        f"Skipped deliveries by `slack_delivery.reason`: {json.dumps(t3['skipped_by_reason'])}. Failed: {t3['delivery_failures']}. Not recorded: {t3['delivery_not_recorded']}. Fold notices (`fold_slack_delivery.ok`): {t3['fold_deliveries']}. "
        f"Parts prepared (`len(slack_messages)`): {t3['slack_messages_prepared_all_cycles']:,} across all due cycles, {t3['slack_messages_prepared_delivered_cycles']:,} in delivered cycles, totalling {t3['slack_messages_chars_delivered_cycles']:,} characters of message text (`sum(len(slack_messages[i]))`, delivered cycles). "
        f"`slack_delivery.message_id` differed from the last `results[].message_id` in {t3['message_id_not_last_part']} deliveries."
    )
    add("")

    # ---------------------------------------------------------------- Item 4
    add("## 4. Acquisitions per county per day, observed and at fixed clocks")
    add("")
    add(
        "Mission-active windows are the first and last poll `run_at` at which a mission appears in `results`, `skipped` or `errors`; the window therefore starts at the first observed poll, not at mission creation, and for missions still active at the end of the log it ends at the last poll. "
        "Durations of a county's missions are summed when a county had more than one mission. A fixed clock over the same window needs floor(duration/interval) + 1 acquisitions (one at the window start and one per interval thereafter)."
    )
    add("")
    add("| County | FIPS | Missions | Active days | Due acquisitions (`results[]`) | Due per day | 15 min clock | 1 h clock | 3 h clock | 6 h clock |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for row in t4["counties"]:
        clocks = row["clock_acquisitions_required"]
        add(
            f"| {row['county']}, {row['state']} | {row['fips']} | {len(row['mission_ids'])} | {_fmt(row['active_duration_days'], 2)} | {row['due_acquisitions']:,} | {_fmt(row['due_per_day'], 2)} | "
            + " | ".join(f"{clocks[label]:,}" for label in CLOCKS)
            + " |"
        )
    tot = t4["totals"]
    add(
        f"| All | | {len(t4['missions'])} | {_fmt(tot['mission_active_days_summed'], 2)} | {tot['due_acquisitions']:,} | {_fmt(tot['due_per_mission_day'], 2)} | "
        + " | ".join(f"{tot['clock_acquisitions_required'][label]:,}" for label in CLOCKS)
        + " |"
    )
    add("")
    add("Observed acquisitions as a fraction of each clock's requirement (`due_acquisitions` / clock):")
    add("")
    add("| County | 15 min | 1 h | 3 h | 6 h |")
    add("|---|---|---|---|---|")
    for row in t4["counties"]:
        add(f"| {row['county']}, {row['state']} | " + " | ".join(_fmt(row["observed_over_clock"][label], 3) for label in CLOCKS) + " |")
    add("| All | " + " | ".join(_fmt(tot["observed_over_clock"][label], 3) for label in CLOCKS) + " |")
    add("")
    add("Per mission:")
    add("")
    add("| Mission | First poll `run_at` | Last poll `run_at` | Active days | Opportunities | Due | Skips | `mission_spec.heartbeat_interval_seconds` seen on due results and `skipped[].heartbeat_interval_seconds` |")
    add("|---|---|---|---|---|---|---|---|")
    for row in t4["missions"]:
        add(
            f"| `{row['mission_id']}` | {row['first_poll_at']} | {row['last_poll_at']} | {_fmt(row['active_duration_days'], 2)} | {row['opportunities']:,} | {row['due_acquisitions']:,} | {row['cadence_skips']:,} | {json.dumps(row['heartbeat_intervals_seconds'])} |"
        )
    add("")
    add(
        "Realized inter-acquisition gap by configured interval, parsed from `cycle_due_reason` (`\"due (<elapsed>s elapsed, interval <interval>s)\"`; the one `first_run` cycle has no gap):"
    )
    add("")
    add("| Configured interval (s) | n | Median elapsed (s) | IQR (s) | Max (s) | Median / configured |")
    add("|---|---|---|---|---|---|")
    for interval, stats in t4["realized_elapsed_seconds_by_configured_interval"].items():
        add(
            f"| {int(interval):,} | {stats['n']:,} | {_fmt(stats['median'], 0)} | {_fmt(stats.get('q1'), 0)} to {_fmt(stats.get('q3'), 0)} | {_fmt(stats['max'], 0)} | {_fmt(stats.get('median_over_configured'), 3)} |"
        )
    add("")
    add(
        f"Unparsed `cycle_due_reason` values: {json.dumps(t4['cycle_due_reason_unparsed'])}. Interval values on due results (`mission_spec.heartbeat_interval_seconds`): {json.dumps(t4['heartbeat_interval_seconds_on_due_results'])}; by `mission_spec.current_event_phase`: {json.dumps(t4['phase_by_interval_on_due_results'])}. "
        "The 800 s interval is the one the Escambia and Harris missions carried across early_watch, elevated_watch, imminent and active_incident; the Coconino and King missions carried 21,600 s in early_watch, and 43,200 s appears in recovery. Because the cron polls every 15 minutes, an 800 s interval becomes due on the next poll, so its realized gaps cluster around the 900 s poll spacing."
    )
    add("")

    # ---------------------------------------------------------------- Item 5
    add("## 5. Quota, HTTP status, bytes, tokens and LLM usage")
    add("")
    probes = t5["key_path_probes"]

    def _probe(name: str) -> str:
        hits = probes.get(name, {})
        return ", ".join(f"`{k}`" for k in hits) if hits else "no key"

    add(
        f"A full-depth scan of all {p['distinct_key_paths']:,} distinct key paths in the log was probed by name. "
        f"Token, prompt, completion, usage, LLM or model keys: {_probe('token_or_llm_usage')}. "
        f"Quota, rate-limit, retry or throttle keys: {_probe('quota_or_rate_limit')}. "
        f"HTTP status keys (`status_code`, `http_status`, `response_code`): {_probe('http_status')}. "
        f"Byte or size keys: {_probe('byte_counts')}. Cost or billing keys: {_probe('cost')}."
    )
    add("")
    add(
        f"The only LLM-related key is `llm_verdict` on `article_evidence.top_sources[]` and `article_evidence.high_confidence_articles[]`; it is null in all {t5['article_llm_verdict_null']:,} occurrences and non-null in {t5['article_llm_verdict_non_null']}, consistent with the detector's `ARTICLE_LLM_VERDICT_ENABLED` gate defaulting to off. No token counts or model names appear anywhere. "
        f"HTTP status codes appear only as text inside error strings on failed requests ({json.dumps(t5['http_status_codes_only_inside_error_strings'])}, from `fetch_errors[].error` and `failed_fetches[].failure_reason`); successful requests record no status code. "
        f"The only byte quantity is `source_snapshots[].byte_length`, the UTF-8 length of the canonical JSON the runtime persists per official source snapshot (`len(canonical.encode(\"utf-8\"))` in `county_flood_bridge`), not the size of any HTTP response: "
        + "; ".join(
            f"`{name}` n = {s['n']:,}, median {_fmt(s['median'], 0)} B, max {_fmt(s['max'], 0)} B, sum {_fmt(s['sum'], 0)} B"
            for name, s in t5["source_snapshot_byte_length_by_source_family"].items()
        )
        + f". `csv_truncated` flags by source: {json.dumps(t5['csv_truncated_flags'])}. "
        f"No `verification_trigger` key is present ({t5['verification_trigger_key_present']}), so the trigger was off; no `ontology_ledger` key is present ({t5['ontology_ledger_key_present']}). "
        f"The poll-level `cron` block records only scheduler state (fields: {', '.join(f'`{k}`' for k in t5['cron_block_fields'])})."
    )
    add("")

    # ---------------------------------------------------------------- Reconciliation
    add("## Reconciliation with `eval/results/deployment_summary.json`")
    add("")
    if recon.get("available"):
        add("| Check | This script | deployment_summary.json | Match |")
        add("|---|---|---|---|")
        for name, check in recon["checks"].items():
            add(f"| {name} | {check['this_script']} | {check['deployment_summary']} | {'yes' if check['match'] else 'no'} |")
        add("")
        add(
            f"All scalar checks match: {recon['all_checks_match']}. Mission windows (first and last poll `run_at`, opportunity counts) agree with the union of each mission's `configuration_epochs.mission_heartbeat` epochs: {recon['all_mission_windows_match']}."
        )
    else:
        add(f"`{recon.get('published_path')}` was not found; no reconciliation performed.")
    add("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------


def summarize(log_path: Path) -> Dict[str, Any]:
    digest = hashlib.sha256()
    acc = CostAccumulator()
    with log_path.open("rb") as stream:
        for record in iter_concatenated_json(stream, digest=digest):
            acc.add(record)
        size = stream.tell()
    try:
        shown = str(log_path.resolve().relative_to(ROOT))
    except ValueError:
        shown = str(log_path)
    return acc.finalize({"path": shown, "bytes": size, "sha256": digest.hexdigest()})


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path, nargs="?", default=ROOT / DEFAULT_LOG, help="heartbeat cron log (read-only)")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help="directory for summary.json and summary.md")
    parser.add_argument("--published", type=Path, default=DEPLOYMENT_SUMMARY, help="deployment_summary.json to reconcile against")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.out_dir.resolve() == args.log.resolve().parent and args.log.name in ("summary.json", "summary.md"):
        parser.error("refusing to write next to the log")

    summary = summarize(args.log)
    summary["reconciliation"] = reconcile(summary, args.published)
    markdown = render_markdown(summary, summary["reconciliation"])

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.out_dir / "summary.md").write_text(markdown, encoding="utf-8")
    print(f"wrote {args.out_dir / 'summary.json'} and {args.out_dir / 'summary.md'}")
    print(f"reconciliation: scalars {summary['reconciliation'].get('all_checks_match')}, mission windows {summary['reconciliation'].get('all_mission_windows_match')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
