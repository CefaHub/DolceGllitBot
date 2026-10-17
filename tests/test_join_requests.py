"""Tests of join request alert delivery, independent of real Telegram or aiogram."""
import ast
import asyncio
import copy
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))


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

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: user must open bot first")
        self.sent.append((chat_id, text, kwargs))


def isolated(*names):
    nodes = []
    for node in SOURCE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            clone = copy.deepcopy(node)
            clone.decorator_list = []
            nodes.append(clone)
    if len(nodes) != len(names):
        raise AssertionError("Requested join handler missing")
    ns = {
        "Bot": object, "Message": object, "types": SimpleNamespace(ChatJoinRequest=object),
        "InlineKeyboardButton": FakeButton,
        "InlineKeyboardMarkup": FakeKeyboard,
        "logger": logging.getLogger("test.join_requests"),
        "ADMINS": [101, 202], "activated": set(),
        "ALLOWED_LINK_CODE": "test-approved-code",
        "pending_requests": {}, "save_moderation_state": lambda: None,
        "is_admin": lambda uid: uid in (101, 202),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), ns)
    return ns


def fake_request(bot, link=None):
    events = []
    async def decline():
        events.append("declined")
    request = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        from_user=SimpleNamespace(id=987, mention_html=lambda: "<b>New member</b>"),
        invite_link=SimpleNamespace(invite_link=link) if link else None,
        bot=bot, decline=decline,
    )
    return request, events


class JoinDeliveryTests(unittest.TestCase):
    def test_admin_delivery_does_not_require_activation_file(self):
        ns = isolated("notify_join_admins")
        bot = FakeBot()
        sent = asyncio.run(ns["notify_join_admins"](bot, "Test request"))
        self.assertEqual(sent, 2)
        self.assertEqual([row[0] for row in bot.sent], [101, 202])

    def test_blocked_admin_does_not_block_other_recipients(self):
        ns = isolated("notify_join_admins")
        bot = FakeBot(blocked={101})
        with self.assertLogs("test.join_requests", level="WARNING") as logs:
            count = asyncio.run(ns["notify_join_admins"](bot, "Test request"))
        self.assertEqual(count, 1)
        self.assertEqual([row[0] for row in bot.sent], [202])
        self.assertIn("JOIN ADMIN DELIVERY FAILED", "\n".join(logs.output))

    def test_allowed_link_creates_request_buttons_even_after_restart(self):
        ns = isolated("is_allowed_join_link", "notify_join_admins", "handle_join_request")
        bot = FakeBot()
        request, declines = fake_request(bot, "https://t.me/+test-approved-code")
        asyncio.run(ns["handle_join_request"](request))
        self.assertFalse(declines)
        self.assertEqual(ns["pending_requests"], {-100123: {987}})
        self.assertEqual(len(bot.sent), 2)
        keyboard = bot.sent[0][2]["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "approve_join:-100123:987")
        self.assertEqual(keyboard.inline_keyboard[0][1].callback_data, "decline_join:-100123:987")

    def test_other_link_is_declined_but_admins_see_the_reason(self):
        ns = isolated("is_allowed_join_link", "notify_join_admins", "handle_join_request")
        bot = FakeBot()
        request, declines = fake_request(bot, "https://t.me/+different-code")
        asyncio.run(ns["handle_join_request"](request))
        self.assertEqual(declines, ["declined"])
        self.assertFalse(ns["pending_requests"])
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("отклонена", bot.sent[0][1])
        self.assertIn("отличается", bot.sent[0][1])

    def test_missing_link_is_not_silent(self):
        ns = isolated("is_allowed_join_link", "notify_join_admins", "handle_join_request")
        bot = FakeBot()
        request, declines = fake_request(bot)
        asyncio.run(ns["handle_join_request"](request))
        self.assertEqual(declines, ["declined"])
        self.assertEqual(len(bot.sent), 2)

    def test_state_write_failure_still_notifies(self):
        ns = isolated("is_allowed_join_link", "notify_join_admins", "handle_join_request")
        def fail_write():
            raise OSError("Read-only mounted data volume")
        ns["save_moderation_state"] = fail_write
        bot = FakeBot()
        req, declines = fake_request(bot, "https://t.me/+test-approved-code")
        with self.assertLogs("test.join_requests", level="ERROR"):
            asyncio.run(ns["handle_join_request"](req))
        self.assertEqual(len(bot.sent), 2)
        self.assertFalse(declines)


if __name__ == "__main__":
    unittest.main()
