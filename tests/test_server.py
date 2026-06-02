from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ebook_enricher.enrich import EnrichResult
from ebook_enricher.server import app


@pytest.fixture(autouse=True)
def _reset_tracker():
    # Each test starts with a clean tracker
    import ebook_enricher.server as srv
    srv._tracker = None
    yield
    srv._tracker = None


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_enrich_calls_enrich_file(client, bare_epub: Path, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    with patch(
        "ebook_enricher.server.enrich_file",
        new=AsyncMock(return_value=EnrichResult(status="enriched", series="Test")),
    ) as mock:
        resp = client.post("/enrich", json={"path": str(bare_epub)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "enriched"
    assert body["series"] == "Test"
    mock.assert_awaited_once()


def test_enrich_missing_path(client, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    resp = client.post("/enrich", json={})
    assert resp.status_code == 422  # Pydantic validation error


def test_enrich_without_token_returns_error(client, monkeypatch, bare_epub: Path):
    monkeypatch.delenv("HARDCOVER_TOKEN", raising=False)
    resp = client.post("/enrich", json={"path": str(bare_epub)})
    assert resp.status_code == 500
    assert "HARDCOVER_TOKEN" in resp.json()["detail"]


def test_backfill_iterates_folder(client, tmp_path: Path, bare_epub: Path, monkeypatch):
    monkeypatch.setattr("ebook_enricher.server.BACKFILL_DELAY_S", 0)
    # Copy the bare_epub into a sub-folder the backfill will walk
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    import shutil
    shutil.copy(bare_epub, books_dir / "one.epub")
    shutil.copy(bare_epub, books_dir / "two.epub")

    monkeypatch.setenv("EBOOKS_PATH", str(books_dir))
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")

    with patch(
        "ebook_enricher.server.enrich_file",
        new=AsyncMock(return_value=EnrichResult(status="enriched")),
    ) as mock:
        resp = client.post("/backfill")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["enriched"] == 2
    assert mock.await_count == 2


def test_backfill_skips_status_file(client, tmp_path: Path, bare_epub: Path, monkeypatch):
    monkeypatch.setattr("ebook_enricher.server.BACKFILL_DELAY_S", 0)
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    import shutil
    from ebook_enricher.status_epub import STATUS_FILENAME, write_status_epub
    shutil.copy(bare_epub, books_dir / "one.epub")
    # Drop a fake status file that backfill must NOT enrich
    write_status_epub(books_dir, title="old status", body="body")

    monkeypatch.setenv("EBOOKS_PATH", str(books_dir))
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")

    with patch(
        "ebook_enricher.server.enrich_file",
        new=AsyncMock(return_value=EnrichResult(status="enriched")),
    ) as mock:
        resp = client.post("/backfill")
    assert resp.status_code == 200
    # Should have enriched exactly 1 (the status file was skipped)
    assert mock.await_count == 1
    assert resp.json()["total"] == 1


def test_enrich_records_to_tracker(client, tmp_path: Path, bare_epub: Path, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    monkeypatch.setenv("EBOOKS_PATH", str(tmp_path))
    # Simulate 3 consecutive auth errors -> status EPUB appears
    with patch(
        "ebook_enricher.server.enrich_file",
        new=AsyncMock(return_value=EnrichResult(status="auth_error", reason="Not authorized")),
    ):
        for _ in range(3):
            resp = client.post("/enrich", json={"path": str(bare_epub)})
            assert resp.status_code == 200
    from ebook_enricher.status_epub import STATUS_FILENAME
    assert (tmp_path / STATUS_FILENAME).exists()


def test_backfill_summary_splits_error_types(client, tmp_path: Path, bare_epub: Path, monkeypatch):
    monkeypatch.setattr("ebook_enricher.server.BACKFILL_DELAY_S", 0)
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    import shutil
    # Three files: auth error, network error, enriched
    for i in range(3):
        shutil.copy(bare_epub, books_dir / f"book{i}.epub")

    monkeypatch.setenv("EBOOKS_PATH", str(books_dir))
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")

    results = [
        EnrichResult(status="auth_error", reason="not authorized"),
        EnrichResult(status="network_error", reason="connection refused"),
        EnrichResult(status="enriched"),
    ]
    with patch(
        "ebook_enricher.server.enrich_file",
        new=AsyncMock(side_effect=results),
    ):
        resp = client.post("/backfill")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["auth_errors"] == 1
    assert body["network_errors"] == 1
    assert body["enriched"] == 1
    # Generic "errors" bucket should NOT have picked these up
    assert body["errors"] == 0


def test_enrich_endpoint_passes_correct_series(client, bare_epub: Path, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    fake = AsyncMock(return_value=EnrichResult(status="enriched", series="S",
                                               series_corrected=True))
    with patch("ebook_enricher.server.enrich_file", new=fake):
        resp = client.post("/enrich", json={"path": str(bare_epub)})
    assert resp.status_code == 200
    fake.assert_awaited_once()
    _, kwargs = fake.await_args
    assert kwargs.get("correct_series") is True
    assert resp.json().get("series_corrected") is True


def test_backfill_counts_series_corrected(client, tmp_path, monkeypatch, bare_epub: Path):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    import ebook_enricher.server as server
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    import shutil; shutil.copy(bare_epub, books_dir / "a.epub")
    monkeypatch.setattr(server, "_ebooks_path", lambda: books_dir)
    monkeypatch.setattr(server, "BACKFILL_DELAY_S", 0)
    fake = AsyncMock(return_value=EnrichResult(status="enriched", series="S",
                                               series_corrected=True))
    with patch("ebook_enricher.server.enrich_file", new=fake):
        resp = client.post("/backfill")
    assert resp.status_code == 200
    body = resp.json()
    assert body["series_corrected"] == 1
    fake.assert_awaited_once()
    _, kwargs = fake.await_args
    assert kwargs.get("correct_series") is True
