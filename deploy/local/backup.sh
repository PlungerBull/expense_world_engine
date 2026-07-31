#!/bin/zsh
# Local backup — deploy/local profile (see README.md).
# pg_dump custom format (already compressed) into iCloud Drive, rotate 30,
# append one log line per run. A silently-failing backup is a sev-1: check
# backup.log's newest line if in doubt.
#
# Triggered on login + every ~24h of uptime rather than at a wall-clock hour,
# because the laptop's open hours are unpredictable and a 02:30 job on a closed
# machine simply never runs. That means this can fire several times a day, so
# it self-limits to one dump per calendar day: KEEP=30 then buys 30 *days* of
# history rather than 30 dumps spanning an arbitrary window.
# Force an extra dump with: FORCE=1 backup.sh
set -euo pipefail

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
DB="expense_world"
# Google Drive (via Drive for Desktop's local mount), NOT iCloud Drive.
# iCloud is TCC-protected against launchd agents in a way that fails *silently*:
# new files can be created, but directory listing returns empty and existing
# files can't be modified — so rotation silently kept everything and the log
# append failed. Google Drive's mount has no such restriction (verified:
# mkdir/create/append/list/delete all succeed from a launchd job), so no Full
# Disk Access grant is needed. Drive for Desktop handles the upload itself.
BK="$HOME/Library/CloudStorage/GoogleDrive-alexterfer@gmail.com/My Drive/expense_world/backups"
KEEP=30

# mkdir -p must run before anything writes here: the agent may only enumerate and
# modify a directory it created itself, so this call is what earns that right.
mkdir -p "$BK"

# At login this agent starts in parallel with Homebrew's postgres service, and
# pg_dump against a socket that doesn't exist yet aborts the run under `set -e`:
# exit 1, no dump that day, healthcheck red until the next fire 24h later. Wait
# for the socket instead of racing it. 60s is far beyond a normal local start.
tries=0
while ! pg_isready -q; do
  tries=$(( tries + 1 ))
  if (( tries >= 30 )); then
    echo "$(date '+%Y-%m-%d %H:%M:%S') FAIL postgres not up after 60s" >> "$BK/backup.log"
    exit 1
  fi
  sleep 2
done
TODAY="$(date +%Y-%m-%d)"
# null_glob so a no-match yields an empty array instead of a zsh glob error.
setopt null_glob
todays_dumps=("$BK"/expense_world-"$TODAY"_*.dump)
unsetopt null_glob
if [[ "${FORCE:-0}" != "1" && ${#todays_dumps[@]} -gt 0 ]]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP already have a dump for $TODAY" >> "$BK/backup.log"
  exit 0
fi

STAMP="$(date +%Y-%m-%d_%H%M)"
OUT="$BK/expense_world-$STAMP.dump"

pg_dump -Fc "$DB" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
SIZE=$(du -h "$OUT" | cut -f1 | tr -d ' ')

# Rotate: keep newest $KEEP dumps. null_glob because under $KEEP dumps is the
# normal case, and a bare unmatched glob is a hard error in zsh (`no matches
# found`), not an empty list. Sort newest-first by name — the timestamp is in
# the filename, so this needs no stat() and no dependence on mtime.
setopt null_glob
all_dumps=("$BK"/expense_world-*.dump)
unsetopt null_glob
if (( ${#all_dumps[@]} > KEEP )); then
  print -rl -- ${(On)all_dumps} | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; done
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') OK $OUT ($SIZE)" >> "$BK/backup.log"
