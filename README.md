# ebook-enricher

Small HTTP service that enriches EPUB metadata (series name + position, description, genres) and **replaces or adds covers** using the [Hardcover](https://hardcover.app) GraphQL API. POST a path to an EPUB at the `/enrich` endpoint; it looks the book up on Hardcover and patches the file in place.

Runs as a Docker container. The typical use case is wiring it into whatever pipeline puts EPUBs into your library — a file watcher, a Syncthing receive-only folder with an on-change hook, a cron job, or a manual `curl` for one-off enrichment.

## What it writes, what it doesn't

Only **empty** fields get populated. Title, author and ISBN are never touched — those are usually correct in the EPUB already, and we'd rather not risk churn.

| Hardcover field                          | EPUB target                                         |
|------------------------------------------|-----------------------------------------------------|
| `featured_series.series.name`            | `<meta name="calibre:series" content="..."/>`       |
| `featured_series.position`               | `<meta name="calibre:series_index" content="..."/>` |
| `description`                            | `<dc:description>`                                  |
| `genres` (top 5, deduped case-insensitively) | multiple `<dc:subject>` elements                |
| `image` (with editions fallback)         | EPUB-embedded cover bytes (see Cover replacement below) |

Integer series positions are written as `"1"` not `"1.0"` — Calibre's convention, which KOReader reads verbatim.

If `calibre:series` is already set, the service exits immediately without querying Hardcover. This makes backfill idempotent and leaves any metadata you've manually curated alone.

**File mtime is preserved across the rewrite.** Downstream readers (KOReader bookshelf, Kindle "Recently Added") key their added-date on mtime, so an enrichment pass doesn't artificially jump enriched books to the top of those views.

## Cover replacement

When the enricher matches a book with ≥80% confidence and Hardcover has a cover of sufficient quality, it replaces (or adds) the EPUB's cover during the same atomic rewrite as the metadata update.

Two paths:

- **REPLACE** — EPUB has an existing `<meta name="cover">` declared in OPF. Hardcover's image is fetched, resized to fit Kindle PaperWhite 5 dimensions (longest edge 1648 px, JPEG quality 85), and swapped in. The original cover bytes are preserved as `<book>.original.jpg` next to the EPUB. If the sidecar already exists, it's never overwritten — the sidecar always holds the *true* original.

- **ADD** — EPUB has no cover declared. The enricher mutates the OPF to register a new manifest item + `<meta name="cover"/>` tag, then writes the image bytes at `<opf_dir>/images/cover.jpg` during the same single-pass zip rewrite. No sidecar (nothing to preserve — the original was nothing). A second enrich pass takes the REPLACE path automatically because the OPF now declares the cover.

**Quality gates** (cover replacement only — metadata enrichment proceeds independently):

- Reported image width must be ≥ 500 px (catches placeholder thumbnails).
- Downloaded payload must be ≥ 50 KB (catches tracking pixels / broken assets).

**Editions fallback.** If the canonical search hit's cover is missing or below the 500 px gate, the enricher queries all editions of the matched book on Hardcover and picks the highest-resolution candidate that passes:

- Width ≥ 500 px and aspect ratio in [0.55, 0.85] (rejects audiobook square art).
- Not an audio format (rejects audiobook covers regardless of aspect).
- Language matches the EPUB's `<dc:language>` (primary-subtag comparison — `en` matches `en-US`, `en-GB`, etc.). Editions with no language tag pass through.

Tiebreak: largest pixel area, then most-popular by user count.

If no edition qualifies, the cover swap is skipped — metadata still writes normally. This protects against downgrading a high-resolution publisher cover with a low-resolution Hardcover thumbnail.

## The fuzzy-match gate

Hardcover's search is fuzzy and sometimes returns aggregate results (box sets, omnibus editions) above the specific book. The enricher fetches the top 3 hits and scores each against the EPUB's title and author using `max(rapidfuzz.token_set_ratio, rapidfuzz.partial_ratio)`. The winner must clear 80% on both title *and* author, with ties broken by closest title length.

A wrong-metadata outcome is worse than no-metadata — if no candidate clears the gate, the EPUB is left untouched and the response is `{"status": "low_confidence"}`.

## How to deploy

Requires Docker + docker compose, a `HARDCOVER_TOKEN` from your Hardcover account settings, and a bind mount to the ebook folder you want enriched.

```bash
git clone https://github.com/AndyHazz/ebook-enricher.git
cd ebook-enricher
echo "HARDCOVER_TOKEN=eyJhbGci..." > .env
chmod 600 .env
docker compose up -d --build
```

Edit the `volumes:` entry in `docker-compose.yml` to bind-mount your EPUB folder. The default (`./ebooks:/data/media/ebooks`) works if you drop books into an `ebooks/` subdirectory next to the compose file.

### Network topology

The service listens on `8000/tcp` inside its container. It does **not** expose a host port by default — it's intended to be reached from a sibling container or a process on the host Docker network. Attach it to an existing Docker network by editing the `networks:` block in `docker-compose.yml`:

```yaml
networks:
  your_network_name:
    external: true
```

Then reach it from sibling containers as `http://ebook-enricher:8000/`.

## API

`GET /health` → `{"status": "ok"}`

`POST /enrich` — body `{"path": "/data/media/ebooks/<file>.epub"}`. Returns a status envelope; HTTP 200 regardless of outcome so the caller can distinguish "worked" from "didn't find anything" from "broken":

```json
{"status": "enriched", "reason": null, "series": "Southern Reach"}
```

Possible statuses:

| `status`         | Meaning                                                             |
|------------------|---------------------------------------------------------------------|
| `enriched`       | Metadata written (cover may or may not have been replaced — see logs). |
| `skipped`        | `calibre:series` already set; no change made. `reason=already_enriched`. |
| `no_match`       | Hardcover returned zero hits.                                       |
| `low_confidence` | Hits returned but none cleared the 80% fuzzy gate.                  |
| `rate_limited`   | Two 429s in a row from Hardcover (unusual at normal call rates).    |
| `auth_error`     | GraphQL `errors` array or HTTP 401/403 — token is wrong or expired. |
| `network_error`  | Can't reach Hardcover, or HTTP 4xx/5xx (non-auth).                  |
| `error`          | Local issue — file missing, disk full, unexpected exception.        |

Cover replacement is best-effort and **never blocks metadata enrichment** — a failed cover download, an oversized image that won't resize, or an OPF that can't be mutated all log a warning and let the metadata write proceed.

`POST /backfill` — walks `$EBOOKS_PATH` (defaults to `/data/media/ebooks`) and enriches every `*.epub`. 1-second delay between calls to stay under Hardcover's 60 req/min limit. Returns a summary:

```json
{"total": 35, "enriched": 26, "skipped": 1, "no_match": 2,
 "low_confidence": 6, "rate_limited": 0,
 "auth_errors": 0, "network_errors": 0, "errors": 0}
```

## Triggering enrichment

You can fire `/enrich` from anywhere that can reach the service over HTTP. Examples:

```bash
# One-off: enrich a single file
curl -sS -X POST http://ebook-enricher:8000/enrich \
    -H 'Content-Type: application/json' \
    -d '{"path":"/data/media/ebooks/Example Book.epub"}'

# On-change hook (inotifywait):
inotifywait -m -e close_write --format '%w%f' /data/media/ebooks/ | while read f; do
    case "$f" in *.epub)
        curl -sf -X POST http://ebook-enricher:8000/enrich \
            -H 'Content-Type: application/json' -d "{\"path\":\"$f\"}" || true
    esac
done
```

One important caveat: **the file `/enrich` operates on must be a standalone copy, not a hardlink to any source you care about**. The service rewrites the EPUB in place, which changes its content hash. If the file shares an inode with a source of truth (e.g. a library master), the original will be mutated along with the enriched copy. Always enrich a dedicated copy.

## User-visible error surfacing (the "status EPUB")

If enrichment hits the same actionable error three times in a row — expired token, Hardcover outage — the service drops a small EPUB into the ebooks folder titled e.g. *⚠️ ebook-enricher: Hardcover API rejected the token*, explaining what's wrong and how to fix it. It sorts to the top of your library (filename prefix `_`) so you'll see it the next time you open KOReader.

Any successful enrichment clears the status EPUB automatically. State is in-memory; restarting the container resets the counters.

## Configuration

All via environment variables:

| Var                | Default                  | Purpose                                       |
|--------------------|--------------------------|-----------------------------------------------|
| `HARDCOVER_TOKEN`  | *(required)*             | Hardcover Bearer JWT — get one from your account settings |
| `EBOOKS_PATH`      | `/data/media/ebooks`     | Root folder walked by `/backfill` and where the status EPUB lands |
| `LOG_LEVEL`        | `INFO`                   | Standard Python logging level                |

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
pytest -v
```

Test suite is 145 tests across 8 modules. EPUB fixtures are generated programmatically in `tests/conftest.py` — no binary files checked in. Hardcover API calls are mocked with `respx`, no network required.

### Code layout

```
ebook_enricher/
├── matcher.py          # Pure fuzzy-match scoring (rapidfuzz)
├── epub_meta.py        # EPUB read/write via stdlib zipfile + ElementTree
├── hardcover.py        # Async GraphQL client + editions fallback (httpx)
├── cover.py            # Cover download, resize, sidecar, OPF mutation
├── enrich.py           # Orchestrator — ties everything together
├── server.py           # FastAPI HTTP surface
├── status_epub.py      # Generates the status EPUB
└── status_tracker.py   # Counts consecutive errors, triggers status-EPUB writes
```

Each module has at most one external dependency and can be tested in isolation. `enrich.py` is the only place that knows about all the others.

## Known limitations

- **Hardcover coverage gaps** for very new, self-published, or non-English titles. The German edition of *Never Flinch* ("Kein Zurück") is what Hardcover indexes primarily — our fuzzy gate correctly rejects it but we end up `low_confidence` rather than enriched.
- **Hardcover's 500 px image ceiling** — the majority of Hardcover's cover library is bulk-imported and server-side normalised to exactly 500 px height; only user-uploaded covers retain native resolution. The editions fallback exists specifically to find those higher-res user uploads when the canonical hit is a 500 px thumbnail. For some books no higher-res cover exists anywhere on Hardcover and the EPUB's existing publisher cover is the best you'll get.
- **EPUB 2 `opf:` prefixed attributes** (e.g. `opf:role="aut"` on `dc:creator`) lose their namespace on round-trip because Python's stdlib ElementTree doesn't fully round-trip attribute namespaces. Not a functional issue — EPUB validators accept the result — but worth knowing.
- **In-memory status counters** don't survive a container restart. If Hardcover has been down for a day and you restart, you'll need three more failures before the status EPUB reappears.
