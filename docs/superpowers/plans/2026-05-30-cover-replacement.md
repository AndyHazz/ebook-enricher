# Cover Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the enricher matches a book with ≥80% confidence and Hardcover has a cover, replace the EPUB's existing cover image with the Hardcover version, preserving the displaced original as a `<book>.original.jpg` sidecar.

**Architecture:** New `ebook_enricher/cover.py` module owns pure cover ops (OPF parsing for cover path, sidecar I/O, image download). Existing `hardcover.py` gains optional `image_url/width/height` fields on `HardcoverBook`. Existing `write_meta()` gains an optional `cover_override=(zip_path, bytes)` parameter so the cover swap rides the existing atomic single-pass zip rewrite — no second open/rewrite. Existing `enrich_file()` orchestrates: after a successful match, try to prepare a cover override before calling `write_meta`.

**Tech Stack:** Python 3.12, httpx (already a dep for the GraphQL client), zipfile + **defusedxml.ElementTree** (defended against XXE / billion-laughs in case a malicious EPUB enters the pipeline), pytest + respx (already used for HTTP mocking). `defusedxml` is a drop-in replacement for stdlib `xml.etree.ElementTree` — added as a new dep in Task 1 (single line in `pyproject.toml`).

---

## File Structure

**Repo (`~/projects/ebook-enricher/`):**
- Create: `ebook_enricher/cover.py` — `find_cover_path_in_opf`, `save_sidecar_if_absent`, `download_cover`, size thresholds
- Modify: `ebook_enricher/hardcover.py` — add `image_url`, `image_width`, `image_height` to `HardcoverBook` + parse from hit
- Modify: `ebook_enricher/epub_meta.py:131` — `write_meta` gains optional `cover_override` parameter, branch in zip-rewrite loop
- Modify: `ebook_enricher/enrich.py:47` — `enrich_file` orchestrates cover prep before metadata write
- Create: `tests/test_cover.py` — unit tests for the new module
- Modify: `tests/test_hardcover.py` — extend hit-parsing tests for new fields
- Modify: `tests/test_epub_meta.py` — test `cover_override` parameter
- Modify: `tests/test_enrich.py` — integration tests for the orchestration

**Deployed:**
- `/opt/stacks/ebook-enricher/` (existing docker-compose stack on plexypi) — rebuild + restart
- `/mnt/us/ebooks/.stignore` on both PW5 and PW3 — append `(?d)*.original.jpg`

---

### Task 1: Project setup — feature branch + defusedxml dependency

**Files:**
- `pyproject.toml` (add `defusedxml==0.7.1`)

- [ ] **Step 1: Create feature branch**

```bash
cd ~/projects/ebook-enricher
git checkout main
git pull
git checkout -b feature/cover-replacement
```

- [ ] **Step 2: Add `defusedxml` to runtime dependencies**

Edit `pyproject.toml`. Find the `dependencies` block (around line 10):

```toml
dependencies = [
    "fastapi==0.115.5",
    "uvicorn[standard]==0.32.1",
    "httpx==0.28.0",
    "rapidfuzz==3.10.1",
    "pydantic==2.10.2",
]
```

Add `defusedxml`:

```toml
dependencies = [
    "fastapi==0.115.5",
    "uvicorn[standard]==0.32.1",
    "httpx==0.28.0",
    "rapidfuzz==3.10.1",
    "pydantic==2.10.2",
    "defusedxml==0.7.1",
]
```

- [ ] **Step 3: Install + verify clean baseline**

```bash
source .venv/bin/activate
pip install -e .
pytest -q
```

Expected: existing tests all pass (97 from prior work). `defusedxml` installs cleanly.

- [ ] **Step 4: Commit the dep addition on its own**

```bash
git add pyproject.toml
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "build: add defusedxml dep for safe OPF parsing in cover module"
```

---

### Task 2: HardcoverBook image fields + parser

**Files:**
- Modify: `ebook_enricher/hardcover.py:44-52` (dataclass), `ebook_enricher/hardcover.py` (`_parse_hit`)
- Modify: `tests/test_hardcover.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hardcover.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_hardcover.py -v -k "image_fields or no_image_block"
```

Expected: 2 tests FAIL with `AttributeError: 'HardcoverBook' object has no attribute 'image_url'`.

- [ ] **Step 3: Extend `HardcoverBook` dataclass**

In `ebook_enricher/hardcover.py`, replace the existing dataclass (around lines 44-52):

