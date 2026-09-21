"""Build a dated news corpus for a past event from GDELT's raw file archive.

GDELT's document API rate-limits historical queries too aggressively to sweep a
multi-day window, but the underlying Global Knowledge Graph files it is built
from are published as plain archives on a static host and are not rate-limited.
Each file covers a fifteen-minute slice and carries, for every article
processed in that slice, GKG 2.1's ``V2.1DATE`` publication timestamp, the URL,
the outlet, the geographic entities the article refers to, and the thematic
tags GDELT assigned it.

That GDELT field is the reason to prefer this over a web archive. An archive
records when a crawler captured a page, which during a major event is dominated
by bulk crawls that first-capture large numbers of much older stories; ordering
a replay by capture time measures the crawler's schedule rather than the news.
The GKG codebook defines ``V2.1DATE`` as document publication time at
15-minute resolution. The replay treats it as its as-of field; it is not an
independently recovered precise page timestamp.

Articles are selected by the place they refer to, not by whether they concern
flooding. This mirrors what the live system's news query does — retrieve what is
being written about the monitored county, then score it — and it keeps the
off-topic coverage that shares a dateline with the event, which is exactly the
material the belief layer has to hold at arm's length.

Lives outside `watcher/` because it is evaluation scaffolding, not runtime.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.news_archive import headline_from_url, load, save  # noqa: E402,F401

GKG_URL = "http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip"
_HEADERS = {"User-Agent": "OpenClaw-disaster-sentinel-eval/1.0 (research)"}

#: GKG 2.1 column positions used here. Column 1 is ``V2.1DATE``.
_COL_DATE = 1
_COL_SOURCE = 3
_COL_URL = 4
_COL_THEMES = 7
_COL_LOCATIONS = 9
_COL_TONE = 15

#: A slice is missing from the archive often enough that it is not an error.
_MISSING = (404, 403)


def _slices(start: datetime, end: datetime) -> List[str]:
    """Fifteen-minute slice stamps covering [start, end)."""
    out: List[str] = []
    cur = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    while cur < end:
        out.append(cur.strftime("%Y%m%d%H%M%S"))
        cur += timedelta(minutes=15)
    return out


def _fetch_slice(stamp: str, *, attempts: int = 4) -> Optional[str]:
    url = GKG_URL.format(stamp=stamp)
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                blob = resp.read()
            archive = zipfile.ZipFile(io.BytesIO(blob))
            return archive.read(archive.namelist()[0]).decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            if exc.code in _MISSING:
                return None
        except Exception:
            continue
    return None


def _parse_stamp(raw: str) -> Optional[str]:
    try:
        dt = datetime.strptime(raw.strip(), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _mentions_place(locations: str, place_terms: Sequence[str]) -> bool:
    lowered = locations.lower()
    return any(term.lower() in lowered for term in place_terms)


def _tone(raw: str) -> Optional[float]:
    try:
        return round(float(raw.split(",")[0]), 3)
    except (ValueError, IndexError, AttributeError):
        return None


def parse_slice(
    text: str, *, place_terms: Sequence[str]
) -> List[Dict[str, Any]]:
    """Article records from one slice that refer to the monitored place."""
    out: List[Dict[str, Any]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        col = line.split("\t")
        if len(col) <= _COL_TONE:
            continue
        if not _mentions_place(col[_COL_LOCATIONS], place_terms):
            continue
        published = _parse_stamp(col[_COL_DATE])
        url = col[_COL_URL].strip()
        if not published or not url:
            continue
        themes = [t for t in col[_COL_THEMES].split(";") if t]
        out.append(
            {
                "url": url,
                "title": headline_from_url(url),
                "domain": col[_COL_SOURCE].strip(),
                "published_at": published,
                "themes": themes,
                "flood_themed": any("FLOOD" in t for t in themes),
                "tone": _tone(col[_COL_TONE]),
                "source": "gdelt_gkg",
            }
        )
    return out


def build_news_corpus(
    *,
    start: str,
    end: str,
    place_terms: Sequence[str],
    county: str = "",
    state: str = "",
    workers: int = 8,
    progress_every: int = 96,
) -> Dict[str, Any]:
    """Sweep every slice in the window and collect place-relevant articles."""
    begin = datetime.strptime(start, "%Y%m%d").replace(tzinfo=timezone.utc)
    finish = datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc)
    stamps = _slices(begin, finish)
    print(f"sweeping {len(stamps)} slices ({start}..{end})", flush=True)

    by_url: Dict[str, Dict[str, Any]] = {}
    missing_slices: List[str] = []

    def work(stamp: str) -> Tuple[str, Optional[str]]:
        return stamp, _fetch_slice(stamp)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, (stamp, text) in enumerate(pool.map(work, stamps), start=1):
            if text is None:
                missing_slices.append(stamp)
            else:
                for article in parse_slice(text, place_terms=place_terms):
                    by_url.setdefault(article["url"], article)
            if done % progress_every == 0:
                print(
                    f"  {done}/{len(stamps)} slices, {len(by_url)} articles, "
                    f"{len(missing_slices)} slices missing",
                    flush=True,
                )

    articles = sorted(by_url.values(), key=lambda a: a["published_at"])
    print(
        f"done: {len(articles)} articles, {len(missing_slices)} slices missing",
        flush=True,
    )
    return {
        "schema": "sentinel-news-corpus/2",
        "county": county,
        "state": state,
        "window": {"start": start, "end": end},
        "source": "gdelt_gkg",
        "as_of_field": "GKG_V2_1_DATE",
        "as_of_semantics": (
            "GKG 2.1 document publication timestamp at 15-minute resolution; "
            "not an independently recovered precise page timestamp"
        ),
        "place_terms": list(place_terms),
        "slices_swept": len(stamps),
        "slices_missing": len(missing_slices),
        "missing_slices": missing_slices,
        "slice_coverage_fraction": round(
            (len(stamps) - len(missing_slices)) / len(stamps), 6
        ) if stamps else None,
        "articles": articles,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive YYYYMMDD")
    parser.add_argument("--end", required=True, help="Exclusive YYYYMMDD")
    parser.add_argument("--place-term", action="append", required=True)
    parser.add_argument("--county", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.end <= args.start:
        parser.error("--end must be later than --start")
    corpus = build_news_corpus(
        start=args.start,
        end=args.end,
        place_terms=args.place_term,
        county=args.county,
        state=args.state,
        workers=args.workers,
    )
    target = Path(args.out)
    save(corpus, target)
    print(f"wrote {target} ({target.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
