from eval import news_gdelt


def _gkg_row() -> str:
    columns = [""] * 16
    columns[1] = "20170823000000"
    columns[3] = "example.org"
    columns[4] = "https://example.org/harvey-flooding-in-houston"
    columns[7] = "ENV_FLOODING;WEATHER"
    columns[9] = "Houston, Texas, United States"
    columns[15] = "-2.5,0,0"
    return "\t".join(columns)


def test_build_news_corpus_records_as_of_and_missing_slice_coverage(monkeypatch):
    stamps = ["20170823000000", "20170823001500"]
    monkeypatch.setattr(news_gdelt, "_slices", lambda start, end: stamps)
    monkeypatch.setattr(
        news_gdelt,
        "_fetch_slice",
        lambda stamp: _gkg_row() if stamp == stamps[0] else None,
    )

    corpus = news_gdelt.build_news_corpus(
        start="20170823",
        end="20170824",
        place_terms=["Houston, Texas"],
        county="Harris",
        state="TX",
        workers=1,
        progress_every=99,
    )

    assert corpus["schema"] == "sentinel-news-corpus/2"
    assert corpus["as_of_field"] == "GKG_V2_1_DATE"
    assert corpus["slices_swept"] == 2
    assert corpus["slices_missing"] == 1
    assert corpus["missing_slices"] == ["20170823001500"]
    assert corpus["slice_coverage_fraction"] == 0.5
    assert corpus["articles"][0]["published_at"] == "2017-08-23T00:00:00Z"


def test_parse_slice_uses_gkg_v2_1_date_and_place_filter():
    assert news_gdelt.parse_slice(
        _gkg_row(), place_terms=["Houston, Texas"]
    )[0]["published_at"] == "2017-08-23T00:00:00Z"
    assert news_gdelt.parse_slice(
        _gkg_row(), place_terms=["Miami, Florida"]
    ) == []
