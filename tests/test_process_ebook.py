"""Integration tests for process-ebook.py.

Mocks the enricher HTTP call with unittest.mock. Verifies the seed
directory is byte-identical before/after (the seed-protection
invariant the user explicitly cares about).
"""
import hashlib
import http.server
import json
import os
import threading
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    after = _dir_sha256(save)
    assert before == after


def test_source_not_under_save_path_errors(tmp_path):
    """Mismatched source/save-path returns non-zero with clear error."""
    save = tmp_path / "save"
    source = tmp_path / "elsewhere"
    save.mkdir()
    source.mkdir()
    (source / "Book.epub").write_bytes(b"x")
    sync = tmp_path / "sync"
    sync.mkdir()

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(source),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", "http://does.not.matter/enrich",
            "--dry-run",
        ],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    assert result.returncode == 2
    assert "not under save-path" in result.stderr


class _MockEnricherHandler(http.server.BaseHTTPRequestHandler):
    """In-memory mock enricher: reads the posted path, appends ENRICHED
    to the file so we can prove the published file is the modified one.
    Tracks every received path on the class so tests can assert."""

    received_paths: list[str] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        data = json.loads(body)
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )

    epub = sync / "Ready Player One" / "Ready Player One.epub"
    assert epub.exists()
    assert epub.read_bytes() == b"epub-bytes"  # un-enriched


def test_mobi_published_without_enricher_call(tmp_path, mock_enricher):
    """A .mobi keeper (no .epub available) is published WITHOUT calling
    the enricher (it only enriches .epub)."""
    save = tmp_path / "torrents"
    save.mkdir()
    content = save / "Old Book"
    content.mkdir()
    (content / "Old Book.mobi").write_bytes(b"mobi-bytes")
    (content / "Old Book.pdf").write_bytes(b"pdf-bytes")
    sync = tmp_path / "sync"
    sync.mkdir()

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        check=True, capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )

    mobi = sync / "Old Book" / "Old Book.mobi"
    pdf = sync / "Old Book" / "Old Book.pdf"
    assert mobi.exists()
    assert not pdf.exists()
    # mobi was published raw (no enricher modification)
    assert mobi.read_bytes() == b"mobi-bytes"
    # Enricher was NOT called for non-epub
    assert _MockEnricherHandler.received_paths == []


def test_syncthing_junk_files_skipped(tmp_path, mock_enricher):
    """Files matching Syncthing internal patterns (*.parts, .syncthing.*.tmp,
    .stversions/, .stfolder) must NOT be passed through to sync dir."""
    save = tmp_path / "torrents"
    save.mkdir()
    content = save / "SomeBook"
    content.mkdir()
    (content / "SomeBook.epub").write_bytes(b"epub-bytes")
    # Junk files we should never copy
    (content / ".62d4d4dd.parts").write_bytes(b"partial-block-map")
    (content / ".syncthing.tmp.tmp").write_bytes(b"syncthing-temp")
    (content / ".stfolder").write_bytes(b"folder-marker")
    sync = tmp_path / "sync"
    sync.mkdir()

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        check=True, capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )

    # epub published normally
    assert (sync / "SomeBook" / "SomeBook.epub").exists()
    # Junk files NOT in destination
    assert not (sync / "SomeBook" / ".62d4d4dd.parts").exists()
    assert not (sync / "SomeBook" / ".syncthing.tmp.tmp").exists()
    assert not (sync / "SomeBook" / ".stfolder").exists()


def test_hardlinked_same_file_does_not_crash(tmp_path, mock_enricher):
    """If a passthrough source and dest are hardlinks to the same inode
    (legacy from a prior hardlink-based pipeline), the script must
    skip them gracefully rather than crash with SameFileError."""
    save = tmp_path / "torrents"
    save.mkdir()
    content = save / "BookWithHardlink"
    content.mkdir()
    (content / "BookWithHardlink.epub").write_bytes(b"epub-bytes")

    sync = tmp_path / "sync"
    sync.mkdir()
    sync_book_dir = sync / "BookWithHardlink"
    sync_book_dir.mkdir(parents=True)

    # Create a non-ebook file that's hardlinked between source and dest
    src_file = content / "cover.jpg"
    src_file.write_bytes(b"jpg-bytes")
    dest_file = sync_book_dir / "cover.jpg"
    os.link(src_file, dest_file)  # Same inode
    assert src_file.stat().st_ino == dest_file.stat().st_ino

    # Should complete without raising SameFileError
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    assert result.returncode == 0, f"Should not crash. stderr:\n{result.stderr}"
    # epub still published
    assert (sync / "BookWithHardlink" / "BookWithHardlink.epub").exists()
    # cover.jpg still exists (the original hardlink — we didn't delete it)
    assert dest_file.exists()


