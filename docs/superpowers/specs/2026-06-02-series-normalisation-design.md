# Series Normalisation — Design

**Date**: 2026-06-02
**Status**: Approved for implementation

## Problem

The enricher populates `calibre:series` only when the EPUB has no series set:
`enrich_file` exits early with `skipped("already_enriched")` the moment
`meta.series` is truthy, *before* it even queries Hardcover. This has two
consequences for library consistency:

1. **Inconsistent names survive.** Books that arrived from a torrent with a
   slightly-off series name keep it forever. Real cases found in the
   library: *The Hydrogen Sonata* tagged `The Culture` while its eight
   siblings are `Culture`; *The Long Mars* untagged entirely while its
   siblings are `The Long Earth`. The bookshelf groups by exact series-name
   string, so each variant forms its own stack and the series looks "short".

2. **Blanks only get filled opportunistically.** A book the enricher never
   matched (or never ran over) stays untagged and floats as a standalone.

The fix: a correction capability that re-evaluates a book's series against
Hardcover even when a series is already set, overwriting both the name and
the index from Hardcover when there is a confident match.

## Goal

When enabled, `enrich_file` treats Hardcover as the canonical source for
series name and position: it overwrites an existing (possibly wrong)
`calibre:series` / `calibre:series_index` with Hardcover's values on a
confident match, and populates them where blank. This runs both at ingest
(per-file `/enrich`, once per book via the copy-once pipeline) and as a
library-wide pass (`/backfill`).

## Non-goals

- **Correcting any field other than series.** Description, genres and cover
  keep their existing only-if-empty (or existing cover) behaviour. This
  feature is series name + index only.
- **A dry-run / report-then-apply workflow.** The deployment is headless and
  automated; a report nobody reads adds no value. Changes apply directly when
  the match clears the confidence gate. Occasional misfires are acceptable
  and recoverable (the torrent seed still holds the original metadata, and
  Syncthing versioning retains prior copies).
- **A separate `/normalize-series` endpoint or function.** Correction is a
  parameter on the existing `enrich_file`; `/backfill` is the library-wide
  pass. One code path, no duplicated search/match/write plumbing.
- **Preserving the user's manual index scheme.** The user explicitly chose
  "both name and index from Hardcover", accepting that e.g. *The Hydrogen
  Sonata* renumbers from 9 to Hardcover's value.
- **A per-book revert sidecar for series.** Unlike covers (binary, hard to
  reconstruct), series text is cheap to re-derive and low-stakes; the INFO
  log line (old → new) is sufficient audit.

## Constraints

- **Confidence gate is the existing one.** Reuse the 80% title + 80% author
  fuzzy gate (`matcher.score_match`, `TITLE_THRESHOLD`, `AUTHOR_THRESHOLD`).
  No new threshold.
- **Never blank a series.** Two independent guards: (a) no confident match →
  series untouched; (b) confident match but Hardcover hit has no
  `featured_series` → series untouched. A correction can only ever *change*
  a series to another non-empty value, never erase one.
- **Backward compatible.** `correct_series` defaults to `False`, so the
  existing skip-if-series behaviour is preserved for any caller (and all
  current tests) that doesn't opt in. Only the server endpoints pass `True`.
- **No new dependencies.**

## Architecture

One modified module plus a one-line server change. No new modules, no new
endpoint.

```
ebook_enricher/
├── enrich.py     (MOD)  — correct_series param; relax gate; overwrite series
├── server.py     (MOD)  — /enrich and /backfill pass correct_series=True;
│                          /backfill summary gains a series_corrected counter
└── (matcher.py, hardcover.py, epub_meta.py, cover.py unchanged)
```

`write_meta` already updates an existing `calibre:series` element in place
(`_set_or_add_meta` handles both EPUB-2 `name=`/`content=` and EPUB-3
`property=` forms — see `test_write_updates_existing_property_style`), so no
change is needed there to overwrite.

## `enrich.py` changes

### Signature

```python
async def enrich_file(
    path: Path,
    token: str,
    correct_series: bool = False,
) -> EnrichResult:
```

### Relax the early gate

Current:

```python
if meta.series:
    return EnrichResult(status="skipped", reason="already_enriched")
```

New:

```python
if meta.series and not correct_series:
    return EnrichResult(status="skipped", reason="already_enriched")
```

When `correct_series` is `True`, the function proceeds to query Hardcover
even with a series already set. When `False`, behaviour is unchanged.

### Series write becomes overwrite-on-correct

The current series writes are:

```python
if not meta.series and chosen.series_name:
    updates.series = chosen.series_name
if not meta.series_index and chosen.series_position:
    updates.series_index = chosen.series_position
```

New:

```python
if chosen.series_name and (correct_series or not meta.series):
    updates.series = chosen.series_name
if chosen.series_position and (correct_series or not meta.series_index):
    updates.series_index = chosen.series_position
```

- The `chosen.series_name` / `chosen.series_position` truthiness check is the
  "never blank" guard for the standalone case: if Hardcover's hit has no
  `featured_series`, both are `None`, both writes are skipped, and the
  existing tag survives.
- `chosen` is only non-`None` after the confident-match loop, so a failed
  match (`chosen is None`) returns `low_confidence` / `no_match` before this
  block and never touches the series.

Description, genres and cover blocks are unchanged.

### Reporting

`EnrichResult` gains one field:

```python
@dataclass
class EnrichResult:
    status: Status
    reason: Optional[str] = None
    series: Optional[str] = None
    series_corrected: bool = False  # NEW
```

