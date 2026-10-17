"""Regression tests for exact, bot-owned join links and admin notifications.

These tests run without contacting Telegram and never use a real invite code.
"""
import ast
import asyncio
import copy
import json
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
FULL_LINK = "https://t.me/+CompleteTestInviteCode9876"
SECOND_LINK = "https://t.me/+AnotherCompleteInviteCode79"


class FakeButton:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeKeyboard:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeBot:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.sent = []
        self.created = []

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: user must open bot first")
        self.sent.append((chat_id, text, kwargs))

    async def get_me(self):
        return SimpleNamespace(id=550)

    async def create_chat_invite_link(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            invite_link=FULL_LINK,
            creator=SimpleNamespace(id=550),
            creates_join_request=True,
        )


def isolated(*names):
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            clone = copy.deepcopy(node)
            clone.decorator_list = []
            nodes.append(clone)
    assert len(nodes) == len(names), f"Missing function from {names}"
    ns = {
        "Bot": object, "Message": object, "types": SimpleNamespace(ChatJoinRequest=object),
        "InlineKeyboardButton": FakeButton,
        "InlineKeyboardMarkup": FakeKeyboard,
        "logger": logging.getLogger("test.join_requests"),
        "ADMINS": [101, 202], "activated": set(),
        "os": os, "re": re,
        "JOIN_LINK_FILE": "allowed_join_link.json",
        "JOIN_LINK_ENV": "DOLCE_JOIN_INVITE_LINK",
        "BIRTHDAY_CHAT_ID": -100123,
        "read_json": lambda *a: {},
        "write_json": lambda *a: None,
        "pending_requests": {}, "save_moderation_state": lambda: None,
        "is_admin": lambda uid: uid in (101, 202),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), ns)
    return ns


def functions():
    ns = isolated("is_complete_invite_link", "configured_join_link", "is_allowed_join_link",
                  "notify_join_admins", "handle_join_request", "cmd_dolcelink",
                  "cmd_dolcecreatelink")
    ns["read_json"] = lambda name, fallback: {"chat_id": -100123, "link": FULL_LINK}
    return ns


def fake_request(bot, link=None):
    events = []

    async def decline():
        events.append("declined")

    req = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        from_user=SimpleNamespace(id=987, mention_html=lambda: "<b>New member</b>"),
        invite_link=SimpleNamespace(invite_link=link) if link else None,
        bot=bot, decline=decline,
    )
    return req, events


