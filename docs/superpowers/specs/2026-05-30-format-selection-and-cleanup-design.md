# Ebook Format Selection and Cleanup — Design

**Date**: 2026-05-30
**Status**: Approved for implementation

## Problem

MyAnonamouse and other sources frequently ship books in multiple formats — a single torrent may contain `.epub`, `.pdf`, `.mobi`, and `.azw3` versions of the same book. The current `process-ebook.sh` copies every file in the torrent into the Syncthing folder, so the Kindle ends up with duplicates: three or four files for what should be one book.

Two consequences:

1. **Visible clutter on the Kindle.** Each format shows up as a separate item in KOReader's library view. The user has to know which one to open and ignore the rest.
2. **Wasted sync bandwidth and Kindle storage.** Every duplicate is copied to plexypi, replicated by Syncthing to laptop and Kindle, and eats into the Kindle's limited flash.

The current state has 13 books in the sync folder with multiple formats (e.g. Ready Player One has `.epub`, `.lit`, and `.mobi`; Magic for Beginners has `.epub`, `.mobi`, and `.pdf`). The pipeline keeps adding more whenever a multi-format torrent completes.

Separately, the existing copy-then-enrich flow has a smaller issue: Syncthing may briefly see the un-enriched file before the enricher rewrites it in place. Two sync events per book instead of one, with peers occasionally seeing the un-enriched version first.

## Goal

1. **Pipeline rule**: when a torrent contains multiple formats of the same book, only ONE format reaches the Syncthing folder — picked by a fixed preference chain with `.epub` highest.
2. **Enrich before publish**: the file appears in the Syncthing folder already enriched, so Syncthing sees a single create event with the final bytes.
3. **One-shot cleanup**: remove the existing 13 duplicate groups from the sync folder using the same logic.

## Non-goals

- Not changing what gets downloaded — qBit's RSS rules and tag handling stay as-is.
- Not deduplicating ACROSS directories — two copies of the same book in different folders are assumed intentional.
- Not deduplicating same-format-different-stem cases (`Book v1.epub` vs `Book.epub`) — these are usually intentionally different files.
- Not modifying the seed copies in `/data/torrents/ebooks/` — the seeding torrent must remain bit-for-bit intact.
- Not introducing a new persistent service. The enricher API stays unchanged; new logic lives in the qBit-side script.

## Constraints

- **Seed files must remain untouched.** Read-only access during `cp`, never `mv` from seed, never POST seed paths to the enricher.
- **Atomic file appearance in the sync folder.** Syncthing's filesystem watcher must never see a partial or un-enriched file.
- **No qBittorrent reconfiguration.** qBit still calls `process-ebook.sh` with the same arguments. The autorun wiring doesn't change.
- **Shared logic between live pipeline and cleanup.** The two paths must use the same grouping and selection rules so they can't drift apart.
- **Python 3 is already available** in the qBit container (we run `python3 -c ...` ad-hoc today). No new container-level dependencies.

---

## Architecture

Three new files, all under the qBit container's `/config` mount (`/opt/stacks/plexypi/qbittorrent/config/` on the host):

```
process-ebook.sh          (modified — thin entrypoint, ~30 lines)
process-ebook.py          (new   — pipeline logic, called by the .sh)
cleanup-duplicates.py     (new   — one-shot cleanup, runs ad-hoc)
format_selector.py        (new   — shared module imported by both .py files)
```

**Why this shape**: qBit's autorun calling convention stays exactly as-is (`process-ebook.sh %G %D %F %N`). The bash entrypoint becomes a thin pre-validator that shells out to Python. All the interesting logic — grouping, selection, atomic staging, enrich coordination — lives in Python where it's testable and free of bash quoting bugs.

```
qBit finishes download (tag includes "ebook")
   │
   ▼
process-ebook.sh  (validates tag + save_path, then:)
   │
   ▼
python3 process-ebook.py --source <CONTENT_PATH> --dest /data/media/ebooks/...
   │
   │  for each (rel_subdir, stem) group:
   │     1. pick best format using format_selector
   │     2. cp seed-file → /data/media/ebooks/.staging/<uuid>.<ext>
   │     3. POST {path: staging} to ebook-enricher:8000/enrich   (if .epub)
   │     4. os.rename(staging, /data/media/ebooks/<rel_path>)    (atomic)
   │  for each non-ebook file (cover.jpg, .opf, etc):
   │     cp seed-file → /data/media/ebooks/<rel_path>            (no staging needed)
   ▼
Syncthing's fsWatcher sees one create event per file (with enriched content)
```

---

## format_selector module

Pure, side-effect-free. Two functions and one constant.

