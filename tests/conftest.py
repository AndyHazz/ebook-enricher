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


@pytest.fixture
def modern_epub(tmp_path: Path) -> Path:
    """EPUB 3 using property-style meta elements (how modern Calibre writes)."""
    extra = """
    <meta property="calibre:series">Modern Series</meta>
    <meta property="calibre:series_index">3</meta>
    """
    return _build_epub(tmp_path / "modern.epub", extra)


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
    return epub
