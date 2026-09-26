import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "data.sqlite3")
MASTER_KEY = os.getenv("MASTER_KEY", "").strip()
API_ID = int(os.getenv("TELEGRAM_API_ID", "2040"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")
SESSION_FILE = os.getenv("SESSION_FILE", "/root/workspace-dev/business-tg-bot/user_session")

DEFAULT_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_PROMPT = (
    "Ты — мой автоответчик в Telegram. Отвечай естественно, кратко и по существу. "
    "Не говори, что ты бот или ИИ, если это не требуется контекстом. "
    "Учитывай сообщение собеседника и отвечай как обычный человек."
)

HISTORY_LIMIT = 12  # last messages of a dialog sent to the model as context
HISTORY_TTL_HOURS = 24

PROMPT_MODES = {
    "system": "стандартный (только system-роль)",
    "strong": "усиленный (system + напоминание в каждом сообщении)",
    "merge": "внутри сообщения (для API без system-роли)",
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID is not configured")
if not MASTER_KEY:
    raise RuntimeError("MASTER_KEY is not configured")

fernet = Fernet(MASTER_KEY.encode())
dp = Dispatcher()
pending = {}
last_online_tracker = {OWNER_ID: 0.0}

tele_client: Optional[TelegramClient] = None


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS settings (
        owner_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        api_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1/chat/completions',
        api_key_enc TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
        prompt TEXT NOT NULL DEFAULT '',
        delay REAL NOT NULL DEFAULT 2,
        max_replies INTEGER NOT NULL DEFAULT 1,
        only_offline INTEGER NOT NULL DEFAULT 1,
        offline_delay_min INTEGER NOT NULL DEFAULT 5,
        smart_pause_min INTEGER NOT NULL DEFAULT 10,
        reset_hours INTEGER NOT NULL DEFAULT 24
    );

    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS history_chat ON history(owner_id, chat_id, ts);

    CREATE TABLE IF NOT EXISTS reply_counts (
        owner_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        last_reply_time REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(owner_id, chat_id)
    );
    """)

    cols_settings = [r["name"] for r in con.execute("PRAGMA table_info(settings)").fetchall()]
    if "only_offline" not in cols_settings:
        con.execute("ALTER TABLE settings ADD COLUMN only_offline INTEGER NOT NULL DEFAULT 1")
    if "offline_delay_min" not in cols_settings:
        con.execute("ALTER TABLE settings ADD COLUMN offline_delay_min INTEGER NOT NULL DEFAULT 5")
    if "smart_pause_min" not in cols_settings:
        con.execute("ALTER TABLE settings ADD COLUMN smart_pause_min INTEGER NOT NULL DEFAULT 10")
    if "reset_hours" not in cols_settings:
        con.execute("ALTER TABLE settings ADD COLUMN reset_hours INTEGER NOT NULL DEFAULT 24")
    if "prompt_mode" not in cols_settings:
        con.execute("ALTER TABLE settings ADD COLUMN prompt_mode TEXT NOT NULL DEFAULT 'strong'")
    if "strong_mode_migrated" not in cols_settings:
        # One-time switch of existing installs to the strengthened prompt mode.
        con.execute("ALTER TABLE settings ADD COLUMN strong_mode_migrated INTEGER NOT NULL DEFAULT 1")
        con.execute("UPDATE settings SET prompt_mode='strong' WHERE prompt_mode='system'")
    con.execute("UPDATE settings SET prompt=? WHERE owner_id=? AND TRIM(prompt)=''", (DEFAULT_PROMPT, OWNER_ID))

    cols_counts = [r["name"] for r in con.execute("PRAGMA table_info(reply_counts)").fetchall()]
    if "last_reply_time" not in cols_counts:
        con.execute("ALTER TABLE reply_counts ADD COLUMN last_reply_time REAL NOT NULL DEFAULT 0")

    row = con.execute("SELECT owner_id FROM settings WHERE owner_id=?", (OWNER_ID,)).fetchone()
    if not row:
        con.execute(
            "INSERT INTO settings(owner_id, prompt, model, api_url, only_offline, offline_delay_min, smart_pause_min, reset_hours) VALUES(?, ?, ?, ?, 1, 5, 10, 24)",
            (OWNER_ID, DEFAULT_PROMPT, DEFAULT_MODEL, DEFAULT_API_URL),
        )
    con.commit()
    con.close()


def get_settings():
    con = db()
    row = con.execute("SELECT * FROM settings WHERE owner_id=?", (OWNER_ID,)).fetchone()
    con.close()
    if not row:
        init_db()
        con = db()
        row = con.execute("SELECT * FROM settings WHERE owner_id=?", (OWNER_ID,)).fetchone()
        con.close()
    return dict(row)


def set_setting(key, value):
    allowed = {
        "enabled",
        "api_url",
        "api_key_enc",
        "model",
        "prompt",
        "delay",
        "max_replies",
        "only_offline",
        "offline_delay_min",
        "smart_pause_min",
        "reset_hours",
        "prompt_mode",
    }
    if key not in allowed:
        raise ValueError(key)
    con = db()
    con.execute(f"UPDATE settings SET {key}=? WHERE owner_id=?", (value, OWNER_ID))
    con.commit()
    con.close()


def get_api_key() -> str:
    enc = get_settings().get("api_key_enc", "")
    if not enc:
        return ""
    try:
        return fernet.decrypt(enc.encode()).decode()
    except Exception:
        return ""


def set_api_key(value: str):
    enc = fernet.encrypt(value.encode()).decode()
    set_setting("api_key_enc", enc)


def get_count_and_check_reset(chat_id: int) -> int:
    s = get_settings()
    reset_hours = int(s.get("reset_hours", 24))
    now = time.time()

    con = db()
    row = con.execute(
        "SELECT count, last_reply_time FROM reply_counts WHERE owner_id=? AND chat_id=?",
        (OWNER_ID, chat_id),
    ).fetchone()

    if not row:
        con.close()
        return 0

    cnt = int(row["count"])
    last_reply = float(row["last_reply_time"] or 0)

    # If reset_hours > 0 and 24h passed since last reply -> reset count to 0!
    if reset_hours > 0 and last_reply > 0 and (now - last_reply) >= (reset_hours * 3600):
        con.execute(
            "UPDATE reply_counts SET count=0 WHERE owner_id=? AND chat_id=?",
            (OWNER_ID, chat_id),
        )
        con.commit()
        con.close()
        return 0

    con.close()
    return cnt


def increment_count(chat_id: int):
    now = time.time()
    con = db()
    con.execute("""
        INSERT INTO reply_counts(owner_id, chat_id, count, last_reply_time) VALUES(?, ?, 1, ?)
        ON CONFLICT(owner_id, chat_id) DO UPDATE SET count=count+1, last_reply_time=excluded.last_reply_time
    """, (OWNER_ID, chat_id, now))
    con.commit()
    con.close()


def reset_count(chat_id: Optional[int] = None):
    con = db()
    if chat_id is None:
        con.execute("DELETE FROM reply_counts WHERE owner_id=?", (OWNER_ID,))
    else:
        con.execute(
            "DELETE FROM reply_counts WHERE owner_id=? AND chat_id=?",
            (OWNER_ID, chat_id),
        )
    con.commit()
    con.close()


def add_history(chat_id: int, role: str, content: str):
    content = (content or "").strip()
    if not content:
        return
    con = db()
    con.execute(
        "INSERT INTO history(owner_id, chat_id, role, content, ts) VALUES(?, ?, ?, ?, ?)",
        (OWNER_ID, chat_id, role, content, time.time()),
    )
    con.execute(
        "DELETE FROM history WHERE owner_id=? AND ts < ?",
        (OWNER_ID, time.time() - HISTORY_TTL_HOURS * 3600),
    )
    con.commit()
    con.close()


def get_history(chat_id: int) -> list:
    con = db()
    rows = con.execute(
        "SELECT role, content FROM history WHERE owner_id=? AND chat_id=? AND ts >= ? "
        "ORDER BY id DESC LIMIT ?",
        (OWNER_ID, chat_id, time.time() - HISTORY_TTL_HOURS * 3600, HISTORY_LIMIT),
    ).fetchall()
    con.close()
    # Many APIs require the dialog to start with "user" and roles to alternate.
    out = []
    for r in reversed(rows):
        if not out and r["role"] != "user":
            continue
        if out and out[-1]["role"] == r["role"]:
            out[-1]["content"] += "\n" + r["content"]
        else:
            out.append({"role": r["role"], "content": r["content"]})
    return out


def clear_history():
    con = db()
    con.execute("DELETE FROM history WHERE owner_id=?", (OWNER_ID,))
    con.commit()
    con.close()


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def status_text():
    s = get_settings()
    return "🟢 Включён" if s["enabled"] else "🔴 Выключен"


def short_api(url):
    u = (url or "").replace("https://", "").replace("http://", "")
    return u[:38] + "…" if len(u) > 39 else u


async def is_session_active() -> bool:
    global tele_client
    if not tele_client:
        return False
    try:
        if not tele_client.is_connected():
            await tele_client.connect()
        return await tele_client.is_user_authorized()
    except Exception:
        return False


def main_text():
    s = get_settings()
    key = "🟢 Настроен" if s["api_key_enc"] else "🔴 Не настроен"
    offline_mode = f"🟢 Только офлайн (+{s.get('offline_delay_min', 5)}м)" if s.get("only_offline") else "⚪ Всегда"
    reset_h = f"{s.get('reset_hours', 24)}ч" if s.get('reset_hours', 24) > 0 else "Выкл"
    return (
        "🤖 <b>AI AUTO REPLY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Статус        {status_text()}\n"
        f"Модель        <code>{esc(s['model'])}</code>\n"
        f"API           <code>{esc(short_api(s['api_url']))}</code>\n"
        f"API-ключ      {key}\n"
        f"Режим ответа: <b>{offline_mode}</b>\n"
        f"Лимит         <b>{s['max_replies']} отв. (сброс {reset_h})</b>\n\n"
        "Выбери раздел:"
    )


def keyboard_main():
    s = get_settings()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 Выключить автоответчик" if s["enabled"] else "🟢 Включить автоответчик",
            callback_data="toggle"
        )],
        [
            InlineKeyboardButton(text="🧠 ИИ и API", callback_data="page_ai"),
            InlineKeyboardButton(text="💬 Ответы и лимиты", callback_data="page_reply"),
        ],
        [
            InlineKeyboardButton(text="👤 Статус онлайна", callback_data="page_presence"),
            InlineKeyboardButton(text="📊 Статус", callback_data="page_status"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Дополнительно", callback_data="page_more"),
        ],
    ])


async def render(call: CallbackQuery, text, keyboard):
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await call.answer()


@dp.message(CommandStart())
@dp.message(Command("settings"))
@dp.message(Command("menu"))
async def cmd_start(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    await render(call, main_text(), keyboard_main())


@dp.callback_query(F.data == "toggle")
async def toggle_handler(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    new_val = 0 if s["enabled"] else 1
    set_setting("enabled", new_val)
    await render(call, main_text(), keyboard_main())


@dp.callback_query(F.data == "page_presence")
async def page_presence(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    auth = await is_session_active()
    session_status = "🟢 Подключена и активна" if auth else "🔴 Не подключена"
    is_offline = bool(s.get("only_offline"))
    mode_str = "🟢 Только офлайн" if is_offline else "⚪ Всегда"
    delay_min = s.get("offline_delay_min", 5)

    text = (
        "👤 <b>СТАТУС ОНЛАЙНА (MTPROTO)</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Сессия Telegram: <b>{session_status}</b>\n"
        f"Режим ответа: <b>{mode_str}</b>\n"
        f"Задержка после онлайна: <b>{delay_min} мин.</b>\n\n"
        "<i>Бот определяет твой реальный статус в Telegram. Если ты в сети или вышел из сети менее указанного числа минут назад — бот молчит.</i>"
    )

    btn_offline = "🔘 Только офлайн" if is_offline else "⚪ Только офлайн"
    btn_always = "🔘 Всегда" if not is_offline else "⚪ Всегда"

    kb_rows = [
        [
            InlineKeyboardButton(text=btn_offline, callback_data="set_mode_offline"),
            InlineKeyboardButton(text=btn_always, callback_data="set_mode_always"),
        ],
        [InlineKeyboardButton(text="⏱ Изменить минуты задержки после онлайна", callback_data="input_offline_delay")],
        [InlineKeyboardButton(text="🔄 Проверить статус онлайна сейчас", callback_data="check_my_status")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ]

    await render(call, text, InlineKeyboardMarkup(inline_keyboard=kb_rows))


@dp.callback_query(F.data == "set_mode_offline")
async def set_mode_offline(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    set_setting("only_offline", 1)
    await page_presence(call)


@dp.callback_query(F.data == "set_mode_always")
async def set_mode_always(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    set_setting("only_offline", 0)
    await page_presence(call)


@dp.callback_query(F.data == "input_offline_delay")
async def input_offline_delay(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "offline_delay"
    await call.message.answer(
        "⏱ <b>Сколько минут должно пройти после выхода из сети</b>, чтобы бот начал отвечать?\n\n"
        "• <code>0</code> — отвечать сразу как вышел из сети\n"
        "• <code>5</code> — ждать 5 минут после выхода из сети\n"
        "• <code>15</code> — ждать 15 минут\n\n"
        "Отправь число минут (от 0 до 120):",
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "check_my_status")
async def check_my_status(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    suppress, desc = await should_suppress_due_to_online()
    text = (
        f"🔍 <b>Текущая проверка онлайна:</b>\n\n"
        f"• Результат: <b>{'⏸ Бот молчит' if suppress else '💬 Бот отвечает'}</b>\n"
        f"• Инфо: {desc}"
    )
    await call.message.answer(text, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "page_ai")
async def page_ai(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    key = "🟢 Настроен" if s["api_key_enc"] else "🔴 Не настроен"
    text = (
        "🧠 <b>ИИ И API</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Провайдер/API:\n<code>{esc(s['api_url'])}</code>\n\n"
        f"Модель: <code>{esc(s['model'])}</code>\n"
        f"API-ключ: {key}\n\n"
        "Здесь можно настроить подключение к нейросети."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Изменить API", callback_data="api")],
        [InlineKeyboardButton(text="🧠 Изменить модель", callback_data="model")],
        [InlineKeyboardButton(text="🔑 Изменить API-ключ", callback_data="key")],
        [InlineKeyboardButton(text="🔄 Проверить подключение", callback_data="check")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])
    await render(call, text, kb)


@dp.callback_query(F.data == "api")
async def change_api(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "api_url"
    await call.message.answer("🌐 <b>Отправь новый URL API</b> (например: <code>https://api.openai.com/v1/chat/completions</code> или <code>https://openrouter.ai/api/v1/chat/completions</code>):", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "model")
async def change_model(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    text = f"🧠 <b>Выбор модели ИИ</b>\n\nТекущая: <code>{esc(s['model'])}</code>\nВыбери готовую модель или отправь своё название:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="gpt-4o-mini", callback_data="set_model_gpt-4o-mini"),
            InlineKeyboardButton(text="gpt-4o", callback_data="set_model_gpt-4o"),
        ],
        [
            InlineKeyboardButton(text="deepseek-chat", callback_data="set_model_deepseek-chat"),
            InlineKeyboardButton(text="claude-3-5-sonnet", callback_data="set_model_anthropic/claude-3.5-sonnet"),
        ],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="input_model_custom")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="page_ai")],
    ])
    await render(call, text, kb)


@dp.callback_query(F.data.startswith("set_model_"))
async def set_model_preset(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    mod = call.data.replace("set_model_", "")
    set_setting("model", mod)
    await page_ai(call)


@dp.callback_query(F.data == "input_model_custom")
async def input_model_custom(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "model"
    await call.message.answer("🧠 <b>Отправь название модели ИИ</b> (например: <code>gpt-4o-mini</code>, <code>deepseek/deepseek-chat</code>):", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "key")
async def change_key(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "api_key"
    await call.message.answer("🔑 <b>Отправь новый API-ключ</b> сообщением в этот чат. После сохранения сообщение с ключом будет удалено для безопасности.", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "check")
async def check_api_ui(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    key = get_api_key()
    if not key:
        await call.message.answer("❌ API-ключ не настроен. Нажмите «Изменить API-ключ».")
        await call.answer()
        return

    await call.answer("Проверяю API...")
    ok, msg = await test_api(s["api_url"], key, s["model"], effective_prompt(s), s.get("prompt_mode") or "strong")
    res_text = f"✅ <b>API работает отлично!</b>\nОтвет: {esc(msg)}" if ok else f"❌ <b>Ошибка API:</b>\n{esc(msg)}"
    await call.message.answer(res_text, parse_mode="HTML")


@dp.callback_query(F.data == "page_reply")
async def page_reply(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    limit = "Без ограничений" if int(s["max_replies"]) <= 0 else str(s["max_replies"])
    reset_h = f"{s.get('reset_hours', 24)} ч." if s.get('reset_hours', 24) > 0 else "Отключен"
    text = (
        "💬 <b>НАСТРОЙКИ ОТВЕТОВ И ЛИМИТОВ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ Задержка перед ответом: <b>{s['delay']} сек.</b>\n"
        f"🔢 Лимит на один диалог: <b>{limit} отв.</b>\n"
        f"🔄 Автосброс лимита через: <b>{reset_h}</b>\n"
        f"⏳ Задержка после онлайна: <b>{s.get('offline_delay_min', 5)} мин.</b>\n\n"
        "<i>💡 Если бот дал максимальное число ответов (например, 4), следующий раз он ответит этому собеседнику только через указанное время (24 часа).</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Лимит ответов на диалог", callback_data="limit")],
        [InlineKeyboardButton(text="🔄 Время автосброса лимита (часы)", callback_data="input_reset_hours")],
        [InlineKeyboardButton(text="⏱ Задержка перед ответом (сек)", callback_data="delay")],
        [InlineKeyboardButton(text="✏️ Системный промпт", callback_data="prompt")],
        [InlineKeyboardButton(text="🧹 Сбросить все счётчики сейчас", callback_data="reset")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])
    await render(call, text, kb)


@dp.callback_query(F.data == "input_reset_hours")
async def input_reset_hours_ui(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "reset_hours"
    await call.message.answer(
        "🔄 <b>Через сколько часов сбрасывать лимит ответов для каждого собеседника?</b>\n\n"
        "• <code>24</code> — через 24 часа после последнего ответа бот снова сможет отвечать\n"
        "• <code>12</code> — через 12 часов\n"
        "• <code>0</code> — не сбрасывать автоматически (только вручную)\n\n"
        "Отправь число часов (например: <code>24</code>):",
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "delay")
async def change_delay(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "delay"
    await call.message.answer("⏱ <b>Введи задержку перед ответом в секундах</b> (от 0 до 30):", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "limit")
async def change_limit(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "limit"
    await call.message.answer("🔢 <b>Введи лимит ответов на один диалог</b> (число или <code>0</code> для безлимита):", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "prompt")
async def change_prompt(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    mode = s.get("prompt_mode") or "strong"
    mode_txt = PROMPT_MODES.get(mode, mode)
    text = (
        f"✏️ <b>Системный промпт:</b>\n\n<code>{esc(effective_prompt(s))}</code>\n\n"
        f"Способ передачи: <b>{mode_txt}</b>\n"
        "<i>💡 Если модель плохо следует промпту — используй усиленный режим. "
        "Бот также помнит последние сообщения диалога за сутки.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить промпт", callback_data="prompt_edit")],
        [InlineKeyboardButton(text="🔁 Сменить режим передачи", callback_data="prompt_mode_toggle")],
        [InlineKeyboardButton(text="🧹 Очистить память диалогов", callback_data="history_clear")],
        [InlineKeyboardButton(text="🔄 Сбросить на стандартный", callback_data="prompt_reset")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="page_reply")],
    ])
    await render(call, text, kb)


@dp.callback_query(F.data == "prompt_edit")
async def edit_prompt_ui(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    pending[call.from_user.id] = "prompt"
    await call.message.answer("✏️ <b>Отправь новый системный промпт:</b>", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "prompt_mode_toggle")
async def toggle_prompt_mode(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    order = list(PROMPT_MODES)
    cur = s.get("prompt_mode") or "strong"
    new_mode = order[(order.index(cur) + 1) % len(order)] if cur in order else "strong"
    set_setting("prompt_mode", new_mode)
    await call.answer("Режим промпта изменён")
    await change_prompt(call)


@dp.callback_query(F.data == "history_clear")
async def history_clear_ui(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    clear_history()
    await call.answer("Память диалогов очищена!")


@dp.callback_query(F.data == "prompt_reset")
async def reset_prompt_ui(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    set_setting("prompt", DEFAULT_PROMPT)
    await call.answer("Промпт сброшен!")
    await page_reply(call)


@dp.callback_query(F.data == "reset")
async def reset_counts_ui(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    reset_count()
    await call.answer("Счётчики диалогов сброшены!")
    await call.message.answer("🧹 <b>Все счётчики диалогов успешно сброшены.</b>", parse_mode="HTML")


@dp.callback_query(F.data == "page_status")
async def page_status(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    s = get_settings()
    auth = await is_session_active()
    key = "🟢 Есть" if s["api_key_enc"] else "🔴 Нет"
    text = (
        "📊 <b>СТАТУС</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Автоответчик: {status_text()}\n"
        f"MTProto сессия: {'🟢 Активна' if auth else '🔴 Нет'}\n"
        f"Режим ответа: {'🟢 Только офлайн' if s.get('only_offline') else '⚪ Всегда'} (+{s.get('offline_delay_min', 5)} мин)\n"
        f"Лимит ответов: <b>{s['max_replies']} (автосброс {s.get('reset_hours', 24)}ч)</b>\n"
        f"API URL: {'🟢' if s['api_url'] else '🔴'}\n"
        f"API ключ: {key}\n"
        f"Модель: <code>{esc(s['model'])}</code>\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить API", callback_data="check")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])
    await render(call, text, kb)


@dp.callback_query(F.data == "page_more")
async def page_more(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    text = (
        "⚙️ <b>ДОПОЛНИТЕЛЬНО</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔐 API-ключ хранится в зашифрованном виде (Fernet).\n"
        "📱 Статус онлайна отслеживается через официальный MTProto клиент.\n"
        "🔄 Лимиты автоматически сбрасываются по прошествии заданного интервала.\n"
        "💾 Все параметры сохраняются автоматически в SQLite."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Сменить API-ключ", callback_data="key")],
        [InlineKeyboardButton(text="👤 Статус онлайна", callback_data="page_presence")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])
    await render(call, text, kb)


# Owner message text input dispatcher
@dp.message(F.chat.type == "private")
async def handle_private_message(message: Message):
    if message.from_user.id == OWNER_ID:
        last_online_tracker[OWNER_ID] = time.time()
        action = pending.get(message.from_user.id)
        if action:
            del pending[message.from_user.id]
            val = (message.text or "").strip()

            if action == "api_key":
                set_api_key(val)
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.answer("🔐 <b>API-ключ надёжно сохранён в зашифрованном виде!</b>", parse_mode="HTML")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

            if action == "api_url":
                set_setting("api_url", val)
                await message.answer(f"🌐 <b>API URL обновлен:</b> <code>{esc(val)}</code>", parse_mode="HTML")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

            if action == "model":
                set_setting("model", val)
                await message.answer(f"🧠 <b>Модель обновлена:</b> <code>{esc(val)}</code>", parse_mode="HTML")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

            if action == "delay":
                try:
                    d = max(0.0, min(float(val), 30.0))
                    set_setting("delay", d)
                    await message.answer(f"⏱ <b>Задержка установлена:</b> {d} сек.", parse_mode="HTML")
                except ValueError:
                    await message.answer("❌ Введи корректное число от 0 до 30.")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

            if action == "offline_delay":
                try:
                    m = max(0, min(int(val), 1440))
                    set_setting("offline_delay_min", m)
                    await message.answer(f"⏱ <b>Задержка после онлайна установлена:</b> {m} мин.", parse_mode="HTML")
                except ValueError:
                    await message.answer("❌ Введи целое число минут от 0 до 120.")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

            if action == "reset_hours":
                try:
                    h = max(0, min(int(val), 720))
                    set_setting("reset_hours", h)
                    txt = f"<b>{h} ч.</b>" if h > 0 else "<b>отключен (только вручную)</b>"
                    await message.answer(f"🔄 <b>Автосброс лимита установлен на {txt}!</b>", parse_mode="HTML")
                except ValueError:
                    await message.answer("❌ Введи целое число часов (например: 24).")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

            if action == "limit":
                try:
                    l = max(0, int(val))
                    set_setting("max_replies", l)
                    lim_s = "Без ограничений" if l == 0 else str(l)
                    await message.answer(f"🔢 <b>Лимит ответов:</b> {lim_s}", parse_mode="HTML")
                except ValueError:
                    await message.answer("❌ Введи целое положительное число или 0.")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

            if action == "prompt":
                if not val:
                    await message.answer("❌ Промпт не может быть пустым. Отправь текст промпта.")
                    pending[message.from_user.id] = "prompt"
                    return
                set_setting("prompt", val)
                await message.answer("✏️ <b>Системный промпт сохранён!</b>", parse_mode="HTML")
                await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
                return

        if not message.text.startswith("/"):
            await message.answer(main_text(), parse_mode="HTML", reply_markup=keyboard_main())
        return

    # Non-owner private message
    s = get_settings()
    if not s["enabled"]:
        return
    if not message.text or not message.text.strip():
        return

    suppress, reason = await should_suppress_due_to_online()
    if suppress:
        print(f"Skipping direct reply: {reason}")
        return

    limit = int(s["max_replies"])
    if limit > 0 and get_count_and_check_reset(message.chat.id) >= limit:
        return

    try:
        answer = await ask_ai(message.text.strip(), message.chat.id)
        if not answer:
            return

        delay = max(0.0, min(float(s["delay"]), 30.0))
        if delay:
            await asyncio.sleep(delay)

        if not get_settings()["enabled"]:
            return

        await message.answer(answer)
        increment_count(message.chat.id)
    except Exception as e:
        print(f"Direct message error: {type(e).__name__}: {e}")


async def should_suppress_due_to_online() -> tuple[bool, str]:
    s = get_settings()
    if not s.get("only_offline", 1):
        return False, "Режим «Только офлайн» отключён."

    delay_min = int(s.get("offline_delay_min", 5))
    now = time.time()

    global tele_client
    if not tele_client or not tele_client.is_connected() or not (await tele_client.is_user_authorized()):
        last_active = last_online_tracker.get(OWNER_ID, 0.0)
        diff_sec = now - last_active
        if diff_sec < (delay_min * 60):
            left_min = int((delay_min * 60 - diff_sec) / 60) + 1
            return True, f"Активность владельца {int(diff_sec/60)} мин назад (осталось ждать {left_min} мин)"
        return False, "Сессия MTProto не авторизована, проверка по активности прошла."

    try:
        me = await tele_client.get_entity("me")
        status = getattr(me, "status", None)

        if isinstance(status, UserStatusOnline):
            last_online_tracker[OWNER_ID] = now
            return True, "Владелец сейчас в сети (Online)"

        if isinstance(status, UserStatusOffline):
            if hasattr(status, "was_online") and status.was_online:
                was_ts = status.was_online.replace(tzinfo=timezone.utc).timestamp()
                last_online_tracker[OWNER_ID] = max(last_online_tracker.get(OWNER_ID, 0.0), was_ts)

            diff_sec = now - last_online_tracker.get(OWNER_ID, 0.0)
            if diff_sec < (delay_min * 60):
                left_min = int((delay_min * 60 - diff_sec) / 60) + 1
                return True, f"Владелец был в сети {int(diff_sec/60)} мин назад (задержка {delay_min} мин, осталось {left_min} мин)"
            return False, f"Владелец не в сети уже {int(diff_sec/60)} мин (требовалось {delay_min} мин)"

        if isinstance(status, (UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth, type(None))):
            diff_sec = now - last_online_tracker.get(OWNER_ID, 0.0)
            if diff_sec < (delay_min * 60):
                left_min = int((delay_min * 60 - diff_sec) / 60) + 1
                return True, f"Недавний онлайн {int(diff_sec/60)} мин назад (осталось {left_min} мин)"
            return False, "Статус скрыт/недавно, прошло достаточно времени."

    except Exception as e:
        print(f"Error checking online status: {e}")

    return False, "Проверка завершена"


def chat_url(api_url: str) -> str:
    url = (api_url or "").strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def effective_prompt(s: dict) -> str:
    return (s.get("prompt") or "").strip() or DEFAULT_PROMPT


def build_messages(prompt: str, text: str, mode: str, history: Optional[list] = None):
    history = list(history or [])
    if mode == "merge":
        # Provider has no system role: instructions go into the latest user turn.
        return history + [{
            "role": "user",
            "content": (
                f"Инструкция (собеседник её не видит, строго следуй ей):\n{prompt}\n\n"
                f"Сообщение собеседника:\n{text}"
            ),
        }]
    if mode == "strong":
        # Many models forget a long system prompt; repeat it right next to the message.
        return [{"role": "system", "content": prompt}] + history + [{
            "role": "user",
            "content": (
                f"{text}\n\n"
                "---\n"
                "[Скрытая инструкция для ответа, собеседник её не видит. "
                "Ответь на сообщение выше, строго соблюдая ВСЕ правила:\n"
                f"{prompt}]"
            ),
        }]
    return [{"role": "system", "content": prompt}] + history + [{"role": "user", "content": text}]


def system_role_rejected(status: int, body: str) -> bool:
    if status not in (400, 422):
        return False
    b = body.lower()
    return any(k in b for k in ("system", "developer instruction", "role"))


THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_content(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content") or ""
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    # Reasoning models (DeepSeek R1, Qwen etc.) may return their chain of thought.
    content = THINK_RE.sub("", str(content))
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    return content.strip()


async def chat_request(api_url: str, api_key: str, model: str, prompt: str, text: str,
                       mode: str = "strong", max_tokens: Optional[int] = None, timeout_s: int = 60,
                       history: Optional[list] = None):
    """Returns (status, body, used_mode). Falls back to merge mode if the system role is rejected."""
    url = chat_url(api_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        modes = [mode] if mode == "merge" else [mode, "merge"]
        status, body = 0, ""
        for m in modes:
            payload = {"model": model, "messages": build_messages(prompt, text, m, history)}
            if max_tokens:
                payload["max_tokens"] = max_tokens
            async with session.post(url, json=payload, headers=headers) as r:
                status, body = r.status, await r.text()
            if m != "merge" and system_role_rejected(status, body):
                print(f"Provider rejected system role, retrying with prompt in user message: {body[:200]}")
                continue
            return status, body, m
        return status, body, modes[-1]


async def test_api(api_url: str, api_key: str, model: str, prompt: str = DEFAULT_PROMPT, mode: str = "strong"):
    try:
        status, body, used = await chat_request(
            api_url, api_key, model, prompt, "Привет! Ты кто?", mode, max_tokens=200, timeout_s=30
        )
        if status >= 400:
            return False, f"HTTP {status}: {body[:300]}"
        data = json.loads(body)
        if not data.get("choices"):
            return False, "API ответил без choices."
        answer = extract_content(data) or "(пустой ответ)"
        if used != mode:
            answer += "\n\n⚠️ Провайдер не принимает system-роль — промпт передаётся внутри сообщения."
        return True, answer
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def ask_ai(text: str, chat_id: Optional[int] = None):
    s = get_settings()
    key = get_api_key()
    if not key:
        return "Здравствуйте! Владелец сейчас не в сети и ответит вам позже."

    mode = s.get("prompt_mode") or "strong"
    history = get_history(chat_id) if chat_id is not None else []
    if history and history[-1]["role"] == "user":
        text = history.pop()["content"] + "\n" + text
    status, body, used = await chat_request(
        s["api_url"], key, s["model"], effective_prompt(s), text, mode, history=history
    )
    if status >= 400:
        raise RuntimeError(f"HTTP {status}: {body[:300]}")
    if used != mode:
        # Remember that this provider needs the prompt inside the user message.
        set_setting("prompt_mode", used)
    answer = extract_content(json.loads(body))
    if answer and chat_id is not None:
        add_history(chat_id, "user", text)
        add_history(chat_id, "assistant", answer)
    return answer


# Telegram Business / Chat Automation
@dp.business_message()
async def business_message(message: Message):
    if not message.from_user:
        return

    if message.from_user.id == OWNER_ID:
        last_online_tracker[OWNER_ID] = time.time()
        if message.text:
            add_history(message.chat.id, "assistant", message.text)
        return

    s = get_settings()
    if not s["enabled"]:
        return
    if not message.business_connection_id:
        return
    if not message.text or not message.text.strip():
        return

    suppress, reason = await should_suppress_due_to_online()
    if suppress:
        print(f"Skipping business reply: {reason}")
        return

    limit = int(s["max_replies"])
    if limit > 0 and get_count_and_check_reset(message.chat.id) >= limit:
        return

    try:
        answer = await ask_ai(message.text.strip(), message.chat.id)
        if not answer:
            return

        delay = max(0.0, min(float(s["delay"]), 30.0))
        if delay:
            await asyncio.sleep(delay)

        if not get_settings()["enabled"]:
            return

        await message.bot.send_message(
            chat_id=message.chat.id,
            text=answer,
            business_connection_id=message.business_connection_id,
            reply_parameters=None,
        )
        increment_count(message.chat.id)
    except Exception as e:
        print(f"AI Business processing error: {type(e).__name__}: {e}")


async def main():
    global tele_client
    init_db()

    tele_client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    try:
        await tele_client.connect()
        if await tele_client.is_user_authorized():
            print("Telethon MTProto session connected and authorized!")
        else:
            print("Telethon MTProto session connected (awaiting auth).")
    except Exception as e:
        print(f"Telethon init error: {e}")

    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Telegram Business AI Auto Reply bot started successfully!")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "business_message", "business_connection"])


if __name__ == "__main__":
    asyncio.run(main())
