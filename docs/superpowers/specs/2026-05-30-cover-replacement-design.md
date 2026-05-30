# Cover Replacement — Design

**Date**: 2026-05-30
**Status**: Approved for implementation

## Problem

The ebook-enricher writes `calibre:series`, `dc:description`, and friends but explicitly never touches the embedded cover image. As a result, books with low-resolution publisher-embedded covers (Penguin Random House mass-market EPUBs are a common culprit — Hyperion, Endymion, etc.) keep those low-res covers even after enrichment. On a 1448-pixel-tall Kindle PaperWhite 3 screen, the difference between a 500×800 EPUB-embedded cover and a 1463×2401 publisher-quality cover is plainly visible.

The Hardcover API exposes high-resolution edition-specific cover images at `image.url`, with `image.width`/`image.height` reported alongside. For matched books (≥80% confidence — the same gate already used for metadata enrichment), we have everything we need to swap the cover in-place.

## Goal

When the enricher matches a book on Hardcover with ≥80% confidence, replace the EPUB's existing cover image with Hardcover's higher-resolution version. Preserve the displaced original as a sidecar file (`<book>.original.jpg`) for recovery. Keep cover replacement an additive bonus — never block metadata enrichment on a cover failure.

## Non-goals

- **Adding a cover where the EPUB has none.** Rare in practice (Penguin/HarperCollins/Random House all ship covers), and OPF mutation to register a new cover is its own can of worms. Defer until proven needed.
- **Replacing covers for books the enricher would otherwise skip** (e.g. already-enriched books with `calibre:series` set). Cover replacement piggybacks on the existing enrich flow — runs only when enrichment runs.
- **Editing OPF cover references.** The swap is bytes-only at the existing manifest-referenced path. OPF stays unchanged.
- **Image quality scoring.** No dimension comparison gate — if Hardcover has the cover, we replace. The user opted into "always replace" with sidecar recovery as the safety net.
- **Resizing or re-encoding.** We write the JPEG bytes Hardcover serves, as-is.

## Constraints

- **Single-pass zip rewrite.** The existing `write_meta()` does an atomic temp-file + zip rewrite. Cover replacement must happen in the same pass — not a separate open/rewrite (that doubles I/O and risks split atomicity).
- **Sidecar idempotency.** Re-running enrichment must NEVER overwrite the sidecar with a previously-applied Hardcover cover. The sidecar must always hold the *true* original.
- **Network failures must not block metadata enrichment.** A cover download timeout returns the enriched metadata anyway. Cover replacement is best-effort.
- **httpx is already a dependency.** No new packages.

---

## Architecture

One new module, two modified existing modules:

```
ebook_enricher/
├── cover.py          (NEW)   — pure cover ops: parse-OPF-for-cover-path, sidecar write, image download
├── enrich.py         (MOD)   — after metadata write, attempt cover replacement
├── epub_meta.py      (MOD)   — write_meta() gains optional cover_override parameter
├── hardcover.py      (MOD)   — parse image.url/width/height; add fields to HardcoverBook
└── (other files unchanged)
```

`cover.py` knows nothing about Hardcover or enrichment — just file ops. `enrich.py` is where the policy lives (when to call, when to skip).

---

## Data flow

```
enrich_file(path, token):
    meta = read_meta(path)
    if meta.series: return skipped("already_enriched")

    candidates = await search_book(meta.title, meta.author, token)
    chosen = pick_best_match(candidates)   # existing scoring
    if not chosen or below_confidence: return low_confidence/no_match

    # NEW: prepare cover override if available
    cover_override = None
    if chosen.image_url:
        cover_bytes = await download_cover(chosen.image_url)   # may return None on failure
        if cover_bytes:
            existing_cover_path = find_cover_path_in_opf(path)   # returns None if absent
            if existing_cover_path:
                saved = save_sidecar_if_absent(path)   # False if write failed
                if saved:
                    cover_override = (existing_cover_path, cover_bytes)
                # else: skip cover swap to avoid losing the only original

    # Existing metadata write — now also takes optional cover override
    write_meta(path, updates, cover_override=cover_override)
    return enriched(...)
```

The single `write_meta()` call still does one atomic temp-file write, one rename. Cover bytes flow through it alongside the OPF update.

---

## `cover.py` — public interface

```python
def find_cover_path_in_opf(epub_path: Path) -> Optional[str]:
    """Open the EPUB, locate <meta name="cover" content="<id>"/> in OPF,
    resolve to manifest item href. Returns the path-within-zip (e.g.
    'OEBPS/images/cvi.jpg') or None if no cover meta is declared OR the
    declared manifest item isn't present in the zip."""


def save_sidecar_if_absent(epub_path: Path) -> bool:
    """If <epub>.original.jpg does not exist next to the EPUB, extract
    the EPUB's current cover image and write it as the sidecar. The
    sidecar path is `epub_path.parent / (epub_path.stem + ".original.jpg")`
    (so `Endymion.epub` → `Endymion.original.jpg`).

    Idempotent. Returns True if a usable sidecar exists at end of call
    (either pre-existing or just-written). Returns False if we couldn't
    save (no cover in EPUB, disk full, perm error) — caller should skip
    cover swap in that case to avoid losing the only original."""


async def download_cover(url: str, *, timeout_s: int = 10) -> Optional[bytes]:
    """GET the image. Returns bytes on 200, None on any failure (network,
    timeout, non-200, suspiciously-small payload). Logs the reason on
    failure. Never raises."""


# Sanity threshold for "suspiciously small" — below this we reject
MIN_COVER_SIZE_BYTES = 50_000   # 50KB
MIN_COVER_WIDTH = 500           # pixels (we trust Hardcover's reported width)
```

