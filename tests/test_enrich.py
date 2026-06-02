from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

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


@pytest.mark.asyncio
@respx.mock
async def test_enrich_replaces_cover_when_hardcover_has_image(epub_with_cover):
    """When Hardcover returns a hit with image_url, cover gets replaced
    and the original is preserved as a sidecar."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    new_cover_bytes = b"HARDCOVER_NEW_COVER" + b"x" * 80_000
    cover_url = "https://assets.hardcover.app/edition/1/new.jpg"

    # Mock Hardcover search response with image
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 1463, "height": 2401},
                }
            }]}}}
        }),
    )
    # Mock the cover image fetch
    respx.get(cover_url).mock(
        return_value=httpx.Response(200, content=new_cover_bytes)
    )

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    # Cover bytes inside the EPUB are now Hardcover's
    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == new_cover_bytes

    # Sidecar exists with the TRUE original bytes
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    assert sidecar.exists()
    assert sidecar.read_bytes() == COVER_BYTES_ORIGINAL


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_cover_when_hardcover_no_image(epub_with_cover):
    """Hit without image block → metadata written, no sidecar, no cover change."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    # No "image" key
                }
            }]}}}
        }),
    )

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == COVER_BYTES_ORIGINAL
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    assert not sidecar.exists()


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_cover_when_download_fails(epub_with_cover):
    """Cover download returns 503 → metadata still written, no sidecar,
    no cover change."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    cover_url = "https://assets.hardcover.app/edition/1/new.jpg"
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 1463, "height": 2401},
                }
            }]}}}
        }),
    )
    respx.get(cover_url).mock(return_value=httpx.Response(503))

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == COVER_BYTES_ORIGINAL
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    assert not sidecar.exists()


@pytest.mark.asyncio
@respx.mock
async def test_enrich_adds_cover_when_epub_lacks_cover_meta(epub_without_cover):
    """EPUB has no <meta name="cover"> → enricher ADDS one: bytes land at
    OEBPS/images/cover.jpg, OPF gets a manifest item + meta tag. No
    sidecar (nothing to preserve)."""
    from ebook_enricher.enrich import enrich_file
    from ebook_enricher import cover
    import zipfile

    new_cover_bytes = b"NEW_COVER_FOR_NO_COVER_EPUB" + b"x" * 80_000
    cover_url = "https://assets.hardcover.app/edition/1/new.jpg"
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 1463, "height": 2401},
                }
            }]}}}
        }),
    )
    respx.get(cover_url).mock(
        return_value=httpx.Response(200, content=new_cover_bytes)
    )

    result = await enrich_file(epub_without_cover, token="fake-token")
    assert result.status == "enriched"

    # Cover bytes inside the zip at the canonical add location
    with zipfile.ZipFile(epub_without_cover) as zf:
        assert "OEBPS/images/cover.jpg" in zf.namelist()
        # resize may have re-encoded the bytes if image was decodable;
        # our fake bytes aren't a real JPEG so resize is a no-op.
        assert zf.read("OEBPS/images/cover.jpg") == new_cover_bytes

    # OPF now resolves the cover via the standard lookup
    assert cover.find_cover_path_in_opf(epub_without_cover) == "OEBPS/images/cover.jpg"

    # No sidecar — original was "nothing", nothing to preserve
    sidecar = epub_without_cover.parent / (epub_without_cover.stem + ".original.jpg")
    assert not sidecar.exists()


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_cover_when_image_too_small(epub_with_cover):
    """Hardcover hit with image_width < MIN_COVER_WIDTH → editions fallback
    called but returns nothing → skip cover swap.
    The download endpoint is NOT mocked; if reached, respx raises."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    cover_url = "https://assets.hardcover.app/edition/1/thumbnail.jpg"
    respx.post("https://api.hardcover.app/v1/graphql").mock(side_effect=[
        # search()
        httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 100, "height": 150},  # too small
                }
            }]}}}
        }),
        # editions() — returns empty → no winner
        httpx.Response(200, json={"data": {"editions": []}}),
    ])
    # Cover download URL deliberately NOT mocked — if code reaches it, respx raises.

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    # Cover bytes unchanged
    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == COVER_BYTES_ORIGINAL
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    assert not sidecar.exists()