```python
@dataclass
class HardcoverBook:
    id: str
    title: str
    author: str
    description: Optional[str]
    series_name: Optional[str]
    series_position: Optional[str]
    genres: list[str]
    image_url: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
```

- [ ] **Step 4: Update `_parse_hit` to populate the new fields**

In `ebook_enricher/hardcover.py`, find the `_parse_hit` function and update its return statement. The existing return uses keyword args — add the new ones at the end:

```python
def _parse_hit(hit: dict) -> Optional[HardcoverBook]:
    """Parse a single search hit. Hardcover is in beta — skip malformed
    documents rather than crash."""
    doc = hit.get("document") or {}
    book_id = doc.get("id")
    title = doc.get("title")
    if not book_id or not title:
        logger.warning("Skipping malformed Hardcover hit: id=%r title=%r", book_id, title)
        return None

    series_name, series_position = _pick_series(doc)
    image = doc.get("image") or {}
    return HardcoverBook(
        id=str(book_id),
        title=title,
        author=_first_author(doc),
        description=doc.get("description") or None,
        series_name=series_name,
        series_position=series_position,
        genres=_extract_genres(doc),
        image_url=image.get("url"),
        image_width=image.get("width"),
        image_height=image.get("height"),
    )
```

(Keep the existing function body intact; only the `return HardcoverBook(...)` call gets the three new kwargs.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_hardcover.py -v
```

Expected: All hardcover tests PASS (existing + 2 new).

- [ ] **Step 6: Commit**

```bash
git add ebook_enricher/hardcover.py tests/test_hardcover.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: HardcoverBook gains image_url/width/height fields"
```

---

### Task 3: `cover.py` — `download_cover` (async + size guards)

**Files:**
- Create: `ebook_enricher/cover.py`
- Create: `tests/test_cover.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cover.py`:

```python
"""Tests for ebook_enricher.cover — pure cover ops (no enrichment policy)."""
import pytest
import respx
import httpx

from ebook_enricher import cover


@pytest.mark.asyncio
async def test_download_cover_returns_bytes_on_200():
    body = b"x" * 100_000  # 100KB, above MIN_COVER_SIZE_BYTES
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))
        result = await cover.download_cover(url)
    assert result == body


@pytest.mark.asyncio
async def test_download_cover_returns_none_on_5xx():
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(503))
        result = await cover.download_cover(url)
    assert result is None


@pytest.mark.asyncio
async def test_download_cover_returns_none_on_timeout():
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.TimeoutException("timeout"))
        result = await cover.download_cover(url)
    assert result is None


@pytest.mark.asyncio
async def test_download_cover_rejects_tiny_payload():
    body = b"x" * 1_000  # 1KB, below MIN_COVER_SIZE_BYTES (50KB)
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))
        result = await cover.download_cover(url)
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cover.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ebook_enricher.cover'`.

- [ ] **Step 3: Create `cover.py` with `download_cover` and thresholds**

Create `ebook_enricher/cover.py`:

```python
"""Pure cover-image operations: parse OPF for cover path, save sidecar,
download from URL. No enrichment policy here — that lives in enrich.py.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Below these thresholds we treat the candidate cover as a placeholder
# or broken asset and reject the swap.
MIN_COVER_SIZE_BYTES = 50_000   # 50KB — smaller is almost certainly a tracking pixel or placeholder
MIN_COVER_WIDTH = 500           # pixels (we trust Hardcover's reported width when checking)
DOWNLOAD_TIMEOUT_S = 10


async def download_cover(url: str, *, timeout_s: int = DOWNLOAD_TIMEOUT_S) -> Optional[bytes]:
    """GET the image at `url`. Returns bytes on a successful 200 with a
    reasonable payload size. Returns None on any failure (network,
    timeout, non-200, suspiciously small body). Never raises.

    Cover replacement is best-effort: any failure here is logged and
    the caller proceeds without replacing the cover.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("cover download failed (network): %s — %s", url, e)
        return None

    if resp.status_code != 200:
        logger.warning("cover download HTTP %d: %s", resp.status_code, url)
        return None

    data = resp.content
    if len(data) < MIN_COVER_SIZE_BYTES:
        logger.warning(
            "cover download too small (%d bytes < %d): %s",
            len(data), MIN_COVER_SIZE_BYTES, url,
        )
        return None

    return data
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cover.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/cover.py tests/test_cover.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: cover.download_cover with size guards"
```

---

### Task 4: `cover.py` — `find_cover_path_in_opf`

