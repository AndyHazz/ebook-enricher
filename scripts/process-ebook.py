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


# Filenames matching these glob patterns are Syncthing/system internals that
# must never be propagated from a torrent into the sync folder. Most are
# Syncthing's incomplete-download cache or temp files; .stfolder is the
# folder-marker file Syncthing creates per shared folder.
_SKIP_FILENAME_PATTERNS: tuple[str, ...] = (
    "*.parts",          # Syncthing partial-download block maps
    ".syncthing.*.tmp", # Syncthing in-flight temp files
    ".stfolder",
)

_SKIP_PARENT_DIRS: frozenset[str] = frozenset({".stversions", ".staging"})


def _is_skip_file(path: Path) -> bool:
    """True if this file should be silently skipped during passthrough.
    Catches Syncthing internals and our own staging dir."""
    if any(part in _SKIP_PARENT_DIRS for part in path.parts):
        return True
    name = path.name
    return any(path.match(pat) or _glob_match(name, pat) for pat in _SKIP_FILENAME_PATTERNS)


def _glob_match(name: str, pattern: str) -> bool:
    """fnmatch-style glob on a single name (not full path)."""
    import fnmatch
    return fnmatch.fnmatchcase(name, pattern)


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
    files = [f for f in collect_files(source) if not _is_skip_file(f)]
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

    # The enricher writes the displaced original cover as a sidecar named
    # <staging_stem>.original.jpg next to the staging EPUB (see
    # cover.save_sidecar_if_absent). Relocate it alongside the published
    # book as <book>.original.jpg — where the enricher's own in-place runs
    # put it — instead of orphaning it in .staging. Only the .epub keeper
    # is enriched, so only it can have a sidecar.
    if keeper.suffix.lower() == ".epub":
        _relocate_sidecar(staging_dir, staging_path, dest)


def _relocate_sidecar(staging_dir: Path, staging_path: Path, dest: Path) -> None:
    """Move the enricher's staging sidecar to <book>.original.jpg next to
    dest. No-op if no sidecar was written. If a sidecar already exists at
    the dest, drop the staging orphan rather than overwrite — the existing
    sidecar holds the authoritative original."""
    sidecar_src = staging_dir / (staging_path.stem + ".original.jpg")
    if not sidecar_src.exists():
        return
    sidecar_dest = dest.parent / (dest.stem + ".original.jpg")
    if sidecar_dest.exists():
        sidecar_src.unlink()
        return
    os.replace(sidecar_src, sidecar_dest)
    _apply_perms_from_parent(sidecar_dest)


def _passthrough(src: Path, dest: Path) -> None:
    """Copy non-ebook file directly (no staging, no enrich).

    Defensive against legacy hardlinks between torrent seed and sync
    folder (older pipelines used `ln` instead of `cp`). If src and
    dest resolve to the same inode, log and skip rather than crashing
    on shutil.SameFileError.
    """
    if dest.exists() and src.stat().st_ino == dest.stat().st_ino:
        print(
            f"  skip passthrough: {src.name} (hardlink to dest already)",
            file=sys.stderr,
        )
        return
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


def _load_ledger(ledger_path: Path) -> set[str]:
    """Load the copy-once ledger — the set of torrent-relative paths that
    have ever been published. Returns an empty set if the file is missing
    or unreadable (a corrupt ledger must not block publishing entirely)."""
    try:
        with open(ledger_path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def _save_ledger(ledger_path: Path, ledger: set[str]) -> None:
    """Write the ledger atomically (temp file + rename) so a crash mid-write
    can't truncate it. Sorted for stable diffs / human readability."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(sorted(ledger), f, indent=0)
    os.replace(tmp, ledger_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--save-path", type=Path, required=True)
    ap.add_argument("--sync-base", type=Path, required=True)
    ap.add_argument("--enricher-url", required=True)
    ap.add_argument("--staging-subdir", default=".staging")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="republish even if already published per the ledger / present "
             "at dest (escape hatch; also re-records in the ledger)",
    )
    ap.add_argument(
        "--ledger-path",
        type=Path,
        default=None,
        help="JSON copy-once ledger of torrent-relative paths ever published. "
             "Default: <sync-base>/.published-ledger.json. Keep it OUTSIDE "
             "the synced tree in production so it doesn't propagate to devices.",
    )
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

    if not args.sync_base.is_dir():
        print(
            f"sync-base does not exist or is not a directory: {args.sync_base}",
            file=sys.stderr,
        )
        return 2

    staging_dir = args.sync_base / args.staging_subdir
    _sweep_staging(staging_dir)

    ledger_path = args.ledger_path or (args.sync_base / ".published-ledger.json")
    ledger = _load_ledger(ledger_path)

    ebook_jobs, passthrough_jobs = plan_actions(
        args.source, args.save_path, args.sync_base
    )

    # Copy-once ledger. A book is keyed by its torrent-relative path and
    # published exactly once, ever. The ledger is consulted regardless of
    # whether the dest currently exists, so deleting / renaming / moving a
    # book on the sync side is permanent — a later torrent recheck (which
    # re-fires this autorun over the WHOLE torrent content) will NOT
    # resurrect it. Three outcomes per candidate:
    #
    #   in ledger          -> skip (copy-once; honours deletes/renames)
    #   not in ledger, but
    #     dest exists       -> skip + record (backfill: it's already here,
    #                          so treat it as published — covers the
    #                          existing library on first run after deploy)
    #     dest missing      -> publish + record (genuinely new book)
    #
    # --overwrite bypasses both checks and re-records.
    def _rel(p: Path) -> str:
        return str(p.relative_to(args.save_path))

    kept_ebooks, kept_passthrough = [], []
    for keeper, dest, losers in ebook_jobs:
        key = _rel(keeper)
        if not args.overwrite and key in ledger:
            print(f"skip (copy-once, already published): {key}")
        elif not args.overwrite and dest.exists():
            print(f"skip (already present, recording): {key}")
            ledger.add(key)
        else:
            kept_ebooks.append((keeper, dest, losers))
            ledger.add(key)
    for src, dest in passthrough_jobs:
        key = _rel(src)
        if not args.overwrite and key in ledger:
            print(f"skip (copy-once, already published): {key}")
        elif not args.overwrite and dest.exists():
            print(f"skip (already present, recording): {key}")
            ledger.add(key)
        else:
            kept_passthrough.append((src, dest))
            ledger.add(key)
    ebook_jobs, passthrough_jobs = kept_ebooks, kept_passthrough

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

    _save_ledger(ledger_path, ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