@pytest.mark.asyncio
@respx.mock
async def test_enrich_uses_editions_fallback_when_canonical_too_small(epub_with_cover):
    """Top search hit has width<500. Editions fallback finds a 2000x3000
    ebook. Cover gets swapped from the editions URL."""
    from ebook_enricher.enrich import enrich_file
    from io import BytesIO
    from PIL import Image
    import zipfile

    small_url = "https://assets.hardcover.app/small.jpg"
    big_url = "https://assets.hardcover.app/big.jpg"
    big_img = Image.new("RGB", (2000, 3000), (10, 200, 30))
    big_bytes_buf = BytesIO()
    big_img.save(big_bytes_buf, format="JPEG", quality=90)
    big_bytes = big_bytes_buf.getvalue()

    respx.post("https://api.hardcover.app/v1/graphql").mock(side_effect=[
        # First call: search()
        httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 123,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "desc",
                    "featured_series": {"series": {"name": "TS"}, "position": 1},
                    "image": {"url": small_url, "width": 325, "height": 500},
                }
            }]}}}
        }),
        # Second call: editions()
        httpx.Response(200, json={
            "data": {"editions": [
                {
                    "id": 9999,
                    "edition_format": "ebook",
                    "image": {"url": big_url, "width": 2000, "height": 3000},
                    "language": {"code2": "en"},
                    "users_count": 50,
                },
            ]}
        }),
    ])
    respx.get(big_url).mock(return_value=httpx.Response(200, content=big_bytes))

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        published = zf.read("OEBPS/images/cover.jpg")
    img = Image.open(BytesIO(published))
    from ebook_enricher.cover import MAX_COVER_LONG_EDGE
    assert max(img.size) == MAX_COVER_LONG_EDGE


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_fallback_when_canonical_is_large_enough(epub_with_cover):
    """Canonical width >= MIN_COVER_WIDTH → editions endpoint NOT called.
    Cover swap uses the canonical image URL."""
    from ebook_enricher.enrich import enrich_file
    from io import BytesIO
    from PIL import Image
    import zipfile

    good_url = "https://assets.hardcover.app/good.jpg"
    big_img = Image.new("RGB", (1500, 2400), (200, 50, 50))
    big_buf = BytesIO()
    big_img.save(big_buf, format="JPEG", quality=90)
    big_bytes = big_buf.getvalue()

    # Only the search call is mocked. If editions is called, respx raises.
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 456,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "desc",
                    "featured_series": {"series": {"name": "TS"}, "position": 1},
                    "image": {"url": good_url, "width": 1500, "height": 2400},
                }
            }]}}}
        }),
    )
    respx.get(good_url).mock(return_value=httpx.Response(200, content=big_bytes))

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        published = zf.read("OEBPS/images/cover.jpg")
    assert len(published) > 0