**Files:**
- Modify: `ebook_enricher/cover.py`
- Modify: `tests/test_cover.py`
- Use existing EPUB fixture conventions from `tests/conftest.py`

- [ ] **Step 1: Add helper fixtures to conftest.py if not already covering cover scenarios**

Read `tests/conftest.py` first. If there's no existing fixture that builds an EPUB *with* a cover image, append the following helper to `tests/conftest.py`:

```python
COVER_BYTES_ORIGINAL = b"ORIGINAL_COVER_BYTES" + b"x" * 60_000  # > MIN_COVER_SIZE_BYTES


def _opf_with_cover(extra_metadata: str = "") -> str:
    """OPF that declares a cover meta + manifest item, pointing at
    OEBPS/images/cover.jpg."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">test-uid-12345</dc:identifier>
    <dc:title>Test Book Title</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
    <meta name="cover" content="cover-img"/>
    {extra_metadata}
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="cover-img" href="images/cover.jpg" media-type="image/jpeg"/>
  </manifest>
  <spine>
    <itemref idref="nav"/>
  </spine>
</package>
"""


@pytest.fixture
def epub_with_cover(tmp_path) -> Path:
    """A minimal EPUB containing a cover image at OEBPS/images/cover.jpg."""
    epub = tmp_path / "with_cover.epub"
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", _opf_with_cover())
        zf.writestr("OEBPS/nav.xhtml", NAV_XHTML)
        zf.writestr("OEBPS/images/cover.jpg", COVER_BYTES_ORIGINAL)
    return epub


@pytest.fixture
def epub_without_cover(tmp_path) -> Path:
    """A minimal EPUB with no cover meta in OPF."""
    epub = tmp_path / "no_cover.epub"
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", _opf())  # uses existing _opf() without cover
        zf.writestr("OEBPS/nav.xhtml", NAV_XHTML)
    return epub


@pytest.fixture
def epub_with_broken_cover_ref(tmp_path) -> Path:
    """OPF declares cover meta pointing at a manifest id that doesn't exist."""
    epub = tmp_path / "broken_cover.epub"
    bad_opf = _opf_with_cover().replace('content="cover-img"', 'content="missing-id"')
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", bad_opf)
        zf.writestr("OEBPS/nav.xhtml", NAV_XHTML)
        zf.writestr("OEBPS/images/cover.jpg", COVER_BYTES_ORIGINAL)
    return epub
```

Make sure `from pathlib import Path` and `import zipfile` are at the top of conftest.py (they already are).

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_cover.py`:

```python
def test_find_cover_path_finds_standard_meta(epub_with_cover):
    """OPF with <meta name="cover" content="X"/> + manifest item → returns the href."""
    path = cover.find_cover_path_in_opf(epub_with_cover)
    assert path == "OEBPS/images/cover.jpg"


def test_find_cover_path_returns_none_when_no_meta(epub_without_cover):
    """OPF without cover meta → None."""
    assert cover.find_cover_path_in_opf(epub_without_cover) is None


def test_find_cover_path_returns_none_when_manifest_broken(epub_with_broken_cover_ref):
    """OPF cover meta points at a manifest id that doesn't exist → None."""
    assert cover.find_cover_path_in_opf(epub_with_broken_cover_ref) is None
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_cover.py -v -k "find_cover_path"
```

Expected: 3 tests FAIL with `AttributeError: module 'ebook_enricher.cover' has no attribute 'find_cover_path_in_opf'`.

- [ ] **Step 4: Implement `find_cover_path_in_opf`**

Append to `ebook_enricher/cover.py`:

```python
import zipfile
from pathlib import Path
# Use defusedxml's drop-in replacement for ElementTree — a malicious
# EPUB could otherwise feed us a billion-laughs payload or external
# entity reference and OOM/exfiltrate from the enricher container.
import defusedxml.ElementTree as ET


_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def _find_opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    """Look up the OPF path from META-INF/container.xml. Returns None
    if missing or unparseable — caller treats that as 'no cover'."""
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        return None
    rootfile = container.find("container:rootfiles/container:rootfile", _NS)
    if rootfile is None:
        return None
    return rootfile.get("full-path")


