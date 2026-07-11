# WatchPi

A personal JustWatch-style tracker built for a Raspberry Pi 2.

**Architecture:** the browser fetches all metadata (search, posters, streaming
availability) directly from TMDB. The Pi only stores your personal state —
library and watched episodes — in one SQLite file. The server never makes an
outbound request, so it stays fast on 1GB of RAM and your backup is tiny.

```
Phone browser ──► TMDB API      (search, posters, "where to watch")
      │
      └────────► Pi :8001       (Flask + SQLite: your library & watch history)
```

Streaming availability comes from TMDB's `watch/providers` endpoint, which is
powered by JustWatch data.

## Files

```
watchpi/
├── app.py                     # Flask backend (the only code on the Pi's hot path)
├── requirements.txt           # just flask
├── static/index.html          # entire frontend, single file
└── deploy/
    ├── watchpi.service        # systemd unit for the app
    ├── backup.sh              # restic backup (all of /srv/apps, not just this app)
    ├── watchpi-backup.service # oneshot unit for backup.sh
    └── watchpi-backup.timer   # nightly 03:15
```

## Install on the Pi

```bash
sudo mkdir -p /srv/apps
sudo chown $USER /srv/apps
cd /srv/apps                       # IMPORTANT: absolute path, not ~/srv
git clone https://github.com/Salterm27/watchpi.git
cd watchpi

python3 -m venv venv
venv/bin/pip install -r requirements.txt

# edit deploy/watchpi.service if your username isn't sebas (User= line)
sudo cp deploy/watchpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchpi
curl http://localhost:8001/api/health   # → {"ok": true}
```

Open `http://<pi-hostname>:8001` (or route `/watchpi/*` through Caddy later).

## First run

1. Get a free TMDB API key: themoviedb.org → Settings → API (either the v3 key
   or the v4 "API Read Access Token" works).
2. Open the app, the settings sheet appears — paste the key and set your
   2-letter region (e.g. `AR`, `US`, `ES`) for streaming availability.
   Config is saved on the Pi (`data/config.json`), so every device on your
   network shares it — you only enter it once.
3. On your phone: "Add to Home Screen" to get an app-like launcher.

## Backups

`deploy/backup.sh` backs up **all** of `/srv/apps` (so future apps are covered
automatically). Setup is documented in the script header: install restic,
create `/etc/watchpi/restic.env` with your R2/B2 credentials and a repo
passphrase, run `restic init` once, then:

```bash
sudo cp deploy/backup.sh /srv/apps/backup.sh && sudo chmod +x /srv/apps/backup.sh
sudo cp deploy/watchpi-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchpi-backup.timer
sudo systemctl start watchpi-backup.service   # test run now
```

It snapshots each SQLite db with `sqlite3 .backup` (consistent copies), uploads
only changed chunks, keeps 7 daily / 4 weekly / 6 monthly snapshots, and pings
healthchecks.io if configured. Keep the restic passphrase somewhere off the Pi
— without it backups are unrecoverable.

Restore drill:

```bash
source /etc/watchpi/restic.env
restic snapshots
restic restore latest --target /tmp/restore-test
```

## Profiles

The library is shared between everyone; watch progress (episode checkmarks,
movie watched flags) is per profile. The app shows a "Who's watching?" picker
on first launch; the chosen profile is remembered per device. Switch anytime
via the name chip in the header.

## Folders

Organize the library with folders (chips at the top of the Library tab).
Private folders (🔒) are visible only to their owner. Shared folders (👥) are
visible to everyone — and watch progress on their items **syncs between all
profiles** (mark it watched, it's watched for both of you). Add/remove a title
from folders inside its detail sheet.

## Feed

The Feed tab shows what other profiles have been watching, newest first,
grouped per show/season/day ("Vale watched 3 episodes of Severance S1 · 2h ago").
Derived from watch timestamps — no extra tracking.

## New-episode alerts

On open, the app records the visit and asks TMDB (browser-side, as always)
whether any followed show aired episodes since your previous visit. If so, a
banner lists them at the top of the library.

## Auto-deploy (CI/CD)

The Pi polls GitHub every 10 minutes and deploys `main` automatically:
new commits → `git reset --hard` → pip install → service restart.
One-time setup on the Pi:

```bash
chmod +x /srv/apps/watchpi/deploy/autodeploy.sh
sudo cp deploy/watchpi-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchpi-deploy.timer
sudo systemctl start watchpi-deploy.service     # run once now to test
journalctl -u watchpi-deploy -n 20              # see what it did
```

Anything merged to `main` is live at home within ~10 minutes. Local changes
on the Pi get overwritten by the next deploy — treat the GitHub repo as the
only source of truth.

## API (for your launcher app or future scripts)

Endpoints marked (u) require `?user=<profile id>`.

| Method | Path                              | Body                                              |
|--------|-----------------------------------|---------------------------------------------------|
| GET    | /api/users                        | —                                                 |
| POST   | /api/users                        | `{name}`                                          |
| DELETE | /api/users/:id                    | —                                                 |
| GET    | /api/library (u)                  | —                                                 |
| POST   | /api/library                      | `{tmdb_id, media_type, title, poster_path}`       |
| PATCH  | /api/library/:id (u)              | `{watched: bool}` (movies)                        |
| DELETE | /api/library/:id                  | —                                                 |
| GET    | /api/library/:id/episodes (u)     | —                                                 |
| PUT    | /api/library/:id/episodes (u)     | `{episodes: [{season, episode}], watched: bool}`  |
| GET    | /api/folders (u)                  | —                                                 |
| POST   | /api/folders (u)                  | `{name, shared: bool}`                            |
| DELETE | /api/folders/:id (u)              | —                                                 |
| PUT    | /api/folders/:id/items (u)        | `{item_id, member: bool}`                         |
| GET    | /api/feed (u)                     | —                                                 |
| PUT    | /api/users/:id/seen               | `{}` → returns previous open time                 |
| GET    | /api/config                       | —                                                 |
| PUT    | /api/config                       | `{tmdb_key?, region?}`                            |
| GET    | /api/health                       | —                                                 |