@pytest.mark.asyncio
@respx.mock
async def test_enrich_fallback_returns_no_winner_skips_cover(epub_with_cover):
    """Canonical too small, editions all rejected → metadata writes, no cover swap."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    respx.post("https://api.hardcover.app/v1/graphql").mock(side_effect=[
        # search()
        httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 789,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "desc",
                    "featured_series": {"series": {"name": "TS"}, "position": 1},
                    "image": {"url": "https://small.jpg", "width": 300, "height": 450},
                }
            }]}}}
        }),
        # editions() — all too small
        httpx.Response(200, json={
            "data": {"editions": [
                {
                    "id": 1,
                    "edition_format": "Mass Market Paperback",
                    "image": {"url": "https://tiny.jpg", "width": 300, "height": 450},
                    "language": {"code2": "en"},
                    "users_count": 5,
                },
            ]}
        }),
    ])
    # No cover URL is mocked — if download is attempted, respx raises.

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        published = zf.read("OEBPS/images/cover.jpg")
    assert published == COVER_BYTES_ORIGINAL  # unchanged


@pytest.mark.asyncio
@respx.mock
async def test_enrich_resizes_oversized_cover(epub_with_cover):
    """A 2000x3000 Hardcover image is downsized to 1648 max edge before
    being written into the EPUB."""
    from ebook_enricher.enrich import enrich_file
    from ebook_enricher.cover import MAX_COVER_LONG_EDGE
    from io import BytesIO
    from PIL import Image
    import zipfile

    # Make a real big JPEG
    big_img = Image.new("RGB", (2000, 3000), (50, 100, 200))
    big_buf = BytesIO()
    big_img.save(big_buf, format="JPEG", quality=90)
    big_bytes = big_buf.getvalue()
    assert len(big_bytes) > 50_000  # passes MIN_COVER_SIZE_BYTES

    cover_url = "https://assets.hardcover.app/edition/1/big.jpg"
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 2000, "height": 3000},
                }
            }]}}}
        }),
    )
    respx.get(cover_url).mock(return_value=httpx.Response(200, content=big_bytes))

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    # Cover inside EPUB is resized: longest edge <= 1648
    with zipfile.ZipFile(epub_with_cover) as zf:
        published = zf.read("OEBPS/images/cover.jpg")
    img = Image.open(BytesIO(published))
    assert max(img.size) == MAX_COVER_LONG_EDGE
    # And the result is smaller than the original
    assert len(published) < len(big_bytes)


@pytest.mark.asyncio
async def test_correct_series_overwrites_name_and_index(enriched_epub: Path):
    """correct_series=True overwrites an existing (wrong) series name AND
    index from a confident Hardcover match."""
    hc = _make_hc_book(series_name="Test Series", series_position="1.5")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    assert result.series_corrected is True
    meta = read_meta(enriched_epub)
    assert meta.series == "Test Series"       # was "Existing Series"
    assert meta.series_index == "1.5"          # was "2"


@pytest.mark.asyncio
async def test_correct_series_populates_missing(bare_epub: Path):
    """correct_series=True still populates a blank series (parity with the
    default populate path), and reports it as a correction."""
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[_make_hc_book()])):
        result = await enrich_file(bare_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    assert result.series_corrected is True
    meta = read_meta(bare_epub)
    assert meta.series == "Test Series"
    assert meta.series_index == "1.5"


@pytest.mark.asyncio
async def test_correct_series_preserves_on_low_confidence(enriched_epub: Path):
    """No confident match -> existing series is NOT blanked/changed."""
    hc = _make_hc_book(title="Totally Unrelated Book", author="Someone Else",
                       series_name="Wrong Series", series_position="9")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "low_confidence"
    assert result.series_corrected is False
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"    # untouched
    assert meta.series_index == "2"


@pytest.mark.asyncio
async def test_correct_series_preserves_on_standalone_hit(enriched_epub: Path):
    """Confident match but Hardcover hit has no series -> existing series
    is left intact, never blanked."""
    hc = _make_hc_book(series_name=None, series_position=None)
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    assert result.series_corrected is False
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"    # untouched
    assert meta.series_index == "2"


@pytest.mark.asyncio
async def test_correct_series_false_keeps_skip(enriched_epub: Path):
    """Default (correct_series=False) preserves the legacy skip behaviour."""
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock()) as mock:
        result = await enrich_file(enriched_epub, token="fake", correct_series=False)
    assert result.status == "skipped"
    assert result.reason == "already_enriched"
    mock.assert_not_awaited()
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"


@pytest.mark.asyncio
async def test_correct_series_leaves_description_only_if_empty(enriched_epub: Path):
    """Correcting series does NOT overwrite an existing description."""
    hc = _make_hc_book(series_name="Test Series",
                       description="A DIFFERENT description from Hardcover.")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    meta = read_meta(enriched_epub)
    assert meta.description == "Existing description."   # unchanged
