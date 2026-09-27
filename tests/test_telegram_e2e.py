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
    """Accepts sendMessage and records the text; getUpdates is never used here."""
    replies = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        params = {k: v[0] for k, v in parse_qs(body).items()}
        if self.path.endswith("/sendMessage"):
            type(self).replies.append(params.get("text", ""))
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Harness:
    def __init__(self, client, replies):
        self.client = client        # seed/inspect the app directly (same DB)
        self.replies = replies

    def say(self, text, chats):
        """Put a message through the bot exactly as the poll loop would."""
        self.replies.clear()
        bot.handle({"update_id": 1, "message": {"chat": {"id": CHAT}, "text": text}}, chats)
        return "\n".join(self.replies)


@pytest.fixture()
def live(monkeypatch):
    if os.path.exists(watchpi.DB_PATH):
        os.remove(watchpi.DB_PATH)
    watchpi.init_db()
    watchpi.app.config["TESTING"] = True

    app_srv = make_server("127.0.0.1", 0, watchpi.app)
    threading.Thread(target=app_srv.serve_forever, daemon=True).start()

    replies = []
    _StubTelegram.replies = replies
    tg_srv = HTTPServer(("127.0.0.1", 0), _StubTelegram)
    threading.Thread(target=tg_srv.serve_forever, daemon=True).start()

    monkeypatch.setattr(bot, "TOKEN", "test-token")
    monkeypatch.setattr(bot, "API", f"http://127.0.0.1:{app_srv.server_port}")
    monkeypatch.setattr(bot, "TG_API", f"http://127.0.0.1:{tg_srv.server_port}")

    with watchpi.app.test_client() as c:
        yield Harness(c, replies)

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
