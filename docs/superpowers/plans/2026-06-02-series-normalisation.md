# Series Normalisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the enricher treat Hardcover as canonical for series — overwrite an existing (or missing) `calibre:series` + `series_index` from Hardcover on a confident match, at ingest and as a library-wide pass.

**Architecture:** A `correct_series` parameter (default `False`) on `enrich_file`. When `True`, the early skip-if-series gate is bypassed and the series name/index are written from the confident Hardcover match (never blanked). `server.py`'s `/enrich` and `/backfill` pass `True`; `/backfill` counts corrections. Series-only — description/genres/cover are unchanged.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest + respx + unittest.mock. Spec: `docs/superpowers/specs/2026-06-02-series-normalisation-design.md`.

**Test environment note:** Some test deps are not in the host venv. Install once before running: `pip install --break-system-packages --user respx pytest-asyncio rapidfuzz`. `test_server.py` additionally needs `fastapi` — if unavailable locally, run `pytest --ignore=tests/test_server.py` and rely on the live deploy check (Task 3) for the server wiring.

---

### Task 1: `correct_series` parameter on `enrich_file`

**Files:**
- Modify: `ebook_enricher/enrich.py` (the `EnrichResult` dataclass ~line 41, the skip gate ~line 57, the series-write block ~line 120, the signature ~line 48)
- Test: `tests/test_enrich.py` (append)

Context for the implementer: `enrich_file(path, token)` reads EPUB metadata into `meta` (an `EpubMeta`), then — today — returns early as `skipped` if `meta.series` is set. Otherwise it queries Hardcover via `search_book`, scores candidates with `score_match` against `TITLE_THRESHOLD`/`AUTHOR_THRESHOLD` (both 80), and the best passing candidate becomes `chosen` (a `HardcoverBook` with `.series_name`, `.series_position`, `.description`, `.genres`). It builds an `updates` `EpubMeta` writing only empty fields, then calls `write_meta(path, updates, cover_override=..., cover_add=...)`. `write_meta` already updates an existing `calibre:series` in place, so no change is needed there.

Tests use `_make_hc_book(**overrides)` (returns a `HardcoverBook`; defaults: title `"Test Book Title"`, author `"Test Author"`, `series_name="Test Series"`, `series_position="1.5"`, `description`, `genres`) and patch the network with `patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[...]))`. The `enriched_epub` fixture is an EPUB with `series="Existing Series"`, `series_index="2"`, `description="Existing description."` and title/author matching the defaults above.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_enrich.py`:

```python
@pytest.mark.asyncio
async def test_correct_series_overwrites_name_and_index(enriched_epub: Path):
    """correct_series=True overwrites an existing (wrong) series name AND
    index from a confident Hardcover match."""
    hc = _make_hc_book(series_name="Test Series", series_position="1.5")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    assert result.series_corrected is True
    meta = read_meta(enriched_epub)
    assert meta.series == "Test Series"       # was "Existing Series"
    assert meta.series_index == "1.5"          # was "2"


@pytest.mark.asyncio
async def test_correct_series_populates_missing(bare_epub: Path):
    """correct_series=True still populates a blank series (parity with the
    default populate path), and reports it as a correction."""
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[_make_hc_book()])):
        result = await enrich_file(bare_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    assert result.series_corrected is True
    meta = read_meta(bare_epub)
    assert meta.series == "Test Series"
    assert meta.series_index == "1.5"


@pytest.mark.asyncio
async def test_correct_series_preserves_on_low_confidence(enriched_epub: Path):
    """No confident match -> existing series is NOT blanked/changed."""
    # A candidate whose title/author won't clear the 80% gate.
    hc = _make_hc_book(title="Totally Unrelated Book", author="Someone Else",
                       series_name="Wrong Series", series_position="9")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "low_confidence"
    assert result.series_corrected is False
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"    # untouched
    assert meta.series_index == "2"


@pytest.mark.asyncio
async def test_correct_series_preserves_on_standalone_hit(enriched_epub: Path):
    """Confident match but Hardcover hit has no series -> existing series
    is left intact, never blanked."""
    hc = _make_hc_book(series_name=None, series_position=None)
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    assert result.series_corrected is False
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"    # untouched
    assert meta.series_index == "2"


@pytest.mark.asyncio
async def test_correct_series_false_keeps_skip(enriched_epub: Path):
    """Default (correct_series=False) preserves the legacy skip behaviour."""
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock()) as mock:
        result = await enrich_file(enriched_epub, token="fake", correct_series=False)
    assert result.status == "skipped"
    assert result.reason == "already_enriched"
    mock.assert_not_awaited()
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"


