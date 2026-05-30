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
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

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


ENRICH_TIMEOUT_S = 30


def _post_enrich(enricher_url: str, file_path: Path) -> None:
    """POST {"path": str} to enricher_url. Logs failures, never raises."""
    body = json.dumps({"path": str(file_path)}).encode()
    req = Request(
        enricher_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=ENRICH_TIMEOUT_S) as resp:
            if resp.status != 200:
                print(
                    f"  enricher returned HTTP {resp.status} for {file_path}",
                    file=sys.stderr,
                )
    except URLError as e:
        print(f"  enricher unreachable: {e}", file=sys.stderr)
    except Exception as e:  # broad: enricher must never block pipeline
        print(f"  enricher call failed: {type(e).__name__}: {e}", file=sys.stderr)


def _apply_perms_from_parent(dest: Path) -> None:
    """Copy mode/uid/gid from dest.parent so the new file matches the
    surrounding convention (typically 664 docker:users)."""
    st = os.stat(dest.parent)
    try:
        os.chown(dest, st.st_uid, st.st_gid)
    except PermissionError:
        pass  # non-root tests can't chown; production runs as root
    os.chmod(dest, st.st_mode & 0o777 & ~0o111)  # strip exec bits


def _publish_ebook(
    keeper: Path,
    dest: Path,
    staging_dir: Path,
    enricher_url: str,
) -> None:
    """Copy keeper to staging, enrich (if epub), atomic-rename to dest."""
    if keeper.resolve() == dest.resolve():
        raise ValueError(f"refusing to publish into source path: {keeper}")
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_path = staging_dir / (uuid.uuid4().hex + keeper.suffix)
    shutil.copy2(keeper, staging_path)

    if keeper.suffix.lower() == ".epub":
        _post_enrich(enricher_url, staging_path)

    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging_path, dest)
    _apply_perms_from_parent(dest)


def _passthrough(src: Path, dest: Path) -> None:
    """Copy non-ebook file directly (no staging, no enrich)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    _apply_perms_from_parent(dest)


def _sweep_staging(staging_dir: Path, max_age_s: int = 86_400) -> None:
    """Delete stale files in .staging (orphans from killed runs)."""
    if not staging_dir.exists():
        return
    cutoff = time.time() - max_age_s
    for p in staging_dir.iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            try:
                p.unlink()
            except OSError:
                pass


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
        return 0

    try:
        args.source.resolve().relative_to(args.save_path.resolve())
    except ValueError:
        print(
            f"source ({args.source}) is not under save-path ({args.save_path})",
            file=sys.stderr,
        )
        return 2

    staging_dir = args.sync_base / args.staging_subdir
    _sweep_staging(staging_dir)

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

    for keeper, dest, _losers in ebook_jobs:
        _publish_ebook(keeper, dest, staging_dir, args.enricher_url)
    for src, dest in passthrough_jobs:
        _passthrough(src, dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
