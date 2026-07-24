"""API tests: run with  python -m pytest tests/ -v  (needs requirements-dev.txt)."""


def make_user(client, name):
    r = client.post("/api/users", json={"name": name})
    assert r.status_code == 201
    return r.get_json()["id"]


def add_item(client, uid, tmdb_id=1, media_type="tv", title="Show"):
    r = client.post(f"/api/library?user={uid}", json={"tmdb_id": tmdb_id, "media_type": media_type, "title": title})
    assert r.status_code in (200, 201)
    return r.get_json()["id"]


def get_item(client, uid, item_id):
    items = client.get(f"/api/library?user={uid}").get_json()
    return next(i for i in items if i["id"] == item_id)


def in_library(client, uid, item_id):
    return any(i["id"] == item_id for i in client.get(f"/api/library?user={uid}").get_json())


def mark_episodes(client, uid, item_id, pairs, watched=True):
    r = client.put(
        f"/api/library/{item_id}/episodes?user={uid}",
        json={"episodes": [{"season": s, "episode": e} for s, e in pairs], "watched": watched},
    )
    assert r.status_code == 200
    return r.get_json()


def make_folder(client, uid, name, shared):
    r = client.post(f"/api/folders?user={uid}", json={"name": name, "shared": shared})
    assert r.status_code == 201
    return r.get_json()["id"]


def put_in_folder(client, uid, folder_id, item_id, member=True):
    return client.put(f"/api/folders/{folder_id}/items?user={uid}", json={"item_id": item_id, "member": member})


# ---------------------------------------------------------------- users

def test_user_create_list_delete(client):
    uid = make_user(client, "Ana")
    assert [u["name"] for u in client.get("/api/users").get_json()] == ["Ana"]
    assert client.delete(f"/api/users/{uid}").status_code == 204
    assert client.get("/api/users").get_json() == []


def test_user_duplicate_name_conflict(client):
    make_user(client, "Ana")
    assert client.post("/api/users", json={"name": "Ana"}).status_code == 409


def test_user_name_required(client):
    assert client.post("/api/users", json={"name": "  "}).status_code == 400


# ---------------------------------------------------------------- library

def test_library_requires_user(client):
    assert client.get("/api/library").status_code == 400


