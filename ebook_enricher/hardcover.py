"""Hardcover GraphQL client.

Uses Hardcover's full-text `search` endpoint. Earlier iterations used the
`books` table with `_ilike` wildcards for fuzzy matching, but that operator
is disabled on the Hardcover server ("ilike and related operations are not
permitted on this server"). The `search` endpoint is designed for this
use case and gives access to a pre-scored hit list.

Rate limits: 60 req/min. We use async httpx and retry once on 429 after
a short sleep. HTTP 401/403 is classified separately as an auth problem;
other HTTP errors and network failures propagate for the caller to handle.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"
TIMEOUT_S = 20
RETRY_SLEEP_S = 2
PER_PAGE = 3

logger = logging.getLogger(__name__)


class RateLimitedError(Exception):
    """Raised when Hardcover returns 429 after a retry."""


class HardcoverAuthError(Exception):
    """Raised when Hardcover returns a GraphQL errors array.

    This is usually an authentication problem (expired token) but can
    also indicate a malformed query or a schema change. The caller
    should treat it as an actionable error — enrichment cannot proceed
    until the credential or query is fixed.
    """


@dataclass
class HardcoverBook:
    id: str
    title: str
    author: str
    description: Optional[str]
    series_name: Optional[str]
    series_position: Optional[str]
    genres: list[str]


QUERY = """
query SearchBooks($q: String!, $per_page: Int!) {
  search(query: $q, query_type: "books", per_page: $per_page, page: 1) {
    results
  }
}
"""


def _format_position(pos) -> Optional[str]:
    """Format a series position like Calibre: '1' for integers, '1.5' for decimals."""
    if pos is None:
        return None
    if isinstance(pos, float) and pos.is_integer():
        return str(int(pos))
    return str(pos)


def _pick_series(doc: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract series name + position from a search hit's document.

    The search response exposes one `featured_series` object per document
    which is the book's primary series (if any). `featured_series_position`
    is also available as a top-level field.
    """
    featured = doc.get("featured_series") or {}
    if not featured:
        return None, None
    series = featured.get("series") or {}
    name = series.get("name") or None
    pos = featured.get("position")
    if pos is None:
        pos = doc.get("featured_series_position")
    return name, _format_position(pos)


def _first_author(doc: dict) -> str:
    """Prefer structured `contributions[0].author.name`, fall back to `author_names[0]`."""
    contributions = doc.get("contributions") or []
    if contributions:
        author_obj = contributions[0].get("author") or {}
        name = author_obj.get("name")
        if name:
            return name
    names = doc.get("author_names") or []
    return names[0] if names else ""


def _extract_genres(doc: dict) -> list[str]:
    """Take up to 5 genre tags. The search response already returns them
    as a flat list of strings, pre-ranked by Hardcover's own relevance.
    """
    genres = doc.get("genres") or []
    # Preserve order; dedupe case-insensitively to avoid duplicates like
    # "Nonfiction" vs "nonfiction".
    seen = set()
    out: list[str] = []
    for g in genres:
        if not g:
            continue
        key = g.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(g.strip())
        if len(out) >= 5:
            break
    return out


def _parse_hit(hit: dict) -> Optional[HardcoverBook]:
    """Parse a single search hit. Hardcover is in beta — skip malformed
    hits rather than crashing the whole query.
    """
    doc = hit.get("document") or {}
    book_id = doc.get("id")
    title = doc.get("title")
    if book_id is None or not title:
        logger.warning("Skipping malformed Hardcover hit: id=%r title=%r", book_id, title)
        return None
    series_name, series_pos = _pick_series(doc)
    return HardcoverBook(
        id=str(book_id),
        title=title,
        author=_first_author(doc),
        description=doc.get("description"),
        series_name=series_name,
        series_position=series_pos,
        genres=_extract_genres(doc),
    )


async def _post(client: httpx.AsyncClient, token: str, variables: dict):
    resp = await client.post(
        HARDCOVER_URL,
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        json={"query": QUERY, "variables": variables},
        timeout=TIMEOUT_S,
    )
    return resp.status_code, resp


async def search_book(title: str, author: str, token: str) -> list[HardcoverBook]:
    # Combine title and author into one natural-language query — Hardcover's
    # search indexes both title and author_names by default.
    query_text = f"{title} {author}".strip()
    variables = {"q": query_text, "per_page": PER_PAGE}
    async with httpx.AsyncClient() as client:
        for attempt in range(2):
            status, resp = await _post(client, token, variables)
            if status == 429:
                if attempt == 0:
                    await asyncio.sleep(RETRY_SLEEP_S)
                    continue
                raise RateLimitedError("Hardcover returned 429 twice")
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise HardcoverAuthError(f"Hardcover GraphQL errors: {payload['errors']}")
            results = (
                (payload.get("data") or {}).get("search") or {}
            ).get("results") or {}
            hits = results.get("hits") or []
            parsed = [_parse_hit(h) for h in hits]
            return [p for p in parsed if p is not None]
    return []
