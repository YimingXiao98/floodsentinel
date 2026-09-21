"""Frozen official warning trigger streams for hybrid-policy replay.

The hybrid verification-triggered policy consults an official NWS warning
stream between full acquisitions. Replaying that policy requires a frozen,
checksum-bound corpus of historical warnings in which every record carries a
defensible *as-of* timestamp: the moment the product was transmitted to the
public, not the hazard begin time and not the (retrospectively amended) end
time.

Records come from the Iowa Environmental Mesonet VTEC archive, the same
archive the earlier study used as a held-out timing reference. Each row's
``product_id`` begins with the product transmission time (UTC, minute
resolution); that prefix becomes ``available_at``. The VTEC event ``issue``
(hazard begin) and ``expire`` (final end, including post-hoc cancellations)
are retained for audit only and are never used for trigger availability,
because ``issue`` can precede transmission awareness semantics and final
``expire`` embeds information a live consumer did not have.

A county's stream merges the county UGC (SSCnnn, warnings) with the forecast
zones named for the county (SSZnnn, watches/advisories), deduplicated on the
VTEC event identity (year, WFO, phenomena, significance, ETN).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

TRIGGER_CORPUS_SCHEMA = "sentinel-vtec-trigger-corpus/1"

IEM_VTEC_BYUGC_URL = "https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py"
IEM_UGCS_URL = "https://mesonet.agron.iastate.edu/api/1/nws/ugcs.json"
IEM_NWSTEXT_URL = "https://mesonet.agron.iastate.edu/api/1/nwstext/"
_HEADERS = {"User-Agent": "OpenClaw-disaster-sentinel-eval/1.0"}

#: VTEC phenomena that indicate flooding (matches eval.ground_truth).
FLOOD_PHENOMENA = ("FF", "FL", "FA")

#: A pre-window product can only matter while its burst window still overlaps
#: the analysis window, so the fetch extends this many days before the window
#: start. Three days comfortably covers every prespecified burst duration.
FETCH_LOOKBACK_DAYS = 3

#: Zone names are county names with an optional directional/coastal qualifier
#: (e.g. "Inland Harris", "Coastal Harris"). Exact-name matching after
#: stripping one qualifier avoids prefix traps such as Harris vs Harrison.
_ZONE_NAME_QUALIFIERS = (
    "inland",
    "coastal",
    "northern",
    "southern",
    "eastern",
    "western",
    "central",
    "interior",
    "upper",
    "lower",
    "north",
    "south",
    "east",
    "west",
    "northeast",
    "northwest",
    "southeast",
    "southwest",
    "mainland",
    "metro",
    "greater",
    "far",
)

_PRODUCT_ID_TS_RE = re.compile(r"^(\d{12})-")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def product_id_transmission_time(product_id: Optional[str]) -> Optional[datetime]:
    """UTC transmission time encoded as the product_id's YYYYMMDDHHMM prefix."""
    match = _PRODUCT_ID_TS_RE.match(str(product_id or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _request_json(url: str, *, timeout: int = 90, attempts: int = 4) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    retryable = {429, 500, 502, 503, 504}
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable or attempt + 1 >= attempts:
                raise
        except urllib.error.URLError:
            if attempt + 1 >= attempts:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("unreachable retry state")


def county_ugc(state: str, fips: str) -> str:
    state = str(state).strip().upper()
    fips = str(fips).strip()
    if len(state) != 2 or len(fips) != 5 or not fips.isdigit():
        raise ValueError(f"cannot derive county UGC from state={state!r} fips={fips!r}")
    return f"{state}C{fips[2:]}"


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z ]", "", str(value).lower()).strip()


def zone_name_matches_county(zone_name: str, county: str) -> bool:
    """Whether a forecast-zone name denotes this county.

    True when the normalized zone name equals the county name, or equals the
    county name preceded only by recognized qualifier tokens. Suffix matching
    alone would be a trap (``Harrison`` ends without matching ``Harris`` only
    because of the word boundary enforced here).
    """
    target = _normalize_name(county)
    name = _normalize_name(zone_name)
    if not target or not name:
        return False
    if name == target:
        return True
    if name.endswith(" " + target):
        qualifier = name[: -(len(target) + 1)].strip()
        return bool(qualifier) and all(
            token in _ZONE_NAME_QUALIFIERS for token in qualifier.split()
        )
    if name.startswith(target + " "):
        qualifier = name[len(target) + 1 :].strip()
        return bool(qualifier) and all(
            token in _ZONE_NAME_QUALIFIERS for token in qualifier.split()
        )
    return False


def zone_ugcs_for_county(state: str, county: str) -> List[str]:
    """Forecast-zone UGCs whose names denote this county."""
    payload = _request_json(
        f"{IEM_UGCS_URL}?{urllib.parse.urlencode({'state': state.strip().upper()})}"
    )
    out: List[str] = []
    for row in payload.get("data") or []:
        ugc = str(row.get("ugc") or "")
        if len(ugc) != 6 or ugc[2] != "Z":
            continue
        if zone_name_matches_county(str(row.get("name") or ""), county):
            out.append(ugc)
    return sorted(set(out))


def fetch_vtec_events_raw(
    ugc: str, start_date: str, end_date: str
) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"ugc": ugc, "sdate": start_date, "edate": end_date}
    )
    payload = _request_json(f"{IEM_VTEC_BYUGC_URL}?{params}")
    return payload.get("events") or []


