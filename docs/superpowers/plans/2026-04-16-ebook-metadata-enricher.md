# Ebook Metadata Enricher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a small Python HTTP service that enriches EPUB metadata (series, description, genres) using the Hardcover GraphQL API, triggered automatically when qBittorrent finishes an ebook download, without affecting seeding copies.

**Architecture:** FastAPI service in a `python:3-alpine` Docker container. Four focused modules: `matcher` (pure fuzzy matching), `epub_meta` (ebooklib wrapper), `hardcover` (GraphQL client), `enrich` (orchestrator). `server` exposes `POST /enrich`, `POST /backfill`, `GET /health`. qBittorrent's autorun script copies (not hardlinks) each ebook to the Syncthing folder, then `curl`s the enricher's `/enrich` endpoint.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, rapidfuzz, pytest + pytest-asyncio + respx, Docker, docker compose. EPUB editing uses stdlib `zipfile` + `xml.etree.ElementTree` (ebooklib was considered but it's lxml-heavy and can mangle complex EPUBs during round-trips; direct OPF surgery is cleaner and deps-free).

---

## File Structure

```
~/projects/ebook-enricher/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .dockerignore
├── .gitignore
├── .env.example
├── README.md
├── ebook_enricher/
│   ├── __init__.py
│   ├── matcher.py        # Pure fuzzy match functions
│   ├── epub_meta.py      # Read/write EPUB metadata (ebooklib)
│   ├── hardcover.py      # GraphQL client
│   ├── enrich.py         # Orchestrator
│   └── server.py         # FastAPI app
├── tests/
│   ├── __init__.py
│   ├── conftest.py       # Shared fixtures (EPUB generator)
│   ├── test_matcher.py
│   ├── test_epub_meta.py
│   ├── test_hardcover.py
│   ├── test_enrich.py
│   └── test_server.py
└── docs/superpowers/     # specs + plans (already exist)
```

**Deployed to plexypi at**: `/opt/stacks/ebook-enricher/`

---

## Task 1: Project Scaffold

**Files:**
- Create: `~/projects/ebook-enricher/.gitignore`
- Create: `~/projects/ebook-enricher/.dockerignore`
- Create: `~/projects/ebook-enricher/pyproject.toml`
- Create: `~/projects/ebook-enricher/Dockerfile`
- Create: `~/projects/ebook-enricher/docker-compose.yml`
- Create: `~/projects/ebook-enricher/.env.example`
- Create: `~/projects/ebook-enricher/ebook_enricher/__init__.py`
- Create: `~/projects/ebook-enricher/tests/__init__.py`

- [ ] **Step 1: Create `.gitignore`**

```
__pycache__/
*.pyc
.venv/
.env
.pytest_cache/
*.egg-info/
dist/
build/
.coverage
```

- [ ] **Step 2: Create `.dockerignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.git/
docs/
tests/
.env
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ebook-enricher"
version = "0.1.0"
description = "EPUB metadata enricher using Hardcover GraphQL API"
requires-python = ">=3.12"
dependencies = [
    "fastapi==0.115.5",
    "uvicorn[standard]==0.32.1",
    "httpx==0.28.0",
    "rapidfuzz==3.10.1",
    "pydantic==2.10.2",
]

[project.optional-dependencies]
test = [
    "pytest==8.3.4",
    "pytest-asyncio==0.24.0",
    "respx==0.22.0",
]

[tool.hatch.build.targets.wheel]
packages = ["ebook_enricher"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Create `Dockerfile`**

```dockerfile
FROM python:3.12-alpine

WORKDIR /app

# Build deps for rapidfuzz C extension; removed after install
RUN apk add --no-cache --virtual .build-deps gcc g++ musl-dev

COPY pyproject.toml README.md ./
COPY ebook_enricher/ ./ebook_enricher/

RUN pip install --no-cache-dir . \
 && apk del .build-deps

EXPOSE 8000

CMD ["uvicorn", "ebook_enricher.server:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 5: Create `docker-compose.yml`**

```yaml
services:
  ebook-enricher:
    build: .
    image: ebook-enricher:latest
    container_name: ebook-enricher
    restart: unless-stopped
    environment:
      - HARDCOVER_TOKEN=${HARDCOVER_TOKEN}
      - EBOOKS_PATH=/data/media/ebooks
      - LOG_LEVEL=INFO
    volumes:
      - /mnt/data/media/ebooks:/data/media/ebooks
    networks:
      - plexypi_default
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3

networks:
  plexypi_default:
    external: true
```

- [ ] **Step 6: Create `.env.example`**

```
HARDCOVER_TOKEN=
```

- [ ] **Step 7: Create empty package init files**

Create empty files:
- `ebook_enricher/__init__.py` (empty)
- `tests/__init__.py` (empty)

- [ ] **Step 8: Create `README.md`**

```markdown
# ebook-enricher

Enriches EPUB metadata (series, description, genres) on Syncthing-bound ebook copies using the Hardcover GraphQL API.

## Deploy

Deployed to `/opt/stacks/ebook-enricher/` on plexypi. Run:
\`\`\`
cd /opt/stacks/ebook-enricher
docker compose up -d
\`\`\`

## Backfill all existing books

\`\`\`
docker exec ebook-enricher curl -sS -X POST http://localhost:8000/backfill
\`\`\`

## API

- `GET /health` — liveness
- `POST /enrich` — body `{"path": "/data/media/ebooks/<file>.epub"}` — returns status envelope
- `POST /backfill` — walks the ebooks folder, enriches everything

## Development

\`\`\`
python -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
pytest
\`\`\`
```

- [ ] **Step 9: Commit**

```bash
cd ~/projects/ebook-enricher
git add .gitignore .dockerignore pyproject.toml Dockerfile docker-compose.yml .env.example README.md ebook_enricher/__init__.py tests/__init__.py
git commit -m "Scaffold ebook-enricher project structure"
```

---

## Task 2: Matcher Module (Pure Functions)

**Files:**
- Create: `~/projects/ebook-enricher/ebook_enricher/matcher.py`
- Create: `~/projects/ebook-enricher/tests/test_matcher.py`

- [ ] **Step 1: Set up a local venv for TDD iteration**

```bash
cd ~/projects/ebook-enricher
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
```

Expected: all deps installed, `pytest --version` works.

- [ ] **Step 2: Write failing test for high-similarity match**

Create `tests/test_matcher.py`:

```python
from ebook_enricher.matcher import is_confident_match


def test_exact_match_passes():
    assert is_confident_match(
        "Dungeon Crawler Carl", "Matt Dinniman",
        "Dungeon Crawler Carl", "Matt Dinniman",
    ) is True


def test_subtitle_variant_passes():
    # EPUB has "All the Skills: A Deckbuilding LitRPG"
    # Hardcover has "All the Skills"
    assert is_confident_match(
        "All the Skills: A Deckbuilding LitRPG", "Honour Rae",
        "All the Skills", "Honour Rae",
    ) is True


def test_different_book_fails():
    assert is_confident_match(
        "All the Skills", "Honour Rae",
        "The Skills of Success", "Different Author",
    ) is False


def test_same_title_wrong_author_fails():
    assert is_confident_match(
        "The Expanse", "James S. A. Corey",
        "The Expanse", "Someone Else Entirely",
    ) is False


def test_minor_punctuation_passes():
    assert is_confident_match(
        "Sea of Tranquility: A novel", "Emily St. John Mandel",
        "Sea of Tranquility", "Emily St. John Mandel",
    ) is True
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_matcher.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ebook_enricher.matcher'`

- [ ] **Step 4: Write minimal matcher implementation**

Create `ebook_enricher/matcher.py`:

```python
"""Fuzzy matching gate for Hardcover search results.

Returns True only when both title and author similarity exceed the threshold.
Separate thresholds are deliberate: a wrong-author match is a worse failure
than a wrong-subtitle match.
"""
from rapidfuzz import fuzz

TITLE_THRESHOLD = 80
AUTHOR_THRESHOLD = 80


def is_confident_match(
    epub_title: str,
    epub_author: str,
    hc_title: str,
    hc_author: str,
) -> bool:
    title_score = fuzz.token_set_ratio(epub_title.lower(), hc_title.lower())
    author_score = fuzz.token_set_ratio(epub_author.lower(), hc_author.lower())
    return title_score >= TITLE_THRESHOLD and author_score >= AUTHOR_THRESHOLD
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_matcher.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add ebook_enricher/matcher.py tests/test_matcher.py
git commit -m "Add fuzzy match gate for Hardcover results"
```

---

## Task 3: EPUB Metadata Module

**Files:**
- Create: `~/projects/ebook-enricher/tests/conftest.py` (EPUB fixture generator)
- Create: `~/projects/ebook-enricher/ebook_enricher/epub_meta.py`
- Create: `~/projects/ebook-enricher/tests/test_epub_meta.py`

- [ ] **Step 1: Write conftest.py with EPUB fixture generator**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures. Generates minimal EPUB files in tmp_path
so we don't need to commit binary fixtures.
"""
import zipfile
from pathlib import Path

import pytest


MIMETYPE = "application/epub+zip"

CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _opf(extra_metadata: str = "") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">test-uid-12345</dc:identifier>
    <dc:title>Test Book Title</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
    {extra_metadata}
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine>
    <itemref idref="nav"/>
  </spine>
</package>
"""


NAV_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Nav</title></head>
<body>
  <nav epub:type="toc"><ol><li><a href="nav.xhtml">Nav</a></li></ol></nav>
</body>
</html>
"""


def _build_epub(path: Path, extra_metadata: str = "") -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype MUST be first and stored without compression
        z.writestr(
            zipfile.ZipInfo("mimetype"),
            MIMETYPE,
            compress_type=zipfile.ZIP_STORED,
        )
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/content.opf", _opf(extra_metadata))
        z.writestr("OEBPS/nav.xhtml", NAV_XHTML)
    return path


@pytest.fixture
def bare_epub(tmp_path: Path) -> Path:
    """EPUB with only title + author + language. No series, no description."""
    return _build_epub(tmp_path / "bare.epub")


@pytest.fixture
def enriched_epub(tmp_path: Path) -> Path:
    """EPUB that already has calibre:series set — enrichment should skip."""
    extra = """
    <meta name="calibre:series" content="Existing Series"/>
    <meta name="calibre:series_index" content="2"/>
    <dc:description>Existing description.</dc:description>
    """
    return _build_epub(tmp_path / "enriched.epub", extra)
```

- [ ] **Step 2: Write failing tests for read_meta**

Create `tests/test_epub_meta.py`:

```python
from pathlib import Path

from ebook_enricher.epub_meta import EpubMeta, read_meta, write_meta


def test_read_bare_epub(bare_epub: Path):
    meta = read_meta(bare_epub)
    assert meta.title == "Test Book Title"
    assert meta.author == "Test Author"
    assert meta.series is None
    assert meta.series_index is None
    assert meta.description is None


def test_read_enriched_epub(enriched_epub: Path):
    meta = read_meta(enriched_epub)
    assert meta.title == "Test Book Title"
    assert meta.series == "Existing Series"
    assert meta.series_index == "2"
    assert meta.description == "Existing description."


def test_write_series_to_bare_epub(bare_epub: Path):
    write_meta(
        bare_epub,
        EpubMeta(
            title="Test Book Title",
            author="Test Author",
            series="New Series",
            series_index="1",
            description="A new description.",
            subjects=["Fantasy", "Adventure"],
        ),
    )
    meta = read_meta(bare_epub)
    assert meta.series == "New Series"
    assert meta.series_index == "1"
    assert meta.description == "A new description."
    assert set(meta.subjects) == {"Fantasy", "Adventure"}


def test_write_preserves_title_and_author(bare_epub: Path):
    write_meta(
        bare_epub,
        EpubMeta(
            title="Different Title",
            author="Different Author",
            series="New Series",
            series_index=None,
            description=None,
            subjects=[],
        ),
    )
    # Title and author are NOT overwritten by write_meta
    meta = read_meta(bare_epub)
    assert meta.title == "Test Book Title"
    assert meta.author == "Test Author"
    assert meta.series == "New Series"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_epub_meta.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ebook_enricher.epub_meta'`

- [ ] **Step 4: Write epub_meta implementation**

Create `ebook_enricher/epub_meta.py`:

```python
"""EPUB metadata reader/writer.

Uses zipfile + ElementTree directly rather than ebooklib because:
- We only touch the OPF file, which is plain XML inside a zip.
- ebooklib has a heavy dependency on lxml and sometimes mangles complex
  EPUBs during a round-trip write. Manipulating the OPF directly is
  more surgical and preserves everything else in the archive.
- Keeping deps minimal suits the Pi deployment.
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# Register namespaces for output
ET.register_namespace("", NS["opf"])
ET.register_namespace("dc", NS["dc"])


@dataclass
class EpubMeta:
    title: str
    author: str
    series: Optional[str] = None
    series_index: Optional[str] = None
    description: Optional[str] = None
    subjects: list[str] = field(default_factory=list)


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """Parse META-INF/container.xml to locate the OPF path."""
    data = zf.read("META-INF/container.xml")
    root = ET.fromstring(data)
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = root.find("c:rootfiles/c:rootfile", ns)
    if rootfile is None:
        raise ValueError("EPUB container.xml has no rootfile")
    return rootfile.attrib["full-path"]


def _parse_opf(opf_bytes: bytes) -> ET.Element:
    return ET.fromstring(opf_bytes)


def _text(el: Optional[ET.Element]) -> Optional[str]:
    return el.text.strip() if el is not None and el.text else None


def read_meta(path: Path) -> EpubMeta:
    with zipfile.ZipFile(path) as zf:
        opf_path = _find_opf_path(zf)
        root = _parse_opf(zf.read(opf_path))
    metadata = root.find("opf:metadata", NS)
    if metadata is None:
        raise ValueError(f"No metadata block in {path}")

    title = _text(metadata.find("dc:title", NS)) or ""
    author = _text(metadata.find("dc:creator", NS)) or ""
    description = _text(metadata.find("dc:description", NS))
    subjects = [
        s.text.strip() for s in metadata.findall("dc:subject", NS)
        if s.text and s.text.strip()
    ]

    series = None
    series_index = None
    for meta in metadata.findall("opf:meta", NS):
        name = meta.attrib.get("name")
        content = meta.attrib.get("content")
        if name == "calibre:series" and content:
            series = content
        elif name == "calibre:series_index" and content:
            series_index = content

    return EpubMeta(
        title=title,
        author=author,
        series=series,
        series_index=series_index,
        description=description,
        subjects=subjects,
    )


def _set_or_add_meta(metadata: ET.Element, name: str, content: str) -> None:
    """Replace any existing <meta name="X"> element, or add a new one."""
    for meta in metadata.findall("opf:meta", NS):
        if meta.attrib.get("name") == name:
            meta.attrib["content"] = content
            return
    meta = ET.SubElement(metadata, f"{{{NS['opf']}}}meta")
    meta.attrib["name"] = name
    meta.attrib["content"] = content


def _set_or_add_dc(metadata: ET.Element, tag: str, text: str) -> None:
    """Replace existing dc:X element's text, or add a new one."""
    existing = metadata.find(f"dc:{tag}", NS)
    if existing is not None:
        existing.text = text
        return
    el = ET.SubElement(metadata, f"{{{NS['dc']}}}{tag}")
    el.text = text


def write_meta(path: Path, meta: EpubMeta) -> None:
    """Write series, series_index, description, and subjects into the EPUB.

    Title and author are NEVER overwritten — the values on `meta` for
    those fields are ignored. Only the enrichment-owned fields are
    updated.
    """
    with zipfile.ZipFile(path) as zf:
        opf_path = _find_opf_path(zf)
        root = _parse_opf(zf.read(opf_path))

    metadata = root.find("opf:metadata", NS)
    if metadata is None:
        raise ValueError(f"No metadata block in {path}")

    if meta.series:
        _set_or_add_meta(metadata, "calibre:series", meta.series)
    if meta.series_index:
        _set_or_add_meta(metadata, "calibre:series_index", meta.series_index)
    if meta.description:
        _set_or_add_dc(metadata, "description", meta.description)
    if meta.subjects:
        # Remove existing subjects, then add new ones
        for s in metadata.findall("dc:subject", NS):
            metadata.remove(s)
        for subject in meta.subjects:
            el = ET.SubElement(metadata, f"{{{NS['dc']}}}subject")
            el.text = subject

    new_opf_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # Rewrite the zip with the modified OPF and the rest copied verbatim
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".epub")
    import os
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(path) as src, \
             zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                if item.filename == opf_path:
                    dst.writestr(item, new_opf_bytes)
                elif item.filename == "mimetype":
                    # mimetype must be stored uncompressed
                    dst.writestr(item, src.read(item.filename),
                                 compress_type=zipfile.ZIP_STORED)
                else:
                    dst.writestr(item, src.read(item.filename))
        shutil.move(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_epub_meta.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py ebook_enricher/epub_meta.py tests/test_epub_meta.py
git commit -m "Add EPUB metadata reader/writer"
```

---

## Task 4: Hardcover GraphQL Client

**Files:**
- Create: `~/projects/ebook-enricher/ebook_enricher/hardcover.py`
- Create: `~/projects/ebook-enricher/tests/test_hardcover.py`

- [ ] **Step 1: Write failing tests for hardcover.search_book**

Create `tests/test_hardcover.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_hardcover.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ebook_enricher.hardcover'`

- [ ] **Step 3: Write hardcover client implementation**

Create `ebook_enricher/hardcover.py`:

```python
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


def _pick_series(book_series: list[dict]) -> tuple[Optional[str], Optional[str]]:
    if not book_series:
        return None, None
    featured = next((s for s in book_series if s.get("featured")), None)
    chosen = featured or book_series[0]
    name = (chosen.get("series") or {}).get("name")
    pos = chosen.get("position")
    return name, (str(pos) if pos is not None else None)


def _first_author(contributions: list[dict]) -> str:
    if not contributions:
        return ""
    return (contributions[0].get("author") or {}).get("name") or ""


def _parse_book(raw: dict) -> HardcoverBook:
    series_name, series_pos = _pick_series(raw.get("book_series") or [])
    return HardcoverBook(
        id=raw["id"],
        title=raw["title"],
        author=_first_author(raw.get("contributions") or []),
        description=raw.get("description"),
        series_name=series_name,
        series_position=series_pos,
        genres=_extract_genres(raw.get("cached_tags")),
    )


async def _post(client: httpx.AsyncClient, token: str, variables: dict) -> dict:
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
            books = (payload.get("data") or {}).get("books") or []
            return [_parse_book(b) for b in books]
    return []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_hardcover.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/hardcover.py tests/test_hardcover.py
git commit -m "Add Hardcover GraphQL client with rate-limit retry"
```

---

## Task 5: Enrich Orchestrator

**Files:**
- Create: `~/projects/ebook-enricher/ebook_enricher/enrich.py`
- Create: `~/projects/ebook-enricher/tests/test_enrich.py`

- [ ] **Step 1: Write failing tests for enrich_file**

Create `tests/test_enrich.py`:

```python
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ebook_enricher.enrich import EnrichResult, enrich_file
from ebook_enricher.epub_meta import read_meta
from ebook_enricher.hardcover import HardcoverBook


def _make_hc_book(**overrides) -> HardcoverBook:
    defaults = dict(
        id=1,
        title="Test Book Title",
        author="Test Author",
        description="A test description.",
        series_name="Test Series",
        series_position="1.5",
        genres=["Fantasy", "LitRPG"],
    )
    defaults.update(overrides)
    return HardcoverBook(**defaults)


@pytest.mark.asyncio
async def test_enriches_bare_epub(bare_epub: Path):
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[_make_hc_book()])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "enriched"
    meta = read_meta(bare_epub)
    assert meta.series == "Test Series"
    assert meta.series_index == "1.5"
    assert meta.description == "A test description."
    assert "Fantasy" in meta.subjects


@pytest.mark.asyncio
async def test_skips_already_enriched(enriched_epub: Path):
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock()) as mock:
        result = await enrich_file(enriched_epub, token="fake")
    assert result.status == "skipped"
    assert result.reason == "already_enriched"
    mock.assert_not_awaited()  # Never queried Hardcover
    # Existing metadata preserved
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"


@pytest.mark.asyncio
async def test_no_match(bare_epub: Path):
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "no_match"
    meta = read_meta(bare_epub)
    assert meta.series is None


@pytest.mark.asyncio
async def test_low_confidence(bare_epub: Path):
    # Hardcover returns a book with a totally different title
    bad_match = _make_hc_book(title="Completely Different Book", author="Someone Else")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[bad_match])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "low_confidence"
    meta = read_meta(bare_epub)
    assert meta.series is None  # Untouched


@pytest.mark.asyncio
async def test_second_match_wins_if_first_is_low_confidence(bare_epub: Path):
    bad = _make_hc_book(title="Wrong Title", author="Wrong Author")
    good = _make_hc_book()  # matches "Test Book Title" / "Test Author"
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[bad, good])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "enriched"


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path):
    result = await enrich_file(tmp_path / "nope.epub", token="fake")
    assert result.status == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_enrich.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ebook_enricher.enrich'`

- [ ] **Step 3: Write enrich orchestrator**

Create `ebook_enricher/enrich.py`:

```python
"""Per-file enrichment orchestrator.

Pipeline:
  1. Read EPUB metadata.
  2. If calibre:series is already set, skip (respect existing good data).
  3. Query Hardcover for top 3 matches by popularity.
  4. Iterate matches, first one passing is_confident_match wins.
  5. Write back only fields currently empty in the EPUB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ebook_enricher.epub_meta import EpubMeta, read_meta, write_meta
from ebook_enricher.hardcover import HardcoverBook, RateLimitedError, search_book
from ebook_enricher.matcher import is_confident_match

logger = logging.getLogger(__name__)


@dataclass
class EnrichResult:
    status: str  # enriched | skipped | no_match | low_confidence | rate_limited | error
    reason: Optional[str] = None
    series: Optional[str] = None  # For debugging


async def enrich_file(path: Path, token: str) -> EnrichResult:
    try:
        meta = read_meta(path)
    except FileNotFoundError:
        return EnrichResult(status="error", reason=f"file_not_found: {path}")
    except Exception as e:
        logger.exception("Failed to read EPUB %s", path)
        return EnrichResult(status="error", reason=f"read_failed: {e}")

    if meta.series:
        return EnrichResult(status="skipped", reason="already_enriched")

    try:
        candidates = await search_book(meta.title, meta.author, token=token)
    except RateLimitedError:
        return EnrichResult(status="rate_limited")
    except Exception as e:
        logger.exception("Hardcover query failed for %s", path)
        return EnrichResult(status="error", reason=f"hardcover_error: {e}")

    if not candidates:
        return EnrichResult(status="no_match")

    chosen: Optional[HardcoverBook] = None
    for candidate in candidates:
        if is_confident_match(meta.title, meta.author, candidate.title, candidate.author):
            chosen = candidate
            break

    if chosen is None:
        return EnrichResult(status="low_confidence")

    updates = EpubMeta(
        title=meta.title,  # not written, but required by dataclass
        author=meta.author,
    )
    if not meta.series and chosen.series_name:
        updates.series = chosen.series_name
    if not meta.series_index and chosen.series_position:
        updates.series_index = chosen.series_position
    if not meta.description and chosen.description:
        updates.description = chosen.description
    if not meta.subjects and chosen.genres:
        updates.subjects = chosen.genres

    try:
        write_meta(path, updates)
    except Exception as e:
        logger.exception("Failed to write EPUB %s", path)
        return EnrichResult(status="error", reason=f"write_failed: {e}")

    return EnrichResult(status="enriched", series=chosen.series_name)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_enrich.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add ebook_enricher/enrich.py tests/test_enrich.py
git commit -m "Add enrichment orchestrator"
```

---

## Task 6: FastAPI Server + Backfill

**Files:**
- Create: `~/projects/ebook-enricher/ebook_enricher/server.py`
- Create: `~/projects/ebook-enricher/tests/test_server.py`

- [ ] **Step 1: Write failing tests for server endpoints**

Create `tests/test_server.py`:

```python
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ebook_enricher.enrich import EnrichResult
from ebook_enricher.server import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_enrich_calls_enrich_file(client, bare_epub: Path, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    with patch(
        "ebook_enricher.server.enrich_file",
        new=AsyncMock(return_value=EnrichResult(status="enriched", series="Test")),
    ) as mock:
        resp = client.post("/enrich", json={"path": str(bare_epub)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "enriched"
    assert body["series"] == "Test"
    mock.assert_awaited_once()


def test_enrich_missing_path(client, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    resp = client.post("/enrich", json={})
    assert resp.status_code == 422  # Pydantic validation error


def test_enrich_without_token_returns_error(client, monkeypatch, bare_epub: Path):
    monkeypatch.delenv("HARDCOVER_TOKEN", raising=False)
    resp = client.post("/enrich", json={"path": str(bare_epub)})
    assert resp.status_code == 500
    assert "HARDCOVER_TOKEN" in resp.json()["detail"]


def test_backfill_iterates_folder(client, tmp_path: Path, bare_epub: Path, monkeypatch):
    # Copy the bare_epub into a sub-folder the backfill will walk
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    import shutil
    shutil.copy(bare_epub, books_dir / "one.epub")
    shutil.copy(bare_epub, books_dir / "two.epub")

    monkeypatch.setenv("EBOOKS_PATH", str(books_dir))
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")

    with patch(
        "ebook_enricher.server.enrich_file",
        new=AsyncMock(return_value=EnrichResult(status="enriched")),
    ) as mock:
        resp = client.post("/backfill")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["enriched"] == 2
    assert mock.await_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_server.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ebook_enricher.server'`

- [ ] **Step 3: Write server implementation**

Create `ebook_enricher/server.py`:

```python
"""FastAPI HTTP surface.

Thin glue over ebook_enricher.enrich. Every request returns a status
envelope; errors become 5xx only for programming problems (missing
token, etc.) — not for enrichment misses, which are 200 with a
descriptive status string so the caller can distinguish "worked" from
"didn't find anything" from "broken".
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ebook_enricher.enrich import EnrichResult, enrich_file

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="ebook-enricher")

BACKFILL_DELAY_S = 1.0


class EnrichRequest(BaseModel):
    path: str


class BackfillSummary(BaseModel):
    total: int
    enriched: int
    skipped: int
    no_match: int
    low_confidence: int
    rate_limited: int
    errors: int


def _token() -> str:
    token = os.environ.get("HARDCOVER_TOKEN")
    if not token:
        raise HTTPException(
            status_code=500,
            detail="HARDCOVER_TOKEN environment variable not set",
        )
    return token


def _ebooks_path() -> Path:
    return Path(os.environ.get("EBOOKS_PATH", "/data/media/ebooks"))


def _result_to_dict(result: EnrichResult) -> dict:
    return {"status": result.status, "reason": result.reason, "series": result.series}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/enrich")
async def enrich(req: EnrichRequest) -> dict:
    token = _token()
    result = await enrich_file(Path(req.path), token=token)
    return _result_to_dict(result)


@app.post("/backfill")
async def backfill() -> BackfillSummary:
    token = _token()
    root = _ebooks_path()
    summary = {
        "total": 0, "enriched": 0, "skipped": 0, "no_match": 0,
        "low_confidence": 0, "rate_limited": 0, "errors": 0,
    }
    for path in sorted(root.rglob("*.epub")):
        summary["total"] += 1
        result = await enrich_file(path, token=token)
        key = {
            "enriched": "enriched",
            "skipped": "skipped",
            "no_match": "no_match",
            "low_confidence": "low_confidence",
            "rate_limited": "rate_limited",
            "error": "errors",
        }.get(result.status, "errors")
        summary[key] += 1
        logger.info("backfill %s -> %s (%s)", path.name, result.status, result.reason)
        await asyncio.sleep(BACKFILL_DELAY_S)
    return BackfillSummary(**summary)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_server.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run full test suite**

```bash
pytest -v
```

Expected: all tests from tasks 2-6 pass (5 + 4 + 6 + 6 + 5 = 26 tests).

- [ ] **Step 6: Commit**

```bash
git add ebook_enricher/server.py tests/test_server.py
git commit -m "Add FastAPI server with /enrich, /backfill, /health"
```

---

## Task 7: Deploy Stack to plexypi

**Files:**
- Create on plexypi: `/opt/stacks/ebook-enricher/` (contents copied from local repo)
- Modify on plexypi: `/opt/stacks/ebook-enricher/.env` (populate HARDCOVER_TOKEN)

- [ ] **Step 1: Confirm Docker network name qBit currently uses**

```bash
ssh plexypi "docker inspect qbittorrent --format '{{range \$k,\$v := .NetworkSettings.Networks}}{{\$k}}{{end}}'"
```

Expected output: `plexypi_default` (this is the `networks:` entry in the compose file we reference). If the name differs, update `docker-compose.yml`'s `networks:` block accordingly and note the correct name for Step 4.

- [ ] **Step 2: Sync project to plexypi**

```bash
rsync -av --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
    ~/projects/ebook-enricher/ plexypi:/opt/stacks/ebook-enricher/
```

Expected: files copied over.

- [ ] **Step 3: Create `.env` on plexypi with Hardcover token**

Prompt the user for their Hardcover API token, then:

```bash
ssh plexypi "umask 077 && cat > /opt/stacks/ebook-enricher/.env" <<'EOF'
HARDCOVER_TOKEN=<paste-token-here>
EOF
```

Verify permissions:

```bash
ssh plexypi "ls -la /opt/stacks/ebook-enricher/.env"
```

Expected: `-rw------- 1 ... .env`

- [ ] **Step 4: Build and start the container**

```bash
ssh plexypi "cd /opt/stacks/ebook-enricher && docker compose up -d --build"
```

Expected: image built, container started, no errors. First build takes 2-5 minutes on a Pi.

- [ ] **Step 5: Verify the service is healthy**

```bash
ssh plexypi "docker compose -f /opt/stacks/ebook-enricher/docker-compose.yml ps"
```

Expected: container `Up ... (healthy)`.

```bash
ssh plexypi "docker exec ebook-enricher wget -qO- http://localhost:8000/health"
```

Expected: `{"status":"ok"}`

- [ ] **Step 6: Verify qBit can reach the enricher by service name**

```bash
ssh plexypi "docker exec qbittorrent curl -sf http://ebook-enricher:8000/health"
```

Expected: `{"status":"ok"}`

If DNS resolution fails (common with gluetun), fall back by adding `ebook-enricher` to gluetun's `extra_hosts` via the container IP:

```bash
# Only if the above curl fails:
ssh plexypi "docker inspect ebook-enricher --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"
# Then manually add `<ip>    ebook-enricher` to the qBittorrent/gluetun container's hosts file,
# or add `extra_hosts:` to the gluetun compose service and restart.
```

- [ ] **Step 7: Commit and push**

```bash
cd ~/projects/ebook-enricher
# Nothing to commit here unless network name had to be changed.
git status
```

If network name differed:

```bash
git add docker-compose.yml
git commit -m "Update network name to match plexypi stack"
```

---

## Task 8: Update qBittorrent Autorun Script

**Files:**
- Modify on plexypi: `/opt/stacks/plexypi/qbittorrent/config/hardlink-ebooks.sh` — renamed to `process-ebook.sh`
- Modify on plexypi: `/opt/stacks/plexypi/qbittorrent/config/config/qBittorrent.conf` — `[AutoRun]` section

- [ ] **Step 1: Create the new script file**

```bash
ssh plexypi "cat > /opt/stacks/plexypi/qbittorrent/config/process-ebook.sh" <<'EOF'
#!/bin/bash
# qBittorrent autorun: copy ebook torrents to Syncthing folder and
# trigger metadata enrichment. Replaces hardlink-ebooks.sh — we use
# copies (not hardlinks) so metadata edits on the synced copy don't
# corrupt the seeding torrent.
#
# Called with: %G (tags) %D (save path) %F (content path) %N (name)

TAGS="$1"
SAVE_PATH="$2"
CONTENT_PATH="$3"
NAME="$4"

SYNC_BASE="/data/media/ebooks"
SEED_BASE="/data/torrents/ebooks"
ENRICHER_URL="http://ebook-enricher:8000/enrich"

case "$TAGS" in
    *ebook*) ;;
    *) exit 0 ;;
