# ebook-enricher

Small HTTP service that enriches EPUB metadata (series name + position, description, genres) using the [Hardcover](https://hardcover.app) GraphQL API. Designed to sit next to qBittorrent as a Docker sidecar: every time a new ebook finishes downloading, qBit's autorun script copies it into a Syncthing-backed folder and pings this service, which looks up the book on Hardcover and patches the EPUB in place. Syncthing then carries the enriched file to whatever reader you use.

Built for a specific pipeline — qBittorrent → Syncthing folder → Kindle / KOReader — but the service itself is generic: give it a path to an EPUB and an environment variable, it updates the file.

## What it writes, what it doesn't

Only **empty** fields get populated. Title, author, ISBN and cover are never touched — those are usually correct in the EPUB already, and we'd rather not risk churn.

| Hardcover field                          | EPUB target                                         |
|------------------------------------------|-----------------------------------------------------|
| `featured_series.series.name`            | `<meta name="calibre:series" content="..."/>`       |
| `featured_series.position`               | `<meta name="calibre:series_index" content="..."/>` |
| `description`                            | `<dc:description>`                                  |
| `genres` (top 5, deduped case-insensitively) | multiple `<dc:subject>` elements                |

Integer series positions are written as `"1"` not `"1.0"` — Calibre's convention, which KOReader reads verbatim.

If `calibre:series` is already set, the service exits immediately without querying Hardcover. This makes backfill idempotent and leaves any metadata you've manually curated alone.

## The fuzzy-match gate

Hardcover's search is fuzzy and sometimes returns aggregate results (box sets, omnibus editions) above the specific book. The enricher fetches the top 3 hits and scores each against the EPUB's title and author using `max(rapidfuzz.token_set_ratio, rapidfuzz.partial_ratio)`. The winner must clear 80% on both title *and* author, with ties broken by closest title length.

A wrong-metadata outcome is worse than no-metadata — if no candidate clears the gate, the EPUB is left untouched and the response is `{"status": "low_confidence"}`.

## How to deploy

Requires Docker + docker compose, a `HARDCOVER_TOKEN` from your Hardcover account settings, and a bind mount to the ebook folder you want enriched.

```bash
git clone https://github.com/AndyHazz/ebook-enricher.git /opt/stacks/ebook-enricher
cd /opt/stacks/ebook-enricher
echo "HARDCOVER_TOKEN=eyJhbGci..." > .env
chmod 600 .env
docker compose up -d --build
```

The default `docker-compose.yml` expects `/mnt/data/media/ebooks` to exist on the host (my layout). Adjust the `volumes:` entry to match yours.

### Network topology

The service listens on `8000/tcp` inside its container. It does **not** expose a host port by default — it's intended to be reached from a sibling container (qBittorrent, Sonarr, a custom hook) over the Docker network. If you use a different compose project, edit the `networks:` block in `docker-compose.yml`:

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
| `enriched`       | Metadata written.                                                   |
| `skipped`        | `calibre:series` already set; no change made. `reason=already_enriched`. |
| `no_match`       | Hardcover returned zero hits.                                       |
| `low_confidence` | Hits returned but none cleared the 80% fuzzy gate.                  |
| `rate_limited`   | Two 429s in a row from Hardcover (unusual at normal call rates).    |
| `auth_error`     | GraphQL `errors` array or HTTP 401/403 — token is wrong or expired. |
| `network_error`  | Can't reach Hardcover, or HTTP 4xx/5xx (non-auth).                  |
| `error`          | Local issue — file missing, disk full, unexpected exception.        |

`POST /backfill` — walks `$EBOOKS_PATH` (defaults to `/data/media/ebooks`) and enriches every `*.epub`. 1-second delay between calls to stay under Hardcover's 60 req/min limit. Returns a summary:

```json
{"total": 35, "enriched": 26, "skipped": 1, "no_match": 2,
 "low_confidence": 6, "rate_limited": 0,
 "auth_errors": 0, "network_errors": 0, "errors": 0}
```

## Hooking it into qBittorrent

Set qBit's "Run external program on torrent completion" to a small shell script that copies the downloaded ebook into your sync folder and then `curl`s this service. A working example lives in `examples/process-ebook.sh`:

```bash
#!/bin/bash
# qBittorrent autorun. Called with: %G (tags) %D (save path) %F (content path) %N (name)
TAGS="$1"; SAVE_PATH="$2"; CONTENT_PATH="$3"; NAME="$4"

case "$TAGS" in *ebook*) ;; *) exit 0 ;; esac
# ... copy to sync folder ...
curl -sf -m 30 -X POST http://ebook-enricher:8000/enrich \
    -H 'Content-Type: application/json' \
    -d "{\"path\":\"/data/media/ebooks/${NAME}.epub\"}" > /dev/null 2>&1 || true
```

Key detail: use `cp`, not `ln`. Hardlinks share an inode with the seeding copy, so editing the enriched copy's metadata would corrupt the torrent.

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

Test suite is 65 tests across 7 modules. EPUB fixtures are generated programmatically in `tests/conftest.py` — no binary files checked in. Hardcover API calls are mocked with `respx`, no network required.

### Code layout

```
ebook_enricher/
├── matcher.py          # Pure fuzzy-match scoring (rapidfuzz)
├── epub_meta.py        # EPUB read/write via stdlib zipfile + ElementTree
├── hardcover.py        # Async GraphQL client (httpx)
├── enrich.py           # Orchestrator — ties the three above together
├── server.py           # FastAPI HTTP surface
├── status_epub.py      # Generates the status EPUB
└── status_tracker.py   # Counts consecutive errors, triggers status-EPUB writes
```

Each module has at most one external dependency and can be tested in isolation. `enrich.py` is the only place that knows about all the others.

## Known limitations

- **Hardcover coverage gaps** for very new, self-published, or non-English titles. The German edition of *Never Flinch* ("Kein Zurück") is what Hardcover indexes primarily — our fuzzy gate correctly rejects it but we end up `low_confidence` rather than enriched.
- **EPUB 2 `opf:` prefixed attributes** (e.g. `opf:role="aut"` on `dc:creator`) lose their namespace on round-trip because Python's stdlib ElementTree doesn't fully round-trip attribute namespaces. Not a functional issue — EPUB validators accept the result — but worth knowing.
- **In-memory status counters** don't survive a container restart. If Hardcover has been down for a day and you restart, you'll need three more failures before the status EPUB reappears.

