import asyncio
import json
import logging
import os
import random
import re
import shutil
from pathlib import Path
from typing import Dict, Set, List

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)

# ---------- Настройки ----------
TOKEN = "8700266151:AAElnV2fk7P-2sUdyErxcNeOezp0LlY6870"
PHOTOS_BASE_DIR = "photos"
INTUITION_FILE = "groups/intuitioninfo.txt"
SCORES_FILE = "scores.json"
MAX_ROUNDS = 20
TIMEOUT_SECONDS = 60
TEMP_DIR = "temp"

# ---------- Админы (ID) ----------
ADMINS = [5702167274, 5286390518]  # ← замените на реальные

# ---------- Синонимы для Угадайки ----------
ALIASES: Dict[str, List[str]] = {
    "eunchae": ["eunchae", "ынче", "ынчэ"],
    "winter":  ["winter", "винтер", "винтэр"],
    "yunjin":  ["yunjin", "юнджин", "юнжин"],
    "kazuha":  ["кадзуха", "kazuha", "казуха"],
    "chaewon":  ["чевон", "чэвон", "chaewon"],
    "iroha":  ["ироха", "iroha"],
    "leeseo":  ["leeseo", "лисо"],
    "minju":  ["минджу", "minju"],
    "moka":  ["moka", "мока"],
    "rei":  ["рей", "rei"],
    "sakura":  ["сакура", "sakura"],
    "wonhee":  ["вонхи", "wonhee"],
    "wonyoung":  ["вонён", "вонен", "wonyoung"],
    "yunah":  ["yunah", "юна"]
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Загрузка фото ----------
def load_guess_photos():
    base = Path(PHOTOS_BASE_DIR)
    if not base.exists():
        raise FileNotFoundError(f"Папка {PHOTOS_BASE_DIR} не найдена!")
    questions = []
    for person_dir in base.iterdir():
        if not person_dir.is_dir():
            continue
        folder_name = person_dir.name.strip().lower()
        for img in person_dir.glob("*"):
            if img.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
                questions.append({"path": str(img), "folder": folder_name})
    if not questions:
        raise FileNotFoundError("Нет фото в подпапках photos/")
    return questions

ALL_GUESS = load_guess_photos()
logger.info(f"Угадайка: загружено {len(ALL_GUESS)} вопросов")

# ---------- Интуиция ----------
def load_intuition():
    path = Path(INTUITION_FILE)
    if not path.exists():
        logger.warning(f"Файл {INTUITION_FILE} не найден")
        return []
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            match = re.search(r'\{(.*?)\}', line)
            if not match:
                continue
            raw = match.group(1)
            aliases = [a.strip().lower() for a in raw.split("/") if a.strip()]
            if not aliases:
                continue
            description = re.sub(r'\{.*?\}', '', line).strip()
            if not description:
                description = "Кто это?"
            lines.append({"description": description, "aliases": aliases})
    return lines

INTUITION_LINES = load_intuition()
logger.info(f"Интуиция: загружено {len(INTUITION_LINES)} описаний")

# ---------- Рейтинг ----------
def load_scores():
    if not os.path.exists(SCORES_FILE):
        return {}
    with open(SCORES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_scores(scores):
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)

# ---------- Игры ----------
games: Dict[int, dict] = {}
intuition_used: Dict[int, Set[int]] = {}

def get_user_name(user: types.User) -> str:
    return f"@{user.username}" if user.username else user.full_name

async def send_photo_msg(bot: Bot, chat_id: int, path: str, caption="") -> Message:
    return await bot.send_photo(chat_id, FSInputFile(path), caption=caption)

async def timeout_task(chat_id: int, bot: Bot):
    await asyncio.sleep(TIMEOUT_SECONDS)
    game = games.get(chat_id)
    if not game or not game.get("active"):
        return
    game["timer_task"] = None
    if game["game_type"] == "intuition":
        correct = "/".join(game["current_aliases"])
        await bot.send_message(chat_id, f"⏰ Время вышло! Правильный ответ: {correct}")
    else:
        await bot.send_message(chat_id, "⏰ Время на ответ вышло! Никто не получает балл.")
    await show_current_scores(chat_id, bot)
    await advance_game(chat_id, bot)

def pick_unused_guess(used: Set[str]):
    available = [q for q in ALL_GUESS if q["path"] not in used]
    if not available:
        used.clear()
        available = ALL_GUESS.copy()
    return random.choice(available)

def pick_unused_intuition(chat_id: int):
    used = intuition_used.get(chat_id, set())
    available = [i for i in range(len(INTUITION_LINES)) if i not in used]
    if not available:
        used.clear()
        available = list(range(len(INTUITION_LINES)))
    idx = random.choice(available)
    used.add(idx)
    return idx, INTUITION_LINES[idx]["description"], INTUITION_LINES[idx]["aliases"]

async def start_guess_round(chat_id: int, bot: Bot):
    game = games[chat_id]
    game["current_round"] += 1
    q = pick_unused_guess(game["used_items"])
    folder = q["folder"]
    aliases = ALIASES.get(folder, [folder])
    game["current_aliases"] = [a.lower() for a in aliases]
    game["used_items"].add(q["path"])
    msg = await send_photo_msg(bot, chat_id, q["path"], caption="Кто это? 🤔")
    game["photo_message_id"] = msg.message_id
    task = asyncio.create_task(timeout_task(chat_id, bot))
    game["timer_task"] = task

async def start_intuition_round(chat_id: int, bot: Bot):
    game = games[chat_id]
    game["current_round"] += 1
    idx, desc, aliases = pick_unused_intuition(chat_id)
    game["current_aliases"] = aliases
    await bot.send_message(chat_id, f"🎭 *Интуиция* 🎭\n\n{desc}", parse_mode="Markdown")
    task = asyncio.create_task(timeout_task(chat_id, bot))
    game["timer_task"] = task

async def advance_game(chat_id: int, bot: Bot):
    game = games.get(chat_id)
    if not game or not game["active"]:
        return
    if game["current_round"] >= game["rounds"]:
        await end_game(chat_id, bot, stopped=False)
    else:
        await asyncio.sleep(2)
        if game["game_type"] == "guess":
            await start_guess_round(chat_id, bot)
        else:
            await start_intuition_round(chat_id, bot)

async def show_current_scores(chat_id: int, bot: Bot):
    game = games.get(chat_id)
    if not game:
        return
    scores = game["scores"]
    if not scores:
        await bot.send_message(chat_id, "📊 Пока никто не набрал баллов.")
        return
    lines = ["📊 *Текущий счёт:*"]
    for i, (uid, sc) in enumerate(sorted(scores.items(), key=lambda x: x[1], reverse=True), 1):
        name = game["players_names"].get(uid, f"ID{uid}")
        lines.append(f"{i}. {name} — {sc} балл(ов)")
    await bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

async def end_game(chat_id: int, bot: Bot, stopped: bool = False):
    game = games.pop(chat_id, None)
    if not game:
        return
    if game.get("timer_task"):
        game["timer_task"].cancel()
    scores = game["scores"]
    players = game["players_names"]
    global_scores = load_scores()
    for uid, sc in scores.items():
        if uid not in global_scores:
            global_scores[uid] = {"score": 0, "name": players.get(uid, f"ID{uid}")}
        global_scores[uid]["score"] += sc
        global_scores[uid]["name"] = players.get(uid, global_scores[uid]["name"])
    save_scores(global_scores)

    game_type = "🎭 Интуиция" if game.get("game_type") == "intuition" else "🎯 Угадайка"
    prefix = f"🛑 Игра ({game_type}) остановлена!" if stopped else f"🏆 Игра ({game_type}) завершена! 🏆"
    lines = [prefix]
    if scores:
        sort = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        winner = players.get(sort[0][0], f"ID{sort[0][0]}")
        lines.append(f"🥇 Победитель: *{winner}* ({sort[0][1]} балл.)")
        lines.append("\n*Места:*")
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, (uid, sc) in enumerate(sort, 1):
            name = players.get(uid, f"ID{uid}")
            medal = medals.get(i, f"{i}.")
            lines.append(f"{medal} {name} — {sc} балл.")
    else:
        lines.append("Никто не набрал баллов.")
    await bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

# ---------- FSM для админки ----------
class AddStates(StatesGroup):
    waiting_for_aliases = State()
    waiting_for_photos = State()
    confirmation = State()

# ---------- Роутеры ----------
router = Router()
admin_router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

# ---------- Команды админ-панели ----------
@admin_router.message(Command("da"), F.chat.type == "private")
async def cmd_da(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к админ-панели.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /da <имя_папки>\nПример: /da eunchae")
        return
    folder_name = args[1].strip().lower()
    if not re.match(r'^[a-z0-9_]+$', folder_name):
        await message.answer("Имя папки должно содержать только латиницу, цифры и подчёркивания.")
        return

    target_dir = Path(PHOTOS_BASE_DIR) / folder_name
    await state.update_data(folder_name=folder_name)

    if target_dir.exists():
        existing_aliases = ALIASES.get(folder_name, [folder_name])
        await state.update_data(aliases=existing_aliases)
        temp_dir = Path(TEMP_DIR) / folder_name
        temp_dir.mkdir(parents=True, exist_ok=True)
        await state.update_data(temp_dir=str(temp_dir))
        await state.set_state(AddStates.waiting_for_photos)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data="photos_done")
        ]])
        await message.answer(
            "Папка уже существует. Добавляем фото в неё.\n\n"
            "Отправьте фото (по одному). Когда закончите, нажмите «Готово».\n"
            "⚠️ Напоминание: лица на фото должны быть замазаны.",
            reply_markup=kb
        )
        return

    await state.set_state(AddStates.waiting_for_aliases)
    await message.answer(
        "Введите варианты имени через `/` (например: `ынче/ынчэ/eunchae`):"
    )

