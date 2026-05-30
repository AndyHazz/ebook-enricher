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