@pytest.mark.asyncio
async def test_correct_series_leaves_description_only_if_empty(enriched_epub: Path):
    """Correcting series does NOT overwrite an existing description."""
    hc = _make_hc_book(series_name="Test Series",
                       description="A DIFFERENT description from Hardcover.")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[hc])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    meta = read_meta(enriched_epub)
    assert meta.description == "Existing description."   # unchanged
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pip install --break-system-packages --user respx pytest-asyncio rapidfuzz` (once), then
`python3 -m pytest tests/test_enrich.py -k correct_series -v`
Expected: FAIL — `enrich_file() got an unexpected keyword argument 'correct_series'` (and `EnrichResult` has no `series_corrected`).

- [ ] **Step 3: Add the `series_corrected` field to `EnrichResult`**

In `ebook_enricher/enrich.py`, change the dataclass:

```python
@dataclass
class EnrichResult:
    status: Status
    reason: Optional[str] = None
    series: Optional[str] = None  # For debugging
    series_corrected: bool = False  # True when correct_series changed name/index
```

- [ ] **Step 4: Add the parameter and relax the gate**

Change the signature:

```python
async def enrich_file(
    path: Path,
    token: str,
    correct_series: bool = False,
) -> EnrichResult:
```

Change the skip gate from:

```python
    if meta.series:
        return EnrichResult(status="skipped", reason="already_enriched")
```

to:

```python
    if meta.series and not correct_series:
        return EnrichResult(status="skipped", reason="already_enriched")
```

- [ ] **Step 5: Make the series writes overwrite-on-correct**

Replace the two series-write lines:

```python
    if not meta.series and chosen.series_name:
        updates.series = chosen.series_name
    if not meta.series_index and chosen.series_position:
        updates.series_index = chosen.series_position
```

with:

```python
    if chosen.series_name and (correct_series or not meta.series):
        updates.series = chosen.series_name
    if chosen.series_position and (correct_series or not meta.series_index):
        updates.series_index = chosen.series_position

    # series_corrected: only True when we actually wrote a new, different
    # value. The truthiness checks on updates.* mean the standalone case
    # (Hardcover hit has no series -> updates.series stays None ->
    # write_meta skips it -> existing tag survives) reports False.
    series_corrected = correct_series and (
        (bool(updates.series) and updates.series != meta.series)
        or (bool(updates.series_index) and updates.series_index != meta.series_index)
    )
    if series_corrected:
        logger.info(
            "series corrected for %s: name %r -> %r, index %r -> %r",
            path.name, meta.series, updates.series,
            meta.series_index, updates.series_index,
        )