def find_cover_path_in_opf(epub_path: Path) -> Optional[str]:
    """Open the EPUB, locate <meta name="cover" content="<id>"/> in OPF,
    resolve <id> to the manifest item's href (joined to the OPF dir).
    Returns the path-within-zip (e.g. 'OEBPS/images/cover.jpg') or None
    if no cover meta is declared OR the declared manifest item isn't
    present in the zip.
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_path = _find_opf_path(zf)
            if not opf_path:
                return None
            try:
                opf_root = ET.fromstring(zf.read(opf_path))
            except (KeyError, ET.ParseError):
                return None

            # Find <meta name="cover" content="<id>"/> — EPUB 2 style.
            metadata = opf_root.find("opf:metadata", _NS)
            if metadata is None:
                return None
            cover_id = None
            for meta_el in metadata.findall("opf:meta", _NS):
                if meta_el.get("name") == "cover":
                    cover_id = meta_el.get("content")
                    break
            if not cover_id:
                return None

            # Resolve the manifest item by id.
            manifest = opf_root.find("opf:manifest", _NS)
            if manifest is None:
                return None
            for item in manifest.findall("opf:item", _NS):
                if item.get("id") == cover_id:
                    href = item.get("href")
                    if not href:
                        return None
                    # Resolve relative to the OPF dir
                    opf_dir = str(Path(opf_path).parent)
                    if opf_dir and opf_dir != ".":
                        full = f"{opf_dir}/{href}"
                    else:
                        full = href
                    # Must actually exist in the zip
                    if full in zf.namelist():
                        return full
                    return None

            return None
    except zipfile.BadZipFile:
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_cover.py -v
```

Expected: 7 tests PASS (4 from Task 3 + 3 new).

- [ ] **Step 6: Commit**

```bash
git add ebook_enricher/cover.py tests/test_cover.py tests/conftest.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: cover.find_cover_path_in_opf"
```

---

### Task 5: `cover.py` — `save_sidecar_if_absent`

**Files:**
- Modify: `ebook_enricher/cover.py`
- Modify: `tests/test_cover.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cover.py`:

```python
def test_save_sidecar_writes_once(epub_with_cover):
    """First call writes the sidecar; second call is a no-op."""
    sidecar = epub_with_cover.with_suffix("").parent / (
        epub_with_cover.stem + ".original.jpg"
    )
    assert not sidecar.exists()

    ok1 = cover.save_sidecar_if_absent(epub_with_cover)
    assert ok1 is True
    assert sidecar.exists()
    first_bytes = sidecar.read_bytes()

    ok2 = cover.save_sidecar_if_absent(epub_with_cover)
    assert ok2 is True
    # Bytes unchanged — second call did NOT rewrite
    assert sidecar.read_bytes() == first_bytes


def test_save_sidecar_preserves_true_original(epub_with_cover):
    """Sidecar bytes are the original cover bytes, not anything else."""
    cover.save_sidecar_if_absent(epub_with_cover)
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    # COVER_BYTES_ORIGINAL is defined in conftest.py
    from tests.conftest import COVER_BYTES_ORIGINAL
    assert sidecar.read_bytes() == COVER_BYTES_ORIGINAL


def test_save_sidecar_returns_false_when_no_cover(epub_without_cover):
    """No cover in EPUB → can't save sidecar → returns False."""
    ok = cover.save_sidecar_if_absent(epub_without_cover)
    assert ok is False
    sidecar = epub_without_cover.parent / (epub_without_cover.stem + ".original.jpg")
    assert not sidecar.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cover.py -v -k "save_sidecar"
```

Expected: 3 tests FAIL with `AttributeError: module 'ebook_enricher.cover' has no attribute 'save_sidecar_if_absent'`.

- [ ] **Step 3: Implement `save_sidecar_if_absent`**

Append to `ebook_enricher/cover.py`:

```python
def _sidecar_path(epub_path: Path) -> Path:
    """Recovery-sidecar location: same directory, base name with
    .original.jpg suffix. e.g. /a/b/Foo.epub → /a/b/Foo.original.jpg."""
    return epub_path.parent / (epub_path.stem + ".original.jpg")


def save_sidecar_if_absent(epub_path: Path) -> bool:
    """If `<epub>.original.jpg` does not exist next to the EPUB, extract
    the current cover bytes and write them as the sidecar. Idempotent:
    returns True if a usable sidecar exists at end of call (either pre-
    existing or just-written). Returns False if we couldn't save
    (no cover in EPUB, OS error) — caller should skip cover swap in
    that case to avoid losing the only original.
    """
    sidecar = _sidecar_path(epub_path)
    if sidecar.exists():
        return True

    cover_zip_path = find_cover_path_in_opf(epub_path)
    if not cover_zip_path:
        return False

    try:
        with zipfile.ZipFile(epub_path) as zf:
            data = zf.read(cover_zip_path)
    except (zipfile.BadZipFile, KeyError) as e:
        logger.warning("could not read cover from EPUB %s: %s", epub_path, e)
        return False

    try:
        sidecar.write_bytes(data)
    except OSError as e:
        logger.warning("could not write sidecar %s: %s", sidecar, e)
        return False

    return True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cover.py -v
