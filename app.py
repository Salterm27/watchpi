#!/usr/bin/env python3
"""
WatchPi — personal watch-tracker backend.

Stores ONLY personal state (library items, watched episodes) in SQLite.
All metadata (titles, posters, streaming availability) is fetched by the
browser directly from TMDB — this server never talks to the internet.

Designed for a Raspberry Pi 2: Flask + SQLite, no other dependencies.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request, send_from_directory

DB_PATH = os.environ.get("WATCHPI_DB", os.path.join(os.path.dirname(__file__), "data", "watchpi.db"))
CONFIG_PATH = os.path.join(os.path.dirname(DB_PATH), "config.json")
DEFAULT_CONFIG = {"tmdb_key": "", "region": "US"}

app = Flask(__name__, static_folder="static", static_url_path="")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- The library is SHARED between users; watch progress is per user.
-- tmdb_id holds the RAWG id for games (namespaced by media_type).
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id     INTEGER NOT NULL,
    media_type  TEXT    NOT NULL CHECK (media_type IN ('movie', 'tv', 'game')),
    title       TEXT    NOT NULL,
    poster_path TEXT,
    added_at    TEXT    NOT NULL,
    UNIQUE (tmdb_id, media_type)
);

CREATE TABLE IF NOT EXISTS episodes (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    season     INTEGER NOT NULL,
    episode    INTEGER NOT NULL,
    watched_at TEXT    NOT NULL,
    PRIMARY KEY (item_id, user_id, season, episode)
);

CREATE TABLE IF NOT EXISTS movie_watches (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    watched_at TEXT    NOT NULL,
    PRIMARY KEY (item_id, user_id)
);

-- Per-user "stopped watching": greys the title out and mutes new-episode
-- alerts for that user only.
CREATE TABLE IF NOT EXISTS stopped (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stopped_at TEXT    NOT NULL,
    PRIMARY KEY (item_id, user_id)
);

-- Folders organize the library. Visibility and progress sync are driven by
-- folder_members: a folder with one member is private, with several it is
-- shared BETWEEN THOSE MEMBERS ONLY and items in it get synced progress.
-- owner_id records the creator (informational; any member can manage).
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    owner_id   INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS folder_items (
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (folder_id, item_id)
);

CREATE TABLE IF NOT EXISTS folder_members (
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (folder_id, user_id)
);

-- Sync/unsync events (title added to / removed from a shared folder) so the
-- feed can show them to that folder's members.
CREATE TABLE IF NOT EXISTS folder_activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT    NOT NULL CHECK (kind IN ('folder_add', 'folder_remove')),
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    at        TEXT    NOT NULL
);

-- Per-user cache of search-tab suggestions. The browser builds them from
-- TMDB's recommendation endpoints (seeded by the user's folder items) and
-- stores them here so every device shares one weekly refresh.
CREATE TABLE IF NOT EXISTS suggestions (
    user_id   INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    built_at  TEXT NOT NULL,
    seed_hash TEXT NOT NULL,
    data      TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    # v2 migration: if an old single-user db exists, move it aside and start fresh
    cols = [r[1] for r in con.execute("PRAGMA table_info(episodes)").fetchall()]
    if cols and "user_id" not in cols:
        con.close()
        os.replace(DB_PATH, DB_PATH + ".v1.bak")
        con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # v3 migration: track when each user last opened the app
    try:
        con.execute("ALTER TABLE users ADD COLUMN last_open_at TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # v4 migration: explicit folder membership. Folders that predate
    # folder_members get rows here once: legacy shared (owner NULL) folders
    # were visible to everyone, private ones only to their owner.
    users = [r[0] for r in con.execute("SELECT id FROM users").fetchall()]
    orphans = con.execute(
        "SELECT id, owner_id FROM folders WHERE id NOT IN (SELECT folder_id FROM folder_members)"
    ).fetchall()
    for fid, owner in orphans:
        targets = users if owner is None else [owner]
        con.executemany(
            "INSERT OR IGNORE INTO folder_members (folder_id, user_id) VALUES (?,?)",
            [(fid, u) for u in targets],
        )
    # v5 migration: allow media_type 'game'. The CHECK constraint can't be
    # altered in SQLite, so rebuild the items table; child tables reference
    # it by name and survive the rename.
    items_sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()[0]
    if "'game'" not in items_sql:
        con.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE items_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id     INTEGER NOT NULL,
                media_type  TEXT    NOT NULL CHECK (media_type IN ('movie', 'tv', 'game')),
                title       TEXT    NOT NULL,
                poster_path TEXT,
                added_at    TEXT    NOT NULL,
                UNIQUE (tmdb_id, media_type)
            );
            INSERT INTO items_new SELECT * FROM items;
            DROP TABLE items;
            ALTER TABLE items_new RENAME TO items;
            PRAGMA foreign_keys=ON;
        """)
    con.commit()
    con.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def item_to_dict(row, watched=False, ep_count=0, stopped=False, last_watched_at=None):
    return {
        "id": row["id"],
        "tmdb_id": row["tmdb_id"],
        "media_type": row["media_type"],
        "title": row["title"],
        "poster_path": row["poster_path"],
        "added_at": row["added_at"],
        "watched": bool(watched),
        "watched_episodes": ep_count,
        "stopped": bool(stopped),
        "last_watched_at": last_watched_at,
    }


