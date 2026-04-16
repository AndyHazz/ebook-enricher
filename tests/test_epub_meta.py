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
