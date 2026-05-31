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


def test_read_modern_epub_property_style(modern_epub: Path):
    # EPUB 3 stores series via <meta property="calibre:series">X</meta>,
    # not <meta name="..." content="X"/>. read_meta must see both forms.
    meta = read_meta(modern_epub)
    assert meta.series == "Modern Series"
    assert meta.series_index == "3"


def test_write_updates_existing_property_style(modern_epub: Path):
    # If an EPUB already has property-style series, write_meta must update
    # it in place, NOT add a second name-style element alongside.
    write_meta(
        modern_epub,
        EpubMeta(
            title="",
            author="",
            series="Updated Series",
            series_index="5",
        ),
    )
    meta = read_meta(modern_epub)
    assert meta.series == "Updated Series"
    assert meta.series_index == "5"

    # Confirm no duplicate meta elements were added
    import zipfile
    from xml.etree import ElementTree as ET
    from ebook_enricher.epub_meta import NS, _find_opf_path
    with zipfile.ZipFile(modern_epub) as zf:
        root = ET.fromstring(zf.read(_find_opf_path(zf)))
    metadata = root.find("opf:metadata", NS)
    series_metas = [
        m for m in metadata.findall("opf:meta", NS)
        if m.attrib.get("name") == "calibre:series"
        or m.attrib.get("property") == "calibre:series"
    ]
    assert len(series_metas) == 1, f"Expected 1 series meta, got {len(series_metas)}"


def test_write_preserves_file_mode(bare_epub: Path, tmp_path: Path):
    # Simulate the Pi permissions: user-owned, group-readable (664).
    import os
    import stat
    os.chmod(bare_epub, 0o664)
    write_meta(
        bare_epub,
        EpubMeta(
            title="",
            author="",
            series="A Series",
            series_index="1",
        ),
    )
    # After the atomic rename, the file's mode must still be 664.
    # Without preservation, tempfile.mkstemp's 0600 would leak through.
    mode = stat.S_IMODE(bare_epub.stat().st_mode)
    assert mode == 0o664, f"Expected 0o664, got {oct(mode)}"


def test_write_preserves_mtime(bare_epub: Path):
    # The enricher rewrites the EPUB to inject metadata + cover. Without
    # mtime preservation, the rewrite bumps mtime to "now" and the file
    # jumps to the top of every Recently-Added view on downstream
    # devices (KOReader, Kindle). Capture the original mtime, write,
    # then assert it survived the atomic rename.
    import os
    orig_stat = bare_epub.stat()
    # Move mtime back to a known past value so we'd notice clearly if
    # write_meta clobbered it back to "now".
    past_atime = orig_stat.st_atime_ns - 7 * 24 * 3600 * 1_000_000_000
    past_mtime = orig_stat.st_mtime_ns - 7 * 24 * 3600 * 1_000_000_000
    os.utime(bare_epub, ns=(past_atime, past_mtime))

    write_meta(
        bare_epub,
        EpubMeta(title="", author="", series="A Series", series_index="1"),
    )

    after = bare_epub.stat()
    assert after.st_mtime_ns == past_mtime, (
        f"mtime not preserved: expected {past_mtime}, got {after.st_mtime_ns}"
    )


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


def test_read_meta_extracts_language(tmp_path):
    """read_meta returns the dc:language from OPF as meta.language."""
    import zipfile
    from ebook_enricher.epub_meta import read_meta
    from tests.conftest import MIMETYPE, CONTAINER_XML, NAV_XHTML

    # Build a minimal EPUB with dc:language = "en"
    opf = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">test-uid</dc:identifier>
    <dc:title>Test</dc:title>
    <dc:creator>Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine><itemref idref="nav"/></spine>
</package>
'''
    epub = tmp_path / "lang_test.epub"
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", NAV_XHTML)

    meta = read_meta(epub)
    assert meta.language == "en"


def test_read_meta_language_none_when_absent(tmp_path):
    """When OPF has no dc:language, meta.language is None."""
    import zipfile
    from ebook_enricher.epub_meta import read_meta
    from tests.conftest import MIMETYPE, CONTAINER_XML, NAV_XHTML

    epub = tmp_path / "no_lang.epub"
    opf = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">test-uid</dc:identifier>
    <dc:title>Test</dc:title>
    <dc:creator>Author</dc:creator>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine><itemref idref="nav"/></spine>
</package>
'''
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", NAV_XHTML)

    meta = read_meta(epub)
    assert meta.language is None
