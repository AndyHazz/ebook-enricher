from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ebook_enricher.enrich import EnrichResult, enrich_file
from ebook_enricher.epub_meta import read_meta
from ebook_enricher.hardcover import HardcoverBook


def _make_hc_book(**overrides) -> HardcoverBook:
    defaults = dict(
        id="1",
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


@pytest.mark.asyncio
async def test_rate_limited(bare_epub: Path):
    from ebook_enricher.hardcover import RateLimitedError
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(side_effect=RateLimitedError("two 429s")),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "rate_limited"
    # EPUB must be untouched on rate limit
    meta = read_meta(bare_epub)
    assert meta.series is None


@pytest.mark.asyncio
async def test_hardcover_network_error(bare_epub: Path):
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "error"
    assert "hardcover_error" in (result.reason or "")


@pytest.mark.asyncio
async def test_write_failure_reported_as_error(bare_epub: Path):
    good = _make_hc_book()
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(return_value=[good]),
    ), patch(
        "ebook_enricher.enrich.write_meta",
        side_effect=IOError("disk full"),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "error"
    assert "write_failed" in (result.reason or "")


@pytest.mark.asyncio
async def test_auth_error_status(bare_epub: Path):
    from ebook_enricher.hardcover import HardcoverAuthError
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(side_effect=HardcoverAuthError("Hardcover GraphQL errors: [{'message': 'Not authorized'}]")),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "auth_error"
    assert "Not authorized" in (result.reason or "")
    # EPUB untouched
    meta = read_meta(bare_epub)
    assert meta.series is None


@pytest.mark.asyncio
async def test_network_error_status(bare_epub: Path):
    import httpx
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "network_error"
    assert "connection refused" in (result.reason or "")


@pytest.mark.asyncio
async def test_preserves_existing_description_when_enriching_series(
    bare_epub: Path, tmp_path: Path
):
    # Build a fixture with a description already present but no series.
    # Enrichment should fill series but NOT overwrite description.
    import zipfile
    import shutil
    from ebook_enricher.epub_meta import NS, _find_opf_path
    from xml.etree import ElementTree as ET

    # Copy bare fixture and inject a description
    target = tmp_path / "has_desc.epub"
    shutil.copy(bare_epub, target)

    with zipfile.ZipFile(target) as zf:
        opf_path = _find_opf_path(zf)
        root = ET.fromstring(zf.read(opf_path))
    metadata = root.find("opf:metadata", NS)
    el = ET.SubElement(metadata, f"{{{NS['dc']}}}description")
    el.text = "Pre-existing blurb that must not be overwritten."
    new_opf = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    import tempfile, os
    tmp_fd, tmp_zip = tempfile.mkstemp(suffix=".epub", dir=target.parent)
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(target) as src, \
             zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                if item.filename == opf_path:
                    dst.writestr(item, new_opf)
                elif item.filename == "mimetype":
                    dst.writestr(item, src.read(item.filename), compress_type=zipfile.ZIP_STORED)
                else:
                    dst.writestr(item, src.read(item.filename))
        shutil.move(tmp_zip, target)
    finally:
        Path(tmp_zip).unlink(missing_ok=True)

    # Sanity: description is present, series is not
    pre = read_meta(target)
    assert pre.description == "Pre-existing blurb that must not be overwritten."
    assert pre.series is None

    # Enrich: Hardcover offers BOTH series and a different description
    hc = _make_hc_book(description="A DIFFERENT description from Hardcover.")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(target, token="fake")
    assert result.status == "enriched"

    post = read_meta(target)
    assert post.series == "Test Series"  # filled
    # Existing description is preserved, NOT overwritten
    assert post.description == "Pre-existing blurb that must not be overwritten."


@pytest.mark.asyncio
async def test_http_401_classified_as_auth_error(bare_epub: Path):
    import httpx
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.hardcover.app/v1/graphql"))
    err = httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(side_effect=err),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "auth_error"
    meta = read_meta(bare_epub)
    assert meta.series is None


@pytest.mark.asyncio
async def test_http_403_classified_as_auth_error(bare_epub: Path):
    import httpx
    response = httpx.Response(403, request=httpx.Request("POST", "https://api.hardcover.app/v1/graphql"))
    err = httpx.HTTPStatusError("Forbidden", request=response.request, response=response)
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(side_effect=err),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "auth_error"


@pytest.mark.asyncio
async def test_http_503_classified_as_network_error(bare_epub: Path):
    import httpx
    response = httpx.Response(503, request=httpx.Request("POST", "https://api.hardcover.app/v1/graphql"))
    err = httpx.HTTPStatusError("Service Unavailable", request=response.request, response=response)
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(side_effect=err),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "network_error"


@pytest.mark.asyncio
async def test_picks_best_candidate_not_first(bare_epub: Path):
    """When multiple candidates pass the confidence gate, pick the one
    with the highest combined (title + author) score, not the first."""
    # EPUB title is "Test Book Title"
    broader = _make_hc_book(
        title="Test Book Title Box Set: Three Books Collection",
        author="Test Author",
    )
    exact = _make_hc_book(
        title="Test Book Title",
        author="Test Author",
        series_name="Proper Series",
        series_position="1",
    )
    with patch(
        "ebook_enricher.enrich.search_book",
        new=AsyncMock(return_value=[broader, exact]),
    ):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "enriched"
    # The `exact` candidate had "Proper Series" — if we wrongly picked
    # `broader` we'd have lost this metadata (or more likely, inherited
    # generic box-set metadata).
    meta = read_meta(bare_epub)
    assert meta.series == "Proper Series"
