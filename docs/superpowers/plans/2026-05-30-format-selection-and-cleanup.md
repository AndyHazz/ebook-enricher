# Format Selection and Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pick one ebook format per book at copy time (epub > azw3 > mobi > pdf > lit > txt > cbz > cbr), enrich the staging copy before atomic publish into the sync folder, and one-shot-clean existing duplicate groups in `/data/media/ebooks/`.

**Architecture:** Three new Python files in a new `scripts/` directory of the ebook-enricher repo, sharing a `format_selector` module: a `process-ebook.py` pipeline helper (called by the existing `process-ebook.sh` qBit autorun entrypoint), a `cleanup-duplicates.py` one-shot, and the shared module. All deployed to `/opt/stacks/plexypi/qbittorrent/config/` on plexypi.

**Tech Stack:** Python 3.12 (stdlib only — no httpx in qBit container), pytest + respx-style mocking via `unittest.mock`, bash for the entrypoint, shutil/os/pathlib for file ops, urllib.request for the single JSON POST to ebook-enricher.

---

## File Structure

**Repo (`~/projects/ebook-enricher/`):**
- Create: `scripts/format_selector.py` — preference chain + grouping + pick_best
- Create: `scripts/process-ebook.py` — pipeline helper
- Create: `scripts/cleanup-duplicates.py` — one-shot cleanup
- Create: `scripts/process-ebook.sh` — updated thin entrypoint
- Modify: `pyproject.toml` — add `scripts/` to pytest pythonpath
- Create: `tests/test_format_selector.py`
- Create: `tests/test_process_ebook.py`
- Create: `tests/test_cleanup_duplicates.py`

