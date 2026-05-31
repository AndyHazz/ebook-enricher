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


def _ec(*, w, h, fmt="ebook", lang="en", users=10, ed_id=None, url=None):
    """Test helper to build EditionCover."""
    from ebook_enricher.hardcover import EditionCover
    return EditionCover(
        edition_id=ed_id or (w * 1000 + h),
        image_url=url or f"https://example/{w}x{h}.jpg",
        image_width=w,
        image_height=h,
        edition_format=fmt,
        language_code=lang,
        users_count=users,
    )


def test_pick_best_edition_cover_picks_largest():
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=600, h=900),
        _ec(w=2000, h=3000),  # largest area
        _ec(w=1000, h=1500),
    ]
    winner = pick_best_edition_cover(eds, source_language="en")
    assert winner is not None
    assert winner.image_width == 2000


def test_pick_best_edition_cover_rejects_audiobook():
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=3000, h=3000, fmt="Audiobook"),  # square audio — rejected
        _ec(w=1000, h=1500, fmt="ebook"),
    ]
    winner = pick_best_edition_cover(eds, source_language="en")
    assert winner is not None
    assert winner.image_width == 1000
    assert winner.edition_format == "ebook"


def test_pick_best_edition_cover_rejects_audible_format():
    """edition_format containing 'audible' or 'audio' rejected (case-insensitive)."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=2000, h=3000, fmt="Audible Studios"),
        _ec(w=600, h=900, fmt="Paperback"),
    ]
    winner = pick_best_edition_cover(eds, source_language="en")
    assert winner is not None
    assert winner.image_width == 600


def test_pick_best_edition_cover_rejects_square_aspect():
    """1500x1500 (1.0 aspect) is outside [0.55, 0.85] → rejected."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=1500, h=1500, fmt="ebook"),   # square (audio art usually)
        _ec(w=800, h=1200, fmt="ebook"),
    ]
    winner = pick_best_edition_cover(eds, source_language="en")
    assert winner is not None
    assert winner.image_width == 800


def test_pick_best_edition_cover_rejects_wrong_language():
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=3000, h=4500, lang="fr"),    # high-res but French
        _ec(w=800, h=1200, lang="en"),
    ]
    winner = pick_best_edition_cover(eds, source_language="en")
    assert winner is not None
    assert winner.image_width == 800


def test_pick_best_edition_cover_allows_unknown_language():
    """Edition with language_code=None passes the language filter."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=3000, h=4500, lang=None),   # unknown language — should pass
    ]
    winner = pick_best_edition_cover(eds, source_language="en")
    assert winner is not None
    assert winner.image_width == 3000


def test_pick_best_edition_cover_skips_language_filter_when_source_unknown():
    """If source_language is None (EPUB had no dc:language), don't filter by language."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=3000, h=4500, lang="fr"),
    ]
    winner = pick_best_edition_cover(eds, source_language=None)
    assert winner is not None


def test_pick_best_edition_cover_returns_none_when_all_below_min_width():
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [
        _ec(w=400, h=600),
        _ec(w=300, h=450),
    ]
    winner = pick_best_edition_cover(eds, source_language="en", min_width=500)
    assert winner is None


def test_pick_best_edition_cover_empty_list_returns_none():
    from ebook_enricher.hardcover import pick_best_edition_cover
    winner = pick_best_edition_cover([], source_language="en")
    assert winner is None


def test_pick_best_edition_cover_aspect_bounds_inclusive():
    """An aspect at exactly 0.55 or 0.85 must pass (inclusive bounds)."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    # 0.55 ratio: 550x1000
    eds = [_ec(w=550, h=1000)]
    assert pick_best_edition_cover(eds, source_language="en") is not None
    # 0.85 ratio: 850x1000
    eds = [_ec(w=850, h=1000)]
    assert pick_best_edition_cover(eds, source_language="en") is not None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_editions_parses_response():
    from ebook_enricher.hardcover import fetch_editions
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"editions": [
                {
                    "id": 30444498,
                    "edition_format": "ebook",
                    "image": {"url": "https://x/a.jpg", "width": 2470, "height": 4093},
                    "language": {"code2": "en"},
                    "users_count": 29,
                },
                {
                    "id": 30556303,
                    "edition_format": None,
                    "image": {"url": "https://x/b.jpg", "width": 325, "height": 500},
                    "language": {"code2": "en"},
                    "users_count": 9,
                },
                # Edition with no image — should be skipped
                {
                    "id": 99999,
                    "edition_format": "Hardcover",
                    "image": None,
                    "language": {"code2": "en"},
                    "users_count": 1,
                },
            ]}
        }),
    )
    result = await fetch_editions(369986, token="fake-token")
    assert len(result) == 2  # edition with no image is skipped
    assert result[0].edition_id == 30444498
    assert result[0].image_width == 2470
    assert result[0].edition_format == "ebook"
    assert result[0].language_code == "en"
    assert result[0].users_count == 29


@pytest.mark.asyncio
@respx.mock
async def test_fetch_editions_returns_empty_on_error():
    from ebook_enricher.hardcover import fetch_editions
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(500)
    )
    result = await fetch_editions(369986, token="fake-token")
    assert result == []


def test_pick_best_edition_cover_matches_primary_subtag_en_us_vs_en():
    """EPUB declares en-US; Hardcover edition has en. Should MATCH."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [_ec(w=2000, h=3000, lang="en")]
    winner = pick_best_edition_cover(eds, source_language="en-US")
    assert winner is not None


def test_pick_best_edition_cover_matches_primary_subtag_en_vs_en_gb():
    """Source en; Hardcover edition en-GB. Should MATCH."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [_ec(w=2000, h=3000, lang="en-GB")]
    winner = pick_best_edition_cover(eds, source_language="en")
    assert winner is not None


def test_pick_best_edition_cover_primary_subtag_mismatch_still_rejected():
    """Source en-US; edition fr-CA. Should NOT match (primary 'en' vs 'fr')."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [_ec(w=2000, h=3000, lang="fr-CA")]
    winner = pick_best_edition_cover(eds, source_language="en-US")
    assert winner is None


def test_pick_best_edition_cover_language_case_insensitive():
    """Case differences shouldn't block a match (defensive)."""
    from ebook_enricher.hardcover import pick_best_edition_cover
    eds = [_ec(w=2000, h=3000, lang="EN")]
    winner = pick_best_edition_cover(eds, source_language="en-us")
    assert winner is not None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_editions_handles_missing_language_block():
    """Some editions have language=None — language_code should be None on the EditionCover."""
    from ebook_enricher.hardcover import fetch_editions
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"editions": [
                {
                    "id": 1,
                    "edition_format": "ebook",
                    "image": {"url": "https://x.jpg", "width": 1000, "height": 1500},
                    "language": None,
                    "users_count": 5,
                },
            ]}
        }),
    )
    result = await fetch_editions(1, token="t")
    assert len(result) == 1
    assert result[0].language_code is None
