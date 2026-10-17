"""Tests: DolceBot anonymous inbox ONLY for admins, never person-to-person."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dolce_anon import (
    AnonymousTicketStore, create_anonymous_router,
    send_to_admins, send_answer, ANON_BUTTON_TEXT, anonymous_keyboard,
    notify_other_admins_answered,
)


class FakeBot:
    def __init__(self, blocked=(), member_statuses=None, membership_error=False):
        self.blocked = set(blocked)
        self.sent = []
        self.copied = []
        self.member_statuses = member_statuses or {}
        self.membership_error = membership_error
        self.membership_checks = []
        self.next_message_id = {}

    def new_message_id(self, chat_id):
        message_id = self.next_message_id.get(chat_id, 0) + 1
        self.next_message_id[chat_id] = message_id
        return message_id

    async def get_chat_member(self, chat_id, user_id):
        self.membership_checks.append((chat_id, user_id))
        if self.membership_error:
            raise RuntimeError("Telegram membership lookup unavailable")
        status, is_member = self.member_statuses.get(user_id, ("member", True))
        return SimpleNamespace(status=status, is_member=is_member)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        message_id = self.new_message_id(chat_id)
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": message_id, **kwargs})
        return SimpleNamespace(message_id=message_id)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.copied.append((chat_id, from_chat_id, message_id))
        return SimpleNamespace(message_id=self.new_message_id(chat_id))


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
        self.router = create_anonymous_router([101, 202], self.path, -10098765)
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
        notices = [m for m in self.bot.sent if m["chat_id"] == 202 and "уже ответил" in m["text"]]
        self.assertEqual(len(notices), 1)
        self.assertEqual(
            notices[0]["reply_parameters"].message_id,
            self.bot.sent[1]["message_id"],
        )
        self.assertFalse(any("уже ответил" in m["text"] for m in self.bot.sent if m["chat_id"] == 101))
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
        self.assertEqual(msg.answers[0][0], "Отправьте анонимный вопрос")
        await self.h["anon_cancel"](msg, state)
        self.assertIsNone(await state.get_state())

    async def test_button_opens_same_short_prompt(self):
        button = anonymous_keyboard().keyboard[0][0]
        self.assertEqual(button.text, ANON_BUTTON_TEXT)
        self.assertTrue(anonymous_keyboard().is_persistent)
        msg = FakeMessage(self.bot, 99887766, ANON_BUTTON_TEXT)
        state = FakeState()
        await self.h["anon_keyboard_button"](msg, state)
        self.assertEqual(msg.answers[0][0], "Отправьте анонимный вопрос")
        self.assertIsNotNone(await state.get_state())
        self.assertEqual(self.bot.membership_checks, [(-10098765, 99887766)])

    async def test_nonmembers_cannot_start_or_send_questions(self):
        for status, is_member in (("left", False), ("kicked", False), ("restricted", False)):
            bot = FakeBot(member_statuses={99887766: (status, is_member)})
            start = FakeMessage(bot, 99887766, "/anon")
            state = FakeState()
            await self.h["anon_command"](start, state)
            self.assertIn("только участникам чата", start.answers[0][0])
            self.assertIsNone(await state.get_state())

            direct = FakeMessage(bot, 99887766, "/anon Secret question")
            await self.h["anon_command"](direct, FakeState())
            self.assertIn("только участникам чата", direct.answers[0][0])
            self.assertEqual(bot.sent, [])

    async def test_member_restricted_with_is_member_can_ask(self):
        bot = FakeBot(member_statuses={99887766: ("restricted", True)})
        msg = FakeMessage(bot, 99887766, "/anon Allowed")
        await self.h["anon_command"](msg, FakeState())
        self.assertEqual({x["chat_id"] for x in bot.sent}, {101, 202})

    async def test_membership_rechecked_after_prompt_before_sending(self):
        bot = FakeBot()
        start = FakeMessage(bot, 99887766, "/anon")
        state = FakeState()
        await self.h["anon_command"](start, state)
        bot.member_statuses[99887766] = ("left", False)
        question = FakeMessage(bot, 99887766, "Am I still a member?")
        await self.h["anon_question_content"](question, state)
        self.assertIn("только участникам чата", question.answers[0][0])
        self.assertEqual(bot.sent, [])

    async def test_membership_check_failure_blocks_submission(self):
        bot = FakeBot(membership_error=True)
        question = FakeMessage(bot, 99887766, "/anon Secret question")
        await self.h["anon_command"](question, FakeState())
        self.assertIn("Не удалось проверить участие", question.answers[0][0])
        self.assertEqual(bot.sent, [])

    async def test_answer_notification_survives_router_restart(self):
        author = FakeMessage(self.bot, 99887766, "/anon Important")
        await self.h["anon_command"](author, FakeState())
        first_msg = next(m for m in self.bot.sent if m["chat_id"] == 202)
        callback_data = self.bot.sent[0]["reply_markup"].inline_keyboard[0][0].callback_data
        recreated = handlers(create_anonymous_router([101, 202], self.path, -10098765))
        state = FakeState()
        await recreated["admin_reply_button"](FakeCallback(self.bot, 101, callback_data), state)
        await recreated["admin_answer_content"](
            FakeMessage(self.bot, 101, "An answer"), state
        )
        notices = [m for m in self.bot.sent if "уже ответил" in m["text"]]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["chat_id"], 202)
        self.assertEqual(notices[0]["reply_parameters"].message_id, first_msg["message_id"])
        record = json.loads((self.path / "anonymous_admin_tickets.json").read_text())
        self.assertNotIn("sender", next(iter(record.values())))
        self.assertNotIn("admin_messages", next(iter(record.values())))

    async def test_notification_for_attachment_replies_to_ticket_prompt(self):
        author = FakeMessage(self.bot, 99887766, media="photo")
        await self.h["anon_question_content"](author, FakeState())
        second_prompt = next(m for m in self.bot.sent if m["chat_id"] == 202)
        callback_data = second_prompt["reply_markup"].inline_keyboard[0][0].callback_data
        state = FakeState()
        await self.h["admin_reply_button"](FakeCallback(self.bot, 101, callback_data), state)
        await self.h["admin_answer_content"](FakeMessage(self.bot, 101, "Done"), state)
        notices = [m for m in self.bot.sent if "уже ответил" in m["text"]]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["reply_parameters"].message_id, second_prompt["message_id"])

    async def test_delivery_failure_does_not_claim_an_answer(self):
        bot = FakeBot()
        author = FakeMessage(bot, 99887766, "/anon Are you there?")
        await self.h["anon_command"](author, FakeState())
        callback_data = bot.sent[0]["reply_markup"].inline_keyboard[0][0].callback_data
        state = FakeState()
        await self.h["admin_reply_button"](FakeCallback(bot, 101, callback_data), state)
        bot.blocked.add(99887766)
        reply = FakeMessage(bot, 101, "Cannot deliver")
        await self.h["admin_answer_content"](reply, state)
        self.assertIn("не доставлен", reply.answers[0][0])
        self.assertFalse(any("уже ответил" in m["text"] for m in bot.sent))
        self.assertIsNotNone(await state.get_state())

    async def test_simultaneous_admin_answers_only_deliver_once(self):
        await self.h["anon_command"](
            FakeMessage(self.bot, 99887766, "/anon question"), FakeState()
        )
        callback_data = self.bot.sent[0]["reply_markup"].inline_keyboard[0][0].callback_data
        states = [FakeState(), FakeState()]
        for uid, state in zip((101, 202), states):
            await self.h["admin_reply_button"](
                FakeCallback(self.bot, uid, callback_data), state
            )
        replies = [FakeMessage(self.bot, 101, "First"), FakeMessage(self.bot, 202, "Second")]
        await asyncio.gather(*(
            self.h["admin_answer_content"](reply, state)
            for reply, state in zip(replies, states)
        ))
        deliveries = [m for m in self.bot.sent if m["chat_id"] == 99887766]
        self.assertEqual(len(deliveries), 1)
        notices = [m for m in self.bot.sent if "уже ответил" in m["text"]]
        self.assertEqual(len(notices), 1)

    async def test_deleted_reply_target_does_not_prevent_other_notices(self):
        # Reply parameters remain optional for older questions without tracked IDs.
        await notify_other_admins_answered(self.bot, {"101": 5, "202": 6}, 101)
        notice = self.bot.sent[-1]
        self.assertEqual(notice["chat_id"], 202)
        self.assertEqual(notice["reply_parameters"].message_id, 6)

    async def test_no_anon_user_delivery_to_other_member(self):
        msg = FakeMessage(self.bot, 99887766, "/anon What happens next?")
        await self.h["anon_command"](msg, FakeState())
        self.assertEqual({x["chat_id"] for x in self.bot.sent}, {101, 202})
        self.assertNotIn(111111, {x["chat_id"] for x in self.bot.sent})


if __name__ == "__main__":
    unittest.main()
