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
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id     INTEGER NOT NULL,
    media_type  TEXT    NOT NULL CHECK (media_type IN ('movie', 'tv')),
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
    con.commit()
    con.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def item_to_dict(row, watched=False, ep_count=0):
    return {
        "id": row["id"],
        "tmdb_id": row["tmdb_id"],
        "media_type": row["media_type"],
        "title": row["title"],
        "poster_path": row["poster_path"],
        "added_at": row["added_at"],
        "watched": bool(watched),
        "watched_episodes": ep_count,
    }


def current_user():
    """Resolve the ?user=<id> query param to a users row, or None."""
    uid = request.args.get("user", "")
    if not uid.isdigit():
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()


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
               WHERE m.item_id = i.id AND m.user_id = :u)          AS w
           FROM items i ORDER BY i.added_at DESC""",
        {"u": user["id"]},
    ).fetchall()
    return jsonify([item_to_dict(r, r["w"], r["ep_count"]) for r in rows])


@app.post("/api/library")
def add_item():
    data = request.get_json(silent=True) or {}
    try:
        tmdb_id = int(data["tmdb_id"])
        media_type = data["media_type"]
        title = str(data["title"]).strip()
    except (KeyError, TypeError, ValueError):
        return jsonify(error="tmdb_id, media_type and title are required"), 400
    if media_type not in ("movie", "tv") or not title:
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
    """Set per-user watched flag (movies). Requires ?user=<id>."""
    user = current_user()
    if user is None:
        return jsonify(error="valid ?user=<id> is required"), 400
    data = request.get_json(silent=True) or {}
    if "watched" not in data:
        return jsonify(error="watched (bool) is required"), 400
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify(error="not found"), 404
    if data["watched"]:
        db.execute(
            "INSERT OR IGNORE INTO movie_watches (item_id, user_id, watched_at) VALUES (?,?,?)",
            (item_id, user["id"], now()),
        )
    else:
        db.execute(
            "DELETE FROM movie_watches WHERE item_id=? AND user_id=?", (item_id, user["id"])
        )
    db.commit()
    return jsonify(item_to_dict(row, data["watched"]))


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
    if watched:
        db.executemany(
            "INSERT OR IGNORE INTO episodes (item_id, user_id, season, episode, watched_at) VALUES (?,?,?,?,?)",
            [(item_id, uid, s, e, ts) for s, e in pairs],
        )
    else:
        db.executemany(
            "DELETE FROM episodes WHERE item_id=? AND user_id=? AND season=? AND episode=?",
            [(item_id, uid, s, e) for s, e in pairs],
        )
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE item_id=? AND user_id=?", (item_id, uid)
    ).fetchone()["c"]
    return jsonify(ok=True, watched_episodes=count)


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
