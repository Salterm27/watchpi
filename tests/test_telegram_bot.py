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
