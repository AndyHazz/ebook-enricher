#!/bin/bash
# Example qBittorrent autorun script that pairs with ebook-enricher.
#
# Install: drop this somewhere qBittorrent can see inside its container
# (e.g. /config/process-ebook.sh), make it executable, then point
# qBittorrent's "Run external program on torrent completion" at:
#   /config/process-ebook.sh "%G" "%D" "%F" "%N"
#
# Assumptions:
#   - Your qBittorrent container has both the seed folder (where the
#     download lands) and the sync folder (where Syncthing picks up
#     finished books) bind-mounted under the same parent directory, so
#     hardlinks/copies work across them.
#   - The ebook-enricher service is reachable as `ebook-enricher:8000`
#     from inside the qBittorrent container (same Docker network).
#   - Torrents you want enriched are tagged `ebook` in qBittorrent.
#
# qBittorrent passes these arguments (see docs for %G/%D/%F/%N):
#   %G — torrent tags (comma-separated)
#   %D — save path (the directory the torrent was saved into)
#   %F — content path (the single file OR the multi-file torrent's root dir)
#   %N — torrent name

TAGS="$1"
SAVE_PATH="$2"
CONTENT_PATH="$3"
NAME="$4"

# Edit these to match your bind mounts:
SYNC_BASE="/data/media/ebooks"
SEED_BASE="/data/torrents/ebooks"
ENRICHER_URL="http://ebook-enricher:8000/enrich"

# Only process torrents tagged 'ebook'
case "$TAGS" in
    *ebook*) ;;
    *) exit 0 ;;
esac

# Only process downloads landing under the seed base
case "$SAVE_PATH" in
    ${SEED_BASE}*) ;;
    *) exit 0 ;;
esac

# Mirror the subfolder structure the torrent was saved in
REL_SUB="${SAVE_PATH#$SEED_BASE}"
SYNC_DIR="${SYNC_BASE}${REL_SUB}"

trigger_enrich() {
    local file="$1"
    case "$file" in
        *.epub)
            # Fire-and-forget; enrichment failure must never block the copy
            curl -sf -m 30 -X POST "$ENRICHER_URL" \
                -H 'Content-Type: application/json' \
                -d "{\"path\":\"$file\"}" > /dev/null 2>&1 || true
            ;;
    esac
}

copy_and_enrich() {
    local src="$1" dst="$2"
    if [ ! -f "$dst" ]; then
        # cp, NOT ln — enrichment edits the file; hardlinks would corrupt
        # the seeding torrent's hash.
        cp "$src" "$dst"
        trigger_enrich "$dst"
    fi
}

if [ -f "$CONTENT_PATH" ]; then
    # Single-file torrent
    FILENAME=$(basename "$CONTENT_PATH")
    mkdir -p "$SYNC_DIR"
    copy_and_enrich "$CONTENT_PATH" "$SYNC_DIR/$FILENAME"
elif [ -d "$CONTENT_PATH" ]; then
    # Multi-file torrent — preserve internal structure
    find "$CONTENT_PATH" -type f | while IFS= read -r file; do
        REL_FILE="${file#$SAVE_PATH/}"
        TARGET="$SYNC_DIR/$REL_FILE"
        TARGET_DIR=$(dirname "$TARGET")
        mkdir -p "$TARGET_DIR"
        copy_and_enrich "$file" "$TARGET"
    done
fi
