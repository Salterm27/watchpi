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
sudo mkdir -p /srv/apps/watchpi
sudo chown pi:pi /srv/apps/watchpi
# copy app.py, requirements.txt, static/ into /srv/apps/watchpi/

cd /srv/apps/watchpi
python3 -m venv venv
venv/bin/pip install -r requirements.txt

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
   The key lives only in your browser's localStorage.
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

## API (for your launcher app or future scripts)

| Method | Path                          | Body                                              |
|--------|-------------------------------|---------------------------------------------------|
| GET    | /api/library                  | —                                                 |
| POST   | /api/library                  | `{tmdb_id, media_type, title, poster_path}`       |
| PATCH  | /api/library/:id              | `{watched: bool}` (movies)                        |
| DELETE | /api/library/:id              | —                                                 |
| GET    | /api/library/:id/episodes     | —                                                 |
| PUT    | /api/library/:id/episodes     | `{episodes: [{season, episode}], watched: bool}`  |
| GET    | /api/health                   | —                                                 |
