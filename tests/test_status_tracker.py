from pathlib import Path

from ebook_enricher.enrich import EnrichResult
from ebook_enricher.status_epub import STATUS_FILENAME
from ebook_enricher.status_tracker import StatusTracker


def _auth() -> EnrichResult:
    return EnrichResult(status="auth_error", reason="Not authorized")


def _network() -> EnrichResult:
    return EnrichResult(status="network_error", reason="connection refused")


def _rate() -> EnrichResult:
    return EnrichResult(status="rate_limited")


def _ok() -> EnrichResult:
    return EnrichResult(status="enriched", series="X")


def _local_error() -> EnrichResult:
    return EnrichResult(status="error", reason="write_failed: disk full")


def test_threshold_triggers_auth_epub(tmp_path: Path):
    t = StatusTracker(tmp_path, threshold=3)
    for _ in range(2):
        t.record(_auth())
    assert not (tmp_path / STATUS_FILENAME).exists()
    t.record(_auth())
    assert (tmp_path / STATUS_FILENAME).exists()


def test_threshold_triggers_network_epub(tmp_path: Path):
    t = StatusTracker(tmp_path, threshold=3)
    t.record(_network())
    t.record(_rate())  # mixed network-type errors both count
    t.record(_network())
    assert (tmp_path / STATUS_FILENAME).exists()


def test_success_resets_and_clears(tmp_path: Path):
    t = StatusTracker(tmp_path, threshold=3)
    for _ in range(3):
        t.record(_auth())
    assert (tmp_path / STATUS_FILENAME).exists()
    t.record(_ok())
    assert not (tmp_path / STATUS_FILENAME).exists()
    # Counter reset too: need 3 MORE errors to retrigger
    t.record(_auth())
    t.record(_auth())
    assert not (tmp_path / STATUS_FILENAME).exists()


def test_mixed_errors_below_threshold_do_not_trigger(tmp_path: Path):
    t = StatusTracker(tmp_path, threshold=3)
    t.record(_auth())
    t.record(_network())  # resets auth counter
    t.record(_auth())
    t.record(_network())
    # Neither counter has reached 3 consecutive
    assert not (tmp_path / STATUS_FILENAME).exists()


def test_generic_error_does_not_affect_counters(tmp_path: Path):
    t = StatusTracker(tmp_path, threshold=3)
    t.record(_auth())
    t.record(_auth())
    t.record(_local_error())  # local error is not an API signal
    t.record(_auth())  # 3rd auth_error — should fire
    assert (tmp_path / STATUS_FILENAME).exists()


def test_idempotent_write(tmp_path: Path):
    """Continuing to see auth errors after the EPUB is written
    must not cause excessive rewrites."""
    t = StatusTracker(tmp_path, threshold=3)
    for _ in range(3):
        t.record(_auth())
    first_mtime = (tmp_path / STATUS_FILENAME).stat().st_mtime_ns
    # Keep adding auth errors — current_status is already "auth", so
    # _maybe_write_auth should no-op.
    t.record(_auth())
    t.record(_auth())
    second_mtime = (tmp_path / STATUS_FILENAME).stat().st_mtime_ns
    assert first_mtime == second_mtime, "EPUB was rewritten despite same status"


def test_healthy_statuses_also_reset(tmp_path: Path):
    t = StatusTracker(tmp_path, threshold=3)
    t.record(_auth())
    t.record(_auth())
    # Any of these should reset counters
    t.record(EnrichResult(status="no_match"))
    t.record(_auth())
    t.record(_auth())
    assert not (tmp_path / STATUS_FILENAME).exists()


def test_rate_limited_alone_triggers_network_epub(tmp_path: Path):
    t = StatusTracker(tmp_path, threshold=3)
    for _ in range(3):
        t.record(_rate())
    assert (tmp_path / STATUS_FILENAME).exists()
