import os
import logging
import asyncio
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# =========================
# Config
# =========================
API_TOKEN = os.getenv("API_TOKEN")  # ОБЯЗАТЕЛЬНО задать в Render -> Environment
DB_PATH = "football_bot.db"

# Базовые настройки
DEFAULT_PLACE = "Chikovani St."
TIMEZONE_SHIFT = 4  # GMT+4 (Тбилиси)
# Авто-игры: только ПОНЕДЕЛЬНИК (0) и ПЯТНИЦА (4) в 21:00, создаются за 48 часов:
#   В среду 21:00 => создаём игру на Пятницу 21:00
#   В субботу 21:00 => создаём игру на Понедельник 21:00

# Чаты
MAIN_CHAT_ID = -1001234567890   # ПОМЕНЯЙ на реальный id основного чата
# TEN_LARI_CHAT_ID сейчас не используем

# Админы
ADMIN_IDS = [1969502668, 192472924]  # ты и ещё админ

# =========================
# Aiogram / Scheduler
# =========================
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
bot = Bot(token=API_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler()

# =========================
# DB init
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # События
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                time DATETIME,
                place TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        # Игроки/голосования
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                extra_count INTEGER DEFAULT 0,
                going BOOLEAN,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events(id)
            )
        """)
        await db.commit()

# =========================
# Utilities (DB helpers)
# =========================
async def create_event(event_dt: datetime, place: str) -> str:
    event_id = str(datetime.now().timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO events (id, time, place, is_active) VALUES (?,?,?,1)",
            (event_id, event_dt.isoformat(), place)
        )
        await db.commit()
    return event_id

async def delete_event(event_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM players WHERE event_id=?", (event_id,))
        await db.execute("DELETE FROM events WHERE id=?", (event_id,))
        await db.commit()

async def get_event(event_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, time, place, is_active FROM events WHERE id=?", (event_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "time": datetime.fromisoformat(row[1]),
                "place": row[2],
                "is_active": bool(row[3]),
            }

async def get_upcoming_events(limit: int = 10):
    now = datetime.utcnow() + timedelta(hours=TIMEZONE_SHIFT)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, time, place FROM events "
            "WHERE is_active=1 AND time >= ? "
            "ORDER BY time ASC LIMIT ?",
            (now.isoformat(), limit),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {"id": r[0], "time": datetime.fromisoformat(r[1]), "place": r[2]}
                for r in rows
            ]

async def get_nearest_event():
    events = await get_upcoming_events(limit=1)
    return events[0] if events else None

async def upsert_participation(event_id: str, user_id: int, username: str | None,
                               full_name: str, going: bool, extra_count: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        # Старая запись удаляем
        await db.execute(
            "DELETE FROM players WHERE event_id=? AND user_id=?",
            (event_id, user_id)
        )
        # Добавляем
        await db.execute(
            "INSERT INTO players (event_id, user_id, username, full_name, extra_count, going) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, user_id, username, full_name, extra_count, 1 if going else 0)
        )
        await db.commit()

async def list_players(event_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, full_name, extra_count, going FROM players "
            "WHERE event_id=? ORDER BY joined_at",
            (event_id,)
        ) as cur:
            return await cur.fetchall()

# =========================
# Keyboards
# =========================
def join_keyboard(event_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Going", callback_data=f"join_{event_id}_yes")],
        [InlineKeyboardButton(text="❌ Not going", callback_data=f"join_{event_id}_no")],
        [
            InlineKeyboardButton(text="➕1", callback_data=f"extra_{event_id}_1"),
            InlineKeyboardButton(text="➕2", callback_data=f"extra_{event_id}_2"),
            InlineKeyboardButton(text="➕3", callback_data=f"extra_{event_id}_3"),
        ],
    ])

# =========================
# Render helpers
# =========================
def fmt_dt(dt: datetime) -> str:
    # Человеческий формат, без угловых скобок (чтобы не ломать HTML)
    # Пример: Fri, 27 Sep 21:00
    return dt.strftime("%a, %d %b %H:%M")

async def render_event(event_id: str) -> str:
    ev = await get_event(event_id)
    if not ev:
        return "Event not found."

    players = await list_players(event_id)
    going = [(u, f, x) for (u, f, x, g) in players if g == 1]
    not_going = [(u, f) for (u, f, x, g) in players if g == 0]

    lines = []
    lines.append(f"⚽ <b>Game</b>")
    lines.append(f"🕒 {fmt_dt(ev['time'])}")
    lines.append(f"📍 {ev['place']}")
    lines.append("")
    lines.append(f"<b>Going ({len(going)}/20)</b>:")

    if going:
        for username, full_name, extra_count in going:
            name = f"@{username}" if username else (full_name or "No name")
            extra = f" +{extra_count}" if extra_count else ""
            lines.append(f"✅ {name}{extra}")
    else:
        lines.append("Nobody yet 👀")

    lines.append("")
    lines.append(f"<b>Not going ({len(not_going)})</b>:")

    if not_going:
        for username, full_name in not_going:
            name = f"@{username}" if username else (full_name or "No name")
            lines.append(f"❌ {name}")
    else:
        lines.append("Nobody declined.")

    return "\n".join(lines)

async def render_events_list(limit: int = 10) -> str:
    events = await get_upcoming_events(limit=limit)
    if not events:
        return "No active games."

    lines = ["<b>Active games:</b>", ""]
    for e in events:
        lines.append(f"• {fmt_dt(e['time'])} — {e['place']}")
    return "\n".join(lines)

# =========================
# Commands
# =========================
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "⚽ <b>Hello! I'm your football bot.</b>\n\n"
        "<b>Commands:</b>\n"
        "/events — list active games\n"
        "/set_place PLACE — change place for the nearest game (admin)\n"
        "/set_time YYYY-MM-DD HH:MM — change date & time for the nearest game (admin)\n"
        "/addevent YYYY-MM-DD HH:MM — add custom event (admin)\n"
        "/delevent EVENT_ID — delete an event by ID (admin)\n"
        "/myid — show your Telegram ID\n"
        "/chatid — show this chat ID"
    )

@router.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"Your Telegram ID is: <code>{message.from_user.id}</code>")

@router.message(Command("chatid"))
async def cmd_chatid(message: types.Message):
    await message.answer(f"Chat ID is: <code>{message.chat.id}</code>")

@router.message(Command("events"))
async def cmd_events(message: types.Message):
    text = await render_events_list(limit=20)
    await message.answer(text)

@router.message(Command("addevent"))
async def cmd_addevent(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /addevent YYYY-MM-DD HH:MM")
        return
    dt_str = f"{parts[1]} {parts[2]}"
    try:
        # время считаем как локальное GMT+4
        local_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        # Сохраняем как локальное (в БД хранится ISO, мы всегда добавляем TZ вручную при расчётах)
        event_id = await create_event(local_dt, DEFAULT_PLACE)
        text = await render_event(event_id)
        await bot.send_message(MAIN_CHAT_ID, f"⚽ <b>New game created!</b>\n\n{text}",
                               reply_markup=join_keyboard(event_id))
    except ValueError:
        await message.answer("Invalid format. Use: /addevent YYYY-MM-DD HH:MM")

@router.message(Command("delevent")))
async def cmd_delevent(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Usage: /delevent EVENT_ID")
        return
    await delete_event(parts[1])
    await message.answer("Event deleted.")

@router.message(Command("set_place")))
async def cmd_set_place(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /set_place PLACE")
        return
    new_place = parts[1].strip()
    nearest = await get_nearest_event()
    if not nearest:
        await message.answer("No upcoming games to update.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET place=? WHERE id=?", (new_place, nearest["id"]))
        await db.commit()
    await message.answer(f"Place updated: <b>{new_place}</b>\nFor game: {fmt_dt(nearest['time'])}")

@router.message(Command("set_time")))
async def cmd_set_time(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /set_time YYYY-MM-DD HH:MM")
        return
    dt_str = f"{parts[1]} {parts[2]}"
    try:
        new_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    except ValueError:
        await message.answer("Invalid format. Use: /set_time YYYY-MM-DD HH:MM")
        return
    nearest = await get_nearest_event()
    if not nearest:
        await message.answer("No upcoming games to update.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET time=? WHERE id=?", (new_dt.isoformat(), nearest["id"]))
        await db.commit()
    await message.answer(f"Time updated to: <b>{fmt_dt(new_dt)}</b>")

# =========================
# Callbacks
# =========================
@router.callback_query()
async def callbacks(callback: CallbackQuery):
    try:
        data = callback.data.split("_")
        action = data[0]
        event_id = data[1]
    except Exception:
        await callback.answer()
        return

    full_name = f"{callback.from_user.first_name or ''} {callback.from_user.last_name or ''}".strip()
    username = callback.from_user.username

    if action == "join":
        going = (data[2] == "yes")
        await upsert_participation(event_id, callback.from_user.id, username, full_name, going, extra_count=0)
        text = await render_event(event_id)
        await callback.message.edit_text(text, reply_markup=join_keyboard(event_id))
        await callback.answer("Updated!")
    elif action == "extra":
        extra = int(data[2])
        await upsert_participation(event_id, callback.from_user.id, username, full_name, True, extra_count=extra)
        text = await render_event(event_id)
        await callback.message.edit_text(text, reply_markup=join_keyboard(event_id))
        await callback.answer(f"Added +{extra}")
    else:
        await callback.answer()

# =========================
# Scheduler tasks
# =========================
async def scheduled_create_48h():
    """
    Запускается по крону:
      - Среда 21:00 (Asia/Tbilisi) -> создаём Пятница 21:00
      - Суббота 21:00 (Asia/Tbilisi) -> создаём Понедельник 21:00
    """
    now_local = datetime.utcnow() + timedelta(hours=TIMEZONE_SHIFT)
    weekday = now_local.weekday()  # 0=Mon ... 6=Sun

    if weekday == 2:  # Wednesday
        # Найти ближайшую пятницу этой недели
        delta_days = (4 - weekday)  # 2 дня
        target = now_local + timedelta(days=delta_days)
    elif weekday == 5:  # Saturday
        # Следующий понедельник
        delta_days = (0 - weekday) % 7  # 2 дня до понедельника
        target = now_local + timedelta(days=delta_days)
    else:
        return  # Не наша точка запуска

    event_dt = target.replace(hour=21, minute=0, second=0, microsecond=0)
    event_id = await create_event(event_dt, DEFAULT_PLACE)
    text = await render_event(event_id)
    await bot.send_message(
        MAIN_CHAT_ID,
        "⚽ <b>New game created!</b>\n\n" + text,
        reply_markup=join_keyboard(event_id)
    )

    # Напоминание за 3 часа до игры
    reminder_time = event_dt - timedelta(hours=3)
    scheduler.add_job(send_reminder, "date", run_date=reminder_time, args=[event_id])

async def send_reminder(event_id: str):
    text = await render_event(event_id)
    await bot.send_message(MAIN_CHAT_ID, "⏰ Reminder: Game soon!\n\n" + text)

# =========================
# HTTP keep-alive (Render web service)
# =========================
async def http_handle(_request):
    return web.Response(text="Bot is running")

async def start_http_server():
    app = web.Application()
    app.router.add_get("/", http_handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()

# =========================
# Main
# =========================
async def main():
    await init_db()

    # План: два cron-триггера — среда и суббота 21:00 Asia/Tbilisi
    scheduler.add_job(
        scheduled_create_48h,
        "cron",
        day_of_week="wed,sat",
        hour=21,
        minute=0,
        timezone="Asia/Tbilisi",
    )
    scheduler.start()

    # поднимем http-сервер, чтобы Render не спал
    asyncio.create_task(start_http_server())

    logging.info("Bot polling started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
