"""Integration tests for process-ebook.py.

Mocks the enricher HTTP call with unittest.mock. Verifies the seed
directory is byte-identical before/after (the seed-protection
invariant the user explicitly cares about).
"""
import hashlib
import http.server
import json
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
