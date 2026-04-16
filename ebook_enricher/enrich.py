"""Per-file enrichment orchestrator.

Pipeline:
  1. Read EPUB metadata.
  2. If calibre:series is already set, skip (respect existing good data).
  3. Query Hardcover for top 3 matches by popularity.
  4. Iterate matches, first one passing is_confident_match wins.
  5. Write back only fields currently empty in the EPUB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ebook_enricher.epub_meta import EpubMeta, read_meta, write_meta
from ebook_enricher.hardcover import HardcoverBook, RateLimitedError, search_book
from ebook_enricher.matcher import is_confident_match

logger = logging.getLogger(__name__)


@dataclass
class EnrichResult:
    status: str  # enriched | skipped | no_match | low_confidence | rate_limited | error
    reason: Optional[str] = None
    series: Optional[str] = None  # For debugging


async def enrich_file(path: Path, token: str) -> EnrichResult:
    try:
        meta = read_meta(path)
    except FileNotFoundError:
        return EnrichResult(status="error", reason=f"file_not_found: {path}")
    except Exception as e:
        logger.exception("Failed to read EPUB %s", path)
        return EnrichResult(status="error", reason=f"read_failed: {e}")

    if meta.series:
        return EnrichResult(status="skipped", reason="already_enriched")

    try:
        candidates = await search_book(meta.title, meta.author, token=token)
    except RateLimitedError:
        return EnrichResult(status="rate_limited")
    except Exception as e:
        logger.exception("Hardcover query failed for %s", path)
        return EnrichResult(status="error", reason=f"hardcover_error: {e}")

    if not candidates:
        return EnrichResult(status="no_match")

    chosen: Optional[HardcoverBook] = None
    for candidate in candidates:
        if is_confident_match(meta.title, meta.author, candidate.title, candidate.author):
            chosen = candidate
            break

    if chosen is None:
        return EnrichResult(status="low_confidence")

    updates = EpubMeta(
        title=meta.title,  # not written, but required by dataclass
        author=meta.author,
    )
    if not meta.series and chosen.series_name:
        updates.series = chosen.series_name
    if not meta.series_index and chosen.series_position:
        updates.series_index = chosen.series_position
    if not meta.description and chosen.description:
        updates.description = chosen.description
    if not meta.subjects and chosen.genres:
        updates.subjects = chosen.genres

    try:
        write_meta(path, updates)
    except Exception as e:
        logger.exception("Failed to write EPUB %s", path)
        return EnrichResult(status="error", reason=f"write_failed: {e}")

    return EnrichResult(status="enriched", series=chosen.series_name)
