"""Anonymous questions to the DolceBot ADMINS only.

Authors send private messages to DolceBot, never to other members. Admins see
the question and an opaque ticket ID, NOT Telegram user ID, name or username.
The bot privately maps question -> sender to deliver multiple anonymous replies.
No original content is written to disk or forwarded with sender attribution.
"""
import asyncio
import html
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyParameters,
)

logger = logging.getLogger(__name__)
TICKET_FILE = "anonymous_admin_tickets.json"
MAX_TEXT = 3000
MAX_TICKETS = 3000
COOLDOWN_SECONDS = 60
MAX_AGE_DAYS = 30
SUPPORTED_MEDIA = ("photo", "video", "voice", "audio", "document", "animation", "video_note")
ANON_BUTTON_TEXT = "📩 Анонимный вопрос"


def anonymous_keyboard() -> ReplyKeyboardMarkup:
    """Persistent shortcut shown below the input field in the bot's private chat."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=ANON_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


class AnonymousQuestion(StatesGroup):
    waiting_for_question = State()


class AnonymousAdminReply(StatesGroup):
    waiting_for_answer = State()


class AnonymousTicketStore:
    """Only server-side mapping; never render this content to any administrator."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid anonymous ticket state")
        return data

    def _save(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _prune(self, data: dict, now: datetime):
        for key in list(data):
            try:
                age = now - datetime.fromisoformat(data[key]["created"])
            except (KeyError, ValueError, TypeError):
                age = timedelta(days=MAX_AGE_DAYS + 1)
            if age > timedelta(days=MAX_AGE_DAYS):
                del data[key]
        if len(data) > MAX_TICKETS:
            excess = len(data) - MAX_TICKETS
            for key in sorted(data, key=lambda k: data[k].get("created", ""))[:excess]:
                del data[key]

    async def issue(self, sender_id: int, author_message_id: int | None = None) -> tuple[str | None, int]:
        """Returns (opaque ticket, seconds_to_wait); no ticket if rate limited."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            data = self._load()
            self._prune(data, now)
            for ticket in data.values():
                if ticket.get("sender") == sender_id:
                    created = datetime.fromisoformat(ticket["created"])
                    seconds = (now - created).total_seconds()
                    if seconds < COOLDOWN_SECONDS:
                        return None, max(1, int(COOLDOWN_SECONDS - seconds) + 1)
            for _ in range(8):
                ticket_id = secrets.token_hex(6)
                if ticket_id not in data:
                    break
            else:
                raise RuntimeError("Could not allocate an anonymous ticket")
            data[ticket_id] = {
                "sender": sender_id,
                "created": now.isoformat(),
                "status": "open",
                "answered": False,
            }
            if isinstance(author_message_id, int):
                data[ticket_id]["author_message_id"] = author_message_id
            self._save(data)
            return ticket_id, 0

    async def get_open(self, ticket_id: str):
        async with self._lock:
            data = self._load()
            self._prune(data, datetime.now(timezone.utc))
            value = data.get(ticket_id)
            if not value or value.get("status") != "open":
                return None
            return dict(value)

    async def set_admin_messages(
        self, ticket_id: str, message_ids: dict[int, int],
        media_message_ids: dict[int, int] | None = None,
    ) -> None:
        """Save private reply targets; never include sender ID in admin messages."""
        async with self._lock:
            data = self._load()
            if ticket_id not in data or data[ticket_id].get("status") != "open":
                return
            data[ticket_id]["admin_messages"] = {
                str(admin_id): message_id
                for admin_id, message_id in message_ids.items()
                if isinstance(admin_id, int) and isinstance(message_id, int)
            }
            if media_message_ids:
                data[ticket_id]["admin_media_messages"] = {
                    str(admin_id): message_id
                    for admin_id, message_id in media_message_ids.items()
                    if isinstance(admin_id, int) and isinstance(message_id, int)
                }
            self._save(data)

    async def find_for_admin_reply(self, admin_id: int, replied_to_id: int) -> str | None:
        """Match a Telegram Reply to a private admin copy of an anonymous question."""
        async with self._lock:
            data = self._load()
            self._prune(data, datetime.now(timezone.utc))
            for question_id, record in data.items():
                if record.get("status") != "open":
                    continue
                for field in ("admin_messages", "admin_media_messages"):
                    if record.get(field, {}).get(str(admin_id)) == replied_to_id:
                        return question_id
            return None

    async def mark_first_answer(self, ticket_id: str) -> bool:
        """Return True only for the first successful reply; never close a question."""
        async with self._lock:
            data = self._load()
            entry = data.get(ticket_id)
            if not entry or entry.get("status") != "open" or entry.get("answered"):
                return False
            entry["answered"] = True
            self._save(data)
            return True

    async def close(self, ticket_id: str) -> bool:
        async with self._lock:
            data = self._load()
            if ticket_id not in data or data[ticket_id].get("status") != "open":
                return False
            data[ticket_id]["status"] = "closed"
            # Once answered/closed, we no longer need to retain sender identity.
            data[ticket_id].pop("sender", None)
            data[ticket_id].pop("admin_messages", None)
            self._save(data)
            return True

    async def discard(self, ticket_id: str):
        async with self._lock:
            data = self._load()
            data.pop(ticket_id, None)
            self._save(data)


def is_supported_content(message: Message):
    return bool(
        (message.text is not None and not message.text.startswith("/"))
        or any(getattr(message, kind, None) is not None for kind in SUPPORTED_MEDIA)
    )


def question_markup(ticket_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"anon:reply:{ticket_id}"),
    ]])


async def send_to_admins(
    bot: Bot, admins: list[int], ticket_id: str, message: Message,
    sent_message_ids: dict[int, int] | None = None,
    media_message_ids: dict[int, int] | None = None,
) -> int:
    """Copy anonymously; optionally record per-admin message IDs for later reply status."""
    delivered = 0
    for admin_id in dict.fromkeys(admins):
        try:
            if message.text is not None:
                sent = await bot.send_message(
                    admin_id,
                    "📩 <b>Анонимный вопрос</b>\n\n"
                    + html.escape(message.text),
                    parse_mode="HTML",
                    reply_markup=question_markup(ticket_id),
                )
            else:
                # Media is COPIED, not forwarded: no original author's Telegram
                # profile is exposed. Never attach the author ID to callback_data.
                copied = await bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                if media_message_ids is not None and isinstance(getattr(copied, "message_id", None), int):
                    media_message_ids[admin_id] = copied.message_id
                sent = await bot.send_message(
                    admin_id,
                    "📩 <b>Анонимный вопрос</b> — вложение выше. "
                    "Ответьте кнопкой или через Reply.",
                    parse_mode="HTML",
                    reply_markup=question_markup(ticket_id),
                )
            delivered += 1
            if sent_message_ids is not None and isinstance(getattr(sent, "message_id", None), int):
                sent_message_ids[admin_id] = sent.message_id
        except Exception as exc:
            logger.warning(
                "ANON ADMIN DELIVERY FAILED admin=%s error=%s",
                admin_id, type(exc).__name__,
            )
    logger.info("ANON ADMIN DELIVERY delivered=%d total=%d", delivered, len(set(admins)))
    return delivered


async def notify_other_admins_answered(
    bot: Bot, admin_messages: dict[str, int], answering_admin_id: int,
) -> None:
    """Reply to each other admin's original anonymous question after delivery succeeds."""
    for admin_id_str, message_id in admin_messages.items():
        try:
            admin_id = int(admin_id_str)
            if admin_id == answering_admin_id or not isinstance(message_id, int):
                continue
            await bot.send_message(
                admin_id,
                "✅ Один из администраторов уже ответил на это сообщение.",
                reply_parameters=ReplyParameters(
                    message_id=message_id, allow_sending_without_reply=True
                ),
            )
        except Exception as exc:
            logger.warning(
                "ANON ANSWER NOTIFICATION FAILED error=%s", type(exc).__name__
            )


async def send_answer(
    bot: Bot, recipient_id: int, ticket_id: str, reply: Message,
    author_message_id: int | None = None,
):
    """Answer as the bot, directly replying to the original question when possible."""
    reply_options = {}
    if isinstance(author_message_id, int):
        reply_options["reply_parameters"] = ReplyParameters(
            message_id=author_message_id, allow_sending_without_reply=True
        )
    if reply.text is not None:
        await bot.send_message(
            recipient_id,
            "💬 <b>Ответ на анонимный вопрос</b>\n\n" + html.escape(reply.text),
            parse_mode="HTML",
            **reply_options,
        )
    else:
        await bot.send_message(
            recipient_id, "💬 Ответ на анонимный вопрос:", **reply_options
        )
        await bot.copy_message(
            chat_id=recipient_id,
            from_chat_id=reply.chat.id,
            message_id=reply.message_id,
            **reply_options,
        )


def create_anonymous_router(admins: list[int], data_dir: Path, chat_id: int) -> Router:
    router = Router(name="anonymous_admin_questions")
    tickets = AnonymousTicketStore(Path(data_dir) / TICKET_FILE)
    admin_ids = frozenset(admins)
    # Ensure concurrent admins do not both trigger the first-answer notification.
    answering_lock = asyncio.Lock()

    class QuestionReplyFilter(Filter):
        async def __call__(self, message: Message):
            if (not message.from_user or message.from_user.id not in admin_ids
                    or not message.reply_to_message
                    or message.chat.type != "private"):
                return False
            question_id = await tickets.find_for_admin_reply(
                message.from_user.id, message.reply_to_message.message_id
            )
            return {"anon_question_id": question_id} if question_id else False

    async def ensure_member(message: Message) -> bool:
        """Check membership on every question, including direct /anon submissions."""
        if not message.from_user:
            return False
        try:
            member = await message.bot.get_chat_member(
                chat_id=chat_id, user_id=message.from_user.id
            )
        except Exception as exc:
            logger.warning("ANON MEMBERSHIP CHECK FAILED error=%s", type(exc).__name__)
            await message.answer("⚠️ Не удалось проверить участие в чате. Попробуйте позже.")
            return False

        if member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and getattr(member, "is_member", False)
        ):
            return True

        await message.answer("❌ Анонимные вопросы доступны только участникам чата Dolce.")
        return False

    async def prompt_for_question(message: Message, state: FSMContext):
        if not await ensure_member(message):
            await state.clear()
            return
        await state.set_state(AnonymousQuestion.waiting_for_question)
        await message.answer("Отправьте анонимный вопрос")

    @router.message(Command("anon"), F.chat.type == "private")
    async def anon_command(message: Message, state: FSMContext):
        if not message.from_user:
            return
        direct = (message.text or "").partition(" ")[2].strip()
        if direct:
            await state.clear()
            await submit_question(message, direct_text=direct)
            return
        await prompt_for_question(message, state)

    @router.message(F.text == ANON_BUTTON_TEXT, F.chat.type == "private")
    async def anon_keyboard_button(message: Message, state: FSMContext):
        await prompt_for_question(message, state)

    @router.message(Command("anon"), F.chat.type.in_(("group", "supergroup")))
    async def anon_from_group(message: Message):
        await message.answer("📩 Для анонимного вопроса администрации напиши мне /anon в личку.")

    @router.message(Command("anon_cancel"), F.chat.type == "private")
    async def anon_cancel(message: Message, state: FSMContext):
        current = await state.get_state()
        if current in (AnonymousQuestion.waiting_for_question.state,
                       AnonymousAdminReply.waiting_for_answer.state):
            await state.clear()
            await message.answer("❎ Отменено.")
        else:
            await message.answer("Сейчас нечего отменять.")

    async def submit_question(message: Message, direct_text: str | None = None):
        if not message.from_user or message.chat.type != "private":
            return
        if not await ensure_member(message):
            return
        content = direct_text if direct_text is not None else message.text
        if content is not None and (
            len(content) > MAX_TEXT or len(html.escape(content)) > 3500
        ):
            await message.answer(
                f"⚠️ Слишком длинный текст. Максимум {MAX_TEXT} символов "
                "(специальные символы учитываются после экранирования)."
            )
            return
        if direct_text is None and not is_supported_content(message):
            await message.answer("Можно отправить текст, фото, видео, голосовое или документ.")
            return
        ticket_id, remaining = await tickets.issue(message.from_user.id, message.message_id)
        if not ticket_id:
            await message.answer(f"⏳ Подожди ещё {remaining} сек. перед следующим вопросом.")
            return
        try:
            if direct_text is not None:
                # For inline /anon <question>, forward only question body,
                # never the sender's bot-command prefix.
                proxy = type("AnonymousContent", (), {
                    "text": direct_text, "chat": message.chat, "message_id": message.message_id
                })()
            else:
                proxy = message
            admin_message_ids: dict[int, int] = {}
            admin_media_ids: dict[int, int] = {}
            delivered = await send_to_admins(
                message.bot, admins, ticket_id, proxy,
                sent_message_ids=admin_message_ids,
                media_message_ids=admin_media_ids,
            )
        except Exception:
            logger.exception("ANON MESSAGE DISPATCH ERROR ticket=%s", ticket_id)
            delivered = 0
        if delivered and admin_message_ids:
            try:
                await tickets.set_admin_messages(ticket_id, admin_message_ids, admin_media_ids)
            except Exception:
                logger.exception("ANON ADMIN MESSAGE IDS SAVE FAILED ticket=%s", ticket_id)
        if not delivered:
            await tickets.discard(ticket_id)
            await message.answer(
                "⚠️ Вопрос пока не отправлен: администраторы недоступны. "
                "Попробуй позже. Админам нужно открыть бота и нажать /start."
            )
        else:
            await message.answer(
                "✅ Анонимный вопрос отправлен. Ответы придут сюда."
            )

    @router.message(AnonymousQuestion.waiting_for_question, F.chat.type == "private")
    async def anon_question_content(message: Message, state: FSMContext):
        if message.text and message.text.startswith("/"):
            await message.answer("Напиши сообщение без команды или нажми /anon_cancel.")
            return
        # One prompt = one submission; errors don't trap user in a stale state.
        await state.clear()
        await submit_question(message)

    async def require_admin(callback: CallbackQuery):
        if (not callback.from_user or callback.from_user.id not in admin_ids
                or not callback.message
                or callback.message.chat.type != "private"
                or callback.message.chat.id != callback.from_user.id):
            await callback.answer("❌ Только для администрации Dolce.", show_alert=True)
            return False
        return True

    @router.callback_query(F.data.startswith("anon:reply:"))
    async def admin_reply_button(callback: CallbackQuery, state: FSMContext):
        if not await require_admin(callback):
            return
        ticket_id = (callback.data or "").removeprefix("anon:reply:")
        if not await tickets.get_open(ticket_id):
            await callback.answer("Этот вопрос больше недоступен.", show_alert=True)
            return
        other_state = await state.get_state()
        if other_state and other_state != AnonymousAdminReply.waiting_for_answer.state:
            await callback.answer("Сначала заверши другое действие в боте.", show_alert=True)
            return
        await state.set_state(AnonymousAdminReply.waiting_for_answer)
        await state.update_data(anon_ticket=ticket_id)
        await callback.answer()
        await callback.message.answer(
            "✍️ Отправьте ответ на анонимный вопрос. "
            "Автор не узнает, кто из администраторов ответил. "
            "На один вопрос можно отвечать несколько раз. Отмена: /anon_cancel"
        )

    @router.callback_query(F.data.startswith("anon:close:"))
    async def admin_close_button(callback: CallbackQuery, state: FSMContext):
        # Old messages may still have this button. It must never close an anon now.
        if not await require_admin(callback):
            return
        await callback.answer(
            "Закрытие отключено: на анонимный вопрос можно отвечать несколько раз.",
            show_alert=True,
        )

    async def deliver_admin_answer(message: Message, question_id: str) -> None:
        if not message.from_user or message.from_user.id not in admin_ids:
            return
        if message.text and message.text.startswith("/"):
            await message.answer("Отправьте ответ без команды.")
            return
        if not is_supported_content(message) or (
            message.text is not None and (
                len(message.text) > MAX_TEXT or len(html.escape(message.text)) > 3500
            )
        ):
            await message.answer("Отправьте текст до 3000 символов или вложение.")
            return

        async with answering_lock:
            record = await tickets.get_open(question_id)
            if not record:
                await message.answer("Этот анонимный вопрос больше недоступен.")
                return
            try:
                await send_answer(
                    message.bot, record["sender"], question_id, message,
                    author_message_id=record.get("author_message_id"),
                )
            except Exception as exc:
                logger.warning(
                    "ANON REPLY DELIVERY FAILED question=%s error=%s",
                    question_id, type(exc).__name__,
                )
                await message.answer("⚠️ Ответ не доставлен. Попробуйте позже.")
                return
            first_answer = await tickets.mark_first_answer(question_id)

        if first_answer:
            await notify_other_admins_answered(
                message.bot, record.get("admin_messages", {}), message.from_user.id
            )
        await message.answer("✅ Ответ отправлен анонимно.")

    @router.message(AnonymousAdminReply.waiting_for_answer, F.chat.type == "private")
    async def admin_answer_content(message: Message, state: FSMContext):
        if not message.from_user or message.from_user.id not in admin_ids:
            await state.clear()
            return
        stored_question_id = (await state.get_data()).get("anon_ticket")
        # An explicit Telegram Reply takes priority over an earlier button press.
        reply_target = getattr(message, "reply_to_message", None)
        replied_question_id = (
            await tickets.find_for_admin_reply(message.from_user.id, reply_target.message_id)
            if reply_target else None
        )
        question_id = replied_question_id or stored_question_id
        if not question_id:
            await state.clear()
            await message.answer("Нажмите «Ответить» под вопросом или ответьте через Reply.")
            return
        await deliver_admin_answer(message, question_id)
        # A button opens one draft; press it again for more answers. Reply needs no button.
        await state.clear()

    @router.message(QuestionReplyFilter())
    async def admin_reply_to_question(
        message: Message, state: FSMContext, anon_question_id: str,
    ):
        await deliver_admin_answer(message, anon_question_id)

    return router