def current_user():
    """Resolve the ?user=<id> query param to a users row, or None."""
    uid = request.args.get("user", "")
    if not uid.isdigit():
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()


def synced_user_ids(db, item_id, user_id):
    """Users whose progress on this item moves together with user_id's:
    everyone in shared folders that contain the item AND user_id belongs to.
    Returns just [user_id] when the item isn't shared with them."""
    rows = db.execute(
        """SELECT DISTINCT fm.user_id
           FROM folder_items fi
           JOIN folder_members me ON me.folder_id = fi.folder_id AND me.user_id = :u
           JOIN folder_members fm ON fm.folder_id = fi.folder_id
           WHERE fi.item_id = :i""",
        {"u": user_id, "i": item_id},
    ).fetchall()
    ids = [r["user_id"] for r in rows]
    return ids if ids else [user_id]


def folder_member_ids(db, folder_id):
    return [r["user_id"] for r in db.execute(
        "SELECT user_id FROM folder_members WHERE folder_id=?", (folder_id,)
    ).fetchall()]


def copy_progress(db, item_id, from_user, to_users):
    """Replace to_users' progress on an item with from_user's (episodes and
    movie watched flag) — the folder tracks one shared position."""
    targets = [u for u in to_users if u != from_user]
    if not targets:
        return
    eps = db.execute(
        "SELECT season, episode, watched_at FROM episodes WHERE item_id=? AND user_id=?",
        (item_id, from_user),
    ).fetchall()
    mv = db.execute(
        "SELECT watched_at FROM movie_watches WHERE item_id=? AND user_id=?",
        (item_id, from_user),
    ).fetchone()
    for u in targets:
        db.execute("DELETE FROM episodes WHERE item_id=? AND user_id=?", (item_id, u))
        db.execute("DELETE FROM movie_watches WHERE item_id=? AND user_id=?", (item_id, u))
        db.executemany(
            "INSERT INTO episodes (item_id, user_id, season, episode, watched_at) VALUES (?,?,?,?,?)",
            [(item_id, u, e["season"], e["episode"], e["watched_at"]) for e in eps],
        )
        if mv:
            db.execute(
                "INSERT INTO movie_watches (item_id, user_id, watched_at) VALUES (?,?,?)",
                (item_id, u, mv["watched_at"]),
            )


# ---------------------------------------------------------------- users

@app.get("/api/users")
def list_users():
    rows = get_db().execute("SELECT id, name FROM users ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/users")
def add_user():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 30:
        return jsonify(error="name is required (max 30 chars)"), 400
    db = get_db()
    try:
        cur = db.execute("INSERT INTO users (name, created_at) VALUES (?,?)", (name, now()))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="a profile with that name already exists"), 409
    return jsonify(id=cur.lastrowid, name=name), 201


@app.delete("/api/users/<int:user_id>")
def delete_user(user_id):
    db = get_db()
    cur = db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="not found"), 404
    return "", 204


# ---------------------------------------------------------------- frontend

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- library