esac

case "$SAVE_PATH" in
    ${SEED_BASE}*) ;;
    *) exit 0 ;;
esac

REL_SUB="${SAVE_PATH#$SEED_BASE}"
SYNC_DIR="${SYNC_BASE}${REL_SUB}"

trigger_enrich() {
    local file="$1"
    case "$file" in
        *.epub)
            curl -sf -m 30 -X POST "$ENRICHER_URL" \
                -H 'Content-Type: application/json' \
                -d "{\"path\":\"$file\"}" > /dev/null 2>&1 || true
            ;;
    esac
}

copy_and_enrich() {
    local src="$1" dst="$2"
    if [ ! -f "$dst" ]; then
        cp "$src" "$dst"
        trigger_enrich "$dst"
    fi
}

if [ -f "$CONTENT_PATH" ]; then
    FILENAME=$(basename "$CONTENT_PATH")
    mkdir -p "$SYNC_DIR"
    copy_and_enrich "$CONTENT_PATH" "$SYNC_DIR/$FILENAME"
elif [ -d "$CONTENT_PATH" ]; then
    find "$CONTENT_PATH" -type f | while IFS= read -r file; do
        REL_FILE="${file#$SAVE_PATH/}"
        TARGET="$SYNC_DIR/$REL_FILE"
        TARGET_DIR=$(dirname "$TARGET")
        mkdir -p "$TARGET_DIR"
        copy_and_enrich "$file" "$TARGET"
    done
