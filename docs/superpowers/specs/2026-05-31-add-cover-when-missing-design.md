# Add Cover When Missing — Design

**Date**: 2026-05-31
**Status**: Approved for implementation

## Problem

The cover-replacement flow (2026-05-30-cover-replacement-design.md) explicitly defers the "EPUB has no embedded cover" case — it only swaps bytes at an *existing* OPF manifest entry. In practice some EPUBs have no cover at all:

- Older FB2-converted text releases (Calibre/`ebook-convert` output without `--cover`)
- Hand-rolled EPUBs from public domain text projects
- Some early indie/SF retail EPUBs (Baen pre-2014 era, etc.)

For these, the enricher finds a Hardcover match, has good cover bytes, and currently throws them away because the OPF lacks `<meta name="cover">`. Confirmed manually on *Shards of Honour* (Bujold): a one-shot script that adds the manifest item + meta tag + image bytes works cleanly. Promote that path into the real enricher.

## Goal

When the enricher matches a book on Hardcover with ≥80% confidence AND the EPUB has no cover declared in OPF, **add** the Hardcover cover: insert the JPEG bytes at a sensible zip path, append a `<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg"/>` manifest entry, and append `<meta name="cover" content="cover-image"/>` inside `<metadata>`.

## Non-goals

- **Generating a cover-page XHTML and inserting it into the spine.** Bookshelf and Kindle shelf-thumbnail rendering only need the `<meta name="cover">` reference. Adding a first-page cover XHTML works for some readers and risks breaking spine ordering for others — defer until proven necessary.
- **Sidecar safety net.** The "original" cover was *nothing*; preserving "nothing" as a sidecar is meaningless. The add path skips sidecar creation entirely. If user runs the enricher again later, the OPF now has a manifest entry, so the REPLACE path runs (with its normal sidecar logic).
- **Reverting an `add` via the same code path.** Removing an added cover is the inverse OPF mutation; out of scope. Users who want to revert can extract the image, delete the manifest item + meta tag manually, or restore the EPUB from torrent seed copy.
- **OPF 3 properties.** OPF 3 covers use `properties="cover-image"` on the manifest item rather than `<meta name="cover">`. The enricher currently writes OPF-2-style `<meta name="cover">` (see `epub_meta.write_meta` cover-override path). Continue with OPF-2-style for the ADD path — modern readers (KOReader, Kindle, Calibre) honour both forms.

## Constraints

- **Single-pass zip rewrite.** Same atomic temp-file + rename as `write_meta`, with the new image bytes added during the same loop. No second open.
- **OPF mutation must preserve existing namespace prefixes and structure.** `ET.tostring` with the existing registered namespaces produces the same shape we already write for OPF updates.
- **Idempotent.** Re-running the enricher on an EPUB whose ADD already succeeded must take the REPLACE path on the second run. The marker is the presence of `<meta name="cover">` after the ADD writes it, which `find_cover_path_in_opf` will then resolve.

## Architecture

Three files modified:

```
ebook_enricher/
├── cover.py        (MOD)  — new add_cover_to_opf() helper (pure OPF mutation, no I/O)
├── epub_meta.py    (MOD)  — write_meta() gains cover_add parameter, mirrors cover_override
└── enrich.py       (MOD)  — branch on existing_cover_path: REPLACE vs ADD
```

No new modules. No new dependencies.

## `cover.py` additions

```python
# Default zip-relative path for newly-added covers. Resolved against the
# OPF's directory so the OPF href works regardless of where OPF lives.
DEFAULT_COVER_HREF = "images/cover.jpg"
DEFAULT_COVER_MANIFEST_ID = "cover-image"


def add_cover_to_opf(
    opf_root: ET.Element,
    opf_path: str,
    cover_bytes: bytes,
) -> tuple[str, str]:
    """Mutate the OPF tree in-place to register a new cover, returning
    (cover_zip_path, cover_href).

    Adds:
      * <item id="cover-image" href="images/cover.jpg" media-type="image/jpeg"/>
        inside the existing <manifest>
      * <meta name="cover" content="cover-image"/>
        inside the existing <metadata>

    The returned cover_zip_path is the absolute path within the zip
    (relative to the zip root) — e.g. "OEBPS/images/cover.jpg" when the
    OPF lives at "OEBPS/Content.opf". The caller writes cover_bytes
    there during the zip rewrite.

    The returned cover_href is the path-relative-to-OPF form used in
    the manifest item — e.g. "images/cover.jpg".

    Raises ValueError if the OPF lacks <metadata> or <manifest>.
    Raises ValueError if a manifest item with id=cover-image already
    exists (caller should have taken the REPLACE path instead).
    """
```

## `epub_meta.write_meta` modification

Current signature:

```python
def write_meta(
    path: Path,
    meta: EpubMeta,
    cover_override: Optional[tuple[str, bytes]] = None,
) -> None: ...
```

New signature:

```python
def write_meta(
    path: Path,
    meta: EpubMeta,
    cover_override: Optional[tuple[str, bytes]] = None,
    cover_add: Optional[tuple[str, bytes]] = None,
) -> None: ...
```

Mutually exclusive — passing both raises ValueError. The two paths are functionally similar but semantically different:

- `cover_override=(zip_path, bytes)`: replace the bytes at zip_path during the rewrite. Assumes the OPF already has a manifest entry pointing at zip_path (caller resolved it via `find_cover_path_in_opf`).
- `cover_add=(zip_path, bytes)`: write the bytes at zip_path (new entry in the zip) AND the OPF has already been mutated to include the corresponding manifest item + cover meta. Caller is responsible for the OPF mutation via `cover.add_cover_to_opf` BEFORE the call.

