#!/bin/bash
# Pyxis GDrive Backup — runs nightly via cron at 02:00
# Syncs proxy data cache, audio briefings, and routes to gdrive:Pyxis/backup/
# Excludes tile_cache (auto-regenerated) and large audio files >10MB

RCLONE=/usr/bin/rclone
REMOTE="gdrive:Pyxis/backup"
LOG="/var/log/pyxis_gdrive_backup.log"
SOURCE="/home/icanjumpuddles/manta-comms"

echo "=== Pyxis GDrive Backup $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" >> "$LOG"

# 1. Data directory: JSON caches, scenario files, route files, audio briefings
$RCLONE sync "$SOURCE/data" "$REMOTE/data" \
    --max-size 10M \
    --log-file "$LOG" \
    --log-level INFO \
    --exclude "*.tmp" \
    --exclude "*.wav"

# 2. Proxy script itself (versioned backup)
STAMP=$(date -u '+%Y%m%d')
$RCLONE copyto "$SOURCE/proxy.py" "$REMOTE/proxy_versions/proxy_$STAMP.py" \
    --log-file "$LOG" \
    --log-level INFO

# 3. Tile cache — only map tiles <1MB (basemap tiles, not large renders)
$RCLONE sync "$SOURCE/tile_cache" "$REMOTE/tile_cache" \
    --max-size 1M \
    --max-age 7d \
    --log-file "$LOG" \
    --log-level INFO

echo "=== Backup complete $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" >> "$LOG"

# Keep log from growing — trim to last 500 lines
tail -500 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