fi
EOF
```

- [ ] **Step 2: Make it executable**

```bash
ssh plexypi "chmod +x /opt/stacks/plexypi/qbittorrent/config/process-ebook.sh"
```

- [ ] **Step 3: Update qBittorrent.conf AutoRun pointer**

The existing `[AutoRun]` line is:

```
program=/config/hardlink-ebooks.sh \"%G\" \"%D\" \"%F\" \"%N\"
```

Replace with:

```bash
ssh plexypi "sed -i 's|hardlink-ebooks.sh|process-ebook.sh|' /opt/stacks/plexypi/qbittorrent/config/config/qBittorrent.conf"
```

Verify:

```bash
ssh plexypi "grep 'program=' /opt/stacks/plexypi/qbittorrent/config/config/qBittorrent.conf"
```

Expected: `program=/config/process-ebook.sh ...`

- [ ] **Step 4: Restart qBittorrent so it re-reads the config**

```bash
ssh plexypi "docker restart qbittorrent"
```

Wait ~30 seconds for gluetun + qBit healthcheck:

```bash
ssh plexypi "docker ps --filter name=qbittorrent --format '{{.Status}}'"
```

Expected: `Up ... (healthy)`

- [ ] **Step 5: Leave the old script in place for one cycle**

Keep `hardlink-ebooks.sh` on disk (untouched) so a rollback is trivial. Delete it only after the new flow is proven:

```bash
# Do NOT run this yet:
# ssh plexypi "rm /opt/stacks/plexypi/qbittorrent/config/hardlink-ebooks.sh"
```

---

## Task 9: End-to-End Manual Test

**No files to change.** This is a smoke test of the full pipeline.

- [ ] **Step 1: Pick a test book and trigger enrichment manually**

Find an existing ebook without series metadata:

```bash
ssh plexypi "python3 -c \"
import zipfile
with zipfile.ZipFile('/mnt/data/media/ebooks/Enshittification - Cory Doctorow.epub') as z:
    for name in z.namelist():
        if name.endswith('.opf'):
            content = z.read(name).decode()
            print('series:' in content.lower() or 'calibre:series' in content)
            break