This split keeps `write_meta` free of OPF-shape concerns — it just does the zip rewrite. The OPF tree it serializes is whatever the caller hands it.

Inside the rewrite loop:

```python
for item in src.infolist():
    if item.filename == opf_path:
        dst.writestr(item, new_opf_bytes)
    elif cover_override and item.filename == cover_override[0]:
        dst.writestr(item, cover_override[1])
    elif item.filename == "mimetype":
        ...
    else:
        dst.writestr(item, src.read(item.filename))

# NEW: after the loop, add the new cover file if cover_add given.
if cover_add is not None:
    dst.writestr(cover_add[0], cover_add[1])
```

## `enrich.py` orchestration

Current flow (post-edition-fallback, simplified):

```python
if candidate_url and (candidate_width is None or candidate_width >= cover.MIN_COVER_WIDTH):
    existing_cover_path = cover.find_cover_path_in_opf(path)
    if existing_cover_path:
        cover_bytes = await cover.download_cover(candidate_url)
        if cover_bytes:
            cover_bytes = cover.resize_cover_if_needed(cover_bytes)
            if cover.save_sidecar_if_absent(path):
                cover_override = (existing_cover_path, cover_bytes)
```

New flow:

```python
cover_override = None
cover_add = None
if candidate_url and (candidate_width is None or candidate_width >= cover.MIN_COVER_WIDTH):
    cover_bytes = await cover.download_cover(candidate_url)
    if cover_bytes:
        cover_bytes = cover.resize_cover_if_needed(cover_bytes)
        existing_cover_path = cover.find_cover_path_in_opf(path)
        if existing_cover_path:
            # REPLACE path (existing)
            if cover.save_sidecar_if_absent(path):
                cover_override = (existing_cover_path, cover_bytes)
        else:
            # ADD path (new). No sidecar.
            cover_zip_path = cover.read_opf_and_add_cover(path, cover_bytes)
            if cover_zip_path:
                cover_add = (cover_zip_path, cover_bytes)

write_meta(path, updates, cover_override=cover_override, cover_add=cover_add)
```

`cover.read_opf_and_add_cover()` is a thin wrapper that:
1. Reads the OPF from the EPUB
2. Calls `add_cover_to_opf` on the parsed tree
3. Stashes the mutated tree on a module-level slot that `write_meta` then picks up

…actually that's gross. Simpler: have `write_meta` itself do the OPF mutation when `cover_add` is given, by calling `cover.add_cover_to_opf` on the OPF tree it already parses. `enrich.py` just passes `cover_add=cover_bytes` (bytes, not a tuple — the path is determined inside).

Revised contract:

- `cover_override=(zip_path, bytes)` — REPLACE; path resolved by caller.
- `cover_add=bytes` — ADD; `write_meta` resolves the zip path via `cover.add_cover_to_opf` against the parsed OPF before serializing.

Clean. `enrich.py` doesn't open the EPUB twice; `write_meta` keeps single-pass invariant.

## Error handling

| Failure | Behaviour |
|---|---|
| OPF lacks `<metadata>` or `<manifest>` (malformed) | `add_cover_to_opf` raises ValueError, `write_meta` propagates, `enrich_file` returns `error` status with reason |
| OPF already has id=cover-image but `find_cover_path_in_opf` returned None (broken cover meta) | `add_cover_to_opf` raises ValueError; caller should have taken REPLACE path. Defensive — points at a data bug elsewhere. |
| `cover_override` and `cover_add` both passed | `write_meta` raises ValueError — programmer error, fail loud |
| Cover bytes too small / download failed | Same as existing path — `cover_bytes` is None, neither path taken, metadata still writes |

In every soft failure case, metadata enrichment still completes — cover ADD is additive, never blocking.

## Testing

**Unit (`tests/test_cover.py`)** — new tests:
- `test_add_cover_to_opf_inserts_manifest_item_and_meta_tag`
- `test_add_cover_to_opf_raises_when_manifest_missing`
- `test_add_cover_to_opf_raises_when_metadata_missing`
- `test_add_cover_to_opf_raises_when_cover_id_already_exists`
- `test_add_cover_to_opf_returns_correct_zip_path_for_oebps_layout`
- `test_add_cover_to_opf_returns_correct_zip_path_for_root_layout`

**Unit (`tests/test_epub_meta.py`)** — new tests:
- `test_write_meta_with_cover_add_inserts_image_into_zip`
- `test_write_meta_with_cover_add_registers_cover_in_opf`
- `test_write_meta_raises_when_both_cover_override_and_cover_add_passed`

**Integration (`tests/test_enrich.py`)** — new tests:
- `test_enrich_adds_cover_when_epub_has_none` — mock Hardcover, EPUB fixture without cover, assert cover bytes present + manifest item appears + meta tag appears
- `test_enrich_takes_replace_path_when_cover_exists` — regression: existing behaviour unchanged

Existing tests must continue to pass without modification.

## Deployment

Standard plexypi rsync + docker-compose rebuild. No new dependencies. No backfill needed — the new path activates only for newly-encountered EPUBs without covers.

## Out of scope / future

- **Cover-page XHTML generation** — defer; current target is shelf thumbnail, not first-page-of-book.
- **OPF 3 properties form** — current OPF-2-style meta works in KOReader/Kindle; revisit if a stricter reader complains.
- **Configurable cover path** — DEFAULT_COVER_HREF is hardcoded to `images/cover.jpg`. If we ever encounter an EPUB where that path conflicts with an existing manifest item, add collision detection. Not blocking for v1.