@admin_router.message(AddStates.waiting_for_aliases, F.text, F.chat.type == "private")
async def process_aliases(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    text = message.text.strip()
    aliases = [a.strip().lower() for a in text.split("/") if a.strip()]
    if not aliases:
        await message.answer("Вы не указали ни одного варианта. Повторите:")
        return
    await state.update_data(aliases=aliases)
    data = await state.get_data()
    folder_name = data["folder_name"]
    temp_dir = Path(TEMP_DIR) / folder_name
    temp_dir.mkdir(parents=True, exist_ok=True)
    await state.update_data(temp_dir=str(temp_dir))
    await state.set_state(AddStates.waiting_for_photos)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово", callback_data="photos_done")
    ]])
    await message.answer(
        "Отправьте фото (по одному). Когда закончите, нажмите кнопку «Готово».\n"
        "⚠️ Напоминание: лица на фото должны быть замазаны.",
        reply_markup=kb
    )

@admin_router.message(AddStates.waiting_for_photos, F.photo, F.chat.type == "private")
async def process_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    data = await state.get_data()
    temp_dir = Path(data["temp_dir"])
    ext = ".jpg"
    img_path = temp_dir / f"{photo.file_unique_id}{ext}"
    await message.bot.download_file(file.file_path, destination=img_path)
    await message.answer("📸 Фото сохранено. Можете отправить ещё или нажать «Готово».")