def test_library_add_is_idempotent_per_profile(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    r1 = client.post(f"/api/library?user={ana}", json={"tmdb_id": 7, "media_type": "movie", "title": "Heat"})
    r2 = client.post(f"/api/library?user={ana}", json={"tmdb_id": 7, "media_type": "movie", "title": "Heat"})
    assert (r1.status_code, r2.status_code) == (201, 200)
    assert r1.get_json()["id"] == r2.get_json()["id"]
    assert len(client.get(f"/api/library?user={ana}").get_json()) == 1
    # Ana's title is NOT in Bob's library
    assert client.get(f"/api/library?user={bob}").get_json() == []
    # Bob adding the same tmdb reuses the catalog row but appears in Bob's library
    r3 = client.post(f"/api/library?user={bob}", json={"tmdb_id": 7, "media_type": "movie", "title": "Heat"})
    assert r3.get_json()["id"] == r1.get_json()["id"]
    assert in_library(client, bob, r1.get_json()["id"])


def test_library_add_requires_user(client):
    assert client.post("/api/library", json={"tmdb_id": 1, "media_type": "tv", "title": "X"}).status_code == 400


def test_library_delete_is_per_profile(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    add_item(client, bob, tmdb_id=1)   # same title, Bob owns it too
    assert client.delete(f"/api/library/{item}?user={ana}").status_code == 204
    assert client.get(f"/api/library?user={ana}").get_json() == []
    assert in_library(client, bob, item)   # still Bob's


def test_library_include_episodes_and_last_watched(client):
    uid = make_user(client, "Ana")
    item = add_item(client, uid)
    mark_episodes(client, uid, item, [(1, 1), (1, 2)])
    d = next(i for i in client.get(f"/api/library?user={uid}&include=episodes").get_json() if i["id"] == item)
    assert sorted(d["episodes"]) == [[1, 1], [1, 2]]
    assert d["watched_episodes"] == 2
    assert d["last_watched_at"] is not None


# ---------------------------------------------------------------- per-user flags

def test_patch_movie_watched(client):
    uid = make_user(client, "Ana")
    item = add_item(client, uid, media_type="movie", title="Heat")
    r = client.patch(f"/api/library/{item}?user={uid}", json={"watched": True})
    assert r.status_code == 200 and r.get_json()["watched"] is True
    assert get_item(client, uid, item)["watched"] is True


def test_patch_reports_real_episode_count(client):
    uid = make_user(client, "Ana")
    item = add_item(client, uid)
    mark_episodes(client, uid, item, [(1, 1), (1, 2), (1, 3)])
    r = client.patch(f"/api/library/{item}?user={uid}", json={"stopped": True})
    assert r.get_json()["watched_episodes"] == 3


def test_stopped_is_personal(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    add_item(client, bob, tmdb_id=1)   # both own the same title
    client.patch(f"/api/library/{item}?user={ana}", json={"stopped": True})
    assert get_item(client, ana, item)["stopped"] is True
    assert get_item(client, bob, item)["stopped"] is False


# ---------------------------------------------------------------- episodes

def test_episode_toggle(client):
    uid = make_user(client, "Ana")
    item = add_item(client, uid)
    assert mark_episodes(client, uid, item, [(1, 1)])["watched_episodes"] == 1
    assert mark_episodes(client, uid, item, [(1, 1)], watched=False)["watched_episodes"] == 0


# ---------------------------------------------------------------- folders

def test_private_folder_hidden_from_others(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    fid = make_folder(client, ana, "Mine", shared=False)
    assert [f["id"] for f in client.get(f"/api/folders?user={ana}").get_json()] == [fid]
    assert client.get(f"/api/folders?user={bob}").get_json() == []


def test_private_folder_protected_from_others(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    fid = make_folder(client, ana, "Mine", shared=False)
    assert put_in_folder(client, bob, fid, item).status_code == 403
    assert client.delete(f"/api/folders/{fid}?user={bob}").status_code == 403


def test_shared_folder_syncs_episodes(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    fid = make_folder(client, ana, "Together", shared=True)
    assert put_in_folder(client, ana, fid, item).status_code == 200
    out = mark_episodes(client, ana, item, [(1, 1)])
    assert out["synced"] is True
    assert get_item(client, bob, item)["watched_episodes"] == 1   # visible via shared folder
    # un-marking syncs too
    mark_episodes(client, ana, item, [(1, 1)], watched=False)
    assert get_item(client, bob, item)["watched_episodes"] == 0


def test_shared_folder_syncs_movie_watched(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana, media_type="movie", title="Heat")
    fid = make_folder(client, ana, "Together", shared=True)
    put_in_folder(client, ana, fid, item)
    client.patch(f"/api/library/{item}?user={ana}", json={"watched": True})
    assert get_item(client, bob, item)["watched"] is True


def test_private_folder_does_not_sync(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    fid = make_folder(client, ana, "Mine", shared=False)
    put_in_folder(client, ana, fid, item)
    assert mark_episodes(client, ana, item, [(1, 1)])["synced"] is False
    assert not in_library(client, bob, item)   # private → Bob can't see it at all


# ---------------------------------------------------------------- feed

def test_feed_shows_only_other_users(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    mark_episodes(client, ana, item, [(1, 1)])
    assert client.get(f"/api/feed?user={ana}").get_json() == []
    feed = client.get(f"/api/feed?user={bob}").get_json()
    assert len(feed) == 1 and feed[0]["user_name"] == "Ana" and feed[0]["kind"] == "episodes"


# ---------------------------------------------------------------- misc

def test_seen_returns_previous_open(client):
    uid = make_user(client, "Ana")
    assert client.put(f"/api/users/{uid}/seen", json={}).get_json()["previous_open_at"] is None
    assert client.put(f"/api/users/{uid}/seen", json={}).get_json()["previous_open_at"] is not None


def test_config_region_validation(client):
    assert client.put("/api/config", json={"region": "USA"}).status_code == 400
    assert client.put("/api/config", json={"region": "ar"}).get_json()["region"] == "AR"


# ---------------------------------------------------------------- restricted sharing

def make_folder_with(client, uid, name, member_ids):
    r = client.post(f"/api/folders?user={uid}", json={"name": name, "member_ids": member_ids})
    assert r.status_code == 201
    return r.get_json()


def test_restricted_folder_visible_to_members_only(client):
    ana, bob, carla = make_user(client, "Ana"), make_user(client, "Bob"), make_user(client, "Carla")
    f = make_folder_with(client, ana, "Us two", [bob])
    assert f["shared"] is True
    assert sorted(m["id"] for m in f["members"]) == sorted([ana, bob])
    assert [x["id"] for x in client.get(f"/api/folders?user={bob}").get_json()] == [f["id"]]
    assert client.get(f"/api/folders?user={carla}").get_json() == []


def test_restricted_folder_syncs_members_only(client):
    ana, bob, carla = make_user(client, "Ana"), make_user(client, "Bob"), make_user(client, "Carla")
    item = add_item(client, ana)
    f = make_folder_with(client, ana, "Us two", [bob])
    put_in_folder(client, ana, f["id"], item)
    mark_episodes(client, ana, item, [(1, 1)])
    assert get_item(client, bob, item)["watched_episodes"] == 1
    assert not in_library(client, carla, item)   # not a member → can't see it


def test_any_member_can_manage(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    f = make_folder_with(client, ana, "Us two", [bob])
    # bob (member, not creator) can add items and delete the folder
    assert put_in_folder(client, bob, f["id"], item).status_code == 200
    assert client.delete(f"/api/folders/{f['id']}?user={bob}").status_code == 204


def test_add_to_shared_folder_copies_adders_progress(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    mark_episodes(client, ana, item, [(1, 1), (1, 2)])          # ana at E2
    mark_episodes(client, bob, item, [(1, 1), (1, 2), (1, 3)])  # bob further, at E3
    f = make_folder_with(client, ana, "Us two", [bob])
    put_in_folder(client, ana, f["id"], item)  # ana adds → her position wins
    assert get_item(client, ana, item)["watched_episodes"] == 2
    assert get_item(client, bob, item)["watched_episodes"] == 2


def test_unshare_persists_progress(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    f = make_folder_with(client, ana, "Us two", [bob])
    put_in_folder(client, ana, f["id"], item)
    mark_episodes(client, ana, item, [(1, 1), (1, 2)])
    put_in_folder(client, ana, f["id"], item, member=False)  # unshare
    assert get_item(client, bob, item)["watched_episodes"] == 2  # bob keeps it
    mark_episodes(client, ana, item, [(1, 3)])  # ana continues alone
    assert get_item(client, ana, item)["watched_episodes"] == 3
    assert get_item(client, bob, item)["watched_episodes"] == 2


def test_new_member_gets_shared_position(client):
    ana, bob, carla = make_user(client, "Ana"), make_user(client, "Bob"), make_user(client, "Carla")
    item = add_item(client, ana)
    f = make_folder_with(client, ana, "Us two", [bob])
    put_in_folder(client, ana, f["id"], item)
    mark_episodes(client, ana, item, [(1, 1), (1, 2)])
    r = client.put(f"/api/folders/{f['id']}/members?user={ana}",
                   json={"member_ids": [ana, bob, carla]})
    assert r.status_code == 200
    assert get_item(client, carla, item)["watched_episodes"] == 2
    # and carla is synced from now on
    mark_episodes(client, bob, item, [(1, 3)])
    assert get_item(client, carla, item)["watched_episodes"] == 3


def test_members_endpoint_requires_membership(client):
    ana, bob, carla = make_user(client, "Ana"), make_user(client, "Bob"), make_user(client, "Carla")
    f = make_folder_with(client, ana, "Us two", [bob])
    r = client.put(f"/api/folders/{f['id']}/members?user={carla}",
                   json={"member_ids": [ana, bob, carla]})
    assert r.status_code == 403


def test_feed_shows_sync_events_to_members_only(client):
    ana, bob, carla = make_user(client, "Ana"), make_user(client, "Bob"), make_user(client, "Carla")
    item = add_item(client, ana)
    f = make_folder_with(client, ana, "Us two", [bob])
    put_in_folder(client, ana, f["id"], item)                  # sync event
    put_in_folder(client, ana, f["id"], item, member=False)    # unsync event
    bob_feed = client.get(f"/api/feed?user={bob}").get_json()
    kinds = sorted(e["kind"] for e in bob_feed)
    assert kinds == ["folder_add", "folder_remove"]
    assert all(e["folder_name"] == "Us two" and e["user_name"] == "Ana" for e in bob_feed)
    assert client.get(f"/api/feed?user={carla}").get_json() == []   # not a member
    assert client.get(f"/api/feed?user={ana}").get_json() == []     # own actions hidden


def test_private_folder_add_makes_no_feed_event(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana)
    r = client.post(f"/api/folders?user={ana}", json={"name": "Mine", "member_ids": []})
    fid = r.get_json()["id"]
    put_in_folder(client, ana, fid, item)
    assert client.get(f"/api/feed?user={bob}").get_json() == []


# ---------------------------------------------------------------- suggestions

def test_suggestions_roundtrip_and_isolation(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    assert client.get(f"/api/suggestions?user={ana}").get_json()["items"] == []
    items = [{"tmdb_id": 42, "media_type": "tv", "title": "Dark", "poster_path": None}]
    r = client.put(f"/api/suggestions?user={ana}", json={"seed_hash": "abc", "items": items})
    assert r.status_code == 200
    got = client.get(f"/api/suggestions?user={ana}").get_json()
    assert got["seed_hash"] == "abc" and got["items"] == items and got["built_at"]
    assert client.get(f"/api/suggestions?user={bob}").get_json()["items"] == []


def test_suggestions_validation(client):
    ana = make_user(client, "Ana")
    assert client.get("/api/suggestions").status_code == 400
    assert client.put(f"/api/suggestions?user={ana}", json={"items": "nope"}).status_code == 400


# ---------------------------------------------------------------- games

def test_game_add_and_finish(client):
    uid = make_user(client, "Ana")
    g = add_item(client, uid, tmdb_id=42, media_type="game", title="Hades")
    r = client.patch(f"/api/library/{g}?user={uid}", json={"watched": True})
    assert r.status_code == 200 and r.get_json()["watched"] is True
    d = get_item(client, uid, g)
    assert d["media_type"] == "game" and d["watched"] is True


def test_game_syncs_in_shared_folder(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    g = add_item(client, ana, tmdb_id=42, media_type="game", title="Hades")
    fid = make_folder(client, ana, "Games", shared=True)
    put_in_folder(client, ana, fid, g)
    client.patch(f"/api/library/{g}?user={ana}", json={"watched": True})
    assert get_item(client, bob, g)["watched"] is True


def test_invalid_media_type_rejected(client):
    uid = make_user(client, "Ana")
    r = client.post(f"/api/library?user={uid}", json={"tmdb_id": 1, "media_type": "book", "title": "X"})
    assert r.status_code == 400


def test_config_rawg_key_roundtrip(client):
    got = client.put("/api/config", json={"rawg_key": " abc123 "}).get_json()
    assert got["rawg_key"] == "abc123"
    assert client.get("/api/config").get_json()["rawg_key"] == "abc123"


def test_v5_migration_allows_games_and_keeps_data():
    """A db created before the 'game' media type (old CHECK constraint) is
    rebuilt in place by init_db() without losing rows or child-table data."""
    import os
    import sqlite3

    import app as watchpi

    if os.path.exists(watchpi.DB_PATH):
        os.remove(watchpi.DB_PATH)
    con = sqlite3.connect(watchpi.DB_PATH)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, last_open_at TEXT);
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
            title TEXT NOT NULL, poster_path TEXT, added_at TEXT NOT NULL,
            UNIQUE (tmdb_id, media_type));
        CREATE TABLE episodes (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            season INTEGER NOT NULL, episode INTEGER NOT NULL, watched_at TEXT NOT NULL,
            PRIMARY KEY (item_id, user_id, season, episode));
        INSERT INTO users (name, created_at) VALUES ('Ana', 't');
        INSERT INTO items (tmdb_id, media_type, title, added_at) VALUES (1, 'tv', 'Old Show', 't');
        INSERT INTO episodes VALUES (1, 1, 2, 3, 't');
    """)
    con.commit()
    con.close()

    watchpi.init_db()

    con = sqlite3.connect(watchpi.DB_PATH)
    assert con.execute("SELECT title FROM items").fetchone()[0] == "Old Show"
    assert con.execute("SELECT season, episode FROM episodes").fetchone() == (2, 3)
    con.execute("INSERT INTO items (tmdb_id, media_type, title, added_at) VALUES (9, 'game', 'Hades', 't')")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM items WHERE media_type='game'").fetchone()[0] == 1
    con.close()


# ---------------------------------------------------------------- per-profile library

def test_others_watched_title_invisible(client):
    """The reported bug: a title Sol watched must not appear in Sebas's library."""
    sol, sebas = make_user(client, "Sol"), make_user(client, "Sebas")
    agatha = add_item(client, sol, tmdb_id=99, title="Agatha All Along")
    mark_episodes(client, sol, agatha, [(1, 1)])
    assert in_library(client, sol, agatha)
    assert not in_library(client, sebas, agatha)


def test_shared_folder_grants_then_removal_keeps_owner(client):
    sol, sebas = make_user(client, "Sol"), make_user(client, "Sebas")
    agatha = add_item(client, sol, tmdb_id=99, title="Agatha All Along")
    f = make_folder_with(client, sol, "Us", [sebas])
    put_in_folder(client, sol, f["id"], agatha)
    assert in_library(client, sebas, agatha)   # visible via shared folder
    mark_episodes(client, sebas, agatha, [(1, 1)])  # engaging → becomes owner
    put_in_folder(client, sol, f["id"], agatha, member=False)  # unshare
    assert in_library(client, sebas, agatha)   # still his (he watched it)


def test_remove_is_per_profile_shared_catalog_kept(client):
    ana, bob = make_user(client, "Ana"), make_user(client, "Bob")
    item = add_item(client, ana, tmdb_id=5)
    add_item(client, bob, tmdb_id=5)   # both own the one catalog row
    assert client.delete(f"/api/library/{item}?user={ana}").status_code == 204
    assert not in_library(client, ana, item)
    assert in_library(client, bob, item)
