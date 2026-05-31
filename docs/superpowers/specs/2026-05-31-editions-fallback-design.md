# Editions Cover Fallback — Design

**Date**: 2026-05-31
**Status**: Approved for implementation

## Problem

The ebook-enricher uses Hardcover's `search()` endpoint to find a book match and uses the search hit's `image` field as the cover source. The search returns one canonical edition per book — often a popular print edition with a low-resolution cover (300×500-ish from Goodreads/LibraryThing-era uploads).

For Endymion the canonical edition was 1463×2401 (high-res), but for **The Fall of Hyperion** the canonical was 325×500 — below `MIN_COVER_WIDTH=500` — so the cover swap was skipped. Manual investigation showed Hardcover *has* a 2470×4093 cover for the same book, just on a different edition (`ed_id=30444498`, the Spectra ebook). The search didn't surface it.

## Goal

When the canonical cover is missing or too small, query Hardcover's editions for the book, filter to plausibly-suitable cover sources, and pick the highest-resolution one. Fall through to no-cover if nothing survives the filter.

## Non-goals

- **Replace existing matches that already pass**: if the canonical is ≥ MIN_COVER_WIDTH and aspect-ratio sane, don't bother querying editions. Keep the cheap path cheap.
- **Match user's specific edition by ISBN**: pure cover-quality optimization, not edition-matching. The user accepts that the cover may be from a different edition than their EPUB.
- **Fetch from non-Hardcover sources** (OpenLibrary, Google Books): single-source fallback for now.
- **Cache edition lookups**: each call adds ~200ms but only fires when canonical is bad. YAGNI.

## Constraints

- **One extra GraphQL request per fallback**. Acceptable.
- **Filter must not be too aggressive**: 56 editions for Fall of Hyperion narrowed down to one suitable hit — losing that one would be bad.
- **Filter must not be too loose**: an audiobook square-art cover or a Polish-language edition's text-on-art cover would look wrong.

---

## Architecture

Single new function in `hardcover.py` + a tiny orchestration tweak in `enrich.py`. No new modules, no new dependencies.

```
ebook_enricher/
├── hardcover.py    (MOD)   — new fetch_editions() + filter/pick helper
├── enrich.py       (MOD)   — gate: if chosen cover too small, call fallback
└── cover.py        (no change)
```

---

## hardcover.py changes

### New dataclass

```python
@dataclass
class EditionCover:
    edition_id: int
    image_url: str
    image_width: int
    image_height: int
    edition_format: Optional[str]   # "ebook", "Mass Market Paperback", "Audiobook", etc.
    language_code: Optional[str]    # ISO-639 from Hardcover (e.g. "en", "fr"); None if unknown
    users_count: int                # popularity tiebreak
```

### New query (separate GraphQL operation)

```graphql
query EditionsForBook($book_id: Int!) {
  editions(where: {book_id: {_eq: $book_id}}, order_by: {users_count: desc}) {
    id
    edition_format
    image { url width height }
    language { code3 }   # or whatever Hardcover exposes; verify in implementation
    users_count
  }
}
```

### New function

```python
async def fetch_editions(book_id: int, token: str) -> list[EditionCover]:
    """Return all editions for a Hardcover book, parsed into EditionCovers.
    Skips editions with no image. Never raises — returns [] on any error."""
```

### Filter + pick

```python
# Aspect-ratio bounds — covers way outside these are likely audiobook squares
# (~1.0), cinema posters, or scanned thumbnails.
MIN_COVER_ASPECT = 0.55   # taller end of book covers
MAX_COVER_ASPECT = 0.85   # squatter end

# Format substrings (case-insensitive) we treat as audio — these always have
# unsuitable cover art for an ebook shelf.
_AUDIO_FORMAT_MARKERS = ("audio", "audible", "spoken")


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
```

---

## enrich.py orchestration tweak

Current flow (post-Task 7):