@app.get("/api/library")
def list_library():
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    db = get_db()
    rows = db.execute(
        """SELECT i.*,
             (SELECT COUNT(*) FROM episodes e
               WHERE e.item_id = i.id AND e.user_id = :u)          AS ep_count,
             EXISTS(SELECT 1 FROM movie_watches m
               WHERE m.item_id = i.id AND m.user_id = :u)          AS w,
             EXISTS(SELECT 1 FROM stopped s
               WHERE s.item_id = i.id AND s.user_id = :u)          AS st,
             (SELECT MAX(watched_at) FROM episodes e
               WHERE e.item_id = i.id AND e.user_id = :u)          AS last_ep_watched,
             (SELECT watched_at FROM movie_watches m
               WHERE m.item_id = i.id AND m.user_id = :u)          AS movie_watched_at
           FROM items i ORDER BY i.added_at DESC""",
        {"u": user["id"]},
    ).fetchall()
    # folder memberships (only folders this user can see: shared or their own)
    memberships = db.execute(
        """SELECT fi.item_id, fi.folder_id FROM folder_items fi
           JOIN folders f ON f.id = fi.folder_id
           WHERE f.owner_id IS NULL OR f.owner_id = ?""",
        (user["id"],),
    ).fetchall()
    by_item = {}
    for m in memberships:
        by_item.setdefault(m["item_id"], []).append(m["folder_id"])
    # optional: full per-item episode lists in one call (?include=episodes)
    eps_by_item = {}
    if request.args.get("include") == "episodes":
        for e in db.execute(
            "SELECT item_id, season, episode FROM episodes WHERE user_id=?", (user["id"],)
        ).fetchall():
            eps_by_item.setdefault(e["item_id"], []).append([e["season"], e["episode"]])
    out = []
    for r in rows:
        last_watched_at = max(filter(None, [r["last_ep_watched"], r["movie_watched_at"]]), default=None)
        d = item_to_dict(r, r["w"], r["ep_count"], r["st"], last_watched_at)
        d["folders"] = by_item.get(r["id"], [])
        if request.args.get("include") == "episodes":
            d["episodes"] = eps_by_item.get(r["id"], [])
        out.append(d)
    return jsonify(out)


