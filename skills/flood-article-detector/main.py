#!/usr/bin/env python3
"""Flood article detector — search, score, and verdict for county flood news."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from article_scorer import (  # noqa: E402
    aggregate_flood_outcome,
    score_and_filter_articles,
    top_n,
)
from article_search import discover_and_fetch, resolve_location  # noqa: E402

FLOOD_ALIASES = {"flood", "flooding", "floods", "flash flood", "flash-flood"}
DISASTER_PREFIX = re.compile(
    r"^(?:(?:monitor|watch|check|run|discover)\s+)?"
    r"(?P<type>flood(?:ing|s)?|hurricane|wildfire|earthquake|tornado)\s+",
    re.I,
)
NON_FLOOD_TYPES = {"hurricane", "wildfire", "earthquake", "tornado"}

ARTICLE_LLM_VERDICT_ENV = "ARTICLE_LLM_VERDICT_ENABLED"
SCHEMA_VERSION = "1.1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_disaster_and_location(raw: str) -> tuple[Optional[str], str]:
    text = raw.strip()
    m = DISASTER_PREFIX.match(text)
    if m:
        disaster = m.group("type").lower()
        location = text[m.end() :].strip()
        return disaster, location

    lowered = text.lower()
    for alias in sorted(FLOOD_ALIASES, key=len, reverse=True):
        if alias in lowered:
            idx = lowered.index(alias)
            location = (text[:idx] + text[idx + len(alias) :]).strip(" ,")
            return "flood", location

    return "flood", text


def _error_envelope(message: str, **extra: Any) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "flood-article-detector",
        "generated_at": _utc_now(),
        "status": "error",
        "detail": message,
        "top_sources": [],
        "flood_outcome": {
            "verdict": "inconclusive",
            "outcome_score": 0.0,
            "confidence": "low",
            "rationale": message,
        },
        "coordinator_integration": {
            "ready": True,
            "merge_key": "article_evidence",
            "threshold": 0.75,
            "wired_in": "coordinator run-coordinator cycle",
        },
        **extra,
    }


def run_detection(
    *,
    location: str = "",
    county: str = "",
    state: str = "",
    fips: str = "",
    disaster: str = "flood",
    max_candidates: int = 20,
    top_k: int = 5,
    llm_verdict: Optional[bool] = None,
    exclude_urls: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    disaster = (disaster or "flood").lower()
    if disaster in NON_FLOOD_TYPES:
        return _error_envelope(
            f"disaster type '{disaster}' not supported in v1; use flood",
            disaster_type=disaster,
        )

    resolved_county, resolved_state, resolved_fips = resolve_location(
        location, county=county, state=state, fips=fips
    )
    if not resolved_county or not resolved_state:
        return _error_envelope(
            f"could not resolve county/state from: {location or county!r}",
            location_input=location or f"{county}, {state}",
        )

    discovery = discover_and_fetch(
        resolved_county,
        resolved_state,
        disaster=disaster,
        max_candidates=max_candidates,
        exclude_urls=set(exclude_urls or []),
    )

    now = datetime.now(timezone.utc)
    scored, omitted = score_and_filter_articles(
        discovery["articles"],
        county=resolved_county,
        state=resolved_state,
        disaster=disaster,
        now=now,
    )

    top_articles = top_n(scored, n=top_k)

    llm_verdict_enabled = (
        llm_verdict
        if llm_verdict is not None
        else os.environ.get(ARTICLE_LLM_VERDICT_ENV, "off") == "on"
    )

    if llm_verdict_enabled:
        from article_verdict import MAX_LLM_VERDICT_ARTICLES, build_article_verdict
        from article_search import fetch_full_text_for_verdict
        from article_scorer import aggregate_flood_outcome_with_verdicts

        for article in top_articles[:MAX_LLM_VERDICT_ARTICLES]:
            try:
                text, text_source = fetch_full_text_for_verdict(article)
                verdict = build_article_verdict(
                    county=resolved_county,
                    state=resolved_state,
                    title=article.title,
                    article_text=text,
                )
                article.llm_verdict = verdict.model_dump()
                article.llm_verdict["text_source"] = text_source
            except Exception:
                article.llm_verdict = None

        outcome = aggregate_flood_outcome_with_verdicts(top_articles)
    else:
        for article in top_articles:
            article.llm_verdict = None
        outcome = aggregate_flood_outcome(top_articles)

    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "flood-article-detector",
        "generated_at": _utc_now(),
        "disaster_type": disaster,
        "location": {
            "county": resolved_county,
            "state": resolved_state,
            "county_fips": str(resolved_fips).zfill(5) if resolved_fips else None,
        },
        "search_summary": {
            "queries_run": discovery["queries_run"],
            "candidates_found": discovery["candidates_found"],
            "candidates_scored": len(scored),
            "candidates_fetched": discovery["candidates_fetched"],
            "skipped_seen": discovery.get("skipped_seen", 0),
            "recency_window": "today_and_yesterday_utc",
            "required_keyword": disaster,
            "omitted": omitted,
        },
        "flood_outcome": outcome,
        "top_sources": [a.to_dict(rank=i + 1) for i, a in enumerate(top_articles)],
        "coordinator_integration": {
            "ready": True,
            "merge_key": "article_evidence",
            "threshold": 0.75,
            "wired_in": "coordinator run-coordinator cycle",
        },
        "status": "ok",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect flood events from recent news articles for a US county"
    )
    parser.add_argument("--location", default="", help='e.g. "Miami-Dade County, Florida"')
    parser.add_argument("--county", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--fips", default="")
    parser.add_argument("--disaster", default="flood")
    parser.add_argument("--request", default="", help="Freeform: flood Miami-Dade County, Florida")
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument(
        "--llm-verdict",
        action="store_true",
        default=None,
        help="Read the top-N articles with an LLM to produce a county-specific flood verdict per article (bounded, optional).",
    )
    parser.add_argument("--format", choices=["json", "human"], default="json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    disaster = args.disaster
    location = args.location

    if args.request.strip():
        disaster, location = _parse_disaster_and_location(args.request)

    llm_verdict_enabled: Optional[bool] = None
    if args.llm_verdict is not None:
        llm_verdict_enabled = args.llm_verdict

    payload = run_detection(
        location=location,
        county=args.county,
        state=args.state,
        fips=args.fips,
        disaster=disaster,
        max_candidates=args.max_candidates,
        top_k=args.top,
        llm_verdict=llm_verdict_enabled,
    )

    if args.format == "human":
        _print_human(payload)
    else:
        print(json.dumps(payload, indent=2))

    return 0 if payload.get("status") == "ok" else 1


def _print_human(payload: Dict[str, Any]) -> None:
    loc = payload.get("location") or {}
    outcome = payload.get("flood_outcome") or {}
    print(
        f"Flood article detector — {loc.get('county')}, {loc.get('state')} "
        f"(FIPS {loc.get('county_fips')})"
    )
    print(
        f"Verdict: {outcome.get('verdict')} "
        f"(score={outcome.get('outcome_score')}, confidence={outcome.get('confidence')})"
    )
    print(outcome.get("rationale", ""))
    print()
    for src in payload.get("top_sources") or []:
        print(
            f"#{src.get('rank')} [{src.get('page_score')}] {src.get('title')} "
            f"— {src.get('published_at') or 'no date'} — {src.get('indication')}"
        )
        print(f"   {src.get('url')}")


if __name__ == "__main__":
    raise SystemExit(main())
