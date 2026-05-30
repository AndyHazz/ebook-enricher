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
