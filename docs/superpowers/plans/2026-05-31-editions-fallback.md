# Editions Cover Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** When Hardcover's canonical search hit has no image or width < `MIN_COVER_WIDTH`, query the book's editions, filter to plausibly-suitable covers (skip audio formats, off-language editions, weird aspect ratios), and pick the highest-resolution one.

**Architecture:** Single new function + dataclass in `hardcover.py`, single new branch in `enrich.py`'s cover orchestration. Verified API field shapes: `editions.image.{url,width,height}`, `editions.language.code2` (ISO-639-1 — matches EPUB `dc:language`), `editions.edition_format` (string like "ebook", "Audiobook").

**Tech Stack:** Same as existing enricher — httpx + GraphQL, pytest + respx for tests.

---

## File Structure

- Modify: `ebook_enricher/hardcover.py` — add `EditionCover` dataclass, `EDITIONS_QUERY`, `fetch_editions()`, `pick_best_edition_cover()`, supporting constants
- Modify: `ebook_enricher/enrich.py` — branch in cover orchestration that calls the fallback when canonical is missing or too small
- Modify: `tests/test_hardcover.py` — unit tests for `pick_best_edition_cover` + parsing
- Modify: `tests/test_enrich.py` — integration tests for the fallback orchestration

---

### Task 1: Branch + EditionCover dataclass

**Files:**
- Modify: `ebook_enricher/hardcover.py` (add dataclass)

- [ ] **Step 1: Create feature branch**

```bash
cd ~/projects/ebook-enricher
git checkout main
git pull
git checkout -b feature/editions-fallback
```

- [ ] **Step 2: Add EditionCover dataclass to `hardcover.py`**

After the existing `HardcoverBook` dataclass in `ebook_enricher/hardcover.py`, add:

```python
@dataclass
class EditionCover:
    """One Hardcover edition's cover info — used by the editions-fallback
    when the canonical search hit's cover is missing or too small."""
    edition_id: int
    image_url: str
    image_width: int
    image_height: int
    edition_format: Optional[str]   # "ebook", "Mass Market Paperback", "Audiobook", etc.
    language_code: Optional[str]    # ISO-639-1 (e.g. "en", "fr"); None if unknown
    users_count: int                # popularity tiebreak
```

- [ ] **Step 3: Commit**

```bash
git add ebook_enricher/hardcover.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: EditionCover dataclass"
```

---

### Task 2: pick_best_edition_cover function (TDD)

**Files:**
- Modify: `ebook_enricher/hardcover.py`
- Modify: `tests/test_hardcover.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_hardcover.py`:

```python
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
```

- [ ] **Step 2: Run tests — should fail**

```bash
source .venv/bin/activate
pytest tests/test_hardcover.py -v -k pick_best_edition_cover
```

Expected: 10 tests FAIL with `ImportError: cannot import name 'pick_best_edition_cover'`.

- [ ] **Step 3: Implement `pick_best_edition_cover` + constants**

Append to `ebook_enricher/hardcover.py`:

```python
# Aspect-ratio bounds — covers way outside these are likely audiobook
# squares (~1.0), cinema posters, or scanned thumbnails.
MIN_COVER_ASPECT = 0.55   # taller end of book covers
MAX_COVER_ASPECT = 0.85   # squatter end

# Format substrings (case-insensitive) we treat as audio — always unsuitable.
_AUDIO_FORMAT_MARKERS = ("audio", "audible", "spoken")


def _is_audio_format(fmt: Optional[str]) -> bool:
    if not fmt:
        return False
    f = fmt.lower()
    return any(m in f for m in _AUDIO_FORMAT_MARKERS)


def _aspect_ok(w: int, h: int) -> bool:
    if h <= 0:
        return False
    a = w / h
    return MIN_COVER_ASPECT <= a <= MAX_COVER_ASPECT


def pick_best_edition_cover(
    editions: list[EditionCover],
    *,
    source_language: Optional[str] = None,
    min_width: int = 500,
) -> Optional[EditionCover]:
    """Apply the filter chain + pick winner. None if no edition qualifies.

    Filters (in order):
      1. image_width >= min_width
      2. aspect ratio (w/h) within [MIN_COVER_ASPECT, MAX_COVER_ASPECT]
      3. edition_format not containing an audio marker (case-insensitive)
      4. language matches source (if source_language given AND edition.language_code set)
         — editions with language_code=None pass through (no filter)

    Tiebreak: largest pixel area first, then highest users_count.
    """
    def survives(e: EditionCover) -> bool:
        if e.image_width < min_width:
            return False
        if not _aspect_ok(e.image_width, e.image_height):
            return False
        if _is_audio_format(e.edition_format):
            return False
        if source_language and e.language_code and e.language_code != source_language:
            return False
        return True

    survivors = [e for e in editions if survives(e)]
    if not survivors:
        return None
    survivors.sort(
        key=lambda e: (e.image_width * e.image_height, e.users_count),
        reverse=True,
    )
    return survivors[0]
```