@admin_router.callback_query(F.data == "photos_done", AddStates.waiting_for_photos)
async def photos_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    temp_dir = Path(data["temp_dir"])
    photos = list(temp_dir.glob("*"))
    if not photos:
        await callback.message.answer("Вы не отправили ни одного фото. Отправьте хотя бы одно.")
        return
    aliases = data["aliases"]
    folder_name = data["folder_name"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_add")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")],
    ])
    await callback.message.answer(
        f"📁 *Участник:* `{folder_name}`\n"
        f"🔤 Варианты ответа: {', '.join(aliases)}\n"
        f"🖼 Новых фото: {len(photos)} шт.\n\n"
        f"Подтвердите добавление:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await state.set_state(AddStates.confirmation)

@admin_router.callback_query(F.data == "confirm_add", AddStates.confirmation)
async def confirm_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    folder_name = data["folder_name"]
    temp_dir = Path(data["temp_dir"])
    target_dir = Path(PHOTOS_BASE_DIR) / folder_name
    # Убедимся, что целевая папка существует
    target_dir.mkdir(parents=True, exist_ok=True)
    # Перемещаем файлы
    for f in temp_dir.iterdir():
        shutil.move(str(f), str(target_dir / f.name))
    shutil.rmtree(temp_dir)
    # Обновляем ALIASES и список фото
    aliases = data["aliases"]
    ALIASES[folder_name] = aliases
    global ALL_GUESS
    ALL_GUESS = load_guess_photos()
    await callback.message.answer(f"✅ Участник `{folder_name}` обновлён! Фото добавлены.")
    await state.clear()

@admin_router.callback_query(F.data == "cancel_add", AddStates.confirmation)
async def cancel_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    temp_dir = Path(data["temp_dir"])
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    await callback.message.answer("❌ Добавление отменено.")
    await state.clear()

@admin_router.message(Command("da_cancel"), F.chat.type == "private")
async def cmd_da_cancel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    current_state = await state.get_state()
    if current_state is not None:
        data = await state.get_data()
        temp_dir = data.get("temp_dir")
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir)
        await state.clear()
        await message.answer("🔄 Процесс добавления отменён.")
    else:
        await message.answer("Нет активного процесса добавления.")

