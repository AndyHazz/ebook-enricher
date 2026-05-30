import httpx
import pytest
import respx

from ebook_enricher.hardcover import (
    HardcoverAuthError,
    HardcoverBook,
    RateLimitedError,
    search_book,
)


HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"


def _search_response(hits: list[dict]) -> dict:
    """Build a Hardcover search response with the given hit documents."""
    return {
        "data": {
            "search": {
                "results": {
                    "found": len(hits),
                    "hits": [{"document": h} for h in hits],
                }
            }
        }
    }


SUCCESS_HIT = {
    "id": "638191",
    "title": "All the Skills",
    "description": "A deckbuilding LitRPG adventure.",
    "author_names": ["Honour Rae"],
    "contributions": [{"author": {"name": "Honour Rae"}}],
    "genres": ["LitRPG", "Fantasy", "Progression Fantasy"],
    "featured_series": {
        "featured": True,
        "position": 1.0,
        "series": {"name": "All the Skills"},
    },
}


@pytest.mark.asyncio
@respx.mock
async def test_successful_search():
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([SUCCESS_HIT]))
    )
    results = await search_book("All the Skills", "Honour Rae", token="fake")
    assert len(results) == 1
    book = results[0]
    assert isinstance(book, HardcoverBook)
    assert book.title == "All the Skills"
    assert book.author == "Honour Rae"
    assert book.series_name == "All the Skills"
    assert book.series_position == "1"
    assert "LitRPG" in book.genres
    assert book.description.startswith("A deckbuilding")


@pytest.mark.asyncio
@respx.mock
async def test_empty_search():
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([]))
    )
    results = await search_book("Unknown", "Nobody", token="fake")
    assert results == []


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_retries_once():
    route = respx.post(HARDCOVER_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=_search_response([SUCCESS_HIT])),
        ]
    )
    results = await search_book("Test", "Test", token="fake")
    assert len(results) == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_twice_raises():
    respx.post(HARDCOVER_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        await search_book("Test", "Test", token="fake")


@pytest.mark.asyncio
@respx.mock
async def test_standalone_book_has_no_series():
    standalone = {
        "id": "1",
        "title": "Standalone",
        "description": "No series.",
        "author_names": ["Author"],
        "featured_series": {},
        "genres": [],
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([standalone]))
    )
    results = await search_book("Standalone", "Author", token="fake")
    assert results[0].series_name is None
    assert results[0].series_position is None


@pytest.mark.asyncio
@respx.mock
async def test_graphql_errors_raise():
    error_payload = {
        "errors": [{"message": "Not authorized"}],
        "data": None,
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=error_payload)
    )
    with pytest.raises(HardcoverAuthError, match="Hardcover GraphQL errors"):
        await search_book("Any", "Any", token="fake")


@pytest.mark.asyncio
@respx.mock
async def test_malformed_hit_is_skipped():
    good = SUCCESS_HIT
    malformed = {"description": "bad", "genres": []}
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([good, malformed]))
    )
    results = await search_book("All the Skills", "Honour Rae", token="fake")
    assert len(results) == 1
    assert results[0].title == "All the Skills"


@pytest.mark.asyncio
@respx.mock
async def test_integer_series_position_formatted_without_decimal():
    hit = {
        **SUCCESS_HIT,
        "featured_series": {"featured": True, "position": 1.0, "series": {"name": "X"}},
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([hit]))
    )
    results = await search_book("x", "y", token="fake")
    assert results[0].series_position == "1"


@pytest.mark.asyncio
@respx.mock
async def test_fractional_series_position_preserved():
    hit = {
        **SUCCESS_HIT,
        "featured_series": {"featured": True, "position": 1.5, "series": {"name": "X"}},
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([hit]))
    )
    results = await search_book("x", "y", token="fake")
    assert results[0].series_position == "1.5"


@pytest.mark.asyncio
@respx.mock
async def test_genres_deduped_and_capped():
    hit = {
        **SUCCESS_HIT,
        "genres": ["Nonfiction", "nonfiction", "Politics", "Essays", "History", "Memoir", "Economics"],
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([hit]))
    )
    results = await search_book("x", "y", token="fake")
    genres = results[0].genres
    assert len(genres) == 5
    lowered = [g.lower() for g in genres]
    assert lowered.count("nonfiction") == 1


@pytest.mark.asyncio
@respx.mock
async def test_author_from_contributions_preferred():
    hit = {
        **SUCCESS_HIT,
        "contributions": [{"author": {"name": "Primary Author"}}],
        "author_names": ["Fallback Name"],
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([hit]))
    )
    results = await search_book("x", "y", token="fake")
    assert results[0].author == "Primary Author"


@pytest.mark.asyncio
@respx.mock
async def test_author_falls_back_to_author_names():
    hit = {
        **SUCCESS_HIT,
        "contributions": [],
        "author_names": ["Only In Names"],
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=_search_response([hit]))
    )
    results = await search_book("x", "y", token="fake")
    assert results[0].author == "Only In Names"


def test_parse_hit_extracts_image_fields():
    """A hit with an `image` block populates image_url/width/height."""
    from ebook_enricher.hardcover import _parse_hit

    hit = {
        "document": {
            "id": 42,
            "title": "Test Book",
            "author_names": ["Test Author"],
            "image": {
                "url": "https://assets.hardcover.app/edition/1/abc.jpg",
                "width": 1463,
                "height": 2401,
            },
        }
    }
    book = _parse_hit(hit)
    assert book is not None
    assert book.image_url == "https://assets.hardcover.app/edition/1/abc.jpg"
    assert book.image_width == 1463
    assert book.image_height == 2401


def test_parse_hit_no_image_block():
    """A hit without an `image` block leaves image fields as None."""
    from ebook_enricher.hardcover import _parse_hit

    hit = {
        "document": {
            "id": 42,
            "title": "Test Book",
            "author_names": ["Test Author"],
        }
    }
    book = _parse_hit(hit)
    assert book is not None
    assert book.image_url is None
    assert book.image_width is None
    assert book.image_height is None
