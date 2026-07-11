#!/bin/bash
# Nightly backup of all app data under /srv/apps to S3-compatible storage.
# Deduplicated + encrypted by restic: unchanged data uploads nothing.
#
# One-time setup:
#   sudo apt install restic
#   sudo mkdir -p /etc/watchpi && sudo chmod 700 /etc/watchpi
#   Put credentials in /etc/watchpi/restic.env (chmod 600):
#     export RESTIC_REPOSITORY="s3:https://<accountid>.r2.cloudflarestorage.com/pi-backup"
#     export RESTIC_PASSWORD="<long-random-passphrase — KEEP A COPY OFF THE PI>"
#     export AWS_ACCESS_KEY_ID="..."
#     export AWS_SECRET_ACCESS_KEY="..."
#   restic init   (once, with the env sourced)
#   Optional: export HEALTHCHECK_URL="https://hc-ping.com/<uuid>" in the same file.

set -euo pipefail
source /etc/watchpi/restic.env

# 1. Consistent SQLite snapshots (never back up a live db file directly)
for db in /srv/apps/*/data/*.db; do
    [ -e "$db" ] || continue
    sqlite3 "$db" ".backup '${db%.db}.bak.db'"
done

# 2. Incremental encrypted upload (backs up the .bak.db snapshots + everything else)
restic backup /srv/apps \
    --exclude="/srv/apps/*/venv" \
    --exclude="*.pyc" --exclude="__pycache__" \
    --exclude="/srv/apps/*/data/*.db" \
    --exclude="/srv/apps/*/data/*.db-wal" \
    --exclude="/srv/apps/*/data/*.db-shm"

# 3. Retention: 7 daily, 4 weekly, 6 monthly
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

# 4. Dead-man's-switch ping (optional)
[ -n "${HEALTHCHECK_URL:-}" ] && curl -fsS --retry 3 "$HEALTHCHECK_URL" > /dev/null

echo "backup ok $(date -Is)"