**Deployed to plexypi (`/opt/stacks/plexypi/qbittorrent/config/`):**
- All four files in `scripts/` (rsync'd from repo)

**Syncthing config:**
- Add `.staging/` to `/mnt/data/media/ebooks/.stignore` on plexypi

---

### Task 1: Project setup — scripts/ dir + pytest pythonpath

**Files:**
- Create: `scripts/__init__.py` (empty marker so pytest discovers imports cleanly)
- Modify: `pyproject.toml:28-30`

- [ ] **Step 1: Create scripts dir**

```bash
cd ~/projects/ebook-enricher
mkdir -p scripts
touch scripts/__init__.py
```

- [ ] **Step 2: Add pytest pythonpath**

Modify `pyproject.toml`. Find:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Replace with:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["scripts"]
```

- [ ] **Step 3: Verify existing tests still pass**

```bash
cd ~/projects/ebook-enricher
source .venv/bin/activate
pytest -q
```

Expected: All 65 existing tests pass (unchanged).

- [ ] **Step 4: Commit**

```bash
git add scripts/__init__.py pyproject.toml
git commit -m "chore: add scripts/ dir and pytest pythonpath"
```

---

### Task 2: format_selector — PREFERENCE_CHAIN + is_ebook_ext

**Files:**
- Create: `scripts/format_selector.py`
- Create: `tests/test_format_selector.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_format_selector.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/projects/ebook-enricher
source .venv/bin/activate
pytest tests/test_format_selector.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'format_selector'`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/format_selector.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_format_selector.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/format_selector.py tests/test_format_selector.py
git commit -m "feat: format_selector preference chain + ext check"
```

---

### Task 3: format_selector — group_by_book

**Files:**
- Modify: `scripts/format_selector.py`
- Modify: `tests/test_format_selector.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_format_selector.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_format_selector.py -v
```

Expected: 5 NEW tests FAIL with `ImportError: cannot import name 'group_by_book'`.

- [ ] **Step 3: Implement**

Append to `scripts/format_selector.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_format_selector.py -v
```

Expected: 8 tests PASS (3 from Task 2 + 5 from Task 3).

- [ ] **Step 5: Commit**

```bash
git add scripts/format_selector.py tests/test_format_selector.py
git commit -m "feat: format_selector group_by_book"
```

---

### Task 4: format_selector — pick_best (with tie-break)

**Files:**
- Modify: `scripts/format_selector.py`
- Modify: `tests/test_format_selector.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_format_selector.py`:

```python
from format_selector import pick_best


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_format_selector.py -v
```

Expected: 7 NEW tests FAIL with `ImportError: cannot import name 'pick_best'`.

- [ ] **Step 3: Implement**

Append to `scripts/format_selector.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_format_selector.py -v
```

Expected: 15 tests PASS (8 prior + 7 new).

- [ ] **Step 5: Commit**

```bash
git add scripts/format_selector.py tests/test_format_selector.py
git commit -m "feat: format_selector pick_best with tie-break"
```

---

### Task 5: process-ebook.py — file collection + grouping (no copy yet)

**Files:**
- Create: `scripts/process-ebook.py`
- Create: `tests/test_process_ebook.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_process_ebook.py`:

```python
"""Integration tests for process-ebook.py.

Mocks the enricher HTTP call with unittest.mock. Verifies the seed
directory is byte-identical before/after (the seed-protection
invariant the user explicitly cares about).
"""
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest


def _dir_sha256(root: Path) -> dict[str, str]:
    """Return {relative_path: sha256} for every file under root."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


SCRIPT = Path(__file__).parent.parent / "scripts" / "process-ebook.py"


def _make_torrent(tmp_path: Path) -> tuple[Path, Path]:
    """Build a seed/save dir mimicking a qBit torrent. Returns
    (save_path, content_path)."""
    save = tmp_path / "torrents"
    save.mkdir()
    content = save / "Ready Player One"
    content.mkdir()
    (content / "Ready Player One.epub").write_bytes(b"epub-bytes")
    (content / "Ready Player One.pdf").write_bytes(b"pdf-bytes")
    (content / "cover.jpg").write_bytes(b"jpg-bytes")
    return save, content


def test_collect_files_multi_format(tmp_path, monkeypatch):
    """Helper script lists files and groups them. We test by --dry-run."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", "http://does.not.matter/enrich",
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Ready Player One.epub" in result.stdout
    assert "Ready Player One.pdf" in result.stdout
    # Loser is logged with "skip"
    assert "skip" in result.stdout.lower()
    assert "Ready Player One.pdf" in result.stdout
    # cover.jpg is logged as passthrough
    assert "cover.jpg" in result.stdout


def test_seed_unchanged_in_dry_run(tmp_path):
    """Seed dir is byte-identical after dry-run."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()

    before = _dir_sha256(save)
    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", "http://does.not.matter/enrich",
            "--dry-run",
        ],
        check=True, capture_output=True,
    )
    after = _dir_sha256(save)
    assert before == after
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_process_ebook.py -v
```

Expected: FAIL with "No such file or directory: process-ebook.py".

- [ ] **Step 3: Implement minimal script (collection + dry-run only)**

Create `scripts/process-ebook.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_process_ebook.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/process-ebook.py tests/test_process_ebook.py
git commit -m "feat: process-ebook.py file collection + dry-run"
```

---

### Task 6: process-ebook.py — real copy/enrich/publish path

**Files:**
- Modify: `scripts/process-ebook.py`
- Modify: `tests/test_process_ebook.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_process_ebook.py`:

```python
import http.server
import threading
from urllib.parse import urlparse


class _MockEnricherHandler(http.server.BaseHTTPRequestHandler):
    """In-memory mock enricher: reads the posted path, appends ENRICHED
    to the file so we can prove the published file is the modified one.
    Tracks every received path on the class so tests can assert."""

    received_paths: list[str] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        import json as _json
        data = _json.loads(body)
        path = data["path"]
        _MockEnricherHandler.received_paths.append(path)
        # Modify the staging file to prove enrichment ran
        with open(path, "ab") as f:
            f.write(b"-ENRICHED")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"enriched"}')

    def log_message(self, *a, **kw):  # silence test output
        pass


@pytest.fixture
def mock_enricher():
    """Start a mock enricher HTTP server on a random port."""
    _MockEnricherHandler.received_paths = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockEnricherHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/enrich"
    server.shutdown()


def test_real_run_publishes_chosen_format_only(tmp_path, mock_enricher):
    """Real run: epub published, pdf skipped, cover.jpg passes through,
    enricher was called with the staging path."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    # Published: epub (enriched bytes) + cover.jpg. NOT the pdf.
    epub = sync / "Ready Player One" / "Ready Player One.epub"
    pdf = sync / "Ready Player One" / "Ready Player One.pdf"
    cover = sync / "Ready Player One" / "cover.jpg"
    assert epub.exists()
    assert cover.exists()
    assert not pdf.exists()
    # Proves we published the enricher's output, not the raw seed copy
    assert epub.read_bytes() == b"epub-bytes-ENRICHED"
    assert cover.read_bytes() == b"jpg-bytes"

    # Staging dir is empty after run
    staging = sync / ".staging"
    assert not staging.exists() or not any(staging.iterdir())

    # Enricher received a path that ended up renamed to the final dest
    assert len(_MockEnricherHandler.received_paths) == 1
    # Path it received was inside .staging (not the final dest, not the seed)
    received = _MockEnricherHandler.received_paths[0]
    assert ".staging" in received


def test_seed_byte_identical_after_real_run(tmp_path, mock_enricher):
    """Seed unchanged after a real run (the headline invariant)."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()
    before = _dir_sha256(save)

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        check=True, capture_output=True,
    )
    after = _dir_sha256(save)
    assert before == after


def test_single_file_torrent(tmp_path, mock_enricher):
    """When source is a single file (not a dir), still works."""
    save = tmp_path / "torrents"
    save.mkdir()
    epub = save / "Snow Crash.epub"
    epub.write_bytes(b"snow")
    sync = tmp_path / "sync"
    sync.mkdir()

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(epub),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        check=True, capture_output=True,
    )

    dest = sync / "Snow Crash.epub"
    assert dest.exists()
    assert dest.read_bytes() == b"snow-ENRICHED"


def test_enricher_failure_still_publishes(tmp_path):
    """Enricher unreachable — file still published (un-enriched). Matches
    current behaviour: enrich failure does not block the copy."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", "http://127.0.0.1:1/enrich",  # nothing listens
        ],
        check=True, capture_output=True,
    )

    epub = sync / "Ready Player One" / "Ready Player One.epub"
    assert epub.exists()
    assert epub.read_bytes() == b"epub-bytes"  # un-enriched
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_process_ebook.py -v
```

Expected: 4 NEW tests FAIL — current script raises `NotImplementedError` in real mode.

- [ ] **Step 3: Implement the real copy/enrich/publish path**

In `scripts/process-ebook.py`, replace the `main()` function with this implementation. Also add the imports and helpers at the top of the file (after the existing `from format_selector import ...` line):

```python
import json
import os
import shutil
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import URLError
```

And add these helper functions before `main()`:

```python
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
    assert keeper.resolve() != dest.resolve(), (
        f"refusing to publish into source path: {keeper}"
    )
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
```

Replace `main()` with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_process_ebook.py -v
```

Expected: 6 tests PASS (2 prior + 4 new).

- [ ] **Step 5: Commit**

```bash
git add scripts/process-ebook.py tests/test_process_ebook.py
git commit -m "feat: process-ebook.py copy/enrich/atomic-publish path"
```

---

### Task 7: Updated process-ebook.sh wrapper

**Files:**
- Create: `scripts/process-ebook.sh`

- [ ] **Step 1: Write the new shell entrypoint**

Create `scripts/process-ebook.sh`:

```bash
#!/bin/bash
# qBittorrent autorun: pre-validate, then delegate to Python helper.
# Called with: %G (tags) %D (save path) %F (content path) %N (name)
#
# This script intentionally does NO copying or enrichment — that all
# happens in process-ebook.py. We keep this thin so qBittorrent's
# autorun wiring (which calls a single executable script) doesn't
# need to change as the pipeline evolves.

TAGS="$1"
SAVE_PATH="$2"
CONTENT_PATH="$3"

SYNC_BASE="/data/media/ebooks"
SEED_BASE="/data/torrents/ebooks"
ENRICHER_URL="http://ebook-enricher:8000/enrich"

case "$TAGS" in
    *ebook*) ;;
    *) exit 0 ;;
