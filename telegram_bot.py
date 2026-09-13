#!/usr/bin/env python3
"""
WatchPi Telegram capture bot — an OPTIONAL sidecar.

Text a title from anywhere and it lands in your WatchPi inbox. The Pi dials
OUT and long-polls Telegram, so nothing is exposed: no open ports, no TLS, no
remote access, and it works on cellular or behind CGNAT.

Runs as its own systemd service on purpose:
  * the Flask app still never makes an outbound request — only this sidecar does
  * a flaky poller can't take the web app down; systemd restarts it alone

Captures are stored UNRESOLVED (see the inbox in app.py). This bot never looks
a title up — the browser resolves it when you next open the app.

Config comes from /etc/watchpi/telegram.env (chmod 600), NOT config.json:
GET /api/config is unauthenticated, so a bot token there would be readable by
anyone on the LAN.

    WATCHPI_TELEGRAM_TOKEN=123456:ABC...
    WATCHPI_TELEGRAM_CHATS=12345678:1,87654321:2   # chat_id:profile_id
    WATCHPI_API=http://127.0.0.1:8001
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("WATCHPI_TELEGRAM_TOKEN", "").strip()
API = os.environ.get("WATCHPI_API", "http://127.0.0.1:8001").rstrip("/")
# overridable so the test suite can point at a fake Telegram
TG_API = os.environ.get("WATCHPI_TELEGRAM_API", "https://api.telegram.org").rstrip("/")
OFFSET_PATH = os.environ.get(
    "WATCHPI_TELEGRAM_OFFSET",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "telegram_offset"),
)

POLL_TIMEOUT = 25        # Telegram holds the request open this long
MAX_CAPTURE_LEN = 500    # matches the inbox endpoint's limit
MAX_BACKOFF = 60

URL_RE = re.compile(r"https?://\S+")

HELP = (
    "Send me a title and I'll drop it in your WatchPi inbox — "
    "open the app and tap the right match to add it.\n\n"
    "e.g. Severance\n\n"
    "/whoami — show this chat's id (for setup)"
)


def log(msg):
    print(f"[telegram] {msg}", flush=True)


# ---------------------------------------------------------------- pure logic
# Kept side-effect free so it can be tested without a network or a bot token.

def parse_chats(raw):
    """'123:1,456:2' -> {123: 1, 456: 2}. Malformed entries are skipped loudly."""
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        chat, _, user = part.partition(":")
        try:
            out[int(chat)] = int(user)
        except ValueError:
            log(f"ignoring malformed WATCHPI_TELEGRAM_CHATS entry: {part!r}")
    return out


def clean_text(text):
    """Drop URLs, keep the words — share sheets send 'Severance https://…'.
    A bare link collapses to '' and is rejected by decide()."""
    return " ".join(URL_RE.sub(" ", text or "").split())


def message_of(update):
    """(chat_id, text) for updates we handle, else None."""
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return None
    return int(chat_id), text


def decide(chat_id, text, chats):
    """What to do with a message: ('reply', str) or ('capture', title)."""
    if text.startswith("/"):
        cmd = text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd == "whoami":
            return "reply", f"This chat's id is {chat_id}."
        if cmd in ("start", "help"):
            return "reply", HELP
        return "reply", "Unknown command — just send a title, or /help."

    # the allowlist is the security boundary: without it anyone who finds the
    # bot could write into your inbox
    if chat_id not in chats:
        return "reply", (
            f"This chat isn't linked to a WatchPi profile. Its id is {chat_id} — "
            "ask the owner to add it."
        )

    title = clean_text(text)
    if not title:
        return "reply", "That's just a link — send the title as words too, e.g. “Severance”."
    return "capture", title[:MAX_CAPTURE_LEN]


# ---------------------------------------------------------------- io

def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{TG_API}/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as r:
        return json.load(r)


def send(chat_id, text):
    try:
        tg("sendMessage", chat_id=chat_id, text=text)
    except Exception as e:          # a failed reply must never kill the loop
        log(f"reply to {chat_id} failed: {e}")


def post_capture(user_id, title):
    body = json.dumps({"text": title, "source": "telegram"}).encode()
    req = urllib.request.Request(
        f"{API}/api/inbox?user={user_id}", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status in (200, 201)


def load_offset():
    try:
        with open(OFFSET_PATH) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def save_offset(n):
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    tmp = OFFSET_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(n))
    os.replace(tmp, OFFSET_PATH)     # atomic: a crash can't leave a partial offset


def handle(update, chats):
    parsed = message_of(update)
    if not parsed:
        return
    chat_id, text = parsed
    kind, payload = decide(chat_id, text, chats)
    if kind == "reply":
        send(chat_id, payload)
        return
    try:
        post_capture(chats[chat_id], payload)
        log(f"captured {payload!r} for profile {chats[chat_id]}")
        send(chat_id, f"📥 Added “{payload}” to your inbox.")
    except Exception as e:
        log(f"capture failed: {e}")
        send(chat_id, "Couldn't reach WatchPi just now — try again in a moment.")


def main():
    if not TOKEN:
        log("no WATCHPI_TELEGRAM_TOKEN set — bot disabled (see README). Exiting.")
        return 0
    chats = parse_chats(os.environ.get("WATCHPI_TELEGRAM_CHATS", ""))
    if not chats:
        log("WARNING: no linked chats. Message the bot /whoami to get your id.")
    log(f"polling {TG_API}; {len(chats)} linked chat(s); api={API}")

    offset = load_offset()
    backoff = 1
    while True:
        try:
            resp = tg("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
            backoff = 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                # a webhook is set, or a second poller is running somewhere
                log("409 from Telegram — webhook set or another poller running; waiting 60s")
                time.sleep(60)
                continue
            log(f"Telegram HTTP {e.code}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        except Exception as e:
            log(f"poll failed ({e}); retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        for upd in resp.get("result", []):
            offset = upd.get("update_id", 0) + 1
            handle(upd, chats)
            save_offset(offset)   # per-update, so a crash re-does at most one


if __name__ == "__main__":
    sys.exit(main())