def _event_key(
    *, available_at: datetime, wfo: str, phenomena: str, significance: str, etn: Any
) -> str:
    return "-".join(
        (
            str(available_at.year),
            str(wfo).strip().upper(),
            str(phenomena).strip().upper(),
            str(significance).strip().upper(),
            f"{int(etn):04d}",
        )
    )


def normalize_trigger_events(
    raw_events: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Deduplicated flood trigger events with validated as-of timestamps."""
    anomalies = {
        "non_flood_phenomena": 0,
        "missing_product_timestamp": 0,
        "missing_event_identity": 0,
        "available_after_final_expire": 0,
    }
    by_key: Dict[str, Dict[str, Any]] = {}
    for event in raw_events:
        phenomena = str(event.get("phenomena") or "").strip().upper()
        if phenomena not in FLOOD_PHENOMENA:
            anomalies["non_flood_phenomena"] += 1
            continue
        available_at = product_id_transmission_time(event.get("product_id"))
        if available_at is None:
            anomalies["missing_product_timestamp"] += 1
            continue
        wfo = str(event.get("wfo") or "").strip().upper()
        significance = str(event.get("significance") or "").strip().upper()
        etn = event.get("eventid")
        if not wfo or not significance or etn in (None, ""):
            anomalies["missing_event_identity"] += 1
            continue
        expire_raw = event.get("expire")
        if expire_raw:
            try:
                if available_at > parse_iso(str(expire_raw)):
                    anomalies["available_after_final_expire"] += 1
                    continue
            except ValueError:
                pass
        key = _event_key(
            available_at=available_at,
            wfo=wfo,
            phenomena=phenomena,
            significance=significance,
            etn=etn,
        )
        begins_at = None
        if event.get("issue"):
            try:
                begins_at = _utc_iso(parse_iso(str(event["issue"])))
            except ValueError:
                begins_at = None
        record = {
            "event_key": key,
            "wfo": wfo,
            "phenomena": phenomena,
            "significance": significance,
            "etn": int(etn),
            "name": event.get("name"),
            "hvtec_nwsli": event.get("hvtec_nwsli"),
            "product_id": event.get("product_id"),
            "available_at": _utc_iso(available_at),
            # The hazard begin time is published inside the transmitted
            # product's VTEC string, so it is knowable at available_at and may
            # anchor a burst window for an advance-issued warning.
            "begins_at": begins_at,
            # Audit only: the archive's final expiry embeds post-hoc
            # cancellations and must never be a trigger input.
            "audit_final_expire": event.get("expire"),
            "ugcs": [event.get("ugc")] if event.get("ugc") else [],
        }
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = record
        else:
            if record["available_at"] < existing["available_at"]:
                existing["available_at"] = record["available_at"]
                existing["product_id"] = record["product_id"]
                existing["begins_at"] = record["begins_at"]
            existing["ugcs"] = sorted(set(existing["ugcs"]) | set(record["ugcs"]))
    events = sorted(by_key.values(), key=lambda e: (e["available_at"], e["event_key"]))
    return events, anomalies


def validate_sample_against_raw_products(
    events: Sequence[Dict[str, Any]], *, sample: int = 5
) -> List[Dict[str, Any]]:
    """Cross-check a deterministic sample of product ids against raw text.

    Fetches the archived raw product and confirms it exists and mentions the
    same VTEC phenomena/significance, tying the as-of timestamp to a concrete
    public product rather than trusting one index field.
    """
    rows: List[Dict[str, Any]] = []
    step = max(1, len(events) // max(1, sample))
    for event in list(events)[::step][:sample]:
        product_id = str(event.get("product_id") or "")
        row = {"event_key": event["event_key"], "product_id": product_id}
        try:
            req = urllib.request.Request(
                IEM_NWSTEXT_URL + urllib.parse.quote(product_id), headers=_HEADERS
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8", "replace")
            vtec_token = f".{event['phenomena']}.{event['significance']}."
            row["raw_product_found"] = True
            row["vtec_token_present"] = vtec_token in text
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            row["raw_product_found"] = False
            row["error"] = str(exc)
        rows.append(row)
    return rows


def build_trigger_corpus(
    *,
    county: str,
    state: str,
    fips: str,
    start: str,
    end: str,
    validate_sample: int = 5,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch and freeze one county's flood warning trigger stream."""
    fetch_start = _utc_iso(
        parse_iso(f"{start}T00:00:00Z") - timedelta(days=FETCH_LOOKBACK_DAYS)
    )[:10]
    c_ugc = county_ugc(state, fips)
    z_ugcs = zone_ugcs_for_county(state, county)
    raw: List[Dict[str, Any]] = []
    per_ugc_counts: Dict[str, int] = {}
    for ugc in [c_ugc, *z_ugcs]:
        rows = fetch_vtec_events_raw(ugc, fetch_start, end)
        per_ugc_counts[ugc] = len(rows)
        raw.extend(rows)
    events, anomalies = normalize_trigger_events(raw)
    sample_validation = (
        validate_sample_against_raw_products(events, sample=validate_sample)
        if validate_sample > 0 and events
        else []
    )
    return {
        "schema": TRIGGER_CORPUS_SCHEMA,
        "county": county,
        "state": state,
        "fips": fips,
        "generated_at": generated_at or _utc_iso(datetime.now(timezone.utc)),
        "source": {
            "archive": "Iowa Environmental Mesonet VTEC archive",
            "events_url": IEM_VTEC_BYUGC_URL,
            "ugc_listing_url": IEM_UGCS_URL,
            "raw_product_url": IEM_NWSTEXT_URL,
        },
        "window": {"start": start, "end": end},
        "fetch_window": {"start": fetch_start, "end": end},
        "fetch_lookback_days": FETCH_LOOKBACK_DAYS,
        "ugcs": {"county": c_ugc, "zones": z_ugcs},
        "per_ugc_raw_event_counts": per_ugc_counts,
        "as_of_semantics": (
            "available_at is the product transmission time parsed from the "
            "product_id prefix (UTC, minute resolution); begins_at is the "
            "hazard begin time published inside that product and may anchor "
            "a burst window; audit_final_expire embeds post-hoc cancellations "
            "and is retained for audit only, never as a trigger input"
        ),
        "anomalies": anomalies,
        "raw_product_sample_validation": sample_validation,
        "n_events": len(events),
        "events": events,
    }


# --------------------------------------------------------------------------
# Load-side helpers used by replay
# --------------------------------------------------------------------------


def load_trigger_corpus(path: str | Path) -> Dict[str, Any]:
    corpus = json.loads(Path(path).read_text(encoding="utf-8"))
    if corpus.get("schema") != TRIGGER_CORPUS_SCHEMA:
        raise ValueError(
            f"unexpected trigger corpus schema: {corpus.get('schema')!r} in {path}"
        )
    return corpus


def trigger_events_sorted(corpus: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Events sorted by availability, validated for replay consumption."""
    events = list(corpus.get("events") or [])
    for event in events:
        if not event.get("event_key") or not event.get("available_at"):
            raise ValueError("trigger corpus event missing event_key/available_at")
        parse_iso(str(event["available_at"]))
    return sorted(events, key=lambda e: (str(e["available_at"]), str(e["event_key"])))


def save(corpus: Dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(corpus, indent=2) + "\n", encoding="utf-8")
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="fetch and freeze one county trigger stream")
    build.add_argument("--county", required=True)
    build.add_argument("--state", required=True)
    build.add_argument("--fips", required=True)
    build.add_argument("--start", required=True, help="analysis window start date")
    build.add_argument("--end", required=True, help="analysis window end date")
    build.add_argument("--validate-sample", type=int, default=5)
    build.add_argument("--generated-at", default="")
    build.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    corpus = build_trigger_corpus(
        county=args.county,
        state=args.state,
        fips=args.fips,
        start=args.start,
        end=args.end,
        validate_sample=args.validate_sample,
        generated_at=args.generated_at or None,
    )
    target = save(corpus, args.out)
    print(
        json.dumps(
            {
                "path": str(target),
                "sha256": _sha256(target),
                "n_events": corpus["n_events"],
                "ugcs": corpus["ugcs"],
                "anomalies": corpus["anomalies"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
