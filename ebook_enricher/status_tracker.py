"""Tracks consecutive API-health errors and triggers status EPUB
writes/clears.

Threshold-based: after N consecutive auth_error results, write the
"Hardcover rejected our token" status EPUB. After N consecutive
network_error (or rate_limited) results, write the "Hardcover
unreachable" status EPUB.

Any healthy result (enriched/skipped/no_match/low_confidence) resets
both counters AND clears the status EPUB if one is active.

Generic `error` status is ignored — it signals a local per-file problem
(corrupt EPUB, disk write failure), not an API health issue.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ebook_enricher.enrich import EnrichResult
from ebook_enricher.status_epub import clear_status_epub, write_status_epub

DEFAULT_THRESHOLD = 3

logger = logging.getLogger(__name__)

AUTH_TITLE = "⚠️ ebook-enricher: Hardcover API rejected the token"
AUTH_BODY = (
    "The Hardcover API returned an authentication or authorization error on "
    "the last several enrichment attempts.\n"
    "\n"
    "The most common cause is an expired API token (Hardcover tokens rotate "
    "yearly on Jan 1).\n"
    "\n"
    "To fix: generate a new token in Hardcover account settings, then update "
    "HARDCOVER_TOKEN in /opt/stacks/ebook-enricher/.env on plexypi and run "
    "'docker compose up -d' to reload.\n"
    "\n"
    "This status book will disappear automatically once enrichment succeeds."
)

NETWORK_TITLE = "⚠️ ebook-enricher: Hardcover API unreachable"
NETWORK_BODY = (
    "The service couldn't reach api.hardcover.app on the last several "
    "enrichment attempts.\n"
    "\n"
    "Possible causes:\n"
    "  - Hardcover is having an outage (check their status page)\n"
    "  - plexypi has no internet connection\n"
    "  - DNS is misconfigured on plexypi\n"
    "\n"
    "Try: ssh plexypi and run 'curl -I https://api.hardcover.app' to verify "
    "connectivity.\n"
    "\n"
    "This status book will disappear automatically once enrichment succeeds."
)


class StatusTracker:
    """Per-deployment state for status-EPUB triggering.

    Not thread-safe; assumes serial calls from a single FastAPI event loop.
    """

    def __init__(self, ebooks_path: Path, threshold: int = DEFAULT_THRESHOLD):
        self.ebooks_path = ebooks_path
        self.threshold = threshold
        self.auth_errors = 0
        self.network_errors = 0
        self.current_status: Optional[str] = None  # "auth" | "network" | None

    def record(self, result: EnrichResult) -> None:
        status = result.status
        if status == "auth_error":
            self.auth_errors += 1
            self.network_errors = 0
            self._maybe_write_auth()
        elif status in ("network_error", "rate_limited"):
            self.network_errors += 1
            self.auth_errors = 0
            self._maybe_write_network()
        elif status in ("enriched", "skipped", "no_match", "low_confidence"):
            # API is healthy — reset and clear.
            self.auth_errors = 0
            self.network_errors = 0
            if self.current_status is not None:
                try:
                    clear_status_epub(self.ebooks_path)
                except Exception:
                    logger.exception("Failed to clear status EPUB")
                self.current_status = None
        # status == "error" (generic/local) — leave counters alone.

    def _maybe_write_auth(self) -> None:
        if self.auth_errors >= self.threshold and self.current_status != "auth":
            try:
                write_status_epub(self.ebooks_path, AUTH_TITLE, AUTH_BODY)
                self.current_status = "auth"
                logger.warning("Wrote auth-error status EPUB after %d consecutive failures", self.auth_errors)
            except Exception:
                logger.exception("Failed to write auth status EPUB")

    def _maybe_write_network(self) -> None:
        if self.network_errors >= self.threshold and self.current_status != "network":
            try:
                write_status_epub(self.ebooks_path, NETWORK_TITLE, NETWORK_BODY)
                self.current_status = "network"
                logger.warning("Wrote network-error status EPUB after %d consecutive failures", self.network_errors)
            except Exception:
                logger.exception("Failed to write network status EPUB")
