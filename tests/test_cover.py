"""Tests for ebook_enricher.cover — pure cover ops (no enrichment policy)."""
import pytest
import respx
import httpx

from ebook_enricher import cover


@pytest.mark.asyncio
async def test_download_cover_returns_bytes_on_200():
    body = b"x" * 100_000  # 100KB, above MIN_COVER_SIZE_BYTES
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))
        result = await cover.download_cover(url)
    assert result == body


@pytest.mark.asyncio
async def test_download_cover_returns_none_on_5xx():
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(503))
        result = await cover.download_cover(url)
    assert result is None


@pytest.mark.asyncio
async def test_download_cover_returns_none_on_timeout():
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.TimeoutException("timeout"))
        result = await cover.download_cover(url)
    assert result is None


@pytest.mark.asyncio
async def test_download_cover_rejects_tiny_payload():
    body = b"x" * 1_000  # 1KB, below MIN_COVER_SIZE_BYTES (50KB)
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))
        result = await cover.download_cover(url)
    assert result is None


def test_find_cover_path_finds_standard_meta(epub_with_cover):
    """OPF with <meta name="cover" content="X"/> + manifest item → returns the href."""
    path = cover.find_cover_path_in_opf(epub_with_cover)
    assert path == "OEBPS/images/cover.jpg"


def test_find_cover_path_returns_none_when_no_meta(epub_without_cover):
    """OPF without cover meta → None."""
    assert cover.find_cover_path_in_opf(epub_without_cover) is None


def test_find_cover_path_returns_none_when_manifest_broken(epub_with_broken_cover_ref):
    """OPF cover meta points at a manifest id that doesn't exist → None."""
    assert cover.find_cover_path_in_opf(epub_with_broken_cover_ref) is None


def test_save_sidecar_writes_once(epub_with_cover):
    """First call writes the sidecar; second call is a no-op."""
    sidecar = epub_with_cover.with_suffix("").parent / (
        epub_with_cover.stem + ".original.jpg"
    )
    assert not sidecar.exists()

    ok1 = cover.save_sidecar_if_absent(epub_with_cover)
    assert ok1 is True
    assert sidecar.exists()
    first_bytes = sidecar.read_bytes()

    ok2 = cover.save_sidecar_if_absent(epub_with_cover)
    assert ok2 is True
    # Bytes unchanged — second call did NOT rewrite
    assert sidecar.read_bytes() == first_bytes


def test_save_sidecar_preserves_true_original(epub_with_cover):
    """Sidecar bytes are the original cover bytes, not anything else."""
    cover.save_sidecar_if_absent(epub_with_cover)
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    # COVER_BYTES_ORIGINAL is defined in conftest.py
    from tests.conftest import COVER_BYTES_ORIGINAL
    assert sidecar.read_bytes() == COVER_BYTES_ORIGINAL


def test_save_sidecar_returns_false_when_no_cover(epub_without_cover):
    """No cover in EPUB → can't save sidecar → returns False."""
    ok = cover.save_sidecar_if_absent(epub_without_cover)
    assert ok is False
    sidecar = epub_without_cover.parent / (epub_without_cover.stem + ".original.jpg")
    assert not sidecar.exists()
