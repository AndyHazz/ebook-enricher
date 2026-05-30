#!/usr/bin/env python3
"""One-shot: scan a sync folder, find duplicate-format groups, and
optionally delete the dominated formats. Uses the SAME grouping/
selection logic as the live pipeline (process-ebook.py) via the
shared format_selector module.

By default runs dry: lists what it would delete, makes no changes.
Pass --commit to actually unlink files.

Hard refuses to operate outside /data/media/ebooks unless --allow-root
explicitly authorises a different parent (used by tests).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from format_selector import group_by_book, is_ebook_ext, pick_best


SAFE_ROOT_DEFAULT = Path("/data/media/ebooks")


def _is_under(path: Path, parent: Path) -> bool:
    """True iff path is the same as parent or strictly within it.
    Uses resolved paths to defeat symlink escape."""
    path_r = path.resolve()
    parent_r = parent.resolve()
    try:
        path_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="Library root to clean")
    ap.add_argument(
        "--commit", action="store_true",
        help="Actually delete files (default: dry-run)",
    )
    ap.add_argument(
        "--allow-root", type=Path, default=SAFE_ROOT_DEFAULT,
        help="Override the safe-root assertion (default: /data/media/ebooks)",
    )
    args = ap.parse_args()

    if not _is_under(args.root, args.allow_root):
        print(
            f"refusing: {args.root} is not under safe root {args.allow_root}",
            file=sys.stderr,
        )
        return 2

    if not args.root.exists():
        print(f"root does not exist: {args.root}", file=sys.stderr)
        return 2

    files = [p for p in args.root.rglob("*") if p.is_file() and is_ebook_ext(p.suffix)]
    groups = group_by_book(files)
    multi = {k: v for k, v in groups.items() if len(v) > 1}

    total_losers = 0
    total_bytes = 0
    for group in multi.values():
        keeper, losers = pick_best(group)
        print(f"keep:   {keeper}")
        for loser in losers:
            if not _is_under(loser, args.allow_root):
                raise RuntimeError(f"loser escaped safe root: {loser}")
            if not keeper.exists():
                raise RuntimeError(f"keeper missing before delete: {keeper}")
            size = loser.stat().st_size
            total_losers += 1
            total_bytes += size
            print(f"delete: {loser}  ({size} bytes)")

    print()
    print(
        f"Summary: {len(multi)} duplicate groups, "
        f"{total_losers} files, {total_bytes // (1024 * 1024)} MB"
    )

    if not args.commit:
        print("(dry-run; use --commit to delete)")
        return 0

    for group in multi.values():
        keeper, losers = pick_best(group)
        for loser in losers:
            os.unlink(loser)
    print("committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