# ---------- Публичная команда показа папок ----------
@router.message(Command("dafolders"))
async def cmd_dafolders(message: Message):
    base = Path(PHOTOS_BASE_DIR)
    if not base.exists():
        await message.answer("Папка с фото не найдена.")
        return
    dirs = [d.name for d in base.iterdir() if d.is_dir()]
    if not dirs:
        await message.answer("Нет ни одной папки с фото.")
        return
    dirs.sort()
    text = "📁 *Существующие папки:*\n" + "\n".join(f"• `{d}`" for d in dirs)
    await message.answer(text, parse_mode="Markdown")

# ---------- Игровые команды ----------
@router.message(Command("dainfo"))
async def cmd_dainfo(message: Message):
    await message.answer(
        "👋 Привет! Я DolceBot\n"
        "🎯 Угадайка: угадывайте людей по фото.\n"
        "🎭 Интуиция: угадывайте человека по описанию.\n\n"
        "📌 Команды:\n"
        "/guessgame — начать Угадайку\n"
        "/intuition — начать Интуицию\n"
        "/stopdolce — остановить игру\n"
        "/dolcetop — топ-10 игроков\n"
        "/dolcestat — твоя статистика\n"
        "/dafolders — показать все папки с фото\n"
        "/dainfo — это сообщение\n\n"
        "⚡ Во время игры пишите ответ прямо в чат (reply не нужен).\n\n"
        "🛠 Для админов: команды /da, /da_cancel в ЛС бота.\n"
        "Имя папки при добавлении должно совпадать с английской версией имени айдола."
    )

@router.message(Command("guessgame"))
async def cmd_guessgame(message: Message):
    chat_id = message.chat.id
    if chat_id in games and games[chat_id].get("active"):
        await message.answer("🎮 Уже идёт другая игра! Остановите: /stopdolce")
        return
    games[chat_id] = {
        "active": False,
        "state": "waiting_rounds_guess",
        "game_type": "guess",
        "chat_id": chat_id,
        "rounds": 0,
        "current_round": 0,
        "scores": {},
        "players_names": {},
        "current_aliases": [],
        "photo_message_id": None,
        "timer_task": None,
        "used_items": set()
    }
    await message.answer("🎮 *Угадайка* 🎮\nВведите количество раундов (1–20):", parse_mode="Markdown")