- [ ] **Step 4: Run tests — should pass**

```bash
pytest tests/test_hardcover.py -v -k pick_best_edition_cover
```

Expected: 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/hardcover.py tests/test_hardcover.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: pick_best_edition_cover with aspect + audio + language filters"
```

---

### Task 3: fetch_editions GraphQL query

**Files:**
- Modify: `ebook_enricher/hardcover.py`
- Modify: `tests/test_hardcover.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_hardcover.py`:

```python
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
```

- [ ] **Step 2: Run tests — should fail with ImportError**

```bash
pytest tests/test_hardcover.py -v -k fetch_editions
```

- [ ] **Step 3: Implement `fetch_editions`**

Append to `ebook_enricher/hardcover.py`:

```python
EDITIONS_QUERY = """
query EditionsForBook($book_id: Int!) {
  editions(where: {book_id: {_eq: $book_id}}, order_by: {users_count: desc}) {
    id
    edition_format
    image { url width height }
    language { code2 }
    users_count
  }
}
"""


def _parse_edition(raw: dict) -> Optional[EditionCover]:
    """Parse one editions hit. Returns None if no usable image."""
    image = raw.get("image") or {}
    url = image.get("url")
    w = image.get("width")
    h = image.get("height")
    if not url or not w or not h:
        return None
    language = raw.get("language") or {}
    return EditionCover(
        edition_id=raw["id"],
        image_url=url,
        image_width=int(w),
        image_height=int(h),
        edition_format=raw.get("edition_format"),
        language_code=language.get("code2"),
        users_count=int(raw.get("users_count") or 0),
    )


