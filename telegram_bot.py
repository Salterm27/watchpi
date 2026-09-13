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

import difflib
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
    "open the app and tap the right match to add it. e.g. Severance\n\n"
    "/watching — what you're part-way through\n"
    "/where <title> — the episode to watch next\n"
    "/watched <title> [s2e4] — mark an episode watched\n"
    "/unwatch <title> [s2e4] — undo that\n"
    "/whoami — show this chat's id (for setup)\n\n"
    "Without an episode, /watched marks exactly the one /where reported. "
    "I don't read TMDB, so I can't see where a season ends — if yours just "
    "did, name the episode (s3e1). I'll always say what I marked."
)

# s2e4 / S02E04 / s2 e4 / 2x4
EPISODE_RE = re.compile(r"\bs\s*(\d{1,3})\s*e\s*(\d{1,3})\b|\b(\d{1,3})\s*x\s*(\d{1,3})\b", re.I)


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


def parse_episode(text):
    """Pull an episode reference out of free text -> (season, episode) or None.
    Returns the remaining text too, so '/watched severance s2e4' splits cleanly."""
    m = EPISODE_RE.search(text or "")
    if not m:
        return None, (text or "").strip()
    season, episode = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    return (int(season), int(episode)), rest


def match_title(query, items):
    """(item, candidates). Exactly one match -> (item, []). Ambiguous ->
    (None, [items…]). Nothing -> (None, [])."""
    q = (query or "").strip().lower()
    if not q:
        return None, []
    titles = [(i.get("title", ""), i) for i in items]
    for pick in (
        [i for t, i in titles if t.lower() == q],                  # exact
        [i for t, i in titles if t.lower().startswith(q)],         # prefix
        [i for t, i in titles if q in t.lower()],                  # substring
    ):
        if len(pick) == 1:
            return pick[0], []
        if len(pick) > 1:
            return None, pick
    # last resort: typos ("severence")
    close = difflib.get_close_matches(q, [t.lower() for t, _ in titles], n=3, cutoff=0.7)
    hits = [i for t, i in titles if t.lower() in close]
    if len(hits) == 1:
        return hits[0], []
    return None, hits


def last_watched(item):
    """Highest (season, episode) the user has seen, or None."""
    eps = [tuple(e) for e in (item.get("episodes") or [])]
    return max(eps) if eps else None


def next_up(item):
    """The episode to actually watch next — that's what you want to be told,
    not the one you already saw. Without TMDB we can't see season boundaries,
    so this is last+1 in the same season (or the pilot if nothing's watched)."""
    last = last_watched(item)
    return (last[0], last[1] + 1) if last else (1, 1)


def plan_mark(item, episode):
    """(season, episode, was_guess) for /watched. No explicit episode marks
    exactly the one /where reported, so the two commands never disagree."""
    if episode:
        return episode[0], episode[1], False
    s, e = next_up(item)
    return s, e, True


def format_progress(item):
    """One-line answer to 'where am I on this?' — phrased as what to watch next."""
    title = item.get("title", "?")
    flag = " ⏸ stopped" if item.get("stopped") else ""
    if item.get("media_type") in ("movie", "game"):
        state = "✓ watched" if item.get("watched") else "not watched yet"
        return f"{title} — {state}{flag}"
    s, e = next_up(item)
    n = item.get("watched_episodes") or 0
    if not n:
        return f"{title} — next S{s}E{e} (not started){flag}"
    return f"{title} — next S{s}E{e} · {n} watched{flag}"


def message_of(update):
    """(chat_id, text) for updates we handle, else None."""
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return None
    return int(chat_id), text


def decide(chat_id, text, chats):
    """Parse a message into an intent. PURE — all IO happens in handle().

    ('reply', str) | ('capture', title) | ('watching', None)
    | ('progress', query) | ('mark', {query, episode, watched})
    """
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "whoami":
            return "reply", f"This chat's id is {chat_id}."
        if cmd in ("start", "help"):
            return "reply", HELP

        # everything below touches your library, so it needs a linked chat
        if chat_id not in chats:
            return "reply", unlinked_msg(chat_id)

        if cmd in ("watching", "list"):
            return "watching", None
        if cmd in ("where", "status", "progress"):
            if not arg:
                return "reply", "Which one? e.g. /where severance"
            return "progress", arg
        if cmd in ("watched", "seen", "unwatch", "unseen"):
            if not arg:
                return "reply", f"Which one? e.g. /{cmd} severance s2e4"
            episode, rest = parse_episode(arg)
            if not rest:
                return "reply", f"Which show? e.g. /{cmd} severance s2e4"
            return "mark", {"query": rest, "episode": episode,
                            "watched": cmd in ("watched", "seen")}
        return "reply", "Unknown command — send a title, or /help."

    # the allowlist is the security boundary: without it anyone who finds the
    # bot could write into your inbox
    if chat_id not in chats:
        return "reply", unlinked_msg(chat_id)

    title = clean_text(text)
    if not title:
        return "reply", "That's just a link — send the title as words too, e.g. “Severance”."
    return "capture", title[:MAX_CAPTURE_LEN]


