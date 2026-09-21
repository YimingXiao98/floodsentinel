from unittest.mock import MagicMock

from article_verdict import (
    MAX_LLM_VERDICT_ARTICLES,
    ArticleVerdict,
    build_article_verdict,
    derive_final_verdict,
)


def _mock_client(county_relevance, flood_status, confidence=0.9, quote=None):
    client = MagicMock()
    resp = MagicMock()
    resp.parsed_output = ArticleVerdict(
        county_relevance=county_relevance,
        flood_status=flood_status,
        confidence=confidence,
        rationale="test rationale",
        supporting_quote=quote,
    )
    client.messages.parse.return_value = resp
    return client


def test_build_article_verdict_sets_final_verdict_from_code_not_model():
    client = _mock_client("at_target_county", "confirmed_flooding")
    v = build_article_verdict(
        county="Harris",
        state="Texas",
        title="t",
        article_text="flooding text",
        client=client,
    )
    assert v.final_verdict == "flooding_at_target_county"


def test_build_article_verdict_truncates_long_quote():
    client = _mock_client("at_target_county", "confirmed_flooding", quote="x" * 500)
    v = build_article_verdict(
        county="Harris",
        state="Texas",
        title="t",
        article_text="text",
        client=client,
    )
    assert len(v.supporting_quote) <= 240


def test_max_llm_verdict_articles_constant_is_bounded():
    assert MAX_LLM_VERDICT_ARTICLES <= 5


def test_derive_final_verdict_table():
    assert derive_final_verdict("at_target_county", "confirmed_flooding") == "flooding_at_target_county"
    assert derive_final_verdict("nearby_different_county", "confirmed_flooding") == "flooding_elsewhere_not_target"
    assert derive_final_verdict("at_target_county", "no_flooding_indicated") == "no_flood_evidence"
    assert derive_final_verdict("unclear", "unclear") == "inconclusive"
