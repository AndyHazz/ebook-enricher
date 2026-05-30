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
    "epub", "azw3", "mobi", "pdf", "lit", "txt", "cbz", "cbr", "lrf", "rtf",
)


def is_ebook_ext(ext: str) -> bool:
    """True if `ext` is an ebook format we manage. Case-insensitive.
    Accepts forms like 'epub', '.epub', '.EPUB'."""
    return ext.lower().lstrip(".") in PREFERENCE_CHAIN


def pick_best(
    group: list[Path],
    chain: tuple[str, ...] = PREFERENCE_CHAIN,
) -> tuple[Path, list[Path]]:
    """Return (keeper, losers) for one group.

    Keeper is the file with the highest-priority format in `chain`.
    If multiple files share the keeper's format, the larger file wins
    (heuristic for "higher quality version") and the rest become losers.
    """
    if not group:
        raise ValueError("pick_best called with empty group")

    # Bucket files by their normalised extension.
    by_ext: dict[str, list[Path]] = {}
    for p in group:
        ext = p.suffix.lstrip(".").lower()
        by_ext.setdefault(ext, []).append(p)

    # Walk the chain in priority order; first match wins.
    for ext in chain:
        if ext in by_ext:
            candidates = by_ext[ext]
            if len(candidates) == 1:
                keeper = candidates[0]
            else:
                # Tie-break: largest file wins.
                keeper = max(candidates, key=lambda p: p.stat().st_size)
            losers = [p for p in group if p != keeper]
            return keeper, losers

    # Group contained only unknown extensions — caller filtered wrong.
    raise ValueError(
        f"pick_best: no known ebook extension in {[p.name for p in group]}"
    )


def group_by_book(
    paths: Iterable[Path],
) -> dict[tuple[Path, str], list[Path]]:
    """Group ebook files by (parent_dir, filename_stem).

    Non-ebook extensions are silently filtered out. Returns
    {(dir, stem): [path1, path2, ...]}. Caller decides what to do
    with non-ebook files (typically: copy them through as-is).
    """
    groups: dict[tuple[Path, str], list[Path]] = {}
    for p in paths:
        ext = p.suffix.lstrip(".").lower()
        if ext not in PREFERENCE_CHAIN:
            continue
        key = (p.parent, p.stem)
        groups.setdefault(key, []).append(p)
    return groups