```

Expected: 10 tests PASS (7 prior + 3 new).

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/cover.py tests/test_cover.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: cover.save_sidecar_if_absent"
```

---

### Task 6: `epub_meta.write_meta` — `cover_override` parameter

**Files:**
- Modify: `ebook_enricher/epub_meta.py:131` (signature) and the zip-rewrite loop
- Modify: `tests/test_epub_meta.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_epub_meta.py`:

```python
def test_write_meta_with_cover_override_replaces_cover_bytes(epub_with_cover):
    """cover_override=(zip_path, new_bytes) replaces that file's bytes
    in the EPUB during the single-pass rewrite."""
    from ebook_enricher.epub_meta import read_meta, write_meta
    import zipfile

    meta = read_meta(epub_with_cover)
    # Add a series so write_meta has something to update — its behaviour
    # for cover_override is what we're testing, not the metadata bits.
    meta.series = "Test Series"
    new_cover = b"NEW_COVER_BYTES" + b"x" * 70_000
    write_meta(
        epub_with_cover,
        meta,
        cover_override=("OEBPS/images/cover.jpg", new_cover),
    )
    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == new_cover


def test_write_meta_without_cover_override_leaves_cover(epub_with_cover):
    """When cover_override is None (default), the existing cover is untouched."""
    from ebook_enricher.epub_meta import read_meta, write_meta
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    meta = read_meta(epub_with_cover)
    meta.series = "Test Series"
    write_meta(epub_with_cover, meta)  # no cover_override
    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == COVER_BYTES_ORIGINAL
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_epub_meta.py -v -k "cover_override or without_cover_override"
```

Expected: FAIL on first test with `TypeError: write_meta() got an unexpected keyword argument 'cover_override'`.

- [ ] **Step 3: Update `write_meta` signature + rewrite loop**

In `ebook_enricher/epub_meta.py`, update the `write_meta` function. Find the existing signature at line 131:

```python
def write_meta(path: Path, meta: EpubMeta) -> None:
```

Replace it with:

```python
def write_meta(
    path: Path,
    meta: EpubMeta,
    cover_override: Optional[tuple[str, bytes]] = None,
) -> None:
```

(Make sure `from typing import Optional` is imported at the top of the file — it likely already is, since other signatures use Optional.)

