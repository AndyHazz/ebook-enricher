# ebook-enricher

Enriches EPUB metadata (series, description, genres) on Syncthing-bound ebook copies using the Hardcover GraphQL API.

## Deploy

Deployed to `/opt/stacks/ebook-enricher/` on plexypi. Run:
```
cd /opt/stacks/ebook-enricher
docker compose up -d
```

## Backfill all existing books

```
docker exec ebook-enricher curl -sS -X POST --max-time 1200 http://localhost:8000/backfill
```

## API

- `GET /health` — liveness
- `POST /enrich` — body `{"path": "/data/media/ebooks/<file>.epub"}` — returns status envelope
- `POST /backfill` — walks the ebooks folder, enriches everything

## Development

```
python -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
pytest
```
