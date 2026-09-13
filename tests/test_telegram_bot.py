"""Telegram bot logic tests — pure functions only, no network, no bot token."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import telegram_bot as bot  # noqa: E402

CHATS = {111: 1, 222: 2}


def msg(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


# ---------------------------------------------------------------- allowlist

def test_parse_chats():
    assert bot.parse_chats("111:1,222:2") == {111: 1, 222: 2}
    assert bot.parse_chats(" 111 : 1 ") == {111: 1}
    assert bot.parse_chats("") == {}
    assert bot.parse_chats(None) == {}


def test_parse_chats_skips_malformed():
    assert bot.parse_chats("111:1,garbage,333:x,444:4") == {111: 1, 444: 4}


# ---------------------------------------------------------------- text handling

def test_clean_text_strips_urls_keeps_words():
    assert bot.clean_text("Severance https://tv.apple.com/x") == "Severance"
    assert bot.clean_text("check   this   out") == "check this out"


def test_clean_text_bare_link_is_empty():
    assert bot.clean_text("https://www.netflix.com/title/81234") == ""


def test_message_of():
    assert bot.message_of(msg(111, " Severance ")) == (111, "Severance")
    assert bot.message_of({"message": {"chat": {"id": 111}}}) is None      # no text
    assert bot.message_of({"message": {"text": "hi"}}) is None             # no chat
    assert bot.message_of({}) is None
    # edited messages count too
    assert bot.message_of({"edited_message": {"chat": {"id": 9}, "text": "x"}}) == (9, "x")


# ---------------------------------------------------------------- routing

def test_known_chat_captures():
    assert bot.decide(111, "Severance", CHATS) == ("capture", "Severance")


def test_share_sheet_shape_captures_the_words():
    kind, title = bot.decide(111, "Severance https://tv.apple.com/x", CHATS)
    assert (kind, title) == ("capture", "Severance")


def test_bare_link_is_rejected_not_captured():
    kind, text = bot.decide(111, "https://netflix.com/title/81234", CHATS)
    assert kind == "reply" and "link" in text.lower()


def test_unknown_chat_never_captures_and_reveals_its_id():
    kind, text = bot.decide(999, "Severance", CHATS)
    assert kind == "reply" and "999" in text


def test_whoami_works_for_unknown_chats_so_setup_is_possible():
    kind, text = bot.decide(999, "/whoami", CHATS)
    assert kind == "reply" and "999" in text


def test_help_and_unknown_commands():
    assert bot.decide(111, "/start", CHATS)[0] == "reply"
    assert bot.decide(111, "/help", CHATS)[0] == "reply"
    assert bot.decide(111, "/nope", CHATS)[0] == "reply"
    # commands are never captured as titles
    assert bot.decide(111, "/start", CHATS)[1] != "/start"


def test_command_with_botname_suffix():
    kind, text = bot.decide(111, "/whoami@WatchPiBot", CHATS)
    assert kind == "reply" and "111" in text


def test_long_title_is_truncated_to_the_inbox_limit():
    kind, title = bot.decide(111, "x" * 900, CHATS)
    assert kind == "capture" and len(title) == bot.MAX_CAPTURE_LEN


# ---------------------------------------------------------------- offset

def test_offset_roundtrip_and_default(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "OFFSET_PATH", str(tmp_path / "off"))
    assert bot.load_offset() == 0        # missing file
    bot.save_offset(42)
    assert bot.load_offset() == 42
    (tmp_path / "off").write_text("junk")
    assert bot.load_offset() == 0        # corrupt file doesn't crash the bot


# ---------------------------------------------------------------- progress & marking

LIB = [
    {"id": 1, "title": "Severance", "media_type": "tv", "watched_episodes": 12,
     "stopped": False, "episodes": [[1, 9], [2, 3], [1, 1]]},
    {"id": 2, "title": "Andor", "media_type": "tv", "watched_episodes": 0,
     "stopped": False, "episodes": []},
    {"id": 3, "title": "The Bear", "media_type": "tv", "watched_episodes": 4,
     "stopped": True, "episodes": [[1, 4]]},
    {"id": 4, "title": "Heat", "media_type": "movie", "watched": True, "stopped": False},
]


def test_parse_episode_formats():
    assert bot.parse_episode("severance s2e4") == ((2, 4), "severance")
    assert bot.parse_episode("S02E04 severance") == ((2, 4), "severance")
    assert bot.parse_episode("severance 2x4") == ((2, 4), "severance")
    assert bot.parse_episode("severance s2 e4") == ((2, 4), "severance")


def test_parse_episode_absent_keeps_text():
    assert bot.parse_episode("severance") == (None, "severance")
    assert bot.parse_episode("") == (None, "")


def test_match_title_tiers():
    assert bot.match_title("severance", LIB)[0]["id"] == 1     # exact
    assert bot.match_title("sever", LIB)[0]["id"] == 1         # prefix
    assert bot.match_title("bear", LIB)[0]["id"] == 3          # substring
    assert bot.match_title("severence", LIB)[0]["id"] == 1     # typo


def test_match_title_ambiguous_and_missing():
    two = [{"title": "Alone"}, {"title": "Alone Again"}]
    assert bot.match_title("alone", two)[0]["title"] == "Alone"   # exact beats ambiguity
    item, cands = bot.match_title("alo", two)                     # prefix hits both
    assert item is None and len(cands) == 2
    assert bot.match_title("nothing like this", LIB) == (None, [])


def test_last_watched_uses_the_highest_not_the_last_listed():
    assert bot.last_watched(LIB[0]) == (2, 3)     # despite [1,1] being last in the list
    assert bot.last_watched(LIB[1]) is None


def test_next_up_is_the_one_to_watch_not_the_last_seen():
    assert bot.next_up(LIB[0]) == (2, 4)      # last seen S2E3
    assert bot.next_up(LIB[1]) == (1, 1)      # nothing watched -> pilot


def test_plan_mark_explicit_exact_and_guessed():
    assert bot.plan_mark(LIB[0], (3, 1)) == (3, 1, False)   # explicit is never a guess
    assert bot.plan_mark(LIB[0], None) == (2, 4, True)      # next in the same season
    assert bot.plan_mark(LIB[1], None) == (1, 1, True)      # nothing watched -> pilot


def test_where_and_watched_never_disagree():
    """/where reports an episode; /watched with no argument must mark THAT one."""
    for item in (LIB[0], LIB[1]):
        s, e, _ = bot.plan_mark(item, None)
        assert (s, e) == bot.next_up(item)
        assert f"S{s}E{e}" in bot.format_progress(item)


def test_format_progress_variants():
    assert bot.format_progress(LIB[0]) == "Severance — next S2E4 · 12 watched"
    assert bot.format_progress(LIB[1]) == "Andor — next S1E1 (not started)"
    assert "⏸ stopped" in bot.format_progress(LIB[2])
    assert bot.format_progress(LIB[3]) == "Heat — ✓ watched"


# ---------------------------------------------------------------- new routing

def test_decide_routes_progress_and_aliases():
    for cmd in ("/where severance", "/status severance", "/progress severance"):
        assert bot.decide(111, cmd, CHATS) == ("progress", "severance")


def test_decide_routes_watching_and_alias():
    assert bot.decide(111, "/watching", CHATS) == ("watching", None)
    assert bot.decide(111, "/list", CHATS) == ("watching", None)


def test_decide_routes_mark_with_and_without_episode():
    assert bot.decide(111, "/watched severance s2e4", CHATS) == (
        "mark", {"query": "severance", "episode": (2, 4), "watched": True})
    assert bot.decide(111, "/watched severance", CHATS) == (
        "mark", {"query": "severance", "episode": None, "watched": True})
    assert bot.decide(111, "/unwatch severance s2e4", CHATS) == (
        "mark", {"query": "severance", "episode": (2, 4), "watched": False})


def test_decide_prompts_when_arguments_are_missing():
    assert bot.decide(111, "/where", CHATS)[0] == "reply"
    assert bot.decide(111, "/watched", CHATS)[0] == "reply"
    assert bot.decide(111, "/watched s2e4", CHATS)[0] == "reply"   # episode but no show


def test_library_commands_require_a_linked_chat():
    for cmd in ("/watching", "/where severance", "/watched severance s2e4"):
        kind, text = bot.decide(999, cmd, CHATS)
        assert kind == "reply" and "999" in text


def test_plain_text_still_captures():
    assert bot.decide(111, "Severance", CHATS) == ("capture", "Severance")
