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
