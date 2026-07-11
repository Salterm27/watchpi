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
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id     INTEGER NOT NULL,
    media_type  TEXT    NOT NULL CHECK (media_type IN ('movie', 'tv')),
    title       TEXT    NOT NULL,
    poster_path TEXT,
    added_at    TEXT    NOT NULL,
    watched     INTEGER NOT NULL DEFAULT 0,   -- movies only; tv derives from episodes
    UNIQUE (tmdb_id, media_type)
);

CREATE TABLE IF NOT EXISTS episodes (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    season     INTEGER NOT NULL,
    episode    INTEGER NOT NULL,
    watched_at TEXT    NOT NULL,
    PRIMARY KEY (item_id, season, episode)
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
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def item_to_dict(row, ep_count=0):
    return {
        "id": row["id"],
        "tmdb_id": row["tmdb_id"],
        "media_type": row["media_type"],
        "title": row["title"],
        "poster_path": row["poster_path"],
        "added_at": row["added_at"],
        "watched": bool(row["watched"]),
        "watched_episodes": ep_count,
    }


# ---------------------------------------------------------------- frontend

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- library

@app.get("/api/library")
def list_library():
    db = get_db()
    rows = db.execute(
        """SELECT i.*, COUNT(e.item_id) AS ep_count
           FROM items i LEFT JOIN episodes e ON e.item_id = i.id
           GROUP BY i.id ORDER BY i.added_at DESC"""
    ).fetchall()
    return jsonify([item_to_dict(r, r["ep_count"]) for r in rows])


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
        return jsonify(item_to_dict(row)), 200  # already in library — idempotent
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
    """Set watched flag (movies)."""
    data = request.get_json(silent=True) or {}
    if "watched" not in data:
        return jsonify(error="watched (bool) is required"), 400
    db = get_db()
    cur = db.execute(
        "UPDATE items SET watched=? WHERE id=?", (1 if data["watched"] else 0, item_id)
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="not found"), 404
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return jsonify(item_to_dict(row))


# ---------------------------------------------------------------- episodes

@app.get("/api/library/<int:item_id>/episodes")
def list_episodes(item_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        return jsonify(error="not found"), 404
    rows = db.execute(
        "SELECT season, episode, watched_at FROM episodes WHERE item_id=? ORDER BY season, episode",
        (item_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.put("/api/library/<int:item_id>/episodes")
def set_episodes(item_id):
    """
    Toggle one or many episodes.
    Body: {"episodes": [{"season":1,"episode":2}, ...], "watched": true}
    """
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
    if watched:
        db.executemany(
            "INSERT OR IGNORE INTO episodes (item_id, season, episode, watched_at) VALUES (?,?,?,?)",
            [(item_id, s, e, ts) for s, e in pairs],
        )
    else:
        db.executemany(
            "DELETE FROM episodes WHERE item_id=? AND season=? AND episode=?",
            [(item_id, s, e) for s, e in pairs],
        )
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE item_id=?", (item_id,)
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
