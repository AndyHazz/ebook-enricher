#!/bin/bash
# qBittorrent autorun: pre-validate, then delegate to Python helper.
# Called with: %G (tags) %D (save path) %F (content path) %N (name)
#
# This script intentionally does NO copying or enrichment — that all
# happens in process-ebook.py. We keep this thin so qBittorrent's
# autorun wiring (which calls a single executable script) doesn't
# need to change as the pipeline evolves.

TAGS="$1"
SAVE_PATH="$2"
CONTENT_PATH="$3"

SYNC_BASE="/data/media/ebooks"
SEED_BASE="/data/torrents/ebooks"
ENRICHER_URL="http://ebook-enricher:8000/enrich"
# Copy-once ledger lives in /config (qBittorrent's persistent bind mount),
# deliberately OUTSIDE the synced tree so it never propagates to devices.
LEDGER_PATH="/config/published-ledger.json"

case "$TAGS" in
    *ebook*) ;;
    *) exit 0 ;;
esac

case "$SAVE_PATH" in
    ${SEED_BASE}*) ;;
    *) exit 0 ;;
esac

exec python3 /config/process-ebook.py \
    --source "$CONTENT_PATH" \
    --save-path "$SAVE_PATH" \
    --sync-base "$SYNC_BASE" \
    --enricher-url "$ENRICHER_URL" \
    --ledger-path "$LEDGER_PATH"
