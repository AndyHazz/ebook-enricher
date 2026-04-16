# Ebook Metadata Enricher — Design

**Date**: 2026-04-16
**Status**: Approved for implementation

## Problem

Ebooks downloaded via qBittorrent RSS auto-download and synced to a Kindle (via Syncthing + KOReader) often have incomplete metadata. Series name and position are frequently missing from the EPUB OPF, and descriptions/blurbs are absent. On the Kindle, this makes browsing a collection harder: books don't group by series, and there's no way to see what a book is about before opening it.

The existing pipeline hardlinks downloaded EPUBs from `/mnt/data/torrents/ebooks/` into `/mnt/data/media/ebooks/` (the Syncthing folder). Hardlinks share an inode with the seeding copy, so editing metadata on the synced copy would corrupt the seeding torrent.

## Goal

Automatically enrich EPUB metadata (series, description, genres) on the Kindle-bound copy of each new ebook, without affecting the seeding copy. Support one-shot backfill of the ~277 already-downloaded books.

## Non-goals

- Not a Calibre replacement — no library management, no reorganising files into `Author/Title/` trees.
- Not editing titles, authors, ISBNs, or covers — those are usually already correct in the EPUB, and churn would be more harmful than helpful.
- Not monitoring success via logs — operator won't read them. Must fail safely when Hardcover doesn't match.

## Constraints

- Plexypi is a Raspberry Pi running Ubuntu — keep resource use low.
- No `pip` on the Pi host or in the qBittorrent container — any new Python deps must live in their own container.
- qBittorrent runs under `network_mode: service:gluetun` (VPN) — new service must be reachable from that network.
- Hardcover API is in beta, free, rate-limited to 60 req/min, server-side only.
- Must never block the download pipeline. If enrichment fails, the EPUB still reaches the Kindle.

---

## Architecture

A new Docker stack at `/opt/stacks/ebook-enricher/` running a single always-on container (`python:3-alpine`, ~60MB image). The service exposes an HTTP API on an internal Docker network.

The existing qBittorrent autorun script is renamed `process-ebook.sh` and modified: `ln` becomes `cp`, and for `.epub` files the script POSTs to the enricher's `/enrich` endpoint after copying.

```
qBit finishes download (tag = ebook)
   │
   ▼
/config/process-ebook.sh (runs in qBit container)
   │ cp (not ln!)  /data/torrents/ebooks/X.epub  →  /data/media/ebooks/X.epub
   │ curl -X POST http://ebook-enricher:8000/enrich  -d {"path":"/data/media/ebooks/X.epub"}
   ▼
ebook-enricher container
   │ read EPUB → extract title/author (ebooklib)
   │ query Hardcover GraphQL
   │ fuzzy-match gate (rapidfuzz, ≥80% on title AND author)
   │ patch OPF metadata (series, description, subjects) — only empty fields
   │ save back to disk
   ▼
Syncthing detects change → syncs to Kindle
```

**Why this shape**: Enricher is one small, focused unit with one clear interface. qBit-side script keeps doing what it does best (moving files) and delegates enrichment. Either component can be restarted or rebuilt independently.

---

## Enricher Internals

Single Python service organised into four focused modules:

```
ebook_enricher/
├── server.py       # FastAPI: POST /enrich, POST /backfill, GET /health
├── epub_meta.py    # read/write EPUB metadata (ebooklib wrapper)
├── hardcover.py    # GraphQL client: search_book(title, author)
├── matcher.py      # fuzzy-match gate (pure functions)
└── enrich.py       # orchestrator
```

Each module has one external dependency (ebooklib, httpx, rapidfuzz) or none. `enrich.py` is the only place that knows about all of them.

### Module responsibilities

**`epub_meta.py`** — thin wrapper over ebooklib.
- `read_meta(path) -> EpubMeta` returns title, author, existing series, description, subjects.
- `write_meta(path, updates) -> None` patches only fields present in `updates`, using Calibre's `calibre:series` / `calibre:series_index` convention (KOReader reads these).

