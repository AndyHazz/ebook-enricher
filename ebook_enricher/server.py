"""FastAPI HTTP surface.

Thin glue over ebook_enricher.enrich. Every request returns a status
envelope; errors become 5xx only for programming problems (missing
token, etc.) — not for enrichment misses, which are 200 with a
descriptive status string so the caller can distinguish "worked" from
"didn't find anything" from "broken".
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ebook_enricher.enrich import EnrichResult, enrich_file
from ebook_enricher.status_epub import STATUS_FILENAME
from ebook_enricher.status_tracker import StatusTracker

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="ebook-enricher")

BACKFILL_DELAY_S = 1.0

_tracker: StatusTracker | None = None


def _get_tracker() -> StatusTracker:
    global _tracker
    if _tracker is None:
        _tracker = StatusTracker(_ebooks_path())
    return _tracker


class EnrichRequest(BaseModel):
    path: str


class BackfillSummary(BaseModel):
    total: int
    enriched: int
    skipped: int
    no_match: int
    low_confidence: int
    rate_limited: int
    auth_errors: int
    network_errors: int
    errors: int
    series_corrected: int


def _token() -> str:
    token = os.environ.get("HARDCOVER_TOKEN")
    if not token:
        raise HTTPException(
            status_code=500,
            detail="HARDCOVER_TOKEN environment variable not set",
        )
    return token


def _ebooks_path() -> Path:
    return Path(os.environ.get("EBOOKS_PATH", "/data/media/ebooks"))


def _result_to_dict(result: EnrichResult) -> dict:
    return {
        "status": result.status,
        "reason": result.reason,
        "series": result.series,
        "series_corrected": result.series_corrected,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/enrich")
async def enrich(req: EnrichRequest) -> dict:
    token = _token()
    result = await enrich_file(Path(req.path), token=token, correct_series=True)
    _get_tracker().record(result)
    return _result_to_dict(result)


@app.post("/backfill")
async def backfill() -> BackfillSummary:
    token = _token()
    root = _ebooks_path()
    summary = {
        "total": 0, "enriched": 0, "skipped": 0, "no_match": 0,
        "low_confidence": 0, "rate_limited": 0,
        "auth_errors": 0, "network_errors": 0, "errors": 0,
        "series_corrected": 0,
    }
    tracker = _get_tracker()
    for path in sorted(root.rglob("*.epub")):
        if path.name == STATUS_FILENAME:
            continue  # Don't try to enrich our own status file
        summary["total"] += 1
        result = await enrich_file(path, token=token, correct_series=True)
        tracker.record(result)
        key = {
            "enriched": "enriched",
            "skipped": "skipped",
            "no_match": "no_match",
            "low_confidence": "low_confidence",
            "rate_limited": "rate_limited",
            "auth_error": "auth_errors",
            "network_error": "network_errors",
            "error": "errors",
        }.get(result.status, "errors")
        summary[key] += 1
        if result.series_corrected:
            summary["series_corrected"] += 1
        logger.info("backfill %s -> %s (%s)", path.name, result.status, result.reason)
        await asyncio.sleep(BACKFILL_DELAY_S)
    return BackfillSummary(**summary)