class JoinDeliveryTests(unittest.TestCase):
    def test_strict_full_link_no_substrings_or_redaction(self):
        ns = functions()
        allowed = ns["is_allowed_join_link"]
        valid = ns["is_complete_invite_link"]
        self.assertTrue(valid(FULL_LINK))
        self.assertTrue(allowed(FULL_LINK))
        for invalid in (None, "", "CompleteTestInviteCode9876", "https://t.me/+Complete",
                        FULL_LINK + "wrong", "https://t.me/+…", "https://t.me/+...",
                        "https://t.me/+CompleteTestInviteCode…", SECOND_LINK):
            with self.subTest(invalid=invalid):
                self.assertFalse(allowed(invalid))

    def test_bothost_env_works_if_local_state_missing(self):
        ns = functions()
        ns["read_json"] = lambda *a: {}
        old = os.environ.get("DOLCE_JOIN_INVITE_LINK")
        try:
            os.environ["DOLCE_JOIN_INVITE_LINK"] = FULL_LINK
            self.assertEqual(ns["configured_join_link"](), FULL_LINK)
            self.assertTrue(ns["is_allowed_join_link"](FULL_LINK))
            os.environ["DOLCE_JOIN_INVITE_LINK"] = "https://t.me/+Some…"
            self.assertIsNone(ns["configured_join_link"]())
        finally:
            if old is None:
                os.environ.pop("DOLCE_JOIN_INVITE_LINK", None)
            else:
                os.environ["DOLCE_JOIN_INVITE_LINK"] = old

    def test_admin_delivery_does_not_require_activation_file(self):
        ns = functions()
        bot = FakeBot()
        count = asyncio.run(ns["notify_join_admins"](bot, "Test request"))
        self.assertEqual(count, 2)
        self.assertEqual([row[0] for row in bot.sent], [101, 202])

    def test_blocked_admin_does_not_block_other_recipients(self):
        ns = functions()
        bot = FakeBot(blocked={101})
        with self.assertLogs("test.join_requests", level="WARNING") as logs:
            count = asyncio.run(ns["notify_join_admins"](bot, "Test request"))
        self.assertEqual(count, 1)
        self.assertEqual([row[0] for row in bot.sent], [202])
        self.assertIn("JOIN ADMIN DELIVERY FAILED", "\n".join(logs.output))

    def test_valid_full_link_creates_request_buttons(self):
        ns = functions()
        bot = FakeBot()
        req, declines = fake_request(bot, FULL_LINK)
        asyncio.run(ns["handle_join_request"](req))
        self.assertFalse(declines)
        self.assertEqual(ns["pending_requests"], {-100123: {987}})
        self.assertEqual(len(bot.sent), 2)
        buttons = bot.sent[0][2]["reply_markup"].inline_keyboard[0]
        self.assertEqual(buttons[0].callback_data, "approve_join:-100123:987")
        self.assertEqual(buttons[1].callback_data, "decline_join:-100123:987")

    def test_other_full_link_or_redacted_link_is_declined(self):
        for link in (SECOND_LINK, "https://t.me/+…", None):
            with self.subTest(link=link):
                ns = functions()
                bot = FakeBot()
                req, declines = fake_request(bot, link)
                asyncio.run(ns["handle_join_request"](req))
                self.assertEqual(declines, ["declined"])
                self.assertFalse(ns["pending_requests"])
                self.assertEqual(len(bot.sent), 2)
                self.assertIn("отклонена", bot.sent[0][1])

    def test_missing_config_fails_closed(self):
        ns = functions()
        ns["read_json"] = lambda *a: {}
        old = os.environ.pop("DOLCE_JOIN_INVITE_LINK", None)
        try:
            self.assertFalse(ns["is_allowed_join_link"](FULL_LINK))
        finally:
            if old is not None:
                os.environ["DOLCE_JOIN_INVITE_LINK"] = old

    def test_state_write_failure_still_notifies(self):
        ns = functions()

        def fail_write():
            raise OSError("Read-only volume")

        ns["save_moderation_state"] = fail_write
        bot = FakeBot()
        req, declines = fake_request(bot, FULL_LINK)
        with self.assertLogs("test.join_requests", level="ERROR"):
            asyncio.run(ns["handle_join_request"](req))
        self.assertEqual(len(bot.sent), 2)
        self.assertFalse(declines)

    def test_bot_creates_join_request_link_and_saves_full_value(self):
        ns = functions()
        ns["read_json"] = lambda *a: {}  # first run has no configured link
        saved = {}
        ns["write_json"] = lambda name, data: saved.update({"file": name, **data})
        bot = FakeBot()
        replies = []

        async def answer(text):
            replies.append(text)

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            bot=bot, answer=answer,
        )
        asyncio.run(ns["cmd_dolcecreatelink"](message))
        self.assertEqual(len(bot.created), 1)
        self.assertTrue(bot.created[0]["creates_join_request"])
        self.assertEqual(saved["link"], FULL_LINK)
        self.assertEqual(saved["chat_id"], -100123)
        self.assertIn(FULL_LINK, replies[0])

    def test_cannot_regenerate_already_configured_invitation(self):
        ns = functions()
        bot = FakeBot()
        replies = []

        async def answer(text):
            replies.append(text)

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            bot=bot,
            answer=answer,
        )
        asyncio.run(ns["cmd_dolcecreatelink"](message))
        self.assertFalse(bot.created, "An active link must never be replaced by this command")
        self.assertIn(FULL_LINK, replies[0])
        self.assertIn("НЕ создавалась", replies[0])

    def test_nonadmin_cannot_create_link(self):
        ns = functions()
        bot = FakeBot()
        replies = []

        async def answer(text):
            replies.append(text)

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=987),
            bot=bot, answer=answer,
        )
        asyncio.run(ns["cmd_dolcecreatelink"](message))
        self.assertEqual(bot.created, [])
        self.assertIn("Только", replies[0])

    def test_dolcelink_returns_exact_configured_link(self):
        ns = functions()
        bot = FakeBot()
        replies = []

        async def answer(text):
            replies.append(text)

        asyncio.run(ns["cmd_dolcelink"](SimpleNamespace(answer=answer, bot=bot)))
        self.assertEqual(replies, ["🔗 Актуальная ссылка для входа:\n" + FULL_LINK])


if __name__ == "__main__":
    unittest.main()