# --------------------------------------------------------------------------
# Idempotency: skip already-published destinations (protects manual edits
# from being clobbered when a torrent recheck re-fires the autorun).
# --------------------------------------------------------------------------

def test_skips_publish_when_dest_exists(tmp_path, mock_enricher):
    """If the destination EPUB already exists, the pipeline leaves it
    untouched and does NOT call the enricher. This is the fix for manual
    cover edits being reverted by a torrent recheck."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()
    # Pre-publish a hand-curated version at the dest
    dest_dir = sync / "Ready Player One"
    dest_dir.mkdir(parents=True)
    dest_epub = dest_dir / "Ready Player One.epub"
    dest_epub.write_bytes(b"MANUALLY-CURATED-DO-NOT-CLOBBER")

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    assert result.returncode == 0, result.stderr
    # Dest is untouched
    assert dest_epub.read_bytes() == b"MANUALLY-CURATED-DO-NOT-CLOBBER"
    # Enricher was never called (nothing to enrich)
    assert len(_MockEnricherHandler.received_paths) == 0
    # Skip is logged (dest present, not yet in ledger -> backfill-record)
    assert "skip" in result.stdout.lower()


def test_passthrough_skips_when_dest_exists(tmp_path, mock_enricher):
    """A passthrough (non-ebook) file that already exists at the dest is
    not re-copied."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()
    dest_dir = sync / "Ready Player One"
    dest_dir.mkdir(parents=True)
    dest_cover = dest_dir / "cover.jpg"
    dest_cover.write_bytes(b"EXISTING-COVER")

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
        ],
        check=True, capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    assert dest_cover.read_bytes() == b"EXISTING-COVER"


def test_overwrite_flag_forces_republish(tmp_path, mock_enricher):
    """--overwrite republishes even when the dest exists (escape hatch)."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()
    dest_dir = sync / "Ready Player One"
    dest_dir.mkdir(parents=True)
    dest_epub = dest_dir / "Ready Player One.epub"
    dest_epub.write_bytes(b"OLD")

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", mock_enricher,
            "--overwrite",
        ],
        check=True, capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    # Dest was rebuilt from the seed + enriched
    assert dest_epub.read_bytes() == b"epub-bytes-ENRICHED"


# --------------------------------------------------------------------------
# Sidecar relocation: the enricher writes <staging_stem>.original.jpg next
# to the staging EPUB; the pipeline must move it alongside the published
# book as <book>.original.jpg (not orphan it in .staging).
# --------------------------------------------------------------------------

class _SidecarEnricherHandler(http.server.BaseHTTPRequestHandler):
    """Mock enricher that ALSO writes a sidecar next to the staging file,
    mimicking cover.save_sidecar_if_absent."""

    received_paths: list[str] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length).decode())
        path = Path(data["path"])
        _SidecarEnricherHandler.received_paths.append(str(path))
        with open(path, "ab") as f:
            f.write(b"-ENRICHED")
        # Sidecar: <stem>.original.jpg in the same dir
        sidecar = path.parent / (path.stem + ".original.jpg")
        sidecar.write_bytes(b"ORIGINAL-COVER-BYTES")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"enriched"}')

    def log_message(self, *a, **kw):
        pass


@pytest.fixture
def sidecar_enricher():
    _SidecarEnricherHandler.received_paths = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _SidecarEnricherHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/enrich"
    server.shutdown()


def test_sidecar_relocated_alongside_dest(tmp_path, sidecar_enricher):
    """After publishing, the enricher's staging sidecar is moved to
    <book>.original.jpg next to the dest, and .staging holds no orphan."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"
    sync.mkdir()

    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", sidecar_enricher,
        ],
        check=True, capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    sidecar = sync / "Ready Player One" / "Ready Player One.original.jpg"
    assert sidecar.exists(), "sidecar should be relocated alongside the book"
    assert sidecar.read_bytes() == b"ORIGINAL-COVER-BYTES"
    # No orphan left in .staging
    staging = sync / ".staging"
    orphans = list(staging.glob("*.original.jpg")) if staging.exists() else []
    assert orphans == [], f"staging orphans left behind: {orphans}"


