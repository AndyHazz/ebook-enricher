from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ebook_enricher.enrich import EnrichResult, enrich_file
from ebook_enricher.epub_meta import read_meta
from ebook_enricher.hardcover import HardcoverBook


def _make_hc_book(**overrides) -> HardcoverBook:
    defaults = dict(
        id=1,
        title="Test Book Title",
        author="Test Author",
        description="A test description.",
        series_name="Test Series",
        series_position="1.5",
        genres=["Fantasy", "LitRPG"],
    )
    defaults.update(overrides)
    return HardcoverBook(**defaults)


@pytest.mark.asyncio
async def test_enriches_bare_epub(bare_epub: Path):
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[_make_hc_book()])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "enriched"
    meta = read_meta(bare_epub)
    assert meta.series == "Test Series"
    assert meta.series_index == "1.5"
    assert meta.description == "A test description."
    assert "Fantasy" in meta.subjects


@pytest.mark.asyncio
async def test_skips_already_enriched(enriched_epub: Path):
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock()) as mock:
        result = await enrich_file(enriched_epub, token="fake")
    assert result.status == "skipped"
    assert result.reason == "already_enriched"
    mock.assert_not_awaited()  # Never queried Hardcover
    # Existing metadata preserved
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"


@pytest.mark.asyncio
async def test_no_match(bare_epub: Path):
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "no_match"
    meta = read_meta(bare_epub)
    assert meta.series is None


@pytest.mark.asyncio
async def test_low_confidence(bare_epub: Path):
    # Hardcover returns a book with a totally different title
    bad_match = _make_hc_book(title="Completely Different Book", author="Someone Else")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[bad_match])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "low_confidence"
    meta = read_meta(bare_epub)
    assert meta.series is None  # Untouched


@pytest.mark.asyncio
async def test_second_match_wins_if_first_is_low_confidence(bare_epub: Path):
    bad = _make_hc_book(title="Wrong Title", author="Wrong Author")
    good = _make_hc_book()  # matches "Test Book Title" / "Test Author"
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[bad, good])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "enriched"


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path):
    result = await enrich_file(tmp_path / "nope.epub", token="fake")
    assert result.status == "error"
