"""Tests: DolceBot anonymous inbox ONLY for admins, never person-to-person."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dolce_anon import (
    AnonymousTicketStore, create_anonymous_router,
    send_to_admins, send_answer,
)


class FakeBot:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.sent = []
        self.copied = []

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.copied.append((chat_id, from_chat_id, message_id))


class FakeState:
    def __init__(self):
        self.name = None
        self.data = {}

    async def set_state(self, state):
        self.name = state.state if hasattr(state, "state") else state

    async def get_state(self):
        return self.name

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.name = None
        self.data.clear()


class FakeMessage:
    def __init__(self, bot, uid, text=None, mid=4, media=None):
        self.bot = bot
        self.from_user = SimpleNamespace(
            id=uid, username="secret_user", full_name="Secret Person"
        )
        self.chat = SimpleNamespace(type="private", id=uid)
        self.message_id = mid
        self.text = text
        self.answers = []
        for kind in ("photo", "video", "voice", "audio", "document", "animation", "video_note"):
            setattr(self, kind, object() if kind == media else None)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, bot, uid, data):
        self.bot = bot
        self.from_user = SimpleNamespace(id=uid)
        self.message = FakeMessage(bot, uid)
        self.data = data
        self.responses = []

    async def answer(self, text=None, **kwargs):
        self.responses.append((text, kwargs))


def handlers(router):
    result = {}
    for observer in (router.message, router.callback_query):
        for entry in observer.handlers:
            result[entry.callback.__name__] = entry.callback
    return result


class AnonymousInboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.router = create_anonymous_router([101, 202], self.path)
        self.h = handlers(self.router)
        self.bot = FakeBot()

    async def test_user_anon_reaches_admins_only_no_identity_leak(self):
        msg = FakeMessage(self.bot, 99887766, "/anon Are there auditions?")
        await self.h["anon_command"](msg, FakeState())
        self.assertEqual([m["chat_id"] for m in self.bot.sent], [101, 202])
        self.assertEqual(len(self.bot.copied), 0)
        self.assertIn("Are there auditions?", self.bot.sent[0]["text"])
        for sent in self.bot.sent:
            self.assertNotIn("99887766", sent["text"])
            self.assertNotIn("secret_user", sent["text"])
            self.assertNotIn("Secret Person", sent["text"])
            self.assertNotIn("/anon", sent["text"])
        self.assertIn("анонимно", msg.answers[0][0])
        markup = self.bot.sent[0]["reply_markup"]
        self.assertTrue(markup.inline_keyboard[0][0].callback_data.startswith("anon:reply:"))
        self.assertNotIn("99887766", repr(markup))

    async def test_full_flow_admin_reply_stays_anonymous(self):
        author = FakeMessage(self.bot, 99887766, "/anon When is next game?")
        await self.h["anon_command"](author, FakeState())
        data = self.bot.sent[0]["reply_markup"].inline_keyboard[0][0].callback_data
        callback = FakeCallback(self.bot, 101, data)
        state = FakeState()
        await self.h["admin_reply_button"](callback, state)
        self.assertTrue(callback.responses)
        reply = FakeMessage(self.bot, 101, "Tomorrow at 18:00")
        await self.h["admin_answer_content"](reply, state)
        delivered = [x for x in self.bot.sent if x["chat_id"] == 99887766]
        self.assertEqual(len(delivered), 1)
        self.assertIn("Tomorrow at 18:00", delivered[0]["text"])
        self.assertNotIn("101", delivered[0]["text"])
        self.assertIsNone(await state.get_state())
        another = FakeCallback(self.bot, 202, data)
        await self.h["admin_reply_button"](another, FakeState())
        self.assertIn("закрыто", another.responses[0][0])

    async def test_non_admin_cannot_answer_or_close(self):
        author = FakeMessage(self.bot, 99887766, "/anon Admins only")
        await self.h["anon_command"](author, FakeState())
        rows = self.bot.sent[0]["reply_markup"].inline_keyboard[0]
        for item, handler in zip(rows, ("admin_reply_button", "admin_close_button")):
            callback = FakeCallback(self.bot, 777, item.callback_data)
            await self.h[handler](callback, FakeState())
            self.assertIn("Только для администрации", callback.responses[0][0])
        self.assertFalse(any(m["chat_id"] == 777 for m in self.bot.sent))

    async def test_video_or_voice_uses_copy_not_forward_with_no_sender_label(self):
        for media in ("photo", "voice", "document"):
            self.bot.sent.clear()
            self.bot.copied.clear()
            message = FakeMessage(self.bot, 99887766, media=media)
            count = await send_to_admins(self.bot, [101, 202], "testticket99", message)
            self.assertEqual(count, 2)
            self.assertEqual(self.bot.copied, [(101, 99887766, 4), (202, 99887766, 4)])
            self.assertEqual([x["chat_id"] for x in self.bot.sent], [101, 202])
            self.assertNotIn("99887766", self.bot.sent[0]["text"])

    async def test_ticket_mapping_persists_and_deletes_sender_when_closed(self):
        store = AnonymousTicketStore(self.path / "tickets.json")
        ticket, seconds = await store.issue(555123)
        self.assertEqual(seconds, 0)
        self.assertRegex(ticket, r"^[a-f0-9]{12}$")
        reopened = AnonymousTicketStore(self.path / "tickets.json")
        self.assertEqual((await reopened.get_open(ticket))["sender"], 555123)
        self.assertTrue(await reopened.close(ticket))
        self.assertIsNone(await reopened.get_open(ticket))
        raw = json.loads((self.path / "tickets.json").read_text())
        self.assertNotIn("sender", raw[ticket])

    async def test_spam_control_and_delivery_failure(self):
        first = FakeMessage(self.bot, 99887766, "/anon One")
        await self.h["anon_command"](first, FakeState())
        second = FakeMessage(self.bot, 99887766, "/anon Two")
        await self.h["anon_command"](second, FakeState())
        self.assertIn("Подожди", second.answers[0][0])
        self.assertEqual(len(self.bot.sent), 2)
        blocked = FakeBot(blocked=(101, 202))
        fail = FakeMessage(blocked, 88776655, "/anon Cannot deliver")
        await self.h["anon_command"](fail, FakeState())
        self.assertIn("пока не отправлен", fail.answers[0][0])
        records = json.loads((self.path / "anonymous_admin_tickets.json").read_text())
        self.assertEqual(len(records), 1)

    async def test_cancel_and_prompt(self):
        msg = FakeMessage(self.bot, 99887766, "/anon")
        state = FakeState()
        await self.h["anon_command"](msg, state)
        self.assertIsNotNone(await state.get_state())
        self.assertIn("только администраторы", msg.answers[0][0])
        await self.h["anon_cancel"](msg, state)
        self.assertIsNone(await state.get_state())

    async def test_no_anon_user_delivery_to_other_member(self):
        msg = FakeMessage(self.bot, 99887766, "/anon What happens next?")
        await self.h["anon_command"](msg, FakeState())
        self.assertEqual({x["chat_id"] for x in self.bot.sent}, {101, 202})
        self.assertNotIn(111111, {x["chat_id"] for x in self.bot.sent})


if __name__ == "__main__":
    unittest.main()