\""
```

If this prints `False`, it's a good test candidate.

- [ ] **Step 2: Call /enrich on that file**

```bash
ssh plexypi "docker exec ebook-enricher curl -sS -X POST http://localhost:8000/enrich \
    -H 'Content-Type: application/json' \
    -d '{\"path\":\"/data/media/ebooks/Enshittification - Cory Doctorow.epub\"}'"
```

Expected: JSON response with `"status": "enriched"` or `"status": "no_match"` or `"status": "low_confidence"`. Log the response.

- [ ] **Step 3: Verify the EPUB metadata was updated (if enriched)**

```bash
ssh plexypi "python3 -c \"
import zipfile
with zipfile.ZipFile('/mnt/data/media/ebooks/Enshittification - Cory Doctorow.epub') as z:
    for name in z.namelist():
        if name.endswith('.opf'):
            content = z.read(name).decode()
            import re
            for m in re.finditer(r'<meta name=\\\"calibre:[^/]*/>', content):
                print(m.group())
            for m in re.finditer(r'<dc:description.*?</dc:description>', content, re.S):
                print(m.group()[:200])
            break
\""
```

Expected: Calibre series meta tags visible (if the book had a series and Hardcover matched), and a description.

- [ ] **Step 4: Confirm the seeding copy is untouched**

```bash
ssh plexypi "stat -c '%h %Y %n' '/mnt/data/torrents/ebooks/Enshittification - Cory Doctorow.epub' '/mnt/data/media/ebooks/Enshittification - Cory Doctorow.epub'"
```

Expected:
- Seeding copy `%h` (link count) = 1 OR 2, with its original mtime.
- Synced copy: if it was still a hardlink, both inodes match. If Task 8 / a re-download produced a copy, they diverge. **Important**: for an existing hardlinked book being enriched in place, the edit mutates the file contents, which also mutates the seeding file. This is the "hardlinks → copies is a one-way door" consequence noted in the spec. Do not enrich existing hardlinked books until they are converted to copies (see Step 5).

- [ ] **Step 5: Convert existing hardlinks in Syncthing folder to copies before backfill**

Backfill will edit every matched EPUB. Existing hardlinks between seed and sync copies must be broken first, otherwise enrichment corrupts seeding torrents.

```bash
ssh plexypi "find /mnt/data/media/ebooks -type f -name '*.epub' -links +1 -printf '%p\n' | while read f; do
    tmp=\$(mktemp --tmpdir=\"\$(dirname \"\$f\")\")
    cp --preserve=all \"\$f\" \"\$tmp\"
    mv \"\$tmp\" \"\$f\"
done"
```

Verify all ebooks in the sync folder now have link count 1:

```bash
ssh plexypi "find /mnt/data/media/ebooks -type f -name '*.epub' -links +1 | wc -l"
```

Expected: `0`

- [ ] **Step 6: Run backfill against the whole collection**

```bash
ssh plexypi "docker exec ebook-enricher curl -sS -X POST --max-time 1200 http://localhost:8000/backfill"
```

Expected: JSON summary after a few minutes, e.g. `{"total": 277, "enriched": 180, "skipped": 10, "no_match": 50, "low_confidence": 30, "rate_limited": 0, "errors": 7}`. Numbers will vary.

- [ ] **Step 7: Verify Syncthing picks up changes**

```bash
ssh plexypi "docker exec syncthing curl -s -H 'X-API-Key: KcbmeJfEnk6cAPCHHG6GsFrFpXMvCXxZ' 'http://localhost:8384/rest/db/status?folder=9vq6c-9skem'" | python3 -c "import sys,json;d=json.load(sys.stdin);print('state:',d['state'],'needFiles:',d['needFiles'])"
```

Expected: state may be `syncing` briefly while the Kindle pulls updated files, then back to `idle`.

- [ ] **Step 8: Verify on Kindle (manual)**

On the Kindle, open KOReader's history/library. Pick a book that was enriched. Confirm the series name and description now appear in the book's details view.

- [ ] **Step 9: Trigger a fresh download via RSS auto-download**

Wait for or force an RSS item to match and download. When the torrent completes, verify in qBit logs:

```bash
ssh plexypi "docker logs qbittorrent 2>&1 | tail -30 | grep -E 'process-ebook|enrich|Complete'"
```

Expected: script invocation visible. Confirm the new EPUB in `/mnt/data/media/ebooks/` has enriched metadata via a repeat of Step 3.

- [ ] **Step 10: Clean up the old script**

Only after Steps 2-9 pass:

```bash
ssh plexypi "rm /opt/stacks/plexypi/qbittorrent/config/hardlink-ebooks.sh"
```

- [ ] **Step 11: Final commit**

```bash
cd ~/projects/ebook-enricher
git log --oneline
```

Confirm all tasks have clean commits. If any README updates or small cleanups came out of testing, commit them:

```bash
git add README.md
git commit -m "Document deploy lessons from first run"
```

---

## Task 10: Memory Note

**Files:**
- Modify: `~/.claude/projects/-/memory/MEMORY.md`
- Create: `~/.claude/projects/-/memory/project_ebook_enricher.md`

- [ ] **Step 1: Create project memory file**

`~/.claude/projects/-/memory/project_ebook_enricher.md`:

```markdown
---
name: Ebook Metadata Enricher
description: FastAPI sidecar that enriches EPUB metadata via Hardcover GraphQL when qBit finishes a download
type: project
---

## ebook-enricher (plexypi)

- **Repo**: `~/projects/ebook-enricher/`
- **Deploy**: `/opt/stacks/ebook-enricher/` on plexypi
- **Image**: `python:3-alpine` based, FastAPI + uvicorn
- **Network**: joins `plexypi_default` so qBit can reach it as `ebook-enricher:8000`
- **Trigger**: qBit autorun `process-ebook.sh` (replaced `hardlink-ebooks.sh`) copies ebook to Syncthing folder then POSTs to `/enrich`
- **Important**: hardlinks → copies is one-way; once enriched, sync copy has its own inode. Cannot re-pack into torrent.
- **Backfill**: `docker exec ebook-enricher curl -sS -X POST http://localhost:8000/backfill`
- **Confidence gate**: requires fuzzy match ≥80% on both title and author, else skipped (avoids mis-tagging)
- **Fields written**: `calibre:series`, `calibre:series_index`, `dc:description`, `dc:subject` — only if currently empty
- **Fields never written**: title, author, ISBN, cover
- **Token**: `HARDCOVER_TOKEN` in `/opt/stacks/ebook-enricher/.env`, rotates yearly on Jan 1

## Spec & Plan
- Spec: `~/projects/ebook-enricher/docs/superpowers/specs/2026-04-16-ebook-metadata-enricher-design.md`
- Plan: `~/projects/ebook-enricher/docs/superpowers/plans/2026-04-16-ebook-metadata-enricher.md`
```

- [ ] **Step 2: Add pointer to MEMORY.md**

Add this line to `~/.claude/projects/-/memory/MEMORY.md` under a suitable section:

```markdown
## Ebook Metadata Enricher
- See [project_ebook_enricher.md](project_ebook_enricher.md) — FastAPI sidecar enriches EPUB metadata via Hardcover after qBit downloads; requires fuzzy match ≥80% to avoid mis-tagging
```

- [ ] **Step 3: No commit needed**

Memory files are not in a git repo.