def unlinked_msg(chat_id):
    return (f"This chat isn't linked to a WatchPi profile. Its id is {chat_id} — "
            "ask the owner to add it.")


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


def get_library(user_id):
    req = urllib.request.Request(f"{API}/api/library?user={user_id}&include=episodes")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def put_episode(user_id, item_id, season, episode, watched):
    body = json.dumps({"episodes": [{"season": season, "episode": episode}],
                       "watched": watched}).encode()
    req = urllib.request.Request(
        f"{API}/api/library/{item_id}/episodes?user={user_id}", data=body,
        headers={"Content-Type": "application/json"}, method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


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


def resolve(user_id, query):
    """(item, error_reply). Shared by /where and /watched."""
    item, candidates = match_title(query, get_library(user_id))
    if item:
        return item, None
    if candidates:
        names = "\n".join(f"• {c['title']}" for c in candidates[:6])
        return None, f"Which one?\n{names}"
    return None, (f"“{query}” isn't in your library. Send it as plain text "
                  "and I'll put it in your inbox.")


def do_capture(chat_id, user_id, title):
    post_capture(user_id, title)
    log(f"captured {title!r} for profile {user_id}")
    msg = f"📥 Added “{title}” to your inbox."
    # already tracked? say so rather than letting a duplicate look like a no-op
    existing, _ = match_title(title, get_library(user_id))
    if existing:
        msg += f"\n(You already have {existing['title']} — /where {title} for progress.)"
    send(chat_id, msg)


def do_watching(chat_id, user_id):
    items = [i for i in get_library(user_id)
             if i.get("media_type") == "tv" and (i.get("watched_episodes") or 0) > 0
             and not i.get("stopped")]
    items.sort(key=lambda i: i.get("last_watched_at") or "", reverse=True)
    if not items:
        send(chat_id, "You're not part-way through any series right now.")
        return
    send(chat_id, "\n".join(format_progress(i) for i in items[:8]))


def do_mark(chat_id, user_id, intent):
    item, err = resolve(user_id, intent["query"])
    if err:
        send(chat_id, err)
        return
    if item.get("media_type") != "tv":
        send(chat_id, f"{item['title']} isn't a series — mark it in the app.")
        return
    season, episode, guessed = plan_mark(item, intent["episode"])
    if not intent["watched"] and not intent["episode"]:
        last = last_watched(item)                 # /unwatch with no episode = undo last
        if not last:
            send(chat_id, f"Nothing watched on {item['title']} yet.")
            return
        season, episode, guessed = last[0], last[1], False
    res = put_episode(user_id, item["id"], season, episode, intent["watched"])
    verb = "✓ Marked" if intent["watched"] else "↩︎ Unmarked"
    msg = f"{verb} {item['title']} S{season}E{episode} · {res.get('watched_episodes')} total"
    if res.get("synced"):
        msg += " · synced with your folder"
    if guessed:
        msg += (f"\n(the one /where reported — if season {season} actually ended, "
                f"/unwatch and try /watched {intent['query']} s{season + 1}e1)")
    send(chat_id, msg)


def handle(update, chats):
    parsed = message_of(update)
    if not parsed:
        return
    chat_id, text = parsed
    kind, payload = decide(chat_id, text, chats)
    if kind == "reply":
        send(chat_id, payload)
        return
    user_id = chats[chat_id]
    try:
        if kind == "capture":
            do_capture(chat_id, user_id, payload)
        elif kind == "watching":
            do_watching(chat_id, user_id)
        elif kind == "progress":
            item, err = resolve(user_id, payload)
            send(chat_id, err or format_progress(item))
        elif kind == "mark":
            do_mark(chat_id, user_id, payload)
    except Exception as e:
        log(f"{kind} failed: {e}")
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