```

- [ ] **Step 6: Thread `series_corrected` into the success return**

Find the final success return (currently `return EnrichResult(status="enriched", series=chosen.series_name)`) and change it to:

```python
    return EnrichResult(
        status="enriched",
        series=chosen.series_name,
        series_corrected=series_corrected,
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_enrich.py -k correct_series -v`
Expected: PASS (6 tests).

- [ ] **Step 8: Run the full enrich suite for regressions**

Run: `python3 -m pytest tests/test_enrich.py -v`
Expected: PASS — all prior tests still green (default `correct_series=False` preserves behaviour; `test_skips_already_enriched` still passes).

- [ ] **Step 9: Commit**

```bash
git add ebook_enricher/enrich.py tests/test_enrich.py
git commit -m "feat(enrich): correct_series — overwrite series name/index from Hardcover

When correct_series=True, enrich_file re-evaluates series even if set and
overwrites name+index from a confident match. Never blanks (no-match or
standalone-hit leaves the existing tag). Default False preserves the skip
behaviour. EnrichResult gains series_corrected for reporting.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire `correct_series=True` into the server endpoints

**Files:**
- Modify: `ebook_enricher/server.py` (`BackfillSummary` model ~line 44, `_result_to_dict` ~line 70, `/enrich` ~line 79, `/backfill` ~line 87)
- Test: `tests/test_server.py` (append)

Context: `/enrich` calls `enrich_file(Path(req.path), token=token)`. `/backfill` walks `*.epub` under the ebooks root, calls `enrich_file(path, token=token)` per file, maps `result.status` into a `summary` dict, and returns `BackfillSummary(**summary)`. `_result_to_dict` builds the `/enrich` JSON response.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server.py`. The file already imports `AsyncMock, patch`, `pytest`, `TestClient`, `EnrichResult`, and defines a `client` fixture plus an autouse `_reset_tracker`. Existing tests set the token via `monkeypatch.setenv("HARDCOVER_TOKEN", "fake")` and patch `ebook_enricher.server.enrich_file`. Mirror exactly:

```python
def test_enrich_endpoint_passes_correct_series(client, bare_epub: Path, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    fake = AsyncMock(return_value=EnrichResult(status="enriched", series="S",
                                               series_corrected=True))
    with patch("ebook_enricher.server.enrich_file", new=fake):
        resp = client.post("/enrich", json={"path": str(bare_epub)})
    assert resp.status_code == 200
    # correct_series=True was passed through
    _, kwargs = fake.await_args
    assert kwargs.get("correct_series") is True
    assert resp.json().get("series_corrected") is True


def test_backfill_counts_series_corrected(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HARDCOVER_TOKEN", "fake")
    import ebook_enricher.server as server
    # One epub in a temp ebooks root
    (tmp_path / "a.epub").write_bytes(b"x")
    monkeypatch.setattr(server, "_ebooks_path", lambda: tmp_path)
    monkeypatch.setattr(server, "BACKFILL_DELAY_S", 0)
    fake = AsyncMock(return_value=EnrichResult(status="enriched", series="S",
                                               series_corrected=True))
    with patch("ebook_enricher.server.enrich_file", new=fake):
        resp = client.post("/backfill")
    assert resp.status_code == 200
    body = resp.json()
    assert body["series_corrected"] == 1
    _, kwargs = fake.await_args
    assert kwargs.get("correct_series") is True
```

(Confirm the `_ebooks_path` and `BACKFILL_DELAY_S` symbol names exist in `server.py` before relying on the monkeypatch — both are module-level in the current code. If `_ebooks_path` is read via a different accessor, patch whatever `/backfill` calls to choose its root.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_server.py -k "correct_series or series_corrected" -v`
Expected: FAIL — `correct_series` not passed (kwarg missing) and `series_corrected` not in the backfill summary / response.

- [ ] **Step 3: Add `series_corrected` to the `BackfillSummary` model**

In `ebook_enricher/server.py`, add the field:

```python
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
```

- [ ] **Step 4: Surface `series_corrected` in the `/enrich` response**

Change `_result_to_dict`:

```python
def _result_to_dict(result: EnrichResult) -> dict:
    return {
        "status": result.status,
        "reason": result.reason,
        "series": result.series,
        "series_corrected": result.series_corrected,
    }
```

- [ ] **Step 5: Pass `correct_series=True` from `/enrich`**

```python
@app.post("/enrich")
async def enrich(req: EnrichRequest) -> dict:
    token = _token()
    result = await enrich_file(Path(req.path), token=token, correct_series=True)
    _get_tracker().record(result)
    return _result_to_dict(result)
```

- [ ] **Step 6: Pass `correct_series=True` from `/backfill` and count corrections**

In the `/backfill` summary dict, add the counter key:

```python
    summary = {
        "total": 0, "enriched": 0, "skipped": 0, "no_match": 0,
        "low_confidence": 0, "rate_limited": 0,
        "auth_errors": 0, "network_errors": 0, "errors": 0,
        "series_corrected": 0,
    }
```

In the loop, pass the flag and increment the counter:

```python
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
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_server.py -k "correct_series or series_corrected" -v`
Expected: PASS (2 tests). If `fastapi` is not installed locally, install it (`pip install --break-system-packages --user fastapi`) or defer to the Task 3 live check and note the skip.

- [ ] **Step 8: Run the full server suite for regressions**

Run: `python3 -m pytest tests/test_server.py -v`
Expected: PASS — existing endpoint tests still green (the new summary field is additive).

- [ ] **Step 9: Commit**

```bash
git add ebook_enricher/server.py tests/test_server.py
git commit -m "feat(server): /enrich and /backfill correct series; count corrections

Both endpoints pass correct_series=True so ingest and the library-wide
backfill pass normalise series from Hardcover. BackfillSummary gains a
series_corrected counter; /enrich response surfaces series_corrected.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Deploy and run the library-wide correction pass

**Files:** none (deploy + operational verification)

Context: the container lives at `/opt/stacks/ebook-enricher/` on plexypi (host paths under `/mnt/data/media/ebooks`). It's reached on the `plexypi_default` docker network; get its IP with `docker inspect`. The enricher container has no `curl`, so call it from the host using the container IP.

- [ ] **Step 1: Run the full local suite (minus server if fastapi absent)**

Run: `python3 -m pytest tests/ --ignore=tests/test_server.py -q`
Expected: PASS (existing count + 6 new from Task 1).

- [ ] **Step 2: Deploy the changed modules + tests to plexypi**

```bash
scp ebook_enricher/enrich.py ebook_enricher/server.py \
    plexypi:/opt/stacks/ebook-enricher/ebook_enricher/
scp tests/test_enrich.py tests/test_server.py \
    plexypi:/opt/stacks/ebook-enricher/tests/
```

- [ ] **Step 3: Rebuild and restart the container**

```bash
ssh plexypi 'cd /opt/stacks/ebook-enricher && docker compose build ebook-enricher && docker compose up -d ebook-enricher'
```
Expected: `Container ebook-enricher Started`.

- [ ] **Step 4: Health check**

```bash
ssh plexypi 'IP=$(docker inspect ebook-enricher --format "{{(index .NetworkSettings.Networks \"plexypi_default\").IPAddress}}"); curl -s "http://$IP:8000/health"'
```
Expected: `{"status":"ok"}`.

- [ ] **Step 5: Spot-check correction on the two known cases (single-file `/enrich`)**

```bash
ssh plexypi 'IP=$(docker inspect ebook-enricher --format "{{(index .NetworkSettings.Networks \"plexypi_default\").IPAddress}}"); \
  curl -s -X POST -H "Content-Type: application/json" \
  -d "{\"path\": \"/data/media/ebooks/9 - Hydrogen Sonata, The.epub\"}" \
  http://$IP:8000/enrich'
```
Expected JSON includes `"series_corrected"` (true if Hardcover differs from the current tag; either way the response returns 200 and the series is never blanked). Verify the on-disk tag:
```bash
ssh plexypi 'unzip -p "/mnt/data/media/ebooks/9 - Hydrogen Sonata, The.epub" "*.opf" | grep -o "calibre:series[^/]*content=\"[^\"]*\""'
```

- [ ] **Step 6: Run the library-wide backfill pass**

```bash
ssh plexypi 'IP=$(docker inspect ebook-enricher --format "{{(index .NetworkSettings.Networks \"plexypi_default\").IPAddress}}"); curl -s -X POST http://$IP:8000/backfill'
```
Expected: a `BackfillSummary` JSON with a non-zero `series_corrected` count (this runs ~1 req/sec, several minutes for the full library — run it backgrounded or with a long curl timeout). Cross-check a few series in the bookshelf afterwards once the Kindle stale-sweep re-extracts.

- [ ] **Step 7: Push to origin**

```bash
git push origin main
```

---

## Notes for the executor

- **Don't blank** is the invariant that matters most — the two "preserves" tests (Task 1, steps for low-confidence and standalone-hit) guard it. If either fails, stop and re-read the truthiness guards in Step 5.
- The pipeline's copy-once ledger means `/enrich` at ingest runs once per book, so `correct_series=True` there is a one-time cost — no per-recheck churn.
- `write_meta` preserves mtime/mode/owner, so a series correction won't bump a book to the top of Recently-Added.
