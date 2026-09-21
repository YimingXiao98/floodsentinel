"""
article_verdict.py -- the optional, flag-gated "read and conclude" step.

Adds a per-article structured verdict on top of the existing deterministic
scoring: given an article's (fetched, best-effort full) text, ask an LLM
whether it describes flooding AT THE TARGET COUNTY specifically, as opposed
to flooding somewhere else, a general regional risk mention, or no flooding
at all.

This module is the one deliberate exception to this skill's "deterministic,
LLM-free" design principle -- it is off by default (see main.py wiring) and
bounded to at most MAX_LLM_VERDICT_ARTICLES per run. If it fails for any
reason, callers must treat a missing verdict as "no LLM verdict available"
and fall back to the legacy rule-based outcome -- never raise out of this
module in a way that breaks the deterministic pipeline.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

MAX_LLM_VERDICT_ARTICLES = 5

# Cheap/fast model: this runs up to 5x per skill invocation, potentially
# many times a day across many counties via the coordinator. Classification
# of this kind doesn't need adaptive thinking or a large model. Upgrade to
# a larger model here (single line change) if accuracy testing shows it's
# needed -- don't default to the expensive model for a bulk classification task.
LLM_VERDICT_MODEL = "claude-haiku-4-5-20251001"

MAX_ARTICLE_TEXT_CHARS = 6000  # truncate before sending to the model; flood-relevant info is front-loaded in news articles


class ArticleVerdict(BaseModel):
    """One article's structured, county-specific flood conclusion."""

    county_relevance: Literal[
        "at_target_county",
        "nearby_different_county",
        "state_or_region_general",
        "unrelated_location",
        "unclear",
    ]
    flood_status: Literal[
        "confirmed_flooding",
        "flood_risk_or_warning",
        "flooding_receding_or_resolved",
        "no_flooding_indicated",
        "unclear",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(description="One or two sentences explaining the verdict.")
    supporting_quote: Optional[str] = Field(
        default=None,
        description="A short (<= ~200 char) excerpt from the article that most supports the verdict, or null if none.",
    )

    # NOT set by the model -- filled in by derive_final_verdict() in code.
    final_verdict: Optional[
        Literal[
            "flooding_at_target_county",
            "flooding_elsewhere_not_target",
            "no_flood_evidence",
            "inconclusive",
        ]
    ] = None


def derive_final_verdict(county_relevance: str, flood_status: str) -> str:
    """Code-derived compound verdict -- see spec section 2.1 for the truth table.
    This is deliberately NOT delegated to the model: the model sets the two
    independent fields (county_relevance, flood_status) and this function
    combines them, so the county-specificity rule is enforced deterministically.
    """
    if county_relevance == "at_target_county":
        if flood_status in ("confirmed_flooding", "flood_risk_or_warning", "flooding_receding_or_resolved"):
            return "flooding_at_target_county"
        if flood_status == "no_flooding_indicated":
            return "no_flood_evidence"
        return "inconclusive"
    if county_relevance in ("nearby_different_county", "state_or_region_general", "unrelated_location"):
        return "flooding_elsewhere_not_target"
    return "inconclusive"


def build_article_verdict(
    *,
    county: str,
    state: str,
    title: str,
    article_text: str,
    client=None,
) -> ArticleVerdict:
    """Call the LLM once for one article and return a fully-populated
    ArticleVerdict (final_verdict included, derived in code).

    `client` may be injected for testing (an object with a
    `.messages.parse(...)` method matching the anthropic SDK's interface).
    If None, a real `anthropic.Anthropic()` client is constructed.

    Raises on hard failure (bad API response, network error) -- callers in
    main.py MUST wrap this call in try/except per operating instruction #6.
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    text = (article_text or "")[:MAX_ARTICLE_TEXT_CHARS]

    resp = client.messages.parse(
        model=LLM_VERDICT_MODEL,
        max_tokens=500,
        system=(
            "You are a careful news-reading assistant for a disaster monitoring system. "
            f"Read the article below and determine two things about it, specifically with "
            f"respect to {county} County, {state}: "
            "(1) whether the article's flooding content is actually about this specific county, "
            "a nearby/different county, a general state/regional mention, or unrelated; "
            "(2) the current flood status the article describes. "
            "Base your answer only on the article text provided. If the article does not "
            "mention this county by name or an unambiguous place within it, do not assume "
            "it is about this county."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Target county: {county} County, {state}\n\nArticle title: {title}\n\nArticle text:\n{text}",
            }
        ],
        output_format=ArticleVerdict,
    )
    verdict = resp.parsed_output
    verdict.final_verdict = derive_final_verdict(verdict.county_relevance, verdict.flood_status)
    if verdict.supporting_quote:
        verdict.supporting_quote = verdict.supporting_quote[:240]
    return verdict