esac

case "$SAVE_PATH" in
    ${SEED_BASE}*) ;;
    *) exit 0 ;;
esac

exec python3 /config/process-ebook.py \
    --source "$CONTENT_PATH" \
    --save-path "$SAVE_PATH" \
    --sync-base "$SYNC_BASE" \
    --enricher-url "$ENRICHER_URL"
```

- [ ] **Step 2: Mark executable and verify syntax**

```bash
cd ~/projects/ebook-enricher
chmod +x scripts/process-ebook.sh
bash -n scripts/process-ebook.sh && echo "syntax OK"
```

Expected: `syntax OK`.

- [ ] **Step 3: Commit**

```bash
git add scripts/process-ebook.sh
git commit -m "feat: thin process-ebook.sh delegating to Python helper"
```

---

### Task 8: cleanup-duplicates.py — dry-run mode

**Files:**
- Create: `scripts/cleanup-duplicates.py`
- Create: `tests/test_cleanup_duplicates.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cleanup_duplicates.py`:

```python
"""Tests for cleanup-duplicates.py one-shot."""
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parent.parent / "scripts" / "cleanup-duplicates.py"


def _make_library(tmp_path: Path) -> Path:
    """Realistic-ish sync-folder layout with mixed-format duplicates."""
    root = tmp_path / "ebooks"
    root.mkdir()

    # Group with all 3 formats present
    (root / "Ready Player One.epub").write_bytes(b"epub")
    (root / "Ready Player One.mobi").write_bytes(b"mobi")
    (root / "Ready Player One.pdf").write_bytes(b"pdf")

    # Group with mobi+pdf only (no epub) — mobi wins
    (root / "Snow Crash.mobi").write_bytes(b"mobi")
    (root / "Snow Crash.pdf").write_bytes(b"pdf")

    # Single-format group (no-op, must not be touched)
    (root / "Solo Book.epub").write_bytes(b"epub")

    # Cover/metadata files (not ebook ext) — must not be touched
    (root / "Ready Player One.jpg").write_bytes(b"jpg")
    (root / "Solo Book.opf").write_bytes(b"opf")

    return root