async def fetch_editions(book_id: int, token: str) -> list[EditionCover]:
    """Return all editions for a Hardcover book, parsed into EditionCovers.
    Skips editions with no usable image. Never raises — returns [] on any
    network or parse error.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(
                HARDCOVER_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "query": EDITIONS_QUERY,
                    "variables": {"book_id": int(book_id)},
                },
            )
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("fetch_editions network error for book_id=%s: %s", book_id, e)
        return []

    if resp.status_code != 200:
        logger.warning(
            "fetch_editions HTTP %d for book_id=%s", resp.status_code, book_id
        )
        return []

    try:
        data = resp.json()
    except ValueError:
        return []
    if data.get("errors"):
        logger.warning("fetch_editions GraphQL errors for book_id=%s: %s",
                       book_id, data["errors"])
        return []

    raw_eds = (data.get("data") or {}).get("editions") or []
    out = []
    for raw in raw_eds:
        ec = _parse_edition(raw)
        if ec:
            out.append(ec)
    return out
```

(Verify the existing module already imports `httpx`, `logger`, defines `HARDCOVER_URL` and `TIMEOUT_S`. If not, use whatever names exist. The function should follow the patterns of `search_book` which already exists in the file.)

- [ ] **Step 4: Run tests — should pass**

```bash
pytest tests/test_hardcover.py -v
```

Expected: All hardcover tests pass.

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/hardcover.py tests/test_hardcover.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: fetch_editions GraphQL query for cover fallback"
```

---

### Task 4: Wire fallback into enrich orchestration

**Files:**
- Modify: `ebook_enricher/enrich.py`
- Modify: `tests/test_enrich.py`

- [ ] **Step 1: Write failing integration tests**

Append to `tests/test_enrich.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_enrich_uses_editions_fallback_when_canonical_too_small(epub_with_cover):
    """Top search hit has width<500. Editions fallback finds a 2000x3000
    ebook. Cover gets swapped from the editions URL."""
    from ebook_enricher.enrich import enrich_file
    from io import BytesIO
    from PIL import Image
    import zipfile

    # The Hardcover search returns a small canonical cover
    small_url = "https://assets.hardcover.app/small.jpg"
    big_url = "https://assets.hardcover.app/big.jpg"
    big_img = Image.new("RGB", (2000, 3000), (10, 200, 30))
    big_bytes_buf = BytesIO()
    big_img.save(big_bytes_buf, format="JPEG", quality=90)
    big_bytes = big_bytes_buf.getvalue()

    # Search response with too-small canonical
    respx.post("https://api.hardcover.app/v1/graphql").mock(side_effect=[
        # First call: search()
        httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 123,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "desc",
                    "featured_series": {"series": {"name": "TS"}, "position": 1},
                    "image": {"url": small_url, "width": 325, "height": 500},
                }
            }]}}}
        }),
        # Second call: editions()
        httpx.Response(200, json={
            "data": {"editions": [
                {
                    "id": 9999,
                    "edition_format": "ebook",
                    "image": {"url": big_url, "width": 2000, "height": 3000},
                    "language": {"code2": "en"},
                    "users_count": 50,
                },
            ]}
        }),
    ])
    # Cover download for the big edition's URL
    respx.get(big_url).mock(return_value=httpx.Response(200, content=big_bytes))

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    # The cover swap used the editions URL (the big one), not the search URL
    with zipfile.ZipFile(epub_with_cover) as zf:
        published = zf.read("OEBPS/images/cover.jpg")
    # After resize the longest edge should be MAX_COVER_LONG_EDGE
    img = Image.open(BytesIO(published))
    from ebook_enricher.cover import MAX_COVER_LONG_EDGE
    assert max(img.size) == MAX_COVER_LONG_EDGE


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_fallback_when_canonical_is_large_enough(epub_with_cover):
    """Canonical width >= MIN_COVER_WIDTH → editions endpoint NOT called.
    Cover swap uses the canonical image URL."""
    from ebook_enricher.enrich import enrich_file
    from io import BytesIO
    from PIL import Image
    import zipfile

    good_url = "https://assets.hardcover.app/good.jpg"
    big_img = Image.new("RGB", (1500, 2400), (200, 50, 50))
    big_buf = BytesIO()
    big_img.save(big_buf, format="JPEG", quality=90)
    big_bytes = big_buf.getvalue()

    # Only the search call is mocked. If editions is called, respx raises.
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 456,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "desc",
                    "featured_series": {"series": {"name": "TS"}, "position": 1},
                    "image": {"url": good_url, "width": 1500, "height": 2400},
                }
            }]}}}
        }),
    )
    respx.get(good_url).mock(return_value=httpx.Response(200, content=big_bytes))

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        published = zf.read("OEBPS/images/cover.jpg")
    assert len(published) > 0


@pytest.mark.asyncio
@respx.mock
async def test_enrich_fallback_returns_no_winner_skips_cover(epub_with_cover):
    """Canonical too small, editions all rejected → metadata writes, no cover swap."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    respx.post("https://api.hardcover.app/v1/graphql").mock(side_effect=[
        # search()
        httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 789,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "desc",
                    "featured_series": {"series": {"name": "TS"}, "position": 1},
                    "image": {"url": "https://small.jpg", "width": 300, "height": 450},
                }
            }]}}}
        }),
        # editions() — all too small
        httpx.Response(200, json={
            "data": {"editions": [
                {
                    "id": 1,
                    "edition_format": "Mass Market Paperback",
                    "image": {"url": "https://tiny.jpg", "width": 300, "height": 450},
                    "language": {"code2": "en"},
                    "users_count": 5,
                },
            ]}
        }),
    ])
    # No cover URL is mocked — if download is attempted, respx raises.

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        published = zf.read("OEBPS/images/cover.jpg")
    assert published == COVER_BYTES_ORIGINAL  # unchanged