@router.message(Command("intuition"))
async def cmd_intuition(message: Message):
    chat_id = message.chat.id
    if chat_id in games and games[chat_id].get("active"):
        await message.answer("🎮 Уже идёт другая игра! Остановите: /stopdolce")
        return
    if not INTUITION_LINES:
        await message.answer("❌ Нет описаний. Добавьте файл groups/intuitioninfo.txt")
        return
    if chat_id not in intuition_used:
        intuition_used[chat_id] = set()
    games[chat_id] = {
        "active": False,
        "state": "waiting_rounds_intuition",
        "game_type": "intuition",
        "chat_id": chat_id,
        "rounds": 0,
        "current_round": 0,
        "scores": {},
        "players_names": {},
        "current_aliases": [],
        "photo_message_id": None,
        "timer_task": None,
    }
    await message.answer("🎭 *Интуиция* 🎭\nВведите количество раундов (1–20):", parse_mode="Markdown")

@router.message(Command("stopdolce"))
async def cmd_stopdolce(message: Message):
    chat_id = message.chat.id
    game = games.get(chat_id)
    if not game or not game.get("active"):
        await message.answer("❌ Нет активной игры.")
        return
    await message.answer("🛑 Останавливаю игру...")
    await end_game(chat_id, message.bot, stopped=True)

@router.message(Command("dolcetop"))
async def cmd_dolcetop(message: Message):
    scores = load_scores()
    if not scores:
        await message.answer("🏅 Пока никто не играл.")
        return
    top = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)[:10]
    lines = ["🏅 *Топ-10 игроков* 🏅"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, (uid, data) in enumerate(top, 1):
        medal = medals.get(i, f"{i}.")
        lines.append(f"{medal} {data['name']} — {data['score']} балл.")
    await message.answer("\n".join(lines), parse_mode="Markdown")

@router.message(Command("dolcestat"))
async def cmd_dolcestat(message: Message):
    uid = str(message.from_user.id)
    scores = load_scores()
    if uid not in scores:
        await message.answer("📊 Вы ещё не играли.")
        return
    sort = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    rank = next(i for i, (u, _) in enumerate(sort, 1) if u == uid)
    data = scores[uid]
    await message.answer(
        f"📊 *{data['name']}*\n"
        f"🏅 Место: {rank}\n"
        f"🎯 Баллы: {data['score']}",
        parse_mode="Markdown"
    )

# ---------- Текст во время игры ----------
@router.message(F.text)
async def handle_text(message: Message):
    chat_id = message.chat.id
    game = games.get(chat_id)
    if not game:
        return

    text = message.text.strip()

    if game["state"].startswith("waiting_rounds"):
        try:
            rounds = int(text)
        except ValueError:
            await message.answer("❌ Введите число от 1 до 20.")
            return
        if not 1 <= rounds <= MAX_ROUNDS:
            await message.answer(f"❌ Допустимо 1–{MAX_ROUNDS}.")
            return

        game["rounds"] = rounds
        game["active"] = True
        if game["game_type"] == "guess":
            game["state"] = "playing_guess"
            game["scores"] = {}
            game["players_names"] = {}
            await message.answer(f"🎮 Угадайка: {rounds} раунд(ов). Поехали!")
            await start_guess_round(chat_id, message.bot)
        else:
            game["state"] = "playing_intuition"
            game["scores"] = {}
            game["players_names"] = {}
            await message.answer(f"🎭 Интуиция: {rounds} раунд(ов). Поехали!")
            await start_intuition_round(chat_id, message.bot)
        return

    if game.get("active") and game["state"] in ("playing_guess", "playing_intuition"):
        user_answer = text.lower()
        if user_answer in game.get("current_aliases", []):
            user = message.from_user
            uid = str(user.id)
            game["scores"][uid] = game["scores"].get(uid, 0) + 1
            game["players_names"][uid] = get_user_name(user)

            if game.get("timer_task"):
                game["timer_task"].cancel()
                game["timer_task"] = None

            await message.answer(f"✅ {get_user_name(user)} угадал(а)! +1 балл.")
            await show_current_scores(chat_id, message.bot)
            await advance_game(chat_id, message.bot)

# ---------- Запуск ----------
async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(router)
    Path(TEMP_DIR).mkdir(exist_ok=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
