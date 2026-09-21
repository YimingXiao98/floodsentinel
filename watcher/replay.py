"""Retrospective replay of a historical flood event through the live pipeline.

Evaluation needs the system to run over a past event as if it were live. Only
the *fetch* is replaced: observations come from a frozen corpus instead of the
network, and every downstream decision (classification, verdict, confidence,
claims, phase) runs through the real code so an ablation measures the real
system.

Corpus format (JSON on disk)::

    {
      "county": "Harris", "state": "TX", "fips": "48201",
      "window": {"start": "...Z", "end": "...Z"},
      "sites": [
        {"site_no": "08074000", "shef_id": "HOUT2", "name": "...",
         "action_stage": 40.0, "flood_stage": 50.0, ...,
         "observations": [{"t": "...Z", "stage": 12.3, "flow": 400.0}, ...]}
      ]
    }

The central correctness property is **no lookahead**: at virtual time *t* the
replay may only expose observations timestamped at or before *t*.
"""

from __future__ import annotations

import bisect
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from watcher.claims.confidence import (
    provisional_at_observation,
    worst_qualifier,
)

_COUNTY_FLOOD_SKILL_DIR = (
    Path(__file__).resolve().parent.parent / "skills" / "county-flood-data"
)

USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
USGS_GAGE_HEIGHT_CODE = "00065"
USGS_DISCHARGE_CODE = "00060"
_HEADERS = {"User-Agent": "OpenClaw-disaster-sentinel-replay/1.0"}
_SITE_BATCH = 25


def _county_flood_on_path() -> None:
    skill_dir = str(_COUNTY_FLOOD_SKILL_DIR)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Corpus construction (network; run once per event)
# --------------------------------------------------------------------------


def _request_json(
    url: str, timeout: int = 60, *, attempts: int = 4
) -> Dict[str, Any]:
    """Fetch JSON, retrying transient archive/API failures.

    Corpus construction is an offline, one-time operation and USGS sometimes
    answers historical batch requests with 429/5xx responses.  Short bounded
    retries make the frozen-artifact command reproducible without hiding a
    permanent client or data error.
    """
    req = urllib.request.Request(url, headers=_HEADERS)
    retryable_http = {429, 500, 502, 503, 504}
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable_http or attempt + 1 >= attempts:
                raise
        except urllib.error.URLError:
            if attempt + 1 >= attempts:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("unreachable corpus request retry state")


