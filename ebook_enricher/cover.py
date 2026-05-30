"""Pure cover-image operations: parse OPF for cover path, save sidecar,
download from URL. No enrichment policy here — that lives in enrich.py.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Below these thresholds we treat the candidate cover as a placeholder
# or broken asset and reject the swap.
MIN_COVER_SIZE_BYTES = 50_000   # 50KB — smaller is almost certainly a tracking pixel or placeholder
MIN_COVER_WIDTH = 500           # pixels (we trust Hardcover's reported width when checking)
DOWNLOAD_TIMEOUT_S = 10


async def download_cover(url: str, *, timeout_s: int = DOWNLOAD_TIMEOUT_S) -> Optional[bytes]:
    """GET the image at `url`. Returns bytes on a successful 200 with a
    reasonable payload size. Returns None on any failure (network,
    timeout, non-200, suspiciously small body). Never raises.

    Cover replacement is best-effort: any failure here is logged and
    the caller proceeds without replacing the cover.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("cover download failed (network): %s — %s", url, e)
        return None

    if resp.status_code != 200:
        logger.warning("cover download HTTP %d: %s", resp.status_code, url)
        return None

    data = resp.content
    if len(data) < MIN_COVER_SIZE_BYTES:
        logger.warning(
            "cover download too small (%d bytes < %d): %s",
            len(data), MIN_COVER_SIZE_BYTES, url,
        )
        return None

    return data
