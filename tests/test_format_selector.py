"""Tests for the format_selector module (used by both process-ebook.py
and cleanup-duplicates.py)."""
from pathlib import Path

import pytest

from format_selector import (
    PREFERENCE_CHAIN,
    is_ebook_ext,
    group_by_book,
    pick_best,
)


def test_preference_chain_order():
    """epub is highest, rtf is lowest (lrf and rtf are dead formats kept
    as last-resort fallbacks so they still get deduped when paired with
    a better format)."""
    assert PREFERENCE_CHAIN[0] == "epub"
    assert PREFERENCE_CHAIN[1] == "azw3"
    assert PREFERENCE_CHAIN[2] == "mobi"
    assert PREFERENCE_CHAIN[3] == "pdf"
    assert PREFERENCE_CHAIN[-2] == "lrf"
    assert PREFERENCE_CHAIN[-1] == "rtf"


def test_is_ebook_ext_known():
    """All chain entries are recognised, case-insensitive, with or without leading dot."""
    for ext in PREFERENCE_CHAIN:
        assert is_ebook_ext(ext) is True
        assert is_ebook_ext("." + ext) is True
        assert is_ebook_ext(ext.upper()) is True


def test_is_ebook_ext_unknown():
    """Non-ebook extensions return False."""
    assert is_ebook_ext("jpg") is False
    assert is_ebook_ext(".opf") is False
    assert is_ebook_ext("") is False
    assert is_ebook_ext("epubx") is False  # near miss


def test_group_by_book_same_dir_same_stem(tmp_path):
    """Two files with same stem in same dir form one group."""
    epub = tmp_path / "Ready Player One.epub"
    pdf = tmp_path / "Ready Player One.pdf"
    epub.touch()
    pdf.touch()
    groups = group_by_book([epub, pdf])
    assert len(groups) == 1
    assert set(next(iter(groups.values()))) == {epub, pdf}


def test_group_by_book_different_stems(tmp_path):
    """Files with different stems are separate groups even in same dir."""
    a = tmp_path / "BookA.epub"
    b = tmp_path / "BookB.epub"
    a.touch()
    b.touch()
    groups = group_by_book([a, b])
    assert len(groups) == 2


def test_group_by_book_different_dirs(tmp_path):
    """Same stem in different dirs are NOT grouped (intentional editions)."""
    d1 = tmp_path / "edition_one"
    d2 = tmp_path / "edition_two"
    d1.mkdir(); d2.mkdir()
    a = d1 / "Book.epub"
    b = d2 / "Book.epub"
    a.touch()
    b.touch()
    groups = group_by_book([a, b])
    assert len(groups) == 2


def test_group_by_book_only_ebook_extensions(tmp_path):
    """Non-ebook extensions are not grouped (cover.jpg etc.)."""
    epub = tmp_path / "Book.epub"
    cover = tmp_path / "Book.jpg"
    epub.touch()
    cover.touch()
    groups = group_by_book([epub, cover])
    assert len(groups) == 1
    assert next(iter(groups.values())) == [epub]


def test_group_by_book_empty():
    """Empty input yields empty dict."""
    assert group_by_book([]) == {}


def test_pick_best_epub_wins_over_pdf(tmp_path):
    """EPUB beats PDF."""
    epub = tmp_path / "Book.epub"
    pdf = tmp_path / "Book.pdf"
    epub.write_bytes(b"x" * 100)
    pdf.write_bytes(b"x" * 999_999)  # PDF larger but EPUB still wins
    keeper, losers = pick_best([epub, pdf])
    assert keeper == epub
    assert losers == [pdf]


def test_pick_best_falls_back_to_mobi(tmp_path):
    """No EPUB present — chain falls back to next available format."""
    mobi = tmp_path / "Book.mobi"
    pdf = tmp_path / "Book.pdf"
    mobi.write_bytes(b"x")
    pdf.write_bytes(b"x")
    keeper, losers = pick_best([mobi, pdf])
    assert keeper == mobi
    assert losers == [pdf]


def test_pick_best_full_chain_priority(tmp_path):
    """All formats present — epub wins, others lose in chain order."""
    files = []
    for ext in ("pdf", "epub", "mobi", "azw3", "txt"):
        p = tmp_path / f"Book.{ext}"
        p.write_bytes(b"x")
        files.append(p)
    keeper, losers = pick_best(files)
    assert keeper.suffix == ".epub"
    assert len(losers) == 4


def test_pick_best_single_file(tmp_path):
    """Single-file group returns (file, [])."""
    f = tmp_path / "Solo.epub"
    f.touch()
    keeper, losers = pick_best([f])
    assert keeper == f
    assert losers == []


def test_pick_best_tiebreak_keeps_larger(tmp_path):
    """Two files with same winning format — larger wins, smaller loses."""
    a = tmp_path / "Book_v1.epub"
    b = tmp_path / "Book_v2.epub"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"x" * 200)
    keeper, losers = pick_best([a, b])
    assert keeper == b   # larger wins
    assert losers == [a]


def test_pick_best_empty_raises():
    """Empty group is a caller bug."""
    with pytest.raises(ValueError):
        pick_best([])


def test_pick_best_uses_lowercase_ext(tmp_path):
    """Extension casing doesn't matter (FAT-filesystem fixtures)."""
    epub_upper = tmp_path / "Book.EPUB"
    mobi = tmp_path / "Book.mobi"
    epub_upper.write_bytes(b"x")
    mobi.write_bytes(b"x")
    keeper, losers = pick_best([epub_upper, mobi])
    assert keeper == epub_upper
    assert losers == [mobi]
