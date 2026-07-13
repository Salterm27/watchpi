#!/bin/bash
# Pull-based auto-deploy: checks GitHub every 10 minutes (see the timer);
# if main has new commits, updates the app and restarts the service.
# Runs as root via watchpi-deploy.service — no webhooks, no exposed ports.

set -euo pipefail
REPO=/srv/apps/watchpi
BRANCH=main
APPUSER=sebas
PORT=8001   # keep in sync with WATCHPI_PORT in deploy/watchpi.service

# service must answer /api/health within ~20s of a restart
healthy() {
  for _ in $(seq 1 10); do
    if curl -sf "http://localhost:$PORT/api/health" >/dev/null; then return 0; fi
    sleep 2
  done
  return 1
}

# run git as the repo owner — root running git in a user-owned repo trips
# git's "dubious ownership" safety check
GIT() { runuser -u "$APPUSER" -- git -C "$REPO" "$@"; }

GIT fetch --quiet origin "$BRANCH"
LOCAL=$(GIT rev-parse HEAD)
REMOTE=$(GIT rev-parse "origin/$BRANCH")

[ "$LOCAL" = "$REMOTE" ] && exit 0   # nothing new — the common case

echo "deploying $LOCAL -> $REMOTE"
GIT reset --hard "origin/$BRANCH"

# deps (no-op when requirements.txt unchanged)
runuser -u "$APPUSER" -- "$REPO/venv/bin/pip" install -q -r "$REPO/requirements.txt"

# refresh systemd units if the repo's copies changed
cp "$REPO/deploy/watchpi.service" /etc/systemd/system/
chmod +x "$REPO/deploy/autodeploy.sh"   # web uploads drop the exec bit
systemctl daemon-reload
systemctl restart watchpi

if healthy; then
  echo "deployed $(GIT log -1 --oneline)"
  exit 0
fi

# health check failed — roll back to the commit that was running before
echo "health check FAILED after deploy — ROLLING BACK to $LOCAL"
GIT reset --hard "$LOCAL"
runuser -u "$APPUSER" -- "$REPO/venv/bin/pip" install -q -r "$REPO/requirements.txt"
cp "$REPO/deploy/watchpi.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart watchpi
if healthy; then
  echo "ROLLED BACK to $(GIT log -1 --oneline) — fix $REMOTE and push again"
else
  echo "ROLLBACK ALSO UNHEALTHY — manual intervention needed (journalctl -u watchpi)"
fi
exit 1
