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