**`hardcover.py`** — GraphQL client.
- `search_book(title, author, token) -> Optional[HardcoverBook]` issues one query, returns top 3 matches ordered by `users_read_count desc`.
- Handles 429 rate limiting with exponential backoff (retry once, then give up).

**`matcher.py`** — pure functions.
- `is_confident_match(epub_title, epub_author, hc_title, hc_author) -> bool` uses `rapidfuzz.ratio()`, requires both title and author ≥80%.

**`enrich.py`** — orchestration.
1. Read EPUB meta.
2. If `calibre:series` already set, skip (respect existing good metadata).
3. Query Hardcover.
4. Iterate top 3 matches, first one passing `is_confident_match` wins.
5. Write back only fields currently empty in the EPUB.

**`server.py`** — FastAPI glue. `POST /enrich` calls `enrich.enrich_file(path)` and returns a status envelope.

---

## Data Flow

### Hardcover GraphQL query

One query per book, pulls everything in a single round trip:

```graphql
query SearchBook($title: String!, $author: String!) {
  books(
    where: {
      _and: [
        { title: { _ilike: $title } },
        { contributions: { author: { name: { _ilike: $author } } } }
      ]
    }
    order_by: { users_read_count: desc }
    limit: 3
  ) {
    id title description
    cached_tags
    book_series {
      position
      featured
      series { name }
    }
    contributions { author { name } }
  }
}
```

### What gets written

Only if the field is currently empty in the EPUB:

| Hardcover field                          | EPUB target                                          |
|------------------------------------------|------------------------------------------------------|
| `book_series.series.name`                 | `<meta name="calibre:series" content="..."/>`        |
| `book_series.position`                    | `<meta name="calibre:series_index" content="..."/>`  |
| `description`                             | `<dc:description>`                                   |
| `cached_tags.Genre` (up to 5)             | multiple `<dc:subject>` elements                     |

**Series selection**: prefer the entry where `featured=true`. If no entry is featured, use the first entry in the `book_series` array. If the array is empty, skip series metadata and continue with description/genres.

**Genre selection**: `cached_tags.Genre` is a user-contributed tag list. Take up to 5 tags ordered by `count` (tag frequency) if present, otherwise in array order.

**Not written**: title, author, ISBN, cover.

### Error handling

Every error is caught and reported in the HTTP response. Nothing ever raised past the server layer:

| Condition                             | Response                                       | EPUB touched? |
|---------------------------------------|------------------------------------------------|---------------|
| Hardcover returns 0 results           | `{"status":"no_match"}`                        | No            |
| All top 3 fail confidence gate        | `{"status":"low_confidence"}`                  | No            |
| EPUB already has `calibre:series`     | `{"status":"skipped","reason":"already_enriched"}` | No        |
| EPUB corrupt / unreadable             | `{"status":"error","reason":"..."}`            | No            |
| Hardcover 429                         | retry once with backoff, then `rate_limited`   | No            |
| Hardcover unreachable                 | `{"status":"error","reason":"network"}`        | No            |

qBit's autorun script logs the HTTP response but never acts on it — enrichment failure must never block the copy. Hardcover being down must not stop ebooks reaching the Kindle.

### Idempotency

The "already has `calibre:series`" check makes `/enrich` idempotent. Running backfill twice does no extra work on previously-enriched books.

### Backfill

