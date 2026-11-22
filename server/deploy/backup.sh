#!/usr/bin/env bash
# Safe online backup of the WSB SQLite database.
#
#   server/deploy/backup.sh
#   DB_PATH=/var/lib/wsb/telemetry.db BACKUP_DIR=/var/backups/wsb server/deploy/backup.sh
#
# Uses `sqlite3 .backup`, which is safe while the API is running (WAL mode).
# Old backups are pruned after BACKUP_RETENTION_DAYS (default 7; the legacy
# RETENTION_DAYS variable is still honoured, but RETENTION_DAYS in .env is the
# server's telemetry retention, not a backup setting).
set -euo pipefail

# Backups and their directory must not be readable by other users.
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="${DB_PATH:-${WSB_DB_PATH:-$(cd "$SCRIPT_DIR/.." && pwd)/data/telemetry.db}}"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$DB_PATH")/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-${RETENTION_DAYS:-7}}"

if [ ! -f "$DB_PATH" ]; then
  echo "backup: database not found at $DB_PATH - nothing to back up"
  exit 0
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "backup: sqlite3 CLI is required" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/wsb-telemetry-$STAMP.db"

if [ -e "$OUT" ]; then
  OUT="$BACKUP_DIR/wsb-telemetry-$STAMP-$$.db"
fi
PARTIAL="$OUT.partial"

cleanup() {
  if [ -n "${PARTIAL:-}" ] && [ -e "$PARTIAL" ]; then
    rm -f "$PARTIAL"
  fi
}
trap cleanup EXIT

# Online, consistent copy (does not block writers and flushes the WAL).
# The incomplete copy never carries the final .db name.
sqlite3 "$DB_PATH" ".backup '$PARTIAL'"

CHECK="$(sqlite3 "$PARTIAL" 'PRAGMA quick_check;')"
if [ "$CHECK" != "ok" ]; then
  echo "backup: integrity check failed for $OUT: $CHECK" >&2
  rm -f "$PARTIAL"
  PARTIAL=""
  exit 1
fi

chmod 600 "$PARTIAL"
mv "$PARTIAL" "$OUT"
PARTIAL=""
chmod 600 "$OUT"

SIZE="$(wc -c <"$OUT" | tr -d ' ')"
echo "backup: wrote $OUT (${SIZE} bytes)"

# Prune backups older than BACKUP_RETENTION_DAYS (0 disables pruning).
if [ "$BACKUP_RETENTION_DAYS" -gt 0 ] 2>/dev/null; then
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'wsb-telemetry-*.db' \
    -mtime "+$BACKUP_RETENTION_DAYS" -print -delete |
    while IFS= read -r old; do echo "backup: pruned $old"; done
else
  echo "backup: pruning disabled (BACKUP_RETENTION_DAYS=$BACKUP_RETENTION_DAYS)"
fi