# --------------------------------------------------------------------------
# Copy-once ledger: a published book is recorded permanently. Deleting or
# renaming it on the sync side must NOT cause a re-copy on the next run.
# Keyed by torrent-relative path. Ledger lives outside the sync folder.
# --------------------------------------------------------------------------

def _run(content, save, sync, enricher, ledger, *extra):
    return subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--source", str(content),
            "--save-path", str(save),
            "--sync-base", str(sync),
            "--enricher-url", enricher,
            "--ledger-path", str(ledger),
            *extra,
        ],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )


def test_copy_once_deleted_dest_not_republished(tmp_path, mock_enricher):
    """Publish once, delete the dest, run again — the book is NOT
    resurrected because the ledger remembers it was published."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"; sync.mkdir()
    ledger = tmp_path / "published-ledger.json"

    r1 = _run(content, save, sync, mock_enricher, ledger)
    assert r1.returncode == 0, r1.stderr
    dest = sync / "Ready Player One" / "Ready Player One.epub"
    assert dest.exists()

    # User deletes the book from the sync folder
    import shutil as _sh
    _sh.rmtree(sync / "Ready Player One")
    assert not dest.exists()

    # Torrent recheck re-fires the autorun
    r2 = _run(content, save, sync, mock_enricher, ledger)
    assert r2.returncode == 0, r2.stderr
    assert not dest.exists(), "deleted book must NOT be resurrected"
    assert "already published" in r2.stdout.lower()


def test_ledger_backfills_preexisting_dest(tmp_path, mock_enricher):
    """A book already present at the dest but not yet in the ledger is
    recorded (backfill), so deleting it afterwards still won't resurrect."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"; sync.mkdir()
    ledger = tmp_path / "published-ledger.json"
    # Pre-existing library copy, ledger empty
    dest_dir = sync / "Ready Player One"; dest_dir.mkdir(parents=True)
    (dest_dir / "Ready Player One.epub").write_bytes(b"PREEXISTING")

    # First run: dest exists, not in ledger -> skip + record
    _run(content, save, sync, mock_enricher, ledger)
    import json as _j
    recorded = _j.loads(ledger.read_text())
    assert any("Ready Player One.epub" in k for k in recorded), recorded

    # Delete + re-run: must stay gone
    (dest_dir / "Ready Player One.epub").unlink()
    _run(content, save, sync, mock_enricher, ledger)
    assert not (dest_dir / "Ready Player One.epub").exists()


def test_overwrite_republishes_even_when_in_ledger(tmp_path, mock_enricher):
    """--overwrite bypasses the ledger and republishes (escape hatch)."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"; sync.mkdir()
    ledger = tmp_path / "published-ledger.json"
    _run(content, save, sync, mock_enricher, ledger)
    dest = sync / "Ready Player One" / "Ready Player One.epub"
    dest.write_bytes(b"LOCAL-EDIT")

    _run(content, save, sync, mock_enricher, ledger, "--overwrite")
    assert dest.read_bytes() == b"epub-bytes-ENRICHED"


def test_ledger_file_created_and_persisted(tmp_path, mock_enricher):
    """The ledger JSON is written and contains the published key."""
    save, content = _make_torrent(tmp_path)
    sync = tmp_path / "sync"; sync.mkdir()
    ledger = tmp_path / "published-ledger.json"
    _run(content, save, sync, mock_enricher, ledger)
    assert ledger.exists()
    import json as _j
    keys = _j.loads(ledger.read_text())
    assert any("Ready Player One.epub" in k for k in keys)
