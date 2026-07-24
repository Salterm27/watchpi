# WatchPi

A personal JustWatch-style tracker built for a Raspberry Pi 2.

**Architecture:** the browser fetches all metadata (search, posters, streaming
availability) directly from TMDB. The Pi only stores your personal state —
each profile's library and watched episodes — in one SQLite file. The server
never makes an outbound request, so it stays fast on 1GB of RAM and your backup
is tiny.

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
automatically). Requires `sudo apt install restic sqlite3` (sqlite3 is used to
take consistent db snapshots and is NOT preinstalled on Raspberry Pi OS Lite).
Setup is documented in the script header: install restic,
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

Each profile has its **own library** — you see only the titles you added, plus
anything in a shared folder you belong to (see Folders). Watch progress
(episode checkmarks, movie watched flags) is per profile too. Adding a title,
or watching any of it, puts it in your library; a title you engage with via a
shared folder stays yours even if it later leaves the folder. The app shows a
"Who's watching?" picker on first launch; the chosen profile is remembered per
device. Switch anytime via the name chip in the header.

## Library groups

The Library tab organizes titles into three groups, in order:

- **▶ Up next** — things you can actually watch now: series with an unwatched
  *aired* episode, unwatched movies, unfinished games. Always shown first.
- **✓ Watched it all** — fully-watched movies, finished games, and series
  you're caught up on (every aired episode seen). A caught-up show that's still
  airing jumps back to Up next automatically the moment a new episode drops.
- **⏸ Stopped** — abandoned/shelved titles, at the bottom.

The bottom two groups are collapsible and start collapsed (remembered per
device). "Caught up" needs each show's episode structure from TMDB, so the
verdict is cached on your device — the library groups instantly on repeat
opens, then quietly revalidates in the background. Grouping composes with the
folder and Movies/Series/Games filter chips.

## Folders

Organize the library with folders (chips at the top of the Library tab).
When creating a folder you pick who it's shared with — private folders (🔒)
are yours alone, shared folders (👥) are visible **only to their members**
and any member can manage them (edit members, add/remove titles, delete).

Watch progress on a shared folder's titles **syncs between its members** —
the folder tracks one shared position:

- **Adding a title** to a shared folder copies the adder's current progress
  to every member (the app asks for confirmation, since it replaces theirs).
- **While shared**, every mark/unmark propagates to all members.
- **Removing a title** (or leaving) stops the sync; everyone keeps the
  progress they had at that moment.
- **New members** joining get the folder's current position on its titles.

Add/remove a title from folders inside its detail sheet ("＋ New folder"
there creates one on the spot); edit members via Library → ＋ Folders.

## Tonight

The 🌙 Tonight tab is for the "I can't decide what to watch" moment. The
problem is never a lack of options — it's choosing from an open grid while
tired. So Tonight never shows a grid: pick how long you have (~30 min /
~1 hour / movie night / whatever) and who's watching (just you, or a shared
folder — synced progress means everyone in it is at the same episode), tap
**🎲 Pick for me**, and you get exactly ONE card: title, next-up episode and
runtime, where to stream. Accept it or reroll — but only 3 times, because
endless rerolling is just scrolling with extra steps. Candidates come from
your own already-vetted library: shows you're mid-binge on rank first
(freshest first), then shows with a recently aired episode, then unwatched
movies. Caught-up shows, stopped titles, watched movies and games are never
offered. Your choices are remembered per device.

## Feed

The Feed tab shows what other profiles have been watching, newest first,
grouped per show/season/day ("Vale watched 3 episodes of Severance S1 · 2h ago").
Members of shared folders also see sync events there ("Ana added Severance
to 👥 Us two — progress now syncs"). Derived from watch timestamps plus a
small folder-activity log.

## Suggestions

The Search tab shows "✨ Suggested for you" while the box is empty, and an
All / Movies / Series / Games chip row filters both suggestions and search
results. Movies & TV come from TMDB's own per-title recommendations (the
data behind every "More like this" row), seeded with the titles across your
folders: each seed's recommendations are aggregated, titles recommended by
several of your seeds rank first, and anything already in the library is
dropped. Games use RAWG's free tier: other entries in your games' series
rank highest, topped up by a discovery query over your games' most common
genres. Results are cached on the Pi per profile (`/api/suggestions`) so
every device shares one batch; it rebuilds on Fridays and only if your
folders/library changed since the last build (a Refresh button forces it).
Tune the knobs in the `SUGGEST` constant in `static/index.html`.

