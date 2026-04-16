import httpx
import pytest
import respx

from ebook_enricher.hardcover import HardcoverBook, search_book


HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"

SUCCESS_RESPONSE = {
    "data": {
        "books": [
            {
                "id": 1,
                "title": "All the Skills",
                "description": "A deckbuilding LitRPG adventure.",
                "cached_tags": {
                    "Genre": [
                        {"tag": "LitRPG", "count": 50},
                        {"tag": "Fantasy", "count": 30},
                        {"tag": "Progression Fantasy", "count": 20},
                    ]
                },
                "book_series": [
                    {
                        "position": 1.0,
                        "featured": True,
                        "series": {"name": "All the Skills"},
                    }
                ],
                "contributions": [
                    {"author": {"name": "Honour Rae"}}
                ],
            }
        ]
    }
}

EMPTY_RESPONSE = {"data": {"books": []}}


@pytest.mark.asyncio
@respx.mock
async def test_successful_search():
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )
    results = await search_book("All the Skills", "Honour Rae", token="fake")
    assert len(results) == 1
    book = results[0]
    assert isinstance(book, HardcoverBook)
    assert book.title == "All the Skills"
    assert book.author == "Honour Rae"
    assert book.series_name == "All the Skills"
    assert book.series_position == "1.0"
    assert "LitRPG" in book.genres
    assert book.description.startswith("A deckbuilding")


@pytest.mark.asyncio
@respx.mock
async def test_empty_search():
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=EMPTY_RESPONSE)
    )
    results = await search_book("Unknown", "Nobody", token="fake")
    assert results == []


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_retries_once():
    route = respx.post(HARDCOVER_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=SUCCESS_RESPONSE),
        ]
    )
    results = await search_book("Test", "Test", token="fake")
    assert len(results) == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_twice_raises():
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(429)
    )
    from ebook_enricher.hardcover import RateLimitedError
    with pytest.raises(RateLimitedError):
        await search_book("Test", "Test", token="fake")


@pytest.mark.asyncio
@respx.mock
async def test_series_without_featured_flag_still_picked():
    """If no entry is featured, first entry wins."""
    payload = {
        "data": {
            "books": [
                {
                    "id": 2,
                    "title": "Book",
                    "description": "Desc",
                    "cached_tags": {},
                    "book_series": [
                        {
                            "position": 2.0,
                            "featured": False,
                            "series": {"name": "First Series"},
                        },
                        {
                            "position": 1.0,
                            "featured": False,
                            "series": {"name": "Second Series"},
                        },
                    ],
                    "contributions": [{"author": {"name": "Author"}}],
                }
            ]
        }
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )
    results = await search_book("Book", "Author", token="fake")
    assert results[0].series_name == "First Series"


@pytest.mark.asyncio
@respx.mock
async def test_no_series_returns_none():
    payload = {
        "data": {
            "books": [
                {
                    "id": 3,
                    "title": "Standalone",
                    "description": "No series.",
                    "cached_tags": {},
                    "book_series": [],
                    "contributions": [{"author": {"name": "A"}}],
                }
            ]
        }
    }
    respx.post(HARDCOVER_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )
    results = await search_book("Standalone", "A", token="fake")
    assert results[0].series_name is None
    assert results[0].series_position is None