Update the docstring (the function's existing docstring is right after the signature):

```python
    """Write series, series_index, description, and subjects into the EPUB.

    Title and author are NEVER overwritten — the values on `meta` for
    those fields are ignored. Only the enrichment-owned fields are
    updated.

    If `cover_override=(zip_path, bytes)` is provided, the zip member at
    that path is also replaced with the given bytes during the same
    single-pass rewrite — keeping the operation atomic.
    """
```

Then find the zip-rewrite loop (around line 172):

```python
            for item in src.infolist():
                if item.filename == opf_path:
                    dst.writestr(item, new_opf_bytes)
                elif item.filename == "mimetype":
                    # mimetype must be stored uncompressed
                    dst.writestr(item, src.read(item.filename),
                                 compress_type=zipfile.ZIP_STORED)
                else:
                    dst.writestr(item, src.read(item.filename))
```

Replace with:

```python
            for item in src.infolist():
                if item.filename == opf_path:
                    dst.writestr(item, new_opf_bytes)
                elif cover_override is not None and item.filename == cover_override[0]:
                    dst.writestr(item, cover_override[1])
                elif item.filename == "mimetype":
                    # mimetype must be stored uncompressed
                    dst.writestr(item, src.read(item.filename),
                                 compress_type=zipfile.ZIP_STORED)
                else:
                    dst.writestr(item, src.read(item.filename))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_epub_meta.py -v
```

Expected: All epub_meta tests PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/epub_meta.py tests/test_epub_meta.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: write_meta cover_override parameter"
```

---

### Task 7: `enrich.py` — orchestrate cover replacement

**Files:**
- Modify: `ebook_enricher/enrich.py:47` and surrounding region (after the match is selected)
- Modify: `tests/test_enrich.py`

- [ ] **Step 1: Write the failing tests**

Read the current `tests/test_enrich.py` first to match the existing pattern (it uses respx-mocked Hardcover responses and the fixtures from conftest). Append these tests:

```python
@pytest.mark.asyncio
@respx.mock
async def test_enrich_replaces_cover_when_hardcover_has_image(epub_with_cover):
    """When Hardcover returns a hit with image_url, cover gets replaced
    and the original is preserved as a sidecar."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    new_cover_bytes = b"HARDCOVER_NEW_COVER" + b"x" * 80_000
    cover_url = "https://assets.hardcover.app/edition/1/new.jpg"

    # Mock Hardcover search response with image
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 1463, "height": 2401},
                }
            }]}}}
        }),
    )
    # Mock the cover image fetch
    respx.get(cover_url).mock(
        return_value=httpx.Response(200, content=new_cover_bytes)
    )

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    # Cover bytes inside the EPUB are now Hardcover's
    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == new_cover_bytes

    # Sidecar exists with the TRUE original bytes
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    assert sidecar.exists()
    assert sidecar.read_bytes() == COVER_BYTES_ORIGINAL


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_cover_when_hardcover_no_image(epub_with_cover):
    """Hit without image block → metadata written, no sidecar, no cover change."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    # No "image" key
                }
            }]}}}
        }),
    )

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == COVER_BYTES_ORIGINAL
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    assert not sidecar.exists()


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_cover_when_download_fails(epub_with_cover):
    """Cover download returns 503 → metadata still written, no sidecar,
    no cover change."""
    from ebook_enricher.enrich import enrich_file
    from tests.conftest import COVER_BYTES_ORIGINAL
    import zipfile

    cover_url = "https://assets.hardcover.app/edition/1/new.jpg"
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 1463, "height": 2401},
                }
            }]}}}
        }),
    )
    respx.get(cover_url).mock(return_value=httpx.Response(503))

    result = await enrich_file(epub_with_cover, token="fake-token")
    assert result.status == "enriched"

    with zipfile.ZipFile(epub_with_cover) as zf:
        assert zf.read("OEBPS/images/cover.jpg") == COVER_BYTES_ORIGINAL
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    assert not sidecar.exists()


@pytest.mark.asyncio
@respx.mock
async def test_enrich_skips_cover_when_epub_lacks_cover_meta(epub_without_cover):
    """EPUB has no <meta name="cover"> → metadata written, no cover swap,
    no sidecar. Cover download not even attempted."""
    from ebook_enricher.enrich import enrich_file

    cover_url = "https://assets.hardcover.app/edition/1/new.jpg"
    respx.post("https://api.hardcover.app/v1/graphql").mock(
        return_value=httpx.Response(200, json={
            "data": {"search": {"results": {"hits": [{
                "document": {
                    "id": 1,
                    "title": "Test Book Title",
                    "author_names": ["Test Author"],
                    "description": "A description",
                    "featured_series": {"series": {"name": "Test Series"}, "position": 1},
                    "image": {"url": cover_url, "width": 1463, "height": 2401},
                }
            }]}}}
        }),
    )
    # Cover download endpoint is NOT mocked — if the code tries to hit it,
    # respx will raise. We assert that doesn't happen.

    result = await enrich_file(epub_without_cover, token="fake-token")
    assert result.status == "enriched"

    sidecar = epub_without_cover.parent / (epub_without_cover.stem + ".original.jpg")
    assert not sidecar.exists()
```

Also ensure these imports are at the top of `tests/test_enrich.py`:

```python
import httpx
import pytest
import respx
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_enrich.py -v -k "cover"
```

Expected: 4 tests FAIL (cover bytes unchanged after enrich, sidecars not created — because cover replacement isn't wired in yet).

- [ ] **Step 3: Wire cover replacement into `enrich_file`**

In `ebook_enricher/enrich.py`, find the `enrich_file` function (around line 47). The function currently does (paraphrased):

```python
async def enrich_file(path: Path, token: str) -> EnrichResult:
    meta = read_meta(path)
    if meta.series:
        return EnrichResult(status="skipped", reason="already_enriched")
    candidates = await search_book(meta.title, meta.author, token=token)
    # ... scoring + selection logic ...
    chosen = ...
    if not chosen or below_confidence:
        return EnrichResult(status="low_confidence", ...)
    # Update meta from chosen
    meta.series = chosen.series_name
    # ... etc ...
    write_meta(path, meta)
    return EnrichResult(status="enriched", ...)