---

## `epub_meta.write_meta` modification

Current signature is roughly:

```python
def write_meta(path: Path, updates: dict) -> None: ...
```

New signature:

```python
def write_meta(
    path: Path,
    updates: dict,
    cover_override: Optional[tuple[str, bytes]] = None,
) -> None:
    """If cover_override is (zip_path, bytes), replace the bytes at
    that zip member during the temp-file rewrite. Everything else
    (OPF mutation, atomic rename, perms preservation, mtime — separate
    bug, not in scope) is unchanged."""
```

Inside the existing zip rewrite loop:

```python
for item in src.infolist():
    if item.filename == opf_path:
        dst.writestr(item, new_opf_bytes)
    elif cover_override and item.filename == cover_override[0]:
        dst.writestr(item, cover_override[1])    # ← NEW
    elif item.filename == "mimetype":
        ...
    else:
        dst.writestr(item, src.read(item.filename))
```

That's the entire integration. No other change to the rewrite logic, atomic-rename pattern, or perms handling.

---

## `hardcover.py` modification

Extend `HardcoverBook` dataclass:

```python
@dataclass
class HardcoverBook:
    # existing fields unchanged ...
    image_url: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
```

In `_parse_hit()`:

```python
image = hit.get("image") or {}
return HardcoverBook(
    # existing fields ...
    image_url=image.get("url"),
    image_width=image.get("width"),
    image_height=image.get("height"),
)
```

---

## Error handling

| Failure | Behaviour |
|---|---|
| Hardcover hit has no `image.url` | Skip cover step, log "no Hardcover cover for match"; metadata write proceeds |
| Cover download fails (timeout, 4xx/5xx, connection) | Log error, skip cover swap; metadata write proceeds |
| Downloaded image < 50 KB OR Hardcover-reported width < 500 px | Skip — likely a placeholder, not a real cover |
| EPUB has no `<meta name="cover">` in OPF | Log "no existing cover", skip cover swap; metadata write proceeds |
| `<meta name="cover" content="X"/>` exists but manifest item X not in zip | Log "broken cover reference", skip cover swap |
| Sidecar write fails (disk full, perms) | Skip the cover swap entirely (don't risk replacing without recovery option) |
| Sidecar already exists | Don't overwrite; the existing sidecar is the true original — by design |

In every failure case, **metadata enrichment still happens**. Cover replacement is additive.

---

## Testing

**Unit (`tests/test_cover.py`)** — new file:

- `test_find_cover_path_finds_standard_meta` — EPUB with `<meta name="cover" content="cvi"/>` → returns the manifest href
- `test_find_cover_path_returns_none_when_no_meta` — EPUB without cover meta → None
- `test_find_cover_path_returns_none_when_manifest_broken` — EPUB with cover meta pointing at missing id → None
- `test_save_sidecar_writes_once` — call twice, second call is no-op
- `test_save_sidecar_preserves_true_original` — sidecar has original bytes, not the replacement
- `test_download_cover_returns_bytes_on_200` — mock httpx, assert bytes
- `test_download_cover_returns_none_on_5xx` — mock 503, no raise
- `test_download_cover_rejects_tiny_payload` — mock 200 with 10KB body → None

**Integration (`tests/test_enrich.py` extension)**:

- `test_enrich_replaces_cover_when_hardcover_has_image` — mock Hardcover with `image_url`, mock the download, run `enrich_file()`. Assert: EPUB's cover bytes match download; sidecar exists with original bytes.
- `test_enrich_skips_cover_when_hardcover_no_image` — mock Hardcover hit without `image`. Assert: metadata still written, no sidecar created.
- `test_enrich_skips_cover_when_download_fails` — mock download as 503. Assert: metadata still written, no sidecar, log records the skip.
- `test_enrich_skips_cover_when_epub_lacks_cover_meta` — fixture EPUB without `<meta name="cover">`. Assert: metadata written, no sidecar.

**Manual smoke test** before deploying:

- On plexypi: hit `/enrich` with a known-low-res-cover book (Endymion). Verify `<book>.original.jpg` sidecar appears with the old bytes. Open the EPUB on the laptop (e.g. with `unzip -p`) and confirm new cover bytes match Hardcover's URL.

Existing 65 tests all continue to pass.

---

## Deployment

- Standard ebook-enricher deploy (docker-compose pull + restart on plexypi).
- Add `(?d)*.original.jpg` to PW5 and PW3 Kindle `.stignore` so the sidecar JPGs don't sync to Kindles (server-side artifacts only). Two `curl POST /rest/db/ignores` calls.
- One-shot retroactive re-enrichment via `/backfill` — books that previously skipped (because `calibre:series` is set) will STILL skip. To force cover replacement on already-enriched books, a `/backfill?force_covers=true` flag would be needed. Out of scope for v1.

---

## Open questions / out of scope

- **Force cover-only re-pass** for already-enriched books — not in v1. If wanted later, add `force_covers` flag to `/backfill`.
- **mtime preservation on enricher rewrite** — separate known issue (cosmetic), not in scope here.
- **Cover replacement metrics** in `/backfill` summary (count of replaced/skipped/failed covers) — nice to have, not blocking.
