#!/bin/bash
# Pull-based auto-deploy: checks GitHub every 10 minutes (see the timer);
# if main has new commits, updates the app and restarts the service.
# Runs as root via watchpi-deploy.service — no webhooks, no exposed ports.

set -euo pipefail
REPO=/srv/apps/watchpi
BRANCH=main

cd "$REPO"
git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

[ "$LOCAL" = "$REMOTE" ] && exit 0   # nothing new — the common case

echo "deploying $LOCAL -> $REMOTE"
git reset --hard "origin/$BRANCH"
chown -R sebas:sebas "$REPO"

# deps (no-op when requirements.txt unchanged)
sudo -u sebas "$REPO/venv/bin/pip" install -q -r requirements.txt

# refresh systemd units if the repo's copies changed
cp "$REPO/deploy/watchpi.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart watchpi

echo "deployed $(git log -1 --oneline)"
