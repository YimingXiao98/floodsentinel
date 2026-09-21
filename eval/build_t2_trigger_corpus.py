"""Derive an exploratory T2/news verification-trigger corpus (development only).

Walks the 15-minute poll grid over one development window and, at each poll,
runs the production article detector over the frozen GDELT title corpus
exactly as the belief/news audit did. Each *positive-indication* article the
detector admits becomes, at the first poll where it is admitted, one trigger
event with phenomena ``T2`` and significance ``W``. Neutral and denial
articles never become events.

The resulting corpus reuses the trigger-corpus schema, so the exploratory arm
runs through the identical scheduling-only trigger machinery: a T2 event can
request a verification acquisition and nothing else. This arm is exploratory,
title-only-proxy evidence on a development case; it never enters the held-out
primary analysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.news_replay import load_feed  # noqa: E402
from eval.vtec_trigger import TRIGGER_CORPUS_SCHEMA, parse_iso, save, _sha256  # noqa: E402


def build(
    *,
    news_corpus_path: str,
    county: str,
    state: str,
    fips: str,
    start: str,
    end: str,
    poll_interval_seconds: int = 900,
    min_page_score: float = 0.75,
    top_n: int = 5,
) -> Dict[str, Any]:
    feed = load_feed(news_corpus_path, county=county, state=state)
    seen: Dict[str, Dict[str, Any]] = {}
    poll = parse_iso(start)
    stop = parse_iso(end)
    step = timedelta(seconds=poll_interval_seconds)
    polls = 0
    while poll < stop:
        polls += 1
        evidence = feed.score_as_of(
            poll, min_page_score=min_page_score, top_n=top_n
        )
        for article in evidence["high_confidence_articles"]:
            # The detector's positive vocabulary; neutral and denial
            # indications never become trigger events.
            if str(article.get("indication") or "").lower() != "confirms_flood":
                continue
            url = str(article.get("url") or "")
            if not url or url in seen:
                continue
            key = "T2-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
            stamp = poll.isoformat().replace("+00:00", "Z")
            seen[url] = {
                "event_key": key,
                "wfo": "T2",
                "phenomena": "T2",
                "significance": "W",
                "etn": len(seen) + 1,
                "name": "Admitted positive news indication (title-only proxy)",
                "hvtec_nwsli": None,
                "product_id": None,
                "available_at": stamp,
                "begins_at": stamp,
                "audit_final_expire": None,
                "ugcs": [],
                "url": url,
                "title": article.get("title"),
                "page_score": article.get("page_score"),
            }
        poll += step
    events = sorted(seen.values(), key=lambda e: (e["available_at"], e["event_key"]))
    return {
        "schema": TRIGGER_CORPUS_SCHEMA,
        "county": county,
        "state": state,
        "fips": fips,
        "generated_at": None,
        "source": {
            "archive": "frozen GDELT GKG title corpus via production detector",
            "news_corpus": news_corpus_path,
            "news_corpus_sha256": _sha256(Path(news_corpus_path)),
            "detector_entry_point": "score_and_filter_articles",
            "min_page_score": min_page_score,
            "top_n": top_n,
        },
        "window": {"start": start, "end": end},
        "as_of_semantics": (
            "available_at is the first 15-minute poll at which the production "
            "detector admitted the article as a positive indication; articles "
            "become visible only after their GDELT publication slice, so this "
            "errs late, never early. Title-only proxy; exploratory."
        ),
        "role": "exploratory_t2_trigger_development_only",
        "poll_interval_seconds": poll_interval_seconds,
        "polls": polls,
        "anomalies": {},
        "raw_product_sample_validation": [],
        "n_events": len(events),
        "events": events,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news-corpus", required=True)
    parser.add_argument("--county", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--fips", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    corpus = build(
        news_corpus_path=args.news_corpus,
        county=args.county,
        state=args.state,
        fips=args.fips,
        start=args.start,
        end=args.end,
    )
    target = save(corpus, args.out)
    print(
        json.dumps(
            {
                "path": str(target),
                "sha256": _sha256(target),
                "n_events": corpus["n_events"],
                "polls": corpus["polls"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