`series_corrected` is set `True` when `correct_series` was on AND a new,
non-empty series name or index was actually written that differs from what
the EPUB already had (see the guarded computation in Data flow — the
truthiness check ensures the standalone "no Hardcover series" case reports
`False`, not a spurious correction). On a correction, log at INFO:

```
logger.info("series corrected for %s: name %r -> %r, index %r -> %r",
            path.name, meta.series, updates.series,
            meta.series_index, updates.series_index)
```

(Only the fields that actually changed are meaningful; logging both is fine.)

Computing `series_corrected` requires comparing the pre-existing
`meta.series` / `meta.series_index` against the values being written. This
comparison happens in `enrich_file` where both are in scope.

## `server.py` changes

- `/enrich` calls `enrich_file(Path(req.path), token=token, correct_series=True)`.
- `/backfill` calls `enrich_file(path, token=token, correct_series=True)` in
  its loop.
- `/backfill` summary dict gains `"series_corrected": 0`, incremented when a
  result has `series_corrected=True`. Surfaced in the `BackfillSummary`
  response model (add the field).

No change to the `/enrich` response shape is required, but `series_corrected`
may be added to `_result_to_dict` for visibility.

## Data flow

```
enrich_file(path, token, correct_series=True)
  meta = read_meta(path)                      # captures existing series/index
  if meta.series and not correct_series: skip  # (not taken when correcting)
  candidates = search_book(meta.title, meta.author)
  chosen = best candidate clearing 80%/80%     # None if no confident match
  if chosen is None: return low_confidence      # series untouched
  # confident match:
  if chosen.series_name and (correct_series or not meta.series):
      updates.series = chosen.series_name        # overwrite/populate
  if chosen.series_position and (correct_series or not meta.series_index):
      updates.series_index = chosen.series_position
  # description/genres/cover: unchanged (only-if-empty / existing logic)
  # Only count a correction when we actually WROTE a new, different value.
  # Guards the standalone case: if Hardcover has no series, updates.series
  # stays None, write_meta skips it, the existing tag survives, and this
  # must report False (nothing changed) -- hence the truthiness check on
  # updates.* before comparing.
  series_corrected = correct_series and (
      (bool(updates.series) and updates.series != meta.series) or
      (bool(updates.series_index) and updates.series_index != meta.series_index))
  write_meta(path, updates, cover_override=..., cover_add=...)
  return EnrichResult("enriched", series=chosen.series_name,
                      series_corrected=series_corrected)
```

## Error handling

| Situation | Behaviour |
|---|---|
| No confident Hardcover match | `low_confidence` / `no_match`; series untouched |
| Confident match, Hardcover hit is a standalone (no series) | series untouched (truthiness guard) |
| Confident match, Hardcover series == existing | written (idempotent no-op); `series_corrected=False` |
| Confident match, Hardcover series != existing | overwritten; `series_corrected=True`; INFO logged |
| Hardcover query error / rate-limit / auth | existing handling (`network_error` etc.); series untouched |
| `correct_series=False` (default) and series set | `skipped("already_enriched")` — unchanged legacy behaviour |

`write_meta` continues to preserve mtime, mode and ownership; a series-only
correction therefore does not bump the EPUB's Recently-Added position.

## Performance

Removing the early skip means `/backfill` issues one Hardcover query per book
rather than skipping already-enriched ones. At the 1 req/sec backfill delay,
~300 books ≈ 5 minutes. This is an on-demand, headless pass — acceptable.
Per-file `/enrich` at ingest runs once per book (copy-once ledger), so the
extra query there is a one-time cost per book, not per recheck.

## Testing

**`tests/test_enrich.py`** — extend:

- `test_correct_series_overwrites_wrong_name` — EPUB tagged `The Culture`,
  Hardcover returns `Culture` on a confident match; assert EPUB now reads
  `Culture` and `series_corrected=True`.
- `test_correct_series_overwrites_index` — existing index 9, Hardcover says
  10; assert 10 written.
- `test_correct_series_populates_missing` — untagged EPUB, confident match
  with series; assert series + index written.
- `test_correct_series_preserves_on_low_confidence` — existing series, no
  candidate clears the gate; assert series unchanged, status `low_confidence`.
- `test_correct_series_preserves_on_standalone_hit` — existing series,
  confident match but hit has no `featured_series`; assert series unchanged,
  `series_corrected=False`.
- `test_correct_series_false_keeps_skip` — existing series,
  `correct_series=False`; assert `skipped("already_enriched")` (regression).
- `test_correct_series_leaves_description_genres_only_if_empty` — existing
  description present; assert it is not overwritten even while series is
  corrected.

**`tests/test_server.py`** — extend (requires fastapi in the test env):

- `/enrich` passes `correct_series=True` (patch `enrich_file`, assert kwarg).
- `/backfill` passes `correct_series=True` and counts `series_corrected` in
  the summary.

All existing tests must continue to pass unmodified (the default-`False`
parameter guarantees this).

## Deployment

Standard plexypi rsync of `ebook_enricher/` + `docker compose build && up -d`.
No new dependencies, no new endpoint, no config changes.

After deploy, a one-shot `/backfill` corrects the existing library's series
inconsistencies in a single pass (the cases that prompted this feature:
Hydrogen Sonata, Long Mars, and any others lurking). The bookshelf
stale-sweep + re-extraction then propagates the corrected series to devices.

## Out of scope / future

- **Per-book opt-out** (e.g. a `do-not-correct` marker for a deliberately
  custom series) — add only if a real conflict appears.
- **Series-name aliasing** (map Hardcover's name to a user-preferred name) —
  not needed while Hardcover is accepted as canonical.
- **`series_corrected` breakdown in the status EPUB** — backfill log + counter
  is enough for now.
