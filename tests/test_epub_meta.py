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