```

You need to:
1. Add an import: `from ebook_enricher import cover`
2. After choosing `chosen` but BEFORE the `write_meta(path, meta)` call, prepare `cover_override`.
3. Pass `cover_override` to `write_meta`.

Find the `write_meta(path, meta)` call and replace its surrounding region with:

```python
    # Prepare cover override (best-effort — failures here never block
    # metadata enrichment).
    cover_override = None
    if chosen.image_url:
        existing_cover_path = cover.find_cover_path_in_opf(path)
        if existing_cover_path:
            cover_bytes = await cover.download_cover(chosen.image_url)
            if cover_bytes:
                saved = cover.save_sidecar_if_absent(path)
                if saved:
                    cover_override = (existing_cover_path, cover_bytes)
                # else: sidecar save failed — skip the swap to avoid
                # losing the only original.

    write_meta(path, meta, cover_override=cover_override)
```

If the existing code calls `write_meta(path, meta)` somewhere else in the function (e.g. in an error branch), leave those calls unchanged — they don't need cover handling.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_enrich.py -v
```

Expected: All enrich tests PASS (existing + 4 new cover tests).

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/enrich.py tests/test_enrich.py
git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat: enrich.py orchestrates cover replacement"
```

---

### Task 8: Full test suite green

**Files:** none

- [ ] **Step 1: Run full repo test suite**

```bash
cd ~/projects/ebook-enricher
source .venv/bin/activate
pytest -v
```

Expected: 110+ tests PASS (97 prior + 2 hardcover + 10 cover + 2 epub_meta + 4 enrich = 115).

- [ ] **Step 2: If anything fails, fix before deploying**

Do not proceed to Task 9 until the suite is green.

---

### Task 9: Deploy + Kindle `.stignore` update

**Files:**
- `/opt/stacks/ebook-enricher/` on plexypi (rebuild)
- `/mnt/us/ebooks/.stignore` on PW5 and PW3

- [ ] **Step 1: Merge feature branch + push**

```bash
cd ~/projects/ebook-enricher
git checkout main
git merge --ff-only feature/cover-replacement
git push origin main
git branch -d feature/cover-replacement
```

- [ ] **Step 2: Deploy to plexypi (rebuild ebook-enricher container)**

```bash
ssh plexypi 'cd /opt/stacks/ebook-enricher && sudo git pull && sudo docker compose build --no-cache && sudo docker compose up -d'
```

Expected: container rebuilds with the new code, restarts cleanly. Verify with:

```bash
ssh plexypi 'curl -sf http://localhost:8000/health'
```

Wait — the enricher container is on the `plexypi_default` docker network. `localhost:8000` from the plexypi host won't necessarily reach it. Test via the qBit-side container instead:

```bash
ssh plexypi 'docker exec qbittorrent wget -qO- http://ebook-enricher:8000/health'
```

Expected: `{"status":"ok"}` or whatever the existing health endpoint returns.

- [ ] **Step 3: Add `(?d)*.original.jpg` to PW5 .stignore**

```bash
ssh kindle 'KAPI=$(awk -F"[<>]" "/<apikey>/{print \$3}" /var/local/syncthing/config.xml)
CURRENT=$(curl -s -H "X-API-Key: $KAPI" "http://localhost:8384/rest/db/ignores?folder=9vq6c-9skem" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)[\"ignore\"] + [\"(?d)*.original.jpg\"]))")
curl -s -X POST -H "X-API-Key: $KAPI" -H "Content-Type: application/json" "http://localhost:8384/rest/db/ignores?folder=9vq6c-9skem" -d "{\"ignore\": $CURRENT}"
echo
cat /mnt/us/ebooks/.stignore'
```

Expected: `.stignore` now ends with `(?d)*.original.jpg`.

If PW5 doesn't have python3 (it probably doesn't), do it more manually:

```bash
ssh kindle 'KAPI=$(awk -F"[<>]" "/<apikey>/{print \$3}" /var/local/syncthing/config.xml)
curl -s -H "X-API-Key: $KAPI" "http://localhost:8384/rest/db/ignores?folder=9vq6c-9skem"
echo
# Manually inspect, then post the updated list:
curl -s -X POST -H "X-API-Key: $KAPI" -H "Content-Type: application/json" "http://localhost:8384/rest/db/ignores?folder=9vq6c-9skem" -d "{\"ignore\": [\"(?d)*.mobi\", \"(?d)*.azw3\", \"(?d)*.jpg\", \"(?d)*.original.jpg\"]}"'
```

(Substitute the actual ignore list — read it back from the GET first, then add `(?d)*.original.jpg`. Adjust based on what's there.)

- [ ] **Step 4: Add `(?d)*.original.jpg` to PW3 .stignore**

```bash
ssh kindle-pw3 'KAPI=$(awk -F"[<>]" "/<apikey>/{print \$3}" /var/local/syncthing/config.xml)
# Read current
curl -s -H "X-API-Key: $KAPI" "http://localhost:8384/rest/db/ignores?folder=9vq6c-9skem"
echo
# Post updated list (PW3 had: *.sdr/, (?d)*.mobi, (?d)*.azw3, (?d)*.jpg, Fonts/, Test books/, @Series/)
curl -s -X POST -H "X-API-Key: $KAPI" -H "Content-Type: application/json" "http://localhost:8384/rest/db/ignores?folder=9vq6c-9skem" -d "{\"ignore\": [\"*.sdr/\", \"(?d)*.mobi\", \"(?d)*.azw3\", \"(?d)*.jpg\", \"(?d)*.original.jpg\", \"Fonts/\", \"Test books/\", \"@Series/\"]}"'
```

(Verify the existing ignore list by reading first, then post the updated list.)

- [ ] **Step 5: Manual smoke test with a known low-res-cover book**

```bash
# Pick a book that's already in the library with a known mediocre cover
ssh plexypi 'docker exec qbittorrent wget -qO- --post-data "{\"path\":\"/data/media/ebooks/Endymion - Dan Simmons.epub\"}" --header "Content-Type: application/json" http://ebook-enricher:8000/enrich'
```

Expected: response `{"status":"skipped","reason":"already_enriched"}` (because Endymion already has series metadata). So this WON'T actually replace the cover yet.

To force cover replacement on an already-enriched book, manually clear the `calibre:series` first OR add a `force_covers` flag (out of scope for v1 per spec).

For a real smoke test, pick a book that has NOT been enriched yet — easiest is to download a new ebook torrent, which fires the regular pipeline.

- [ ] **Step 6: Verify on Kindle**

Open KOReader → file browser → the newly-downloaded book should show the higher-res cover. The original is at `<book>.original.jpg` on plexypi only (Kindle `.stignore` blocks it from syncing).

---

## Self-Review

**Spec coverage check** — each spec section maps to a task:

- ✓ HardcoverBook image fields — Task 2
- ✓ `cover.py` module (find_cover_path_in_opf, save_sidecar_if_absent, download_cover) — Tasks 3, 4, 5
- ✓ `write_meta` cover_override parameter — Task 6
- ✓ enrich.py orchestration — Task 7
- ✓ Error handling: Hardcover lacks image → Task 7 test #2
- ✓ Error handling: download fails → Task 7 test #3 + Task 3 (download_cover returns None)
- ✓ Error handling: EPUB lacks cover meta → Task 7 test #4 + Task 4 (find_cover returns None)
- ✓ Error handling: tiny payload → Task 3 (`test_download_cover_rejects_tiny_payload`)
- ✓ Sidecar idempotency — Task 5 (`test_save_sidecar_writes_once`)
- ✓ Sidecar preserves true original — Task 5 (`test_save_sidecar_preserves_true_original`)
- ✓ Single-pass zip rewrite — Task 6 implementation
- ✓ Deployment + Kindle stignore — Task 9

**Type consistency check:**
- `HardcoverBook.image_url: Optional[str]` — consistent everywhere
- `cover_override: Optional[tuple[str, bytes]]` — consistent
- `find_cover_path_in_opf(epub_path: Path) -> Optional[str]` — consistent
- `save_sidecar_if_absent(epub_path: Path) -> bool` — consistent
- `download_cover(url: str, *, timeout_s: int) -> Optional[bytes]` — consistent

**Placeholder scan:** clean. The "(out of scope for v1 per spec)" note in Task 9 step 5 explicitly references the spec's open questions — not a TBD.

**Security follow-up (out of scope, worth noting):** `ebook_enricher/epub_meta.py` already uses stdlib `xml.etree.ElementTree` for OPF parsing — it has the same XXE/billion-laughs exposure as the new cover.py would have without `defusedxml`. This plan hardens the new module only. A separate one-line follow-up (switch `import xml.etree.ElementTree as ET` → `import defusedxml.ElementTree as ET` in epub_meta.py) would close the gap; existing tests should pass unchanged since defusedxml is a drop-in. Recommend filing as a follow-up issue but not expanding this plan's scope.