```

- [ ] **Step 2: Run tests — should fail**

```bash
pytest tests/test_enrich.py -v -k fallback
```

Expected: 3 tests fail because the fallback isn't wired yet.

- [ ] **Step 3: Wire fallback into `enrich.py`**

In `ebook_enricher/enrich.py`, find the cover orchestration block (currently starts with `if chosen.image_url and ...`). Replace the URL determination with:

```python
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
            source_language=meta.language,
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
```

Note: this replaces both the old `if chosen.image_url and ...` block AND the inner orchestration that follows it. Read the existing function first to find the exact replacement range, then substitute.

Also make sure `from ebook_enricher import hardcover` is imported at the top of `enrich.py` (it already is, used for `search_book`).

- [ ] **Step 4: Run tests — should pass**

```bash
pytest tests/test_enrich.py -v
```

Expected: All enrich tests pass.

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/enrich.py tests/test_enrich.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: editions-fallback in enrich orchestration"
```

---

### Task 5: Full test suite green + merge + deploy

**Files:** none for git, deploy is runtime

- [ ] **Step 1: Run full test suite**

```bash
pytest -q
```

Expected: 130+ tests pass (122 prior + 10 hardcover unit + 3 enrich integration ≈ 135).

- [ ] **Step 2: Merge + push**

```bash
git checkout main
git merge --ff-only feature/editions-fallback
git push origin main
git branch -d feature/editions-fallback
```

- [ ] **Step 3: Deploy to plexypi**

```bash
rsync -a --delete ebook_enricher/ plexypi:/tmp/eb-eb/
ssh plexypi 'sudo rsync -a --delete /tmp/eb-eb/ /opt/stacks/ebook-enricher/ebook_enricher/ && sudo rm -rf /tmp/eb-eb && cd /opt/stacks/ebook-enricher && sudo docker compose build --no-cache && sudo docker compose up -d'
```

Verify:
```bash
ssh plexypi 'docker exec qbittorrent wget -qO- http://ebook-enricher:8000/health'
```

- [ ] **Step 4: Retroactive validation — run cover swap on a fresh test book through normal flow**

Since both Endymion and Fall of Hyperion have already been swapped, real validation needs a NEW book. Pick any book whose canonical Hardcover hit has a tiny cover. If you don't have one handy, the test we already did manually on Fall of Hyperion proves the URL works.

The integration test in this plan (which mocks both search + editions calls with the expected JSON shapes) is the actual regression test.

---

## Self-Review

**Spec coverage:**
- ✓ EditionCover dataclass — Task 1
- ✓ fetch_editions GraphQL query — Task 3
- ✓ pick_best_edition_cover with all filter rules — Task 2
- ✓ Aspect ratio bounds — Task 2 (`test_pick_best_edition_cover_aspect_bounds_inclusive`)
- ✓ Audio format rejection — Task 2 (test_rejects_audiobook + test_rejects_audible_format)
- ✓ Language filter (with unknown-language passthrough) — Task 2 (3 related tests)
- ✓ Tiebreak: area then users_count — Task 2 (test_picks_largest)
- ✓ Enrich orchestration gate — Task 4
- ✓ "Skip fallback when canonical good" → Task 4 integration test
- ✓ "Fallback finds winner" → Task 4 integration test
- ✓ "Fallback no winner → no cover swap" → Task 4 integration test

**Type consistency:**
- `EditionCover.edition_id: int` — consistent in fetch_editions parser and test helper
- `EditionCover.language_code: Optional[str]` — consistent
- `pick_best_edition_cover(...) -> Optional[EditionCover]` — consistent

**Placeholder scan:** clean.