```python
chosen = pick_best_match(candidates)
...
if chosen.image_url and (chosen.image_width is None or chosen.image_width >= cover.MIN_COVER_WIDTH):
    existing_cover_path = cover.find_cover_path_in_opf(path)
    if existing_cover_path:
        cover_bytes = await cover.download_cover(chosen.image_url)
        ...
```

New flow inserts an editions-fallback when the gate would fail:

```python
chosen = pick_best_match(candidates)
...
# Determine which image URL to use for cover replacement
candidate_url, candidate_width = chosen.image_url, chosen.image_width
if not candidate_url or (candidate_width is not None and candidate_width < cover.MIN_COVER_WIDTH):
    # Top hit's cover is missing or too small — try editions
    editions = await hardcover.fetch_editions(int(chosen.id), token=token)
    best = hardcover.pick_best_edition_cover(
        editions,
        source_language=meta.language,
        min_width=cover.MIN_COVER_WIDTH,
    )
    if best:
        candidate_url, candidate_width = best.image_url, best.image_width
        logger.info(
            "editions fallback: using ed_id=%d (%dx%d) for book_id=%s",
            best.edition_id, best.image_width, best.image_height, chosen.id,
        )

if candidate_url:
    existing_cover_path = cover.find_cover_path_in_opf(path)
    if existing_cover_path:
        cover_bytes = await cover.download_cover(candidate_url)
        ...
```

`EpubMeta` already has a `language` field — we already use it for read but not write. Just pass it through.

---

## Error handling

| Failure | Behaviour |
|---|---|
| `fetch_editions` GraphQL error | Returns `[]`, fallback yields no candidate, no cover swap |
| Editions list empty | Same as above |
| All editions filtered out | Same as above |
| Best edition's URL fetched but returns 5xx | `cover.download_cover` returns None, no swap |
| Source EPUB has no `<dc:language>` | Skip language filter (all editions pass that check) |

In every failure case, metadata enrichment still completes normally.

---

## Testing

**Unit (`tests/test_hardcover.py`)** — extend:
- `test_pick_best_edition_cover_picks_largest` — straightforward area sort
- `test_pick_best_edition_cover_rejects_audiobook` — Audiobook format excluded
- `test_pick_best_edition_cover_rejects_square_aspect` — 1500×1500 cover rejected
- `test_pick_best_edition_cover_rejects_wrong_language` — fr cover with source=en
- `test_pick_best_edition_cover_allows_unknown_language` — language=None edition passes
- `test_pick_best_edition_cover_returns_none_when_all_below_min_width`
- `test_pick_best_edition_cover_empty_list_returns_none`

**Integration (`tests/test_enrich.py`)** — extend:
- `test_enrich_uses_editions_fallback_when_canonical_too_small` — mock search returns 300×500; mock fetch_editions returns one suitable 2000×3000 ebook; assert cover swap uses the editions URL
- `test_enrich_skips_fallback_when_canonical_is_large_enough` — mock search returns 1500×2400; assert fetch_editions is NOT called
- `test_enrich_fallback_returns_no_winner_skips_cover` — small canonical, all editions filtered out; metadata still writes, no cover swap

---

## Deployment

Standard rsync to plexypi + `docker compose build --no-cache && up -d`. No new dependencies.

After deploy, two retroactive validations:
1. Re-run Fall of Hyperion's cover swap through the normal `/enrich` flow (force-clear `calibre:series` first or use a one-shot equivalent) — should pick the Spectra ebook automatically.
2. Verify Endymion's swap still works (top hit was already good).

---

## Out of scope / open questions

- **Cache editions lookups** between sessions — not worth it; only ~10% of books need fallback.
- **OpenLibrary fallback** if Hardcover has no acceptable cover — future enhancement, more complex (separate API + auth model).
- **Force-cover-only re-pass** for already-enriched books — still deferred; this fallback only helps NEW enrichments + the Fall-of-Hyperion-style targeted manual cases.