@app.post("/api/library")
def add_item():
    data = request.get_json(silent=True) or {}
    try:
        tmdb_id = int(data["tmdb_id"])
        media_type = data["media_type"]
        title = str(data["title"]).strip()
    except (KeyError, TypeError, ValueError):
        return jsonify(error="tmdb_id, media_type and title are required"), 400
    if media_type not in ("movie", "tv", "game") or not title:
        return jsonify(error="invalid media_type or empty title"), 400

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO items (tmdb_id, media_type, title, poster_path, added_at) VALUES (?,?,?,?,?)",
            (tmdb_id, media_type, title, data.get("poster_path"), now()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        row = db.execute(
            "SELECT * FROM items WHERE tmdb_id=? AND media_type=?", (tmdb_id, media_type)
        ).fetchone()
        return jsonify(item_to_dict(row)), 200  # already in library — idempotent (shared)
    row = db.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(item_to_dict(row)), 201


@app.delete("/api/library/<int:item_id>")
def delete_item(item_id):
    db = get_db()
    cur = db.execute("DELETE FROM items WHERE id=?", (item_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="not found"), 404
    return "", 204


@app.patch("/api/library/<int:item_id>")
def update_item(item_id):
    """Per-user flags. Requires ?user=<id>.
    Body: {"watched": bool} (movies, syncs in shared folders)
          and/or {"stopped": bool} (always personal — mutes alerts, greys out).
    """
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    data = request.get_json(silent=True) or {}
    if "watched" not in data and "stopped" not in data:
        return jsonify(error="watched (bool) and/or stopped (bool) is required"), 400
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    if "watched" in data:
        # items in a shared folder sync watch state to that folder's members
        uids = synced_user_ids(db, item_id, user["id"])
        if data["watched"]:
            db.executemany(
                "INSERT OR IGNORE INTO movie_watches (item_id, user_id, watched_at) VALUES (?,?,?)",
                [(item_id, u, now()) for u in uids],
            )
        else:
            db.executemany(
                "DELETE FROM movie_watches WHERE item_id=? AND user_id=?",
                [(item_id, u) for u in uids],
            )
    if "stopped" in data:
        if data["stopped"]:
            db.execute(
                "INSERT OR IGNORE INTO stopped (item_id, user_id, stopped_at) VALUES (?,?,?)",
                (item_id, user["id"], now()),
            )
        else:
            db.execute(
                "DELETE FROM stopped WHERE item_id=? AND user_id=?", (item_id, user["id"])
            )
    db.commit()
    w = db.execute(
        "SELECT 1 FROM movie_watches WHERE item_id=? AND user_id=?", (item_id, user["id"])
    ).fetchone() is not None
    st = db.execute(
        "SELECT 1 FROM stopped WHERE item_id=? AND user_id=?", (item_id, user["id"])
    ).fetchone() is not None
    ep_count = db.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE item_id=? AND user_id=?", (item_id, user["id"])
    ).fetchone()["c"]
    return jsonify(item_to_dict(row, w, ep_count, st))


# ---------------------------------------------------------------- episodes

@app.get("/api/library/<int:item_id>/episodes")
def list_episodes(item_id):
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        return jsonify(error="not found"), 404
    rows = db.execute(
        """SELECT season, episode, watched_at FROM episodes
           WHERE item_id=? AND user_id=? ORDER BY season, episode""",
        (item_id, user["id"]),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.put("/api/library/<int:item_id>/episodes")
def set_episodes(item_id):
    """
    Toggle one or many episodes for the current user (?user=<id>).
    Body: {"episodes": [{"season":1,"episode":2}, ...], "watched": true}
    """
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    data = request.get_json(silent=True) or {}
    eps = data.get("episodes")
    watched = data.get("watched")
    if not isinstance(eps, list) or not eps or not isinstance(watched, bool):
        return jsonify(error="episodes (non-empty list) and watched (bool) are required"), 400

    db = get_db()
    if not db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        return jsonify(error="not found"), 404

    try:
        pairs = [(int(e["season"]), int(e["episode"])) for e in eps]
    except (KeyError, TypeError, ValueError):
        return jsonify(error="each episode needs integer season and episode"), 400

    ts = now()
    uid = user["id"]
    # items in a shared folder sync watch state to that folder's members
    uids = synced_user_ids(db, item_id, uid)
    if watched:
        db.executemany(
            "INSERT OR IGNORE INTO episodes (item_id, user_id, season, episode, watched_at) VALUES (?,?,?,?,?)",
            [(item_id, u, s, e, ts) for u in uids for s, e in pairs],
        )
    else:
        db.executemany(
            "DELETE FROM episodes WHERE item_id=? AND user_id=? AND season=? AND episode=?",
            [(item_id, u, s, e) for u in uids for s, e in pairs],
        )
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE item_id=? AND user_id=?", (item_id, uid)
    ).fetchone()["c"]
    return jsonify(ok=True, watched_episodes=count, synced=len(uids) > 1)


# ---------------------------------------------------------------- folders

def folder_to_dict(db, row):
    members = db.execute(
        """SELECT u.id, u.name FROM folder_members fm
           JOIN users u ON u.id = fm.user_id
           WHERE fm.folder_id = ? ORDER BY u.id""",
        (row["id"],),
    ).fetchall()
    items = db.execute(
        "SELECT COUNT(*) AS c FROM folder_items WHERE folder_id=?", (row["id"],)
    ).fetchone()["c"]
    return {
        "id": row["id"],
        "name": row["name"],
        "shared": len(members) > 1,
        "items": items,
        "members": [{"id": m["id"], "name": m["name"]} for m in members],
    }


def user_is_member(db, folder_id, user_id):
    return db.execute(
        "SELECT 1 FROM folder_members WHERE folder_id=? AND user_id=?", (folder_id, user_id)
    ).fetchone() is not None


@app.get("/api/folders")
def list_folders():
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    db = get_db()
    rows = db.execute(
        """SELECT f.* FROM folders f
           JOIN folder_members me ON me.folder_id = f.id
           WHERE me.user_id = ? ORDER BY f.name""",
        (user["id"],),
    ).fetchall()
    out = [folder_to_dict(db, r) for r in rows]
    out.sort(key=lambda f: (not f["shared"], f["name"].lower()))
    return jsonify(out)


@app.post("/api/folders")
def add_folder():
    """Body: {"name", "member_ids": [user ids to share with]} — the creator is
    always a member. Legacy {"shared": true} still means share with everyone."""
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 40:
        return jsonify(error="name is required (max 40 chars)"), 400
    db = get_db()
    member_ids = data.get("member_ids")
    if member_ids is not None:
        if not isinstance(member_ids, list) or not all(isinstance(m, int) for m in member_ids):
            return jsonify(error="member_ids must be a list of user ids"), 400
        known = {r["id"] for r in db.execute("SELECT id FROM users").fetchall()}
        if not set(member_ids) <= known:
            return jsonify(error="unknown user in member_ids"), 400
        members = set(member_ids) | {user["id"]}
    elif data.get("shared"):
        members = {r["id"] for r in db.execute("SELECT id FROM users").fetchall()}
    else:
        members = {user["id"]}
    cur = db.execute(
        "INSERT INTO folders (name, owner_id, created_at) VALUES (?,?,?)",
        (name, user["id"], now()),
    )
    db.executemany(
        "INSERT INTO folder_members (folder_id, user_id) VALUES (?,?)",
        [(cur.lastrowid, u) for u in members],
    )
    db.commit()
    row = db.execute("SELECT * FROM folders WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(folder_to_dict(db, row)), 201


@app.put("/api/folders/<int:folder_id>/members")
def set_folder_members(folder_id):
    """Body: {"member_ids": [...]}. Any member can edit. New members' progress
    on the folder's titles is set to the acting user's (the shared position)."""
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    data = request.get_json(silent=True) or {}
    member_ids = data.get("member_ids")
    if not isinstance(member_ids, list) or not member_ids \
            or not all(isinstance(m, int) for m in member_ids):
        return jsonify(error="member_ids (non-empty list of user ids) is required"), 400
    db = get_db()
    row = db.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    if not user_is_member(db, folder_id, user["id"]):
        return jsonify(error="not your folder"), 403
    known = {r["id"] for r in db.execute("SELECT id FROM users").fetchall()}
    if not set(member_ids) <= known:
        return jsonify(error="unknown user in member_ids"), 400
    before = set(folder_member_ids(db, folder_id))
    after = set(member_ids)
    db.execute("DELETE FROM folder_members WHERE folder_id=?", (folder_id,))
    db.executemany(
        "INSERT INTO folder_members (folder_id, user_id) VALUES (?,?)",
        [(folder_id, u) for u in after],
    )
    # bring newcomers to the folder's shared position on every title in it
    joined = after - before
    if joined:
        item_ids = [r["item_id"] for r in db.execute(
            "SELECT item_id FROM folder_items WHERE folder_id=?", (folder_id,)
        ).fetchall()]
        for iid in item_ids:
            copy_progress(db, iid, user["id"], joined)
    db.commit()
    return jsonify(folder_to_dict(db, row))


@app.delete("/api/folders/<int:folder_id>")
def delete_folder(folder_id):
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    db = get_db()
    row = db.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    if not user_is_member(db, folder_id, user["id"]):
        return jsonify(error="not your folder"), 403
    db.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    db.commit()
    return "", 204


@app.put("/api/folders/<int:folder_id>/items")
def set_folder_item(folder_id):
    """Body: {"item_id": N, "member": true|false}. Adding to a shared folder
    copies the adding user's progress to every member (the shared position)
    and logs a feed event; removing logs one too but leaves progress as-is."""
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    member = data.get("member")
    if not isinstance(item_id, int) or not isinstance(member, bool):
        return jsonify(error="item_id (int) and member (bool) are required"), 400
    db = get_db()
    folder = db.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
    if not folder or not db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        return jsonify(error="not found"), 404
    if not user_is_member(db, folder_id, user["id"]):
        return jsonify(error="not your folder"), 403
    members = folder_member_ids(db, folder_id)
    shared = len(members) > 1
    if member:
        already = db.execute(
            "SELECT 1 FROM folder_items WHERE folder_id=? AND item_id=?", (folder_id, item_id)
        ).fetchone() is not None
        db.execute(
            "INSERT OR IGNORE INTO folder_items (folder_id, item_id) VALUES (?,?)",
            (folder_id, item_id),
        )
        if shared and not already:
            copy_progress(db, item_id, user["id"], members)
            db.execute(
                "INSERT INTO folder_activity (kind, user_id, item_id, folder_id, at) VALUES (?,?,?,?,?)",
                ("folder_add", user["id"], item_id, folder_id, now()),
            )
    else:
        removed = db.execute(
            "DELETE FROM folder_items WHERE folder_id=? AND item_id=?", (folder_id, item_id)
        ).rowcount
        if shared and removed:
            db.execute(
                "INSERT INTO folder_activity (kind, user_id, item_id, folder_id, at) VALUES (?,?,?,?,?)",
                ("folder_remove", user["id"], item_id, folder_id, now()),
            )
    db.commit()
    return jsonify(ok=True, synced=shared)


# ---------------------------------------------------------------- feed

@app.get("/api/feed")
def feed():
    """Recent watch activity by OTHER users, newest first. Episode marks are
    grouped per user+show+season+day."""
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    db = get_db()
    events = []
    ep_rows = db.execute(
        """SELECT u.name AS user_name, i.id AS item_id, i.tmdb_id, i.media_type,
                  i.title, i.poster_path, e.season,
                  COUNT(*) AS n, MAX(e.watched_at) AS at
           FROM episodes e
           JOIN users u ON u.id = e.user_id
           JOIN items i ON i.id = e.item_id
           WHERE e.user_id != ?
           GROUP BY e.user_id, e.item_id, e.season, DATE(e.watched_at)
           ORDER BY at DESC LIMIT 50""",
        (user["id"],),
    ).fetchall()
    for r in ep_rows:
        events.append({**dict(r), "kind": "episodes"})
    mv_rows = db.execute(
        """SELECT u.name AS user_name, i.id AS item_id, i.tmdb_id, i.media_type,
                  i.title, i.poster_path, m.watched_at AS at
           FROM movie_watches m
           JOIN users u ON u.id = m.user_id
           JOIN items i ON i.id = m.item_id
           WHERE m.user_id != ?
           ORDER BY m.watched_at DESC LIMIT 50""",
        (user["id"],),
    ).fetchall()
    for r in mv_rows:
        events.append({**dict(r), "kind": "movie"})
    # sync/unsync events for shared folders this user belongs to
    fa_rows = db.execute(
        """SELECT u.name AS user_name, i.id AS item_id, i.tmdb_id, i.media_type,
                  i.title, i.poster_path, f.name AS folder_name, a.kind, a.at
           FROM folder_activity a
           JOIN users u ON u.id = a.user_id
           JOIN items i ON i.id = a.item_id
           JOIN folders f ON f.id = a.folder_id
           JOIN folder_members me ON me.folder_id = a.folder_id AND me.user_id = :me
           WHERE a.user_id != :me
           ORDER BY a.at DESC LIMIT 50""",
        {"me": user["id"]},
    ).fetchall()
    events.extend(dict(r) for r in fa_rows)
    events.sort(key=lambda e: e["at"], reverse=True)
    return jsonify(events[:50])


# ---------------------------------------------------------------- suggestions

@app.get("/api/suggestions")
def get_suggestions():
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    row = get_db().execute(
        "SELECT built_at, seed_hash, data FROM suggestions WHERE user_id=?", (user["id"],)
    ).fetchone()
    if not row:
        return jsonify(built_at=None, seed_hash=None, items=[])
    try:
        items = json.loads(row["data"])
    except ValueError:
        items = []
    return jsonify(built_at=row["built_at"], seed_hash=row["seed_hash"], items=items)


@app.put("/api/suggestions")
def put_suggestions():
    """Body: {"seed_hash": str, "items": [...]} — browser-built, stored per user."""
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    data = request.get_json(silent=True) or {}
    seed_hash = str(data.get("seed_hash", ""))
    items = data.get("items")
    if not seed_hash or not isinstance(items, list):
        return jsonify(error="seed_hash (str) and items (list) are required"), 400
    db = get_db()
    db.execute(
        """INSERT INTO suggestions (user_id, built_at, seed_hash, data) VALUES (?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET built_at=excluded.built_at,
             seed_hash=excluded.seed_hash, data=excluded.data""",
        (user["id"], now(), seed_hash, json.dumps(items)),
    )
    db.commit()
    return jsonify(ok=True, built_at=now())


# ---------------------------------------------------------------- last open

@app.put("/api/users/<int:user_id>/seen")
def mark_seen(user_id):
    """Record 'user opened the app now'; returns the PREVIOUS open time so the
    client can check TMDB for episodes aired since then."""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    prev = row["last_open_at"]
    db.execute("UPDATE users SET last_open_at=? WHERE id=?", (now(), user_id))
    db.commit()
    return jsonify(previous_open_at=prev)


# ---------------------------------------------------------------- config
# Shared app config (TMDB key, region) stored on the Pi so every device
# gets it automatically. Browsers still call TMDB directly.

def read_config():
    try:
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)


@app.get("/api/config")
def get_config():
    return jsonify(read_config())


@app.put("/api/config")
def set_config():
    data = request.get_json(silent=True) or {}
    cfg = read_config()
    if "tmdb_key" in data:
        cfg["tmdb_key"] = str(data["tmdb_key"]).strip()
    if "rawg_key" in data:
        cfg["rawg_key"] = str(data["rawg_key"]).strip()
    if "region" in data:
        region = str(data["region"]).strip().upper()
        if len(region) != 2 or not region.isalpha():
            return jsonify(error="region must be a 2-letter country code"), 400
        cfg["region"] = region
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    return jsonify(cfg)


# ---------------------------------------------------------------- health

@app.get("/api/health")
def health():
    return jsonify(ok=True)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("WATCHPI_PORT", 8001)))