def _fetch_iv_window(
    site_nos: Sequence[str], start: str, end: str
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Return {site_no: {"stage": [...], "flow": [...]}} for the window."""
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    sites = [s for s in site_nos if s]
    for i in range(0, len(sites), _SITE_BATCH):
        batch = sites[i : i + _SITE_BATCH]
        params = urllib.parse.urlencode(
            {
                "format": "json",
                "sites": ",".join(batch),
                "startDT": start,
                "endDT": end,
                "parameterCd": f"{USGS_DISCHARGE_CODE},{USGS_GAGE_HEIGHT_CODE}",
            }
        )
        payload = _request_json(f"{USGS_IV_URL}?{params}")
        for series in (payload.get("value") or {}).get("timeSeries") or []:
            code = (
                ((series.get("variable") or {}).get("variableCode") or [{}])[0]
            ).get("value")
            site = (
                ((series.get("sourceInfo") or {}).get("siteCode") or [{}])[0]
            ).get("value")
            if not site:
                continue
            key = "stage" if code == USGS_GAGE_HEIGHT_CODE else "flow"
            bucket = out.setdefault(site, {"stage": [], "flow": []})
            for block in series.get("values") or []:
                for point in block.get("value") or []:
                    raw = point.get("value")
                    if raw in (None, "", "-999999"):
                        continue
                    try:
                        val = float(raw)
                    except (TypeError, ValueError):
                        continue
                    bucket[key].append(
                        {
                            "t": _utc_iso(parse_iso(point["dateTime"])),
                            "v": val,
                            "q": point.get("qualifiers") or None,
                        }
                    )
    return out


def _merge_series(
    stage: List[Dict[str, Any]], flow: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Merge stage and flow onto a single timestamp-ordered observation list."""
    by_time: Dict[str, Dict[str, Any]] = {}
    for point in stage:
        entry = by_time.setdefault(point["t"], {"t": point["t"]})
        entry["stage"] = point["v"]
        # Stage decides the flood state, so its qualifier is the one recorded.
        if point.get("q"):
            entry["q"] = point["q"]
    for point in flow:
        by_time.setdefault(point["t"], {"t": point["t"]})["flow"] = point["v"]
    return [by_time[k] for k in sorted(by_time)]


def build_corpus(
    *,
    county: str,
    state: str,
    fips: Optional[str],
    start: str,
    end: str,
    site_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Fetch historical USGS IV readings and CSV thresholds into a corpus."""
    _county_flood_on_path()
    from county_flood.sources.usgs import (
        discover_usgs_sites_by_fips,
        resolve_usgs_sites_from_csv,
    )

    sites_meta, total, truncated = resolve_usgs_sites_from_csv(
        county, state, site_ids=site_ids
    )
    # Production resolves a county's gauges by FIPS as well as by name, and the
    # difference grades how precisely each gauge is placed. Recording which
    # sites FIPS confirms keeps the replay's location term as varied as the live
    # one's, instead of flattening every gauge to the same precision.
    fips_confirmed: set = set()
    if fips and not site_ids:
        try:
            fips_confirmed = {
                str(s["site_no"])
                for s in discover_usgs_sites_by_fips(fips)
                if s.get("site_no")
            }
        except Exception:  # discovery is advisory; a corpus is still usable
            fips_confirmed = set()
    site_nos = [s["site_no"] for s in sites_meta if s.get("site_no")]
    observations = _fetch_iv_window(site_nos, start, end) if site_nos else {}

    sites: List[Dict[str, Any]] = []
    for meta in sites_meta:
        site_no = meta.get("site_no")
        if not site_no:
            continue
        series = observations.get(site_no) or {"stage": [], "flow": []}
        sites.append(
            {
                "site_no": site_no,
                "shef_id": meta.get("shef_id"),
                "name": meta.get("name") or "",
                "location_basis": (
                    "fips_verified" if str(site_no) in fips_confirmed else "name_matched"
                ),
                "action_stage": meta.get("action_stage"),
                "flood_stage": meta.get("flood_stage"),
                "moderate_stage": meta.get("moderate_stage"),
                "major_stage": meta.get("major_stage"),
                "stage_unit": meta.get("stage_unit") or "ft",
                "priority_tier": meta.get("priority_tier"),
                "threshold_source": meta.get("threshold_source") or "csv",
                "observations": _merge_series(series["stage"], series["flow"]),
            }
        )

    return {
        "schema": "sentinel-replay-corpus/1",
        "county": county,
        "state": state,
        "fips": fips,
        "window": {"start": start, "end": end},
        "source": "usgs_nwis_iv",
        "csv_matches": total,
        "csv_truncated": truncated,
        "sites": sites,
    }


def save_corpus(corpus: Dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(corpus, indent=2), encoding="utf-8")
    return target


def load_corpus(path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def corpus_stats(corpus: Dict[str, Any]) -> Dict[str, Any]:
    sites = corpus.get("sites") or []
    counts = [len(s.get("observations") or []) for s in sites]
    scorable = [
        s
        for s in sites
        if s.get("action_stage") is not None or s.get("flood_stage") is not None
    ]
    return {
        "sites": len(sites),
        "sites_with_observations": sum(1 for c in counts if c),
        "sites_scorable": len(scorable),
        "observations_total": sum(counts),
    }


# --------------------------------------------------------------------------
# Virtual clock and no-lookahead access
# --------------------------------------------------------------------------


def iter_ticks(start: str, end: str, interval_seconds: int) -> Iterator[datetime]:
    """Yield virtual timestamps from start to end at a fixed interval."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    current = parse_iso(start)
    stop = parse_iso(end)
    step = timedelta(seconds=interval_seconds)
    while current <= stop:
        yield current
        current += step


def observation_as_of(site: Dict[str, Any], when: datetime) -> Optional[Dict[str, Any]]:
    """Latest observation at or before `when`. Never looks ahead.

    Observations are stored timestamp-ordered, so this is a bisect on the
    timestamp strings (ISO-8601 UTC sorts lexicographically).
    """
    observations = site.get("observations") or []
    if not observations:
        return None
    cutoff = _utc_iso(when)
    times = [o["t"] for o in observations]
    idx = bisect.bisect_right(times, cutoff)
    if idx == 0:
        return None
    return observations[idx - 1]


def observation_age_hours(observation: Dict[str, Any], when: datetime) -> float:
    return (when - parse_iso(observation["t"])).total_seconds() / 3600.0


# --------------------------------------------------------------------------
# Payload assembly (mirrors aggregate_county_flood, minus the network)
# --------------------------------------------------------------------------


def gauges_at(
    corpus: Dict[str, Any],
    when: datetime,
    *,
    max_staleness_hours: Optional[float] = 6.0,
) -> List[Dict[str, Any]]:
    """Build scored gauge dicts as the system would see them at `when`.

    A site whose most recent observation is older than `max_staleness_hours` is
    treated as having no current reading, which is how a dead sensor presents in
    live operation.
    """
    _county_flood_on_path()
    from county_flood.aggregator import _gauge_to_dict
    from county_flood.models import GaugeReading
    from county_flood.sources.stages import resolve_flood_category

    gauges: List[GaugeReading] = []
    for site in corpus.get("sites") or []:
        observation = observation_as_of(site, when)
        stage = flow = None
        observed_at = qualifier = None
        if observation is not None:
            if (
                max_staleness_hours is None
                or observation_age_hours(observation, when) <= max_staleness_hours
            ):
                stage = observation.get("stage")
                flow = observation.get("flow")
                observed_at = observation["t"]
                # Approval happens months after an event, so a retrospective run
                # must score the reading as it stood when published.
                qualifier = provisional_at_observation(
                    worst_qualifier(observation.get("q"))
                )

        classification = resolve_flood_category(
            observed_primary=stage,
            nwps_category=None,
            action_stage=site.get("action_stage"),
            flood_stage=site.get("flood_stage"),
            moderate_stage=site.get("moderate_stage"),
            major_stage=site.get("major_stage"),
            threshold_source=site.get("threshold_source") or "csv",
        )
        gauges.append(
            GaugeReading(
                source="usgs",
                shef_id=site.get("shef_id") or "",
                site_no=site.get("site_no"),
                name=site.get("name") or "",
                observed_primary=stage,
                observed_primary_unit=site.get("stage_unit") or "ft",
                observed_flood_category=classification.category,
                observed_valid_time=observed_at,
                flood_state_score=classification.score,
                streamflow_cfs=flow,
                action_stage=site.get("action_stage"),
                flood_stage=site.get("flood_stage"),
                moderate_stage=site.get("moderate_stage"),
                major_stage=site.get("major_stage"),
                classification_method=classification.method,
                threshold_source=classification.threshold_source,
                data_qualifier=qualifier,
                location_basis=site.get("location_basis"),
                unscored_reason=classification.unscored_reason,
                priority_tier=site.get("priority_tier"),
            )
        )
    return [_gauge_to_dict(g) for g in gauges]


def payload_at(
    corpus: Dict[str, Any],
    when: datetime,
    *,
    prior_gauges: Optional[Sequence[Dict[str, Any]]] = None,
    alerts: Optional[List[Dict[str, Any]]] = None,
    disasters: Optional[List[Dict[str, Any]]] = None,
    max_staleness_hours: Optional[float] = 6.0,
) -> Dict[str, Any]:
    """Assemble a county-flood-data/1 payload for virtual time `when`.

    Mirrors aggregate_county_flood: the same supplementary-signal, verdict and
    confidence functions are used, so replay exercises the real decision logic.
    """
    _county_flood_on_path()
    from county_flood.aggregator import (
        _merge_verdict_with_supplementary,
        _most_common_unit,
        _supplementary_scores,
    )
    from county_flood.confidence import compute_county_flood_confidence
    from county_flood.sources.gauge_delta import apply_supplementary_signals
    from county_flood.sources.stages import regional_verdict_from_gauges

    alerts = list(alerts or [])
    disasters = list(disasters or [])
    gauge_dicts = gauges_at(corpus, when, max_staleness_hours=max_staleness_hours)
    gauge_dicts, supplementary_signals = apply_supplementary_signals(
        gauge_dicts, list(prior_gauges) if prior_gauges else None
    )
    verdict = regional_verdict_from_gauges(gauge_dicts)
    verdict = _merge_verdict_with_supplementary(
        verdict, _supplementary_scores(alerts, disasters)
    )

    with_data = [g for g in gauge_dicts if g.get("observed_primary") is not None]
    scored = [g for g in gauge_dicts if g.get("flood_state_score") is not None]
    primaries = [
        g["observed_primary"] for g in scored if g.get("observed_primary") is not None
    ]
    units = [g.get("observed_primary_unit") for g in scored if g.get("observed_primary_unit")]
    generated_at = _utc_iso(when)

    summary_dict = {
        "average_flood_state": verdict.get("average_flood_state"),
        "gauges_scored": len(scored),
        "gauges_with_data": len(with_data),
        "verdict_gauge_count": verdict.get("verdict_gauge_count", 0),
        "confidence": verdict.get("confidence"),
        "is_flooding": bool(verdict.get("is_flooding")),
        "max_flood_state": verdict.get("max_flood_state"),
    }
    api_confidence = compute_county_flood_confidence(
        summary=summary_dict, gauges=gauge_dicts, generated_at=generated_at
    )

    return {
        "schema": "county-flood-data/1",
        "county": corpus.get("county"),
        "state": corpus.get("state"),
        "fips": corpus.get("fips"),
        "location": f"{corpus.get('county')} County, {corpus.get('state')}",
        "generated_at": generated_at,
        "replay": True,
        "alerts": alerts,
        "disasters": disasters,
        "summary": {
            "average_flood_state": verdict.get("average_flood_state"),
            "average_primary_reading": (
                round(sum(primaries) / len(primaries), 3) if primaries else None
            ),
            "average_primary_unit": _most_common_unit(units),
            "gauges_queried": len(gauge_dicts),
            "gauges_with_data": len(with_data),
            "gauges_scored": len(scored),
            "max_flood_state": verdict.get("max_flood_state"),
            "regional_verdict": verdict.get("regional_verdict"),
            "is_flooding": bool(verdict.get("is_flooding")),
            "confidence": verdict.get("confidence"),
            "evaluation_mode": verdict.get("evaluation_mode"),
            "scoring_coverage": verdict.get("scoring_coverage"),
            "gauges_unscored": verdict.get("gauges_unscored", 0),
            "verdict_gauge_count": verdict.get("verdict_gauge_count", 0),
            "gauges_excluded": verdict.get("gauges_excluded", 0),
            "min_gauges_required": verdict.get("min_gauges_required"),
            "classification_methods": verdict.get("classification_methods"),
            "flooding_gauge_ids": verdict.get("flooding_gauge_ids"),
            "supplementary_signals": supplementary_signals or None,
            "overall_confidence": api_confidence.get("overall_confidence"),
            "confidence_components": api_confidence.get("confidence_components"),
            "evidence_tier": api_confidence.get("evidence_tier"),
            "api_weight_hint": api_confidence.get("weight_hint"),
            "supplementary_scores": verdict.get("supplementary_scores"),
        },
        "sources": {"usgs": {"api_base": USGS_IV_URL, "replay": True}},
        "gauges": gauge_dicts,
    }


def snapshot_at(
    corpus: Dict[str, Any],
    when: datetime,
    *,
    prior_gauges: Optional[Sequence[Dict[str, Any]]] = None,
    alerts: Optional[List[Dict[str, Any]]] = None,
    disasters: Optional[List[Dict[str, Any]]] = None,
    max_staleness_hours: Optional[float] = 6.0,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Snapshot shaped like county_flood_bridge output, for the claim extractor.

    Pass ``payload`` to reuse one already built for this tick. Recomputing it
    without the same ``prior_gauges`` would drop the cross-tick deltas, so the
    supplementary rising/falling signal would never appear in the claims.
    """
    if payload is None:
        payload = payload_at(
            corpus,
            when,
            prior_gauges=prior_gauges,
            alerts=alerts,
            disasters=disasters,
            max_staleness_hours=max_staleness_hours,
        )
    fips = corpus.get("fips") or "unknown"
    source_id = f"src_county_flood_usgs_{fips}"
    stamp = _utc_iso(when).replace(":", "").replace("-", "")
    return {
        "source_id": source_id,
        "snapshot_id": f"snap_{source_id}_{stamp}",
        "snapshot_url": USGS_IV_URL,
        "fetched_at": _utc_iso(when),
        "raw_text": json.dumps(payload),
        "payload": payload,
    }
