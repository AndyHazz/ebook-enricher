"""Hardcover GraphQL client.

One query fetches book + series + tags + description + author in one
round trip. We ask for the top 3 matches by users_read_count so a
popular book outranks a long-tail near-duplicate.

Rate limits: 60 req/min. We use async httpx and retry once on 429 after
a short sleep. Anything else (500s, network) propagates as an exception —
the caller decides how to report it.
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
    id: int
    title: str
    author: str
    description: Optional[str]
    series_name: Optional[str]
    series_position: Optional[str]
    genres: list[str]


QUERY = """
query SearchBook($title: String!, $author: String!) {
  books(
    where: {
      _and: [
        { title: { _ilike: $title } },
        { contributions: { author: { name: { _ilike: $author } } } }
      ]
    }
    order_by: { users_read_count: desc }
    limit: 3
  ) {
    id
    title
    description
    cached_tags
    book_series {
      position
      featured
      series { name }
    }
    contributions {
      author { name }
    }
  }
}
"""


def _extract_genres(cached_tags: Optional[dict]) -> list[str]:
    if not cached_tags:
        return []
    genre_tags = cached_tags.get("Genre") or []
    # Sort by count desc if present; take top 5 tag names
    def _sort_key(entry: dict) -> int:
        return -int(entry.get("count") or 0)
    sorted_tags = sorted(genre_tags, key=_sort_key)
    names = []
    for entry in sorted_tags[:5]:
        name = entry.get("tag") or entry.get("name")
        if name:
            names.append(name)
    return names


def _format_position(pos) -> Optional[str]:
    """Format a series position like Calibre: '1' for integers, '1.5' for decimals."""
    if pos is None:
        return None
    if isinstance(pos, float) and pos.is_integer():
        return str(int(pos))
    return str(pos)


def _pick_series(book_series: list[dict]) -> tuple[Optional[str], Optional[str]]:
    if not book_series:
        return None, None
    featured = next((s for s in book_series if s.get("featured")), None)
    chosen = featured or book_series[0]
    name = (chosen.get("series") or {}).get("name")
    pos = chosen.get("position")
    return name, _format_position(pos)


def _first_author(contributions: list[dict]) -> str:
    if not contributions:
        return ""
    return (contributions[0].get("author") or {}).get("name") or ""


def _parse_book(raw: dict) -> Optional[HardcoverBook]:
    # Hardcover is in beta — schema can be unstable. Skip entries missing
    # required fields rather than crashing the whole query.
    book_id = raw.get("id")
    title = raw.get("title")
    if book_id is None or not title:
        logger.warning("Skipping malformed Hardcover entry: id=%r title=%r", book_id, title)
        return None
    series_name, series_pos = _pick_series(raw.get("book_series") or [])
    return HardcoverBook(
        id=book_id,
        title=title,
        author=_first_author(raw.get("contributions") or []),
        description=raw.get("description"),
        series_name=series_name,
        series_position=series_pos,
        genres=_extract_genres(raw.get("cached_tags")),
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
    variables = {"title": f"%{title}%", "author": f"%{author}%"}
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
            books = (payload.get("data") or {}).get("books") or []
            parsed = [_parse_book(b) for b in books]
            return [p for p in parsed if p is not None]
    return []
