#!/usr/bin/env python3
"""qBittorrent autorun helper: pick one format per book, enrich the
staging copy, and atomically publish into the Syncthing folder.

Called by /config/process-ebook.sh after tag/path validation.

CLI:
    process-ebook.py --source <CONTENT_PATH> --save-path <SAVE_PATH>
                     --sync-base /data/media/ebooks
                     --enricher-url http://ebook-enricher:8000/enrich
                     [--staging-subdir .staging]
                     [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from format_selector import PREFERENCE_CHAIN, group_by_book, is_ebook_ext, pick_best


def collect_files(source: Path) -> list[Path]:
    """Return every file under source (recursive). source may be a
    single file or a directory."""
    if source.is_file():
        return [source]
    return sorted(p for p in source.rglob("*") if p.is_file())


def plan_actions(
    source: Path,
    save_path: Path,
    sync_base: Path,
) -> tuple[list[tuple[Path, Path, list[Path]]], list[tuple[Path, Path]]]:
    """Return (ebook_jobs, passthrough_jobs).

    ebook_jobs: [(keeper_src, dest_path, losers)]
    passthrough_jobs: [(src, dest_path)]
    """
    files = collect_files(source)
    ebooks = [f for f in files if is_ebook_ext(f.suffix)]
    others = [f for f in files if not is_ebook_ext(f.suffix)]

    ebook_jobs: list[tuple[Path, Path, list[Path]]] = []
    for group in group_by_book(ebooks).values():
        keeper, losers = pick_best(group)
        dest = sync_base / keeper.relative_to(save_path)
        ebook_jobs.append((keeper, dest, losers))

    passthrough_jobs = [(f, sync_base / f.relative_to(save_path)) for f in others]
    return ebook_jobs, passthrough_jobs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--save-path", type=Path, required=True)
    ap.add_argument("--sync-base", type=Path, required=True)
    ap.add_argument("--enricher-url", required=True)
    ap.add_argument("--staging-subdir", default=".staging")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.source.exists():
        print(f"source does not exist: {args.source}", file=sys.stderr)
        return 0  # no-op, matches current behaviour

    ebook_jobs, passthrough_jobs = plan_actions(
        args.source, args.save_path, args.sync_base
    )

    for keeper, dest, losers in ebook_jobs:
        print(f"keep: {keeper.name} -> {dest}")
        for loser in losers:
            print(f"  skip (lower priority): {loser.name}")
    for src, dest in passthrough_jobs:
        print(f"passthrough: {src.name} -> {dest}")

    if args.dry_run:
        return 0

    # Real-mode copy/enrich/rename comes in Task 6.
    raise NotImplementedError("real-mode not yet implemented; use --dry-run")


if __name__ == "__main__":
    sys.exit(main())