`POST /backfill` walks `/data/media/ebooks/**/*.epub`, calls per-file enrichment with a 1s delay between calls (safely under Hardcover's 60/min limit). Returns a summary: `{total, enriched, skipped, no_match, low_confidence, errors}`. Synchronous — for 277 books takes ~5 minutes, acceptable for a one-shot op.

---

## Testing

Strategy by module:

**`matcher.py`** — pure-function unit tests, no fixtures or mocks:
- High-similarity pair passes.
- Low-similarity pair fails.
- Punctuation variants (`"Title: Subtitle"` vs `"Title"`) handled correctly.

**`epub_meta.py`** — needs 2-3 real EPUB fixtures in `tests/fixtures/`:
- `bare.epub` — minimal metadata (typical bad case).
- `enriched.epub` — already has `calibre:series` set (tests skip path).
- `modern.epub` — EPUB 3 with namespaced metadata.
Tests: round-trip write/read, existing fields survive, `calibre:series` appears correctly after write.

**`hardcover.py`** — mocked network with `respx` (httpx mock). Fixtures for: successful match, zero results, 429, malformed response. No live API calls in CI.

**`enrich.py` + `server.py`** — 2-3 integration tests. Start FastAPI, POST to `/enrich` with fixture paths, assert EPUB-on-disk state and response body.

**Not tested in automation**:
- Real Hardcover API calls (flaky, depends on their data).
- Real Syncthing behaviour (system integration, covered by manual test).
- The qBit-side `process-ebook.sh` (tested manually with one download).

**Runner**: `pytest` + `pytest-asyncio`. Full suite should complete in under 5 seconds.

---

## Rollout

### Changes to qBit-side script

Rename `hardlink-ebooks.sh` → `process-ebook.sh`. Replace `ln` with `cp`, add HTTP trigger for `.epub` files:

```bash
if [ ! -f "$SYNC_DIR/$FILENAME" ]; then
    cp "$CONTENT_PATH" "$SYNC_DIR/$FILENAME"
    case "$FILENAME" in
        *.epub)
            curl -sf -m 30 -X POST http://ebook-enricher:8000/enrich \
                -H 'Content-Type: application/json' \
                -d "{\"path\":\"$SYNC_DIR/$FILENAME\"}" > /dev/null 2>&1 || true
            ;;
    esac
fi
```

Update `qBittorrent.conf` `[AutoRun]` to point at the new script name.

**Hardlinks → copies is one-way**: existing hardlinks stay as hardlinks (no retroactive conversion). Only new downloads become copies. Worth knowing that once a Syncthing-side file is a copy, deleting the seeding torrent won't free the Syncthing disk space.

### New stack layout

```
/opt/stacks/ebook-enricher/
├── docker-compose.yml
├── .env                    # HARDCOVER_TOKEN=...
├── Dockerfile
├── pyproject.toml
├── ebook_enricher/
│   ├── __init__.py
│   ├── server.py
│   ├── epub_meta.py
│   ├── hardcover.py
│   ├── matcher.py
│   └── enrich.py
└── tests/
    ├── fixtures/
    └── test_*.py
```

### Networking

qBit runs under `network_mode: service:gluetun` (VPN). The enricher needs to be reachable from inside the gluetun network namespace. Easiest approach: add `ebook-enricher` service to the existing `plexypi` compose stack network. qBit resolves `ebook-enricher` via Docker DNS. Gluetun DNS behaviour is occasionally quirky and must be verified at implementation time; if DNS resolution fails, fall back to using a known IP or adding the service to gluetun's `extra_hosts`.

The enricher's bind mount: `/mnt/data/media/ebooks:/data/media/ebooks` (read-write).

### Secrets

`HARDCOVER_TOKEN` in `/opt/stacks/ebook-enricher/.env`, referenced in compose as `${HARDCOVER_TOKEN}`. Matches existing `.env` pattern in `/opt/stacks/plexypi/`.

### Backfill invocation

Documented in the stack README:

```bash
ssh plexypi "docker exec ebook-enricher curl -sS -X POST http://localhost:8000/backfill"
```

### Manual test plan after deploy

1. Download one test torrent through qBit RSS → verify copy (not hardlink) in Syncthing folder → verify book on Kindle shows series/description.
2. Delete enriched metadata, re-enrich → verify idempotency skip.
3. Run `/backfill` against existing collection → review summary.
4. Test a book that exists in your collection but not on Hardcover → verify it's skipped cleanly, not mis-tagged.

---

## Open Questions / Future Work

None blocking implementation. Possible follow-ons (out of scope for this spec):

- Add Google Books as a fallback source for books Hardcover doesn't know about.
- Auto-fetch cover images when the EPUB lacks one.
- Auto-organise books into `@Series/<name>/` subdirectories based on enriched series data.
- Webhook to ntfy on backfill completion summary.
