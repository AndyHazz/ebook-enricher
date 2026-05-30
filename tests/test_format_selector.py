"""Tests for the format_selector module (used by both process-ebook.py
and cleanup-duplicates.py)."""
from pathlib import Path

import pytest

from format_selector import PREFERENCE_CHAIN, is_ebook_ext


def test_preference_chain_order():
    """epub is highest, cbr is lowest."""
    assert PREFERENCE_CHAIN[0] == "epub"
    assert PREFERENCE_CHAIN[1] == "azw3"
    assert PREFERENCE_CHAIN[2] == "mobi"
    assert PREFERENCE_CHAIN[3] == "pdf"
    assert PREFERENCE_CHAIN[-1] == "cbr"


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


from format_selector import group_by_book


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
