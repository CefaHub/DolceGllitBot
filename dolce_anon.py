"""Anonymous questions to the DolceBot ADMINS only.

Authors send private messages to DolceBot, never to other members. Admins see
the question and an opaque ticket ID, NOT Telegram user ID, name or username.
The bot privately maps ticket -> sender to deliver ONE administration reply.
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
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
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

    async def issue(self, sender_id: int) -> tuple[str | None, int]:
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
            }
            self._save(data)
            return ticket_id, 0

    async def get_open(self, ticket_id: str):
        async with self._lock:
            value = self._load().get(ticket_id)
            if not value or value.get("status") != "open":
                return None
            return dict(value)

    async def close(self, ticket_id: str) -> bool:
        async with self._lock:
            data = self._load()
            if ticket_id not in data or data[ticket_id].get("status") != "open":
                return False
            data[ticket_id]["status"] = "closed"
            # Once answered/closed, we no longer need to retain sender identity.
            data[ticket_id].pop("sender", None)
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
        InlineKeyboardButton(text="✅ Закрыть", callback_data=f"anon:close:{ticket_id}"),
    ]])


async def send_to_admins(bot: Bot, admins: list[int], ticket_id: str, message: Message) -> int:
    """No forward_* API; copy_message strips Telegram's forward attribution."""
    delivered = 0
    for admin_id in dict.fromkeys(admins):
        try:
            if message.text is not None:
                await bot.send_message(
                    admin_id,
                    f"📩 <b>Анонимный вопрос #{ticket_id}</b>\n\n"
                    + html.escape(message.text),
                    parse_mode="HTML",
                    reply_markup=question_markup(ticket_id),
                )
            else:
                # Media is COPIED, not forwarded: no original author's Telegram
                # profile is exposed. Never attach the author ID to callback_data.
                await bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                await bot.send_message(
                    admin_id,
                    f"📩 <b>Анонимное обращение #{ticket_id}</b> — вложение выше. "
                    "Ответить можно кнопкой.",
                    parse_mode="HTML",
                    reply_markup=question_markup(ticket_id),
                )
            delivered += 1
        except Exception as exc:
            logger.warning(
                "ANON ADMIN DELIVERY FAILED admin=%s error=%s",
                admin_id, type(exc).__name__,
            )
    logger.info("ANON ADMIN DELIVERY delivered=%d total=%d", delivered, len(set(admins)))
    return delivered


async def send_answer(bot: Bot, recipient_id: int, ticket_id: str, reply: Message):
    """Only bot identity appears to sender; never admin's username/ID."""
    if reply.text is not None:
        await bot.send_message(
            recipient_id,
            f"💌 <b>Ответ администрации на обращение #{ticket_id}</b>\n\n"
            + html.escape(reply.text),
            parse_mode="HTML",
        )
    else:
        await bot.send_message(
            recipient_id, f"💌 Ответ администрации на обращение #{ticket_id}:"
        )
        await bot.copy_message(
            chat_id=recipient_id,
            from_chat_id=reply.chat.id,
            message_id=reply.message_id,
        )


def create_anonymous_router(admins: list[int], data_dir: Path, chat_id: int) -> Router:
    router = Router(name="anonymous_admin_questions")
    tickets = AnonymousTicketStore(Path(data_dir) / TICKET_FILE)
    admin_ids = frozenset(admins)

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
            await message.answer("❎ Анонимное обращение отменено.")
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
        ticket_id, remaining = await tickets.issue(message.from_user.id)
        if not ticket_id:
            await message.answer(f"⏳ Подожди ещё {remaining} сек. перед следующим обращением.")
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
            delivered = await send_to_admins(message.bot, admins, ticket_id, proxy)
        except Exception:
            logger.exception("ANON MESSAGE DISPATCH ERROR ticket=%s", ticket_id)
            delivered = 0
        if not delivered:
            await tickets.discard(ticket_id)
            await message.answer(
                "⚠️ Вопрос пока не отправлен: администраторы недоступны. "
                "Попробуй позже. Админам нужно открыть бота и нажать /start."
            )
        else:
            await message.answer(
                f"✅ Вопрос #{ticket_id} анонимно передан администрации. "
                "Если администратор ответит, сообщение придёт сюда."
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
            await callback.answer("Обращение уже закрыто или устарело.", show_alert=True)
            return
        other_state = await state.get_state()
        if other_state and other_state != AnonymousAdminReply.waiting_for_answer.state:
            await callback.answer("Сначала заверши другое действие в боте.", show_alert=True)
            return
        await state.set_state(AnonymousAdminReply.waiting_for_answer)
        await state.update_data(anon_ticket=ticket_id)
        await callback.answer()
        await callback.message.answer(
            f"✍️ Напиши ответ на анонимное обращение #{ticket_id}. "
            "Я доставлю его автору без раскрытия личности. Отмена: /anon_cancel"
        )

    @router.callback_query(F.data.startswith("anon:close:"))
    async def admin_close_button(callback: CallbackQuery, state: FSMContext):
        if not await require_admin(callback):
            return
        ticket_id = (callback.data or "").removeprefix("anon:close:")
        if await tickets.close(ticket_id):
            await callback.answer("Обращение закрыто.")
            await callback.message.answer(f"✅ Анонимное обращение #{ticket_id} закрыто.")
        else:
            await callback.answer("Уже закрыто или устарело.", show_alert=True)

    @router.message(AnonymousAdminReply.waiting_for_answer, F.chat.type == "private")
    async def admin_answer_content(message: Message, state: FSMContext):
        if not message.from_user or message.from_user.id not in admin_ids:
            await state.clear()
            return
        if message.text and message.text.startswith("/"):
            await message.answer("Напиши текст ответа без команды или /anon_cancel.")
            return
        if not is_supported_content(message) or (
            message.text is not None and (
                len(message.text) > MAX_TEXT or len(html.escape(message.text)) > 3500
            )
        ):
            await message.answer("Ответь текстом до 3000 символов или отправь вложение.")
            return
        thread = (await state.get_data()).get("anon_ticket")
        record = await tickets.get_open(thread) if thread else None
        if not record:
            await state.clear()
            await message.answer("Обращение уже закрыто, ответ не отправлен.")
            return
        try:
            await send_answer(message.bot, record["sender"], thread, message)
        except Exception as exc:
            logger.warning(
                "ANON REPLY DELIVERY FAILED ticket=%s error=%s", thread, type(exc).__name__
            )
            await message.answer("⚠️ Ответ не доставлен: автор заблокировал бота или недоступен.")
            return
        await tickets.close(thread)
        await state.clear()
        await message.answer(f"✅ Ответ на анонимный вопрос #{thread} доставлен.")

    return router
