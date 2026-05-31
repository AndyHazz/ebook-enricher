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
from typing import Literal, Optional

Status = Literal[
    "enriched", "skipped", "no_match", "low_confidence",
    "rate_limited", "auth_error", "network_error", "error",
]

import httpx

from ebook_enricher import cover, hardcover
from ebook_enricher.epub_meta import EpubMeta, read_meta, write_meta
from ebook_enricher.hardcover import (
    HardcoverAuthError,
    HardcoverBook,
    RateLimitedError,
    search_book,
)
from ebook_enricher.matcher import (
    AUTHOR_THRESHOLD,
    TITLE_THRESHOLD,
    score_match,
)

logger = logging.getLogger(__name__)


@dataclass
class EnrichResult:
    status: Status
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
    except HardcoverAuthError as e:
        logger.warning("Hardcover auth error for %s: %s", path, e)
        return EnrichResult(status="auth_error", reason=str(e))
    except httpx.HTTPStatusError as e:
        # HTTP-level error from Hardcover. 401/403 = auth problem;
        # 4xx/5xx otherwise = treat as network/service error.
        if e.response.status_code in (401, 403):
            logger.warning(
                "Hardcover HTTP %d (auth) for %s: %s",
                e.response.status_code, path, e,
            )
            return EnrichResult(status="auth_error", reason=str(e))
        logger.warning(
            "Hardcover HTTP %d for %s: %s",
            e.response.status_code, path, e,
        )
        return EnrichResult(status="network_error", reason=str(e))
    except httpx.HTTPError as e:
        # Connect refused, timeout, DNS failure
        logger.warning("Hardcover network error for %s: %s", path, e)
        return EnrichResult(status="network_error", reason=str(e))
    except Exception as e:
        logger.exception("Hardcover query failed for %s", path)
        return EnrichResult(status="error", reason=f"hardcover_error: {e}")

    if not candidates:
        return EnrichResult(status="no_match")

    # Score every candidate and pick the best — not the first passing one.
    # Hardcover's search can return broader matches (box sets, omnibus
    # editions) before the specific book, so first-passing lost information.
    # When scores tie, prefer the candidate whose title length is closest
    # to the EPUB's title length — a shorter HC title is usually more
    # specific (the standalone book) than a longer one (the box set).
    chosen: Optional[HardcoverBook] = None
    best_key: tuple[int, int] = (-1, -(1 << 30))
    for candidate in candidates:
        t_score, a_score = score_match(
            meta.title, meta.author, candidate.title, candidate.author
        )
        if t_score < TITLE_THRESHOLD or a_score < AUTHOR_THRESHOLD:
            continue
        total = t_score + a_score
        length_penalty = -abs(len(meta.title) - len(candidate.title))
        key = (total, length_penalty)
        if key > best_key:
            chosen = candidate
            best_key = key

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

    # Determine which image URL to use for cover replacement.
    # If the canonical search hit's image is missing or too small, fall
    # back to scanning the book's editions for a higher-resolution alt.
    candidate_url = chosen.image_url
    candidate_width = chosen.image_width
    if not candidate_url or (
        candidate_width is not None
        and candidate_width < cover.MIN_COVER_WIDTH
    ):
        editions = await hardcover.fetch_editions(int(chosen.id), token=token)
        best = hardcover.pick_best_edition_cover(
            editions,
            source_language=None,
            min_width=cover.MIN_COVER_WIDTH,
        )
        if best:
            candidate_url = best.image_url
            candidate_width = best.image_width
            logger.info(
                "editions fallback: using ed_id=%d (%dx%d) for book_id=%s",
                best.edition_id, best.image_width, best.image_height,
                chosen.id,
            )

    # Prepare cover override (best-effort — failures here never block
    # metadata enrichment).
    cover_override = None
    if candidate_url and (
        candidate_width is None
        or candidate_width >= cover.MIN_COVER_WIDTH
    ):
        existing_cover_path = cover.find_cover_path_in_opf(path)
        if existing_cover_path:
            cover_bytes = await cover.download_cover(candidate_url)
            if cover_bytes:
                cover_bytes = cover.resize_cover_if_needed(cover_bytes)
                saved = cover.save_sidecar_if_absent(path)
                if saved:
                    cover_override = (existing_cover_path, cover_bytes)
                # else: sidecar save failed — skip the swap to avoid
                # losing the only original.

    try:
        write_meta(path, updates, cover_override=cover_override)
    except Exception as e:
        logger.exception("Failed to write EPUB %s", path)
        return EnrichResult(status="error", reason=f"write_failed: {e}")

    return EnrichResult(status="enriched", series=chosen.series_name)