def test_dry_run_lists_losers_does_not_delete(tmp_path):
    root = _make_library(tmp_path)
    files_before = {p.name for p in root.iterdir()}

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    # Reports what it would delete
    assert "Ready Player One.mobi" in result.stdout
    assert "Ready Player One.pdf" in result.stdout
    assert "Snow Crash.pdf" in result.stdout
    # Reports what it would keep
    assert "Ready Player One.epub" in result.stdout
    assert "Snow Crash.mobi" in result.stdout
    # Solo Book is not in output (single-format group)
    assert "Solo Book" not in result.stdout

    # Nothing was deleted
    assert {p.name for p in root.iterdir()} == files_before


def test_dry_run_refuses_outside_safe_root(tmp_path):
    """Hard refusal: cleanup must only run inside /data/media/ebooks
    OR a path explicitly marked safe with --allow-root."""
    bad = tmp_path / "not-ebooks"
    bad.mkdir()
    (bad / "anything.epub").write_bytes(b"x")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(bad)],
        capture_output=True, text=True,
    )
    # Refusal mode: non-zero exit, no file ops
    assert result.returncode != 0
    assert "refusing" in result.stderr.lower() or "safe" in result.stderr.lower()
    # File untouched
    assert (bad / "anything.epub").exists()


def test_dry_run_works_with_allow_root_override(tmp_path):
    """--allow-root <path> lets tests run against tmp_path."""
    root = _make_library(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--allow-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Ready Player One.mobi" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cleanup_duplicates.py -v
```

Expected: FAIL with "No such file or directory: cleanup-duplicates.py".

- [ ] **Step 3: Implement dry-run script**

Create `scripts/cleanup-duplicates.py`:

```python
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
            assert _is_under(loser, args.allow_root), (
                f"loser escaped safe root: {loser}"
            )
            assert keeper.exists(), f"keeper missing before delete: {keeper}"
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cleanup_duplicates.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/cleanup-duplicates.py tests/test_cleanup_duplicates.py
git commit -m "feat: cleanup-duplicates.py dry-run + safe-root guard"
```

---

### Task 9: cleanup-duplicates.py — --commit mode

**Files:**
- Modify: `tests/test_cleanup_duplicates.py` (--commit was already implemented in Task 8; this task just adds tests that lock down the behaviour)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cleanup_duplicates.py`:

```python
def test_commit_deletes_losers_keeps_keepers(tmp_path):
    root = _make_library(tmp_path)
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), str(root),
            "--commit", "--allow-root", str(tmp_path),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    # Keepers
    assert (root / "Ready Player One.epub").exists()
    assert (root / "Snow Crash.mobi").exists()
    # Solo group untouched
    assert (root / "Solo Book.epub").exists()
    # Non-ebook files untouched
    assert (root / "Ready Player One.jpg").exists()
    assert (root / "Solo Book.opf").exists()

    # Losers gone
    assert not (root / "Ready Player One.mobi").exists()
    assert not (root / "Ready Player One.pdf").exists()
    assert not (root / "Snow Crash.pdf").exists()


def test_commit_is_idempotent(tmp_path):
    """Running --commit twice in a row is fine; second run finds nothing."""
    root = _make_library(tmp_path)
    subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--commit",
         "--allow-root", str(tmp_path)],
        check=True, capture_output=True,
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--commit",
         "--allow-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Summary: 0 duplicate groups" in result.stdout


def test_symlink_escape_blocked(tmp_path):
    """A symlink inside the safe root pointing outside must not be
    followed for deletion."""
    safe = tmp_path / "safe"
    safe.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "Important.epub"
    real.write_bytes(b"do-not-delete")
    link = safe / "Important.epub"
    link.symlink_to(real)
    # also need a duplicate to trigger a group with >1
    (safe / "Important.pdf").write_bytes(b"dup")

    # The script resolves paths; the resolved symlink points outside
    # safe, so the assertion in main() will fail.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(safe),
         "--commit", "--allow-root", str(safe)],
        capture_output=True, text=True,
    )
    # AssertionError exit code (1 from python uncaught) OR clean refusal
    assert real.exists(), "symlink target outside safe root must not be deleted"
```

- [ ] **Step 2: Run new tests**

```bash
pytest tests/test_cleanup_duplicates.py -v
```

Expected: 6 tests PASS (3 prior + 3 new).

If `test_symlink_escape_blocked` fails (the keeper symlink got followed and `pick_best` chose the symlinked .epub as keeper, then we tried to delete .pdf only), inspect: the test passes if `real` still exists. If it fails it's because we tried to follow a symlink for the keeper. Tighten by also resolving `loser` and re-checking before unlink. If the test passes as-is, no code change needed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_cleanup_duplicates.py
git commit -m "test: cleanup-duplicates commit-mode + symlink-escape coverage"
```

---

### Task 10: Run full test suite

**Files:** none

- [ ] **Step 1: Full repo test suite**

```bash
cd ~/projects/ebook-enricher
source .venv/bin/activate
pytest -v
```

Expected: 80+ tests PASS (65 existing + 15 format_selector + 6 process_ebook + 6 cleanup_duplicates).

- [ ] **Step 2: If anything fails, fix it before deploying**

Do not proceed to Task 11 until the suite is green.

---

### Task 11: Deploy to plexypi + Syncthing .stignore

**Files:**
- `/opt/stacks/plexypi/qbittorrent/config/process-ebook.sh` (plexypi, replacing existing)
- `/opt/stacks/plexypi/qbittorrent/config/process-ebook.py` (plexypi, new)
- `/opt/stacks/plexypi/qbittorrent/config/cleanup-duplicates.py` (plexypi, new)
- `/opt/stacks/plexypi/qbittorrent/config/format_selector.py` (plexypi, new)
- `/mnt/data/media/ebooks/.stignore` (plexypi, new)

- [ ] **Step 1: Back up the current process-ebook.sh on plexypi**

```bash
ssh plexypi 'sudo cp /opt/stacks/plexypi/qbittorrent/config/process-ebook.sh /opt/stacks/plexypi/qbittorrent/config/process-ebook.sh.bak-$(date +%Y%m%d-%H%M%S)'
```

Expected: silent success. Verify with `ls /opt/stacks/plexypi/qbittorrent/config/process-ebook.sh.bak-*`.

- [ ] **Step 2: rsync scripts to plexypi**

```bash
rsync -av --exclude __pycache__ --exclude __init__.py \
    ~/projects/ebook-enricher/scripts/ \
    plexypi:/tmp/ebook-scripts/

ssh plexypi 'sudo mv /tmp/ebook-scripts/* /opt/stacks/plexypi/qbittorrent/config/ && sudo chmod +x /opt/stacks/plexypi/qbittorrent/config/process-ebook.sh /opt/stacks/plexypi/qbittorrent/config/process-ebook.py /opt/stacks/plexypi/qbittorrent/config/cleanup-duplicates.py && rmdir /tmp/ebook-scripts'
```

Expected: 4 files in `/opt/stacks/plexypi/qbittorrent/config/` (.sh, process-ebook.py, cleanup-duplicates.py, format_selector.py), executable bits set on the three runnables.

- [ ] **Step 3: Verify Python can import format_selector inside qBit container**

```bash
ssh plexypi 'docker exec qbittorrent python3 -c "import sys; sys.path.insert(0, \"/config\"); from format_selector import PREFERENCE_CHAIN; print(PREFERENCE_CHAIN)"'
```

Expected: `('epub', 'azw3', 'mobi', 'pdf', 'lit', 'txt', 'cbz', 'cbr')`.

- [ ] **Step 4: Add .staging/ to Syncthing's stignore**

```bash
ssh plexypi 'sudo bash -c "echo .staging/ >> /mnt/data/media/ebooks/.stignore && cat /mnt/data/media/ebooks/.stignore"'
```

Expected: `.staging/` (the file may have been created fresh, that's fine).

- [ ] **Step 5: Tell Syncthing to re-read ignore patterns**

```bash
ssh plexypi 'APIKEY=$(sudo grep -oP "(?<=<apikey>)[^<]+" /opt/stacks/syncthing/config/config.xml) && curl -s -X POST -H "X-API-Key: $APIKEY" "http://localhost:8384/rest/db/scan?folder=9vq6c-9skem"'
```

Expected: empty response (success).

- [ ] **Step 6: Commit deployment note**

```bash
cd ~/projects/ebook-enricher
git log --oneline -1 > /dev/null  # no-op; deployment doesn't change repo
```

No commit needed — deployment is a runtime action, not a source change. Note the timestamp of deployment in your shell history.

---

### Task 12: Run cleanup against real library

**Files:** none (executing the deployed cleanup script)

- [ ] **Step 1: Dry-run cleanup on the real sync folder**

```bash
ssh plexypi 'docker exec qbittorrent python3 /config/cleanup-duplicates.py /data/media/ebooks'
```

Expected: lists ~13 duplicate groups, naming the losers (mobi/pdf/azw3 entries where epub exists, etc.). Summary line at end. **No files deleted yet.**

- [ ] **Step 2: Inspect the output**

Read the dry-run output carefully. Confirm:
- Every "keep" line is the format you'd choose (epub > azw3 > mobi > pdf).
- No surprising "keep" lines (e.g. a `.txt` or `.cbz` you didn't realise you had).
- Total bytes freed looks roughly right (expect tens of MB, not GB — the 256-authors torrent is already gone).

If anything looks wrong, stop and investigate. Don't commit.

- [ ] **Step 3: Commit cleanup**

```bash
ssh plexypi 'docker exec qbittorrent python3 /config/cleanup-duplicates.py /data/media/ebooks --commit'
```

Expected: same output as dry-run plus `committed.` at the end.

- [ ] **Step 4: Verify on plexypi disk**

```bash
ssh plexypi 'find /mnt/data/media/ebooks -name "Ready Player One.*"'
```

Expected: only `Ready Player One.epub` (or `Ready Player One.jpg` if you have a cover). The `.mobi` and `.lit` are gone.

- [ ] **Step 5: Verify Syncthing propagates to Kindle**

Watch the Kindle's file count drop accordingly. The deletes are small (13 books × ~2 files each = ~22 files), should propagate within a minute.

```bash
ssh kindle 'find /mnt/us/calibre/syncthing -name "Ready Player One.*"'
```

Expected: eventually only `Ready Player One.epub`. If it's still showing `.mobi`/`.lit`, the Kindle's index may need a kick (same `state=sync-waiting` issue from earlier — restart Syncthing via KOReader plugin).

- [ ] **Step 6: Smoke test the pipeline (optional, only if you have a new multi-format torrent ready)**

Drop a multi-format ebook torrent into qBit with the `ebook` tag. Watch:
```bash
ssh plexypi 'tail -f /opt/stacks/plexypi/qbittorrent/config/logs/qbittorrent.log'
```

Or check the sync folder directly:
```bash
ssh plexypi 'ls -la /mnt/data/media/ebooks/<TorrentName>/'
```

Expected: only the chosen format appears (with enriched metadata), plus any non-ebook auxiliary files.

---

## Self-Review

**Spec coverage check** (every section/requirement in the spec must have a task):

- ✓ Format chain (epub > azw3 > mobi > pdf > lit > txt > cbz > cbr) — Task 2
- ✓ Grouping rule (dir + filename stem) — Task 3
- ✓ Tie-break (largest wins) — Task 4
- ✓ Enrich-before-publish via staging + atomic rename — Task 6
- ✓ Seed file untouched (sha256 assertion in tests) — Task 6
- ✓ Single-file torrent handling — Task 6 (`test_single_file_torrent`)
- ✓ Multi-file directory torrent — Task 5–6
- ✓ Non-ebook passthrough (cover.jpg etc.) — Task 6
- ✓ Enricher-failure-doesn't-block — Task 6 (`test_enricher_failure_still_publishes`)
- ✓ Permissions copied from parent dir — Task 6 (`_apply_perms_from_parent`)
- ✓ Staging orphan sweep — Task 6 (`_sweep_staging`)
- ✓ Dry-run cleanup with safety guard — Task 8
- ✓ --commit deletes losers, keeps keepers — Task 8 + Task 9 tests
- ✓ Symlink-escape blocked — Task 9
- ✓ Shared format_selector imported by both .py files — Task 5, Task 8 imports
- ✓ Bash wrapper updated — Task 7
- ✓ .stignore for .staging — Task 11
- ✓ Deployment to plexypi — Task 11
- ✓ Real-data cleanup execution — Task 12

No gaps.

**Type consistency check:**
- `group_by_book` returns `dict[tuple[Path, str], list[Path]]` — consistent in all callers.
- `pick_best` returns `tuple[Path, list[Path]]` — consistent in all callers.
- `PREFERENCE_CHAIN` is a `tuple[str, ...]` — consistent.

**Placeholder scan:** clean.
