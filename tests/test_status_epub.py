import zipfile
from pathlib import Path

from ebook_enricher.status_epub import (
    STATUS_FILENAME,
    clear_status_epub,
    write_status_epub,
)


def test_write_creates_epub(tmp_path: Path):
    out = write_status_epub(
        tmp_path,
        title="⚠️ Hardcover API unreachable",
        body="The service couldn't reach api.hardcover.app.\nCheck plexypi's internet connection.",
    )
    assert out == tmp_path / STATUS_FILENAME
    assert out.exists()

    with zipfile.ZipFile(out) as zf:
        # Mimetype must be first and stored
        infos = zf.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        # OPF and content present
        opf = zf.read("OEBPS/content.opf").decode()
        assert "⚠️ Hardcover API unreachable" in opf
        chapter = zf.read("OEBPS/status.xhtml").decode()
        assert "couldn't reach" in chapter
        assert "Check plexypi" in chapter


def test_write_overwrites_existing(tmp_path: Path):
    write_status_epub(tmp_path, title="First", body="First body")
    write_status_epub(tmp_path, title="Second", body="Second body")
    with zipfile.ZipFile(tmp_path / STATUS_FILENAME) as zf:
        opf = zf.read("OEBPS/content.opf").decode()
        assert "Second" in opf
        assert "First" not in opf


def test_clear_removes_file(tmp_path: Path):
    write_status_epub(tmp_path, title="x", body="x")
    assert (tmp_path / STATUS_FILENAME).exists()
    removed = clear_status_epub(tmp_path)
    assert removed is True
    assert not (tmp_path / STATUS_FILENAME).exists()


def test_clear_noop_when_absent(tmp_path: Path):
    removed = clear_status_epub(tmp_path)
    assert removed is False


def test_body_escapes_html(tmp_path: Path):
    write_status_epub(
        tmp_path,
        title="Test",
        body="error: <script>alert('x')</script>",
    )
    with zipfile.ZipFile(tmp_path / STATUS_FILENAME) as zf:
        chapter = zf.read("OEBPS/status.xhtml").decode()
    # The raw <script> tag must NOT appear in the output.
    assert "<script>" not in chapter
    # Escaped form should be there.
    assert "&lt;script&gt;" in chapter
