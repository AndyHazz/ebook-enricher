"""Shared format selection logic for ebook pipeline and cleanup.

Used by both process-ebook.py (live qBit autorun) and
cleanup-duplicates.py (one-shot existing-library cleanup). Same code
path on both sides guarantees identical grouping/selection rules.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


# Highest preference first. New formats can be appended.
PREFERENCE_CHAIN: tuple[str, ...] = (
    "epub", "azw3", "mobi", "pdf", "lit", "txt", "cbz", "cbr",
)


def is_ebook_ext(ext: str) -> bool:
    """True if `ext` is an ebook format we manage. Case-insensitive.
    Accepts forms like 'epub', '.epub', '.EPUB'."""
    return ext.lower().lstrip(".") in PREFERENCE_CHAIN
