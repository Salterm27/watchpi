"""End-to-end: the real bot talking real HTTP to the real app.

The bot's unit tests cover its pure routing logic. These cover the part that
actually breaks in practice — the HTTP contract between the bot and the API.
If an endpoint's shape, query params or response fields change, these fail.

Both ends are real: a live Flask server for the app, and a stub Telegram that
records what the bot sent back. No network, no bot token, deterministic.
The polling loop itself is deliberately out of scope (it's a `while True`);
its offset handling is unit-tested separately.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as watchpi  # noqa: E402
import telegram_bot as bot  # noqa: E402

CHAT = 111


class _StubTelegram(BaseHTTPRequestHandler):
    """Records what the bot sent back; getUpdates is never used here."""
    replies = None
    calls = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        params = {k: v[0] for k, v in parse_qs(body).items()}
        method = self.path.rsplit("/", 1)[-1]
        type(self).calls.append((method, params))
        if method in ("sendMessage", "editMessageText"):
            type(self).replies.append(params.get("text", ""))
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Harness:
    def __init__(self, client, replies, calls):
        self.client = client        # seed/inspect the app directly (same DB)
        self.replies = replies
        self.calls = calls

    def say(self, text, chats):
        """Put a message through the bot exactly as the poll loop would."""
        self.replies.clear()
        bot.handle({"update_id": 1, "message": {"chat": {"id": CHAT}, "text": text}}, chats)
        return "\n".join(self.replies)

    def tap(self, data, chats):
        """Tap an inline button, as the poll loop would deliver it."""
        self.replies.clear()
        self.calls.clear()
        bot.handle({"update_id": 2, "callback_query": {
            "id": "q", "data": data,
            "message": {"message_id": 42, "chat": {"id": CHAT}}}}, chats)
        return "\n".join(self.replies)

    def buttons(self):
        """callback_data of every button in the last message sent/edited."""
        for method, params in reversed(self.calls):
            if method in ("sendMessage", "editMessageText"):
                markup = json.loads(params.get("reply_markup") or "{}")
                return [b["callback_data"] for row in markup.get("inline_keyboard", [])
                        for b in row]
        return []


@pytest.fixture()
def live(monkeypatch):
    if os.path.exists(watchpi.DB_PATH):
        os.remove(watchpi.DB_PATH)
    watchpi.init_db()
    watchpi.app.config["TESTING"] = True

    app_srv = make_server("127.0.0.1", 0, watchpi.app)
    threading.Thread(target=app_srv.serve_forever, daemon=True).start()

    replies, calls = [], []
    _StubTelegram.replies = replies
    _StubTelegram.calls = calls
    tg_srv = HTTPServer(("127.0.0.1", 0), _StubTelegram)
    threading.Thread(target=tg_srv.serve_forever, daemon=True).start()

    monkeypatch.setattr(bot, "TOKEN", "test-token")
    monkeypatch.setattr(bot, "API", f"http://127.0.0.1:{app_srv.server_port}")
    monkeypatch.setattr(bot, "TG_API", f"http://127.0.0.1:{tg_srv.server_port}")

    with watchpi.app.test_client() as c:
        yield Harness(c, replies, calls)

    app_srv.shutdown()
    tg_srv.shutdown()


def seed_user(h, name="Ana"):
    return h.client.post("/api/users", json={"name": name}).get_json()["id"]


def seed_show(h, uid, title="Severance", tmdb_id=95396, episodes=()):
    item = h.client.post(f"/api/library?user={uid}",
                         json={"tmdb_id": tmdb_id, "media_type": "tv", "title": title}).get_json()
    for s, e in episodes:
        h.client.put(f"/api/library/{item['id']}/episodes?user={uid}",
                     json={"episodes": [{"season": s, "episode": e}], "watched": True})
    return item["id"]


def library(h, uid):
    return {i["title"]: i for i in h.client.get(f"/api/library?user={uid}").get_json()}


# ---------------------------------------------------------------- capture

def test_capture_reaches_the_inbox_over_http(live):
    uid = seed_user(live)
    reply = live.say("The Pitt", {CHAT: uid})
    assert "inbox" in reply.lower()
    assert [i["text"] for i in live.client.get(f"/api/inbox?user={uid}").get_json()] == ["The Pitt"]


def test_unknown_chat_writes_nothing(live):
    uid = seed_user(live)
    reply = live.say("The Pitt", {})        # chat not in the allowlist
    assert str(CHAT) in reply
    assert live.client.get(f"/api/inbox?user={uid}").get_json() == []


# ---------------------------------------------------------------- progress

def test_where_reports_the_episode_to_watch_next(live):
    uid = seed_user(live)
    seed_show(live, uid, episodes=[(1, 1), (1, 2), (2, 3)])
    assert "next S2E4" in live.say("/where severance", {CHAT: uid})


def test_watching_lists_shows_in_progress(live):
    uid = seed_user(live)
    seed_show(live, uid, episodes=[(1, 1)])
    seed_show(live, uid, title="Andor", tmdb_id=83867)      # untouched
    reply = live.say("/watching", {CHAT: uid})
    assert "Severance" in reply and "Andor" not in reply


def test_unknown_title_points_back_at_capture(live):
    uid = seed_user(live)
    seed_show(live, uid)
    assert "inbox" in live.say("/where breaking bad", {CHAT: uid}).lower()


# ---------------------------------------------------------------- marking

def test_explicit_mark_changes_state_and_echoes_it(live):
    uid = seed_user(live)
    seed_show(live, uid, episodes=[(1, 1)])
    reply = live.say("/watched severance s1e2", {CHAT: uid})
    assert "S1E2" in reply
    assert library(live, uid)["Severance"]["watched_episodes"] == 2


def test_mark_without_episode_marks_what_where_reported(live):
    uid = seed_user(live)
    seed_show(live, uid, episodes=[(2, 3)])
    assert "next S2E4" in live.say("/where severance", {CHAT: uid})
    assert "S2E4" in live.say("/watched severance", {CHAT: uid})
    assert library(live, uid)["Severance"]["watched_episodes"] == 2


def test_unwatch_reverts(live):
    uid = seed_user(live)
    seed_show(live, uid, episodes=[(1, 1), (1, 2)])
    live.say("/unwatch severance s1e2", {CHAT: uid})
    assert library(live, uid)["Severance"]["watched_episodes"] == 1


def test_shared_folder_mark_syncs_and_says_so(live):
    ana, bob = seed_user(live, "Ana"), seed_user(live, "Bob")
    item = seed_show(live, ana, episodes=[(1, 1)])
    folder = live.client.post(f"/api/folders?user={ana}",
                              json={"name": "Us", "member_ids": [bob]}).get_json()
    live.client.put(f"/api/folders/{folder['id']}/items?user={ana}",
                    json={"item_id": item, "member": True})
    reply = live.say("/watched severance s1e2", {CHAT: ana})
    assert "synced" in reply
    assert library(live, bob)["Severance"]["watched_episodes"] == 2


# ---------------------------------------------------------------- tonight

def seed_folder(h, uid, name, item_ids, member_ids=()):
    f = h.client.post(f"/api/folders?user={uid}",
                      json={"name": name, "member_ids": list(member_ids)}).get_json()
    for i in item_ids:
        h.client.put(f"/api/folders/{f['id']}/items?user={uid}",
                     json={"item_id": i, "member": True})
    return f["id"]


def test_tonight_offers_folders_then_picks_from_the_chosen_one(live):
    ana, bob = seed_user(live, "Ana"), seed_user(live, "Bob")
    sev = seed_show(live, ana, episodes=[(1, 1)])
    seed_show(live, ana, title="Andor", tmdb_id=83867, episodes=[(1, 1), (1, 2)])
    fid = seed_folder(live, ana, "Us two", [sev], member_ids=[bob])

    reply = live.say("/tonight", {CHAT: ana})
    assert "Pick a folder" in reply
    assert live.buttons() == ["tn:all:0", f"tn:{fid}:0"]

    reply = live.tap(f"tn:{fid}:0", {CHAT: ana})
    assert "Severance" in reply and "Andor" not in reply
    assert "Next up S1E2" in reply and "Us two" in reply
    assert ("answerCallbackQuery", {"callback_query_id": "q"}) in [
        (m, {k: v for k, v in p.items() if k != "text"}) for m, p in live.calls]


def test_tonight_by_name_and_reroll(live):
    ana = seed_user(live)
    a = seed_show(live, ana, episodes=[(1, 1)])
    b = seed_show(live, ana, title="Andor", tmdb_id=83867, episodes=[(2, 1)])
    fid = seed_folder(live, ana, "Sci-fi", [a, b])

    first = live.say("/tonight sci", {CHAT: ana})
    assert first.startswith("🌙 Tonight:")
    assert live.buttons() == [f"tn:{fid}:1"]
    second = live.tap(f"tn:{fid}:1", {CHAT: ana})
    assert second != first and "enjoy" in second      # only two candidates
    assert live.buttons() == []


def test_tonight_cannot_read_someone_elses_folder(live):
    ana, bob = seed_user(live, "Ana"), seed_user(live, "Bob")
    sev = seed_show(live, bob)
    bobs = seed_folder(live, bob, "Private", [sev])
    reply = live.tap(f"tn:{bobs}:0", {CHAT: ana})
    assert "Severance" not in reply


def test_tonight_unlinked_chat_gets_nothing(live):
    ana = seed_user(live)
    seed_show(live, ana)
    assert "Severance" not in live.tap("tn:all:0", {})


# ---------------------------------------------------------------- season shape

def test_bot_uses_the_season_shape_the_app_cached(live):
    uid = seed_user(live)
    item = seed_show(live, uid, title="1899", tmdb_id=90669,
                     episodes=[(1, e) for e in range(1, 9)])
    assert "next S1E9" in live.say("/where 1899", {CHAT: uid})        # no shape yet: guess
    r = live.client.put(f"/api/library/{item}/seasons",
                        json={"seasons": [[1, 8]], "last_aired": [1, 8]})
    assert r.status_code == 200
    assert "caught up" in live.say("/where 1899", {CHAT: uid})
    assert "8 episodes" in live.say("/watched 1899 s1e9", {CHAT: uid})
    assert "caught up" in live.say("/watched 1899", {CHAT: uid})
    assert library(live, uid)["1899"]["watched_episodes"] == 8         # nothing written
    assert "1899" not in live.say("/tonight", {CHAT: uid})
