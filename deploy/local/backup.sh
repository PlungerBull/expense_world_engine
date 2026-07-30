#!/bin/zsh
# Nightly local backup — deploy/local profile (see README.md).
# pg_dump custom format (already compressed) into iCloud Drive, rotate 30,
# append one log line per run. A silently-failing backup is a sev-1: check
# backup.log's newest line if in doubt.
set -euo pipefail

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
DB="expense_world"
BK="$HOME/Library/Mobile Documents/com~apple~CloudDocs/expense_world_backups"
KEEP=30

mkdir -p "$BK"
STAMP="$(date +%Y-%m-%d_%H%M)"
OUT="$BK/expense_world-$STAMP.dump"

pg_dump -Fc "$DB" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
SIZE=$(du -h "$OUT" | cut -f1 | tr -d ' ')

# Rotate: keep newest $KEEP dumps
ls -1t "$BK"/expense_world-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; done

echo "$(date '+%Y-%m-%d %H:%M:%S') OK $OUT ($SIZE)" >> "$BK/backup.log"
