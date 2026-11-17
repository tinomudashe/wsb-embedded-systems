#!/usr/bin/env bash
# Safe online backup of the WSB SQLite database.
#
#   server/deploy/backup.sh
#   DB_PATH=/var/lib/wsb/telemetry.db BACKUP_DIR=/var/backups/wsb server/deploy/backup.sh
#
# Uses `sqlite3 .backup`, which is safe while the API is running (WAL mode).
# Old backups are pruned after RETENTION_DAYS (default 7).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="${DB_PATH:-${WSB_DB_PATH:-$(cd "$SCRIPT_DIR/.." && pwd)/data/telemetry.db}}"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$DB_PATH")/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

if [ ! -f "$DB_PATH" ]; then
  echo "backup: database not found at $DB_PATH - nothing to back up"
  exit 0
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "backup: sqlite3 CLI is required" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/wsb-telemetry-$STAMP.db"

if [ -e "$OUT" ]; then
  OUT="$BACKUP_DIR/wsb-telemetry-$STAMP-$$.db"
fi

# Online, consistent copy (does not block writers and flushes the WAL).
sqlite3 "$DB_PATH" ".backup '$OUT'"

CHECK="$(sqlite3 "$OUT" 'PRAGMA quick_check;')"
if [ "$CHECK" != "ok" ]; then
  echo "backup: integrity check failed for $OUT: $CHECK" >&2
  rm -f "$OUT"
  exit 1
fi

SIZE="$(wc -c <"$OUT" | tr -d ' ')"
echo "backup: wrote $OUT (${SIZE} bytes)"

# Prune backups older than RETENTION_DAYS (0 disables pruning).
if [ "$RETENTION_DAYS" -gt 0 ] 2>/dev/null; then
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'wsb-telemetry-*.db' \
    -mtime "+$RETENTION_DAYS" -print -delete |
    while IFS= read -r old; do echo "backup: pruned $old"; done
else
  echo "backup: pruning disabled (RETENTION_DAYS=$RETENTION_DAYS)"
fi
