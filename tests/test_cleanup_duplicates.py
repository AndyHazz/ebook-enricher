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
        [sys.executable, str(SCRIPT), str(root), "--allow-root", str(tmp_path)],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    assert result.returncode == 0, result.stderr
    assert "Ready Player One.mobi" in result.stdout


def test_commit_deletes_losers_keeps_keepers(tmp_path):
    root = _make_library(tmp_path)
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), str(root),
            "--commit", "--allow-root", str(tmp_path),
        ],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--commit",
         "--allow-root", str(tmp_path)],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
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
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    # Non-zero exit OR clean refusal — either way the real file must survive
    assert real.exists(), "symlink target outside safe root must not be deleted"


def test_symlink_escape_blocked_when_loser_symlinks_outside(tmp_path):
    """The dangerous case: a LOSER (lower-priority format) is a symlink
    pointing outside the safe root. The script must refuse to delete
    via the symlink, protecting the real file outside.
    """
    safe = tmp_path / "safe"
    safe.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # Real file outside, that must survive
    real = outside / "Important.mobi"
    real.write_bytes(b"do-not-delete")

    # Symlink inside safe pointing to it (.mobi loses to .epub by priority)
    link = safe / "Important.mobi"
    link.symlink_to(real)

    # Real keeper inside safe (.epub wins)
    (safe / "Important.epub").write_bytes(b"keeper")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(safe),
         "--commit", "--allow-root", str(safe)],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(SCRIPT.parent)},
    )
    # Script raises RuntimeError because resolved loser is outside safe
    assert result.returncode != 0
    # Real file outside must NOT be deleted
    assert real.exists(), "symlink target outside safe root must not be deleted"
    assert real.read_bytes() == b"do-not-delete"
    # Keeper inside safe was also not touched (script halted before delete)
    assert (safe / "Important.epub").exists()