```python
PREFERENCE_CHAIN = ("epub", "azw3", "mobi", "pdf", "lit", "txt", "cbz", "cbr")

def group_by_book(paths: Iterable[Path]) -> dict[tuple[Path, str], list[Path]]:
    """Group files by (parent_dir, filename_stem). Returns
    {(dir, stem): [path1, path2, ...]}. Non-ebook extensions are ignored
    for grouping but the caller can still copy them through separately."""

def pick_best(group: list[Path], chain=PREFERENCE_CHAIN) -> tuple[Path, list[Path]]:
    """Return (keeper, losers). Keeper is the highest-priority format
    in group. Losers are everything else in the group. If multiple files
    share the keeper's format (rare), tie-break on larger file size and
    log the discarded file."""
```

Both production and cleanup paths import these two functions. Same fixtures, same rules, no drift.

---

## process-ebook.py (pipeline)

CLI:
```
python3 process-ebook.py --source <CONTENT_PATH> --save-path <SAVE_PATH>
                         --sync-base /data/media/ebooks
                         --enricher-url http://ebook-enricher:8000/enrich
                         --staging-subdir .staging
```

**Destination path rule** (preserves the existing `process-ebook.sh` layout exactly):

```
dest = sync_base / (source_file.relative_to(save_path))
```

So `save_path=/data/torrents/ebooks` and a source file at `/data/torrents/ebooks/Cline/Ready Player One.epub` becomes `/data/media/ebooks/Cline/Ready Player One.epub`. For single-file torrents where `source` IS a file (not a dir), `source.relative_to(save_path)` yields just the filename — same result the bash script produces today.

Flow:

1. **Collect files.** If `source` is a single file, `files = [source]`. If `source` is a directory, `files = [p for p in source.rglob('*') if p.is_file()]`.
2. **Group ebooks.** `groups = group_by_book(f for f in files if f.suffix.lower().lstrip('.') in PREFERENCE_CHAIN)`. The remaining files (`cover.jpg`, `.opf`, etc.) go in a `passthrough` list.
3. **For each ebook group:**
   a. `keeper, losers = pick_best(group)`.
   b. Log losers (they'll never be copied).
   c. `dest_path = sync_base / keeper.relative_to(save_path)`.
   d. **Safety assertion**: `dest_path != keeper` AND `keeper` is under `save_path` (would otherwise mean trying to write back to the seed — refuse).
   e. Generate a staging path: `sync_base / staging_subdir / (uuid4().hex + keeper.suffix)`.
   f. `shutil.copy2(keeper, staging_path)` — preserves mtime, never touches `keeper`.
   g. If `keeper.suffix.lower() == ".epub"`: `httpx.post(enricher_url, json={"path": str(staging_path)}, timeout=30)`. Log non-200 and proceed (matches current "enrich failure doesn't block copy" behaviour).
   h. `os.makedirs(dest_path.parent, exist_ok=True)`; `os.replace(staging_path, dest_path)` — atomic rename within same filesystem.
4. **For each passthrough file (cover.jpg, .opf, etc.):**
   - `dest_path = sync_base / src.relative_to(save_path)`.
   - `os.makedirs(dest_path.parent, exist_ok=True)`; `shutil.copy2(src, dest_path)`. No staging — these don't need enrichment.

**Permissions**: after `os.replace`, copy permissions and ownership from the parent directory (`os.stat(dest.parent)`) and apply via `os.chmod` and `os.chown`. This matches the existing convention used everywhere else in `/data/media/ebooks/` (typically `664 docker:users`) without hardcoding numeric uid/gid that might drift.

---

## cleanup-duplicates.py (one-shot)

CLI:
```
python3 cleanup-duplicates.py /data/media/ebooks          # dry-run (default)
python3 cleanup-duplicates.py /data/media/ebooks --commit # actually delete
```

Flow:

1. `find` all ebook files under `/data/media/ebooks/`.
2. `group_by_book(files)` (same shared module).
3. For each group with `len > 1`:
   a. `keeper, losers = pick_best(group)`.
   b. Print: `keep: <keeper>  delete: <losers>` (one per line).
4. If `--commit`: for each loser, `os.unlink(loser)`.

**Safety**:
- Hard-coded `assert root.resolve().is_relative_to(Path('/data/media/ebooks'))` — refuses any other root.
- Skips groups with only one file (no-op safety).
- Before deleting a loser, asserts the keeper still exists and is readable.
- Never deletes a file whose path resolves outside the sync folder root (symlink-escape guard).

Syncthing's filesystem watcher catches the deletes and propagates to the Kindle and laptop within seconds.

---

## process-ebook.sh (modified)

Becomes a ~30-line wrapper:

```bash
#!/bin/bash
# qBittorrent autorun: pre-validate, then delegate to Python helper.
# Called with: %G (tags) %D (save path) %F (content path) %N (name)

TAGS="$1"
SAVE_PATH="$2"
CONTENT_PATH="$3"

case "$TAGS" in *ebook*) ;; *) exit 0 ;; esac
case "$SAVE_PATH" in /data/torrents/ebooks*) ;; *) exit 0 ;; esac

exec python3 /config/process-ebook.py \
    --source "$CONTENT_PATH" \
    --save-path "$SAVE_PATH" \
    --sync-base /data/media/ebooks \
    --enricher-url http://ebook-enricher:8000/enrich \
    --staging-subdir .staging
```

Existing comments about "we use copies, not hardlinks, so metadata edits don't corrupt the seeding torrent" stay — that contract is now enforced by Python rather than embedded in bash.

---

## Syncthing config change

Add to the ebooks folder's `.stignore` on **plexypi only** (other peers don't have a staging dir):