## Games

The library can track video games too. Metadata comes from
[RAWG](https://rawg.io) the same way movie/TV data comes from TMDB — straight
from the browser, the Pi never calls out. Grab a free key at
rawg.io/apidocs and paste it in ⚙ Settings (optional: without it, games
simply don't appear in search). Games are tracked like movies: a
**✓ finished** flag plus a personal **⏸ shelved** state that greys the cover;
they join folders, shared-progress sync, the Games filter chip, and the feed
("Ana finished Hades"). The detail sheet shows platforms, Metacritic and
average playtime instead of streaming providers.

## New-episode alerts

On open, the app records the visit and asks TMDB (browser-side, as always)
whether any followed show aired episodes since your previous visit. If so, a
banner lists them at the top of the library.

## Auto-deploy (CI/CD)

The Pi polls GitHub every 10 minutes and deploys `main` automatically:
new commits → `git reset --hard` → pip install → service restart → health
check. If `/api/health` doesn't answer within ~20s of the restart, the deploy
**rolls back** to the previously running commit and restarts again — a bad
push can't leave the app down. Look for "ROLLED BACK" in
`journalctl -u watchpi-deploy` if a deploy didn't stick.
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

Endpoints marked (u) require `?user=<profile id>`. Library items include
`watched`, `watched_episodes`, `stopped` and `last_watched_at` (most recent
watch timestamp for the requesting profile — used for the Series sort).

| Method | Path                              | Body                                              |
|--------|-----------------------------------|---------------------------------------------------|
| GET    | /api/users                        | —                                                 |
| POST   | /api/users                        | `{name}`                                          |
| DELETE | /api/users/:id                    | —                                                 |
| GET    | /api/library (u)                  | — (`&include=episodes` adds per-item episode lists) |
| POST   | /api/library (u)                  | `{tmdb_id, media_type, title, poster_path}` (`media_type`: movie/tv/game; RAWG id for games) |
| PATCH  | /api/library/:id (u)              | `{watched?: bool}` (movies) and/or `{stopped?: bool}` |
| DELETE | /api/library/:id (u)              | — (removes from your library only)                |
| GET    | /api/library/:id/episodes (u)     | —                                                 |
| PUT    | /api/library/:id/episodes (u)     | `{episodes: [{season, episode}], watched: bool}`  |
| GET    | /api/folders (u)                  | — (folders you're a member of, incl. `members`)   |
| POST   | /api/folders (u)                  | `{name, member_ids: [user ids]}` (creator always included) |
| PUT    | /api/folders/:id/members (u)      | `{member_ids: [...]}` (any member; newcomers get your progress) |
| DELETE | /api/folders/:id (u)              | —                                                 |
| PUT    | /api/folders/:id/items (u)        | `{item_id, member: bool}` (add copies your progress to members) |
| GET    | /api/feed (u)                     | —                                                 |
| GET    | /api/suggestions (u)              | — cached suggestion batch `{built_at, seed_hash, items}` |
| PUT    | /api/suggestions (u)              | `{seed_hash, items}` (browser-built, see Suggestions) |
| PUT    | /api/users/:id/seen               | `{}` → returns previous open time                 |
| GET    | /api/config                       | —                                                 |
| PUT    | /api/config                       | `{tmdb_key?, region?, rawg_key?}`                 |
| GET    | /api/health                       | —                                                 |

## Tests

The API has a pytest suite (`tests/`) covering users, the shared library,
episode tracking, folder visibility/permissions and shared-folder sync.
Run it before pushing — remember `main` auto-deploys to the Pi:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

Tests run against a throwaway SQLite file; they never touch `data/watchpi.db`.