```
.staging/
```

This stops Syncthing from scanning the staging dir during enrichment. Without it, Syncthing might briefly index the partial staging file and try to sync it before the atomic rename.

The pattern uses no `(?d)` prefix — we don't want Syncthing deleting the staging dir, just ignoring it. The Python helper manages the staging dir lifecycle (creates `.staging/` on first run, cleans orphans on each invocation via `find .staging -mtime +1 -delete` at the top of `process-ebook.py`).

---

## Error handling

| Failure mode | Behaviour |
|---|---|
| Enricher unreachable / returns 5xx | Log, proceed to atomic rename. Un-enriched file appears in sync. Matches current behaviour. |
| Enricher timeout (>30s) | Same as above. Log timeout, proceed. |
| `shutil.copy2` fails (disk full, perms) | Log error, skip this book, continue to next group. Staging file (if any) cleaned by next run's orphan sweep. |
| `os.replace` fails (target on different filesystem) | Hard error — staging dir and sync dir MUST be on same filesystem. Loud failure so we notice. |
| Source dir doesn't exist | Exit 0 with log message. qBit fires the script for non-ebook torrents too via the tag check, so missing-source is plausible. |
| Two `.epub` files with same stem in same dir (rare) | Tie-break on size, log the smaller as discarded. |
| Empty `CONTENT_PATH` (no files) | Exit 0, no-op. |

---

## Testing

Test pyramid:

**Unit (`tests/test_format_selector.py`)**:
- `group_by_book` correctly clusters files by `(dir, stem)`.
- `pick_best` returns the highest-priority format for every chain position.
- Tie-breaker: same format, different sizes → larger wins.
- Empty group → raises (caller bug).
- Single-file group → returns `(file, [])`.

**Integration (`tests/test_process_ebook.py`)**:
- Set up a tempdir mimicking a multi-format qBit torrent:
  ```
  source/Ready Player One/Ready Player One.epub
  source/Ready Player One/Ready Player One.pdf
  source/Ready Player One/cover.jpg
  ```
- Run `process-ebook.py` against a mock enricher (`respx` returns `{"status": "enriched"}`).
- Assert sync dir contains: `.epub` and `cover.jpg`. Asserts: `.pdf` did NOT appear.
- Assert seed dir is byte-identical (sha256) before and after.
- Assert `.staging/` is empty after the run.

**Integration (`tests/test_cleanup.py`)**:
- Populate a tempdir with the 13 known duplicate-group patterns.
- Dry-run: assert output names every loser, doesn't unlink anything.
- `--commit`: assert losers gone, keepers still present.

**Manual smoke test** before merging to plexypi:
- Drop a multi-format test torrent in qBit's auto-add dir with the `ebook` tag.
- Watch `/data/media/ebooks/` — only the chosen format should appear, enriched.
- Confirm `/data/torrents/ebooks/<torrent>/` unchanged (sha256 manifest).

Existing tests (65 across 7 modules) all still pass — this change is additive.

---

## Cleanup execution plan

Once the script lands:

1. Copy `format_selector.py` and `cleanup-duplicates.py` to plexypi (`/opt/stacks/plexypi/qbittorrent/config/`).
2. `docker exec qbittorrent python3 /config/cleanup-duplicates.py /data/media/ebooks` — dry-run, review output (expect 13 groups, ~22 files deleted, ~250MB freed).
3. If output looks correct: re-run with `--commit`.
4. Watch Syncthing propagate deletes to Kindle and laptop (similar to today's earlier cleanup).

---

## Open questions / out of scope

- **Periodic cleanup as a safety net**: a weekly cron running `cleanup-duplicates.py --commit` would catch any duplicates that slip through (e.g. if the pipeline is bypassed by a manual `cp`). Not implementing yet — wait to see if it's needed.
- **Move staging out of sync folder entirely**: could put `.staging/` under `/data/tmp/` if `/data` is a single filesystem (faster atomic rename, no `.stignore` needed). Verify with `stat -f` before deciding; defer for now.
- **`format_selector` could expose a JSON-only CLI** for non-Python consumers (e.g. a future KOReader-side dedup pass). YAGNI for now.
