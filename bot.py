import os
import logging
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== Configuration (env-first) ======
API_TOKEN = os.environ["API_TOKEN"]  # обязательно укажи в Render
DB_PATH = "football_bot.db"

DEFAULT_GAME_DAYS = [0, 4]  # Monday=0, Friday=4
DEFAULT_GAME_TIME = "21:00"
DEFAULT_PLACE = "Chikovani St."
TIMEZONE_SHIFT = 4  # GMT+4
PLAYER_LIMIT = 20   # max players

MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "-1001234567890"))
TEN_LARI_CHAT_ID = int(os.getenv("TEN_LARI_CHAT_ID", "-1001234567891"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "1969502668").split(",")]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()


# ====== Database initialization ======
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                time DATETIME,
                place TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                status TEXT,  -- "yes" / "no"
                extra_count INTEGER DEFAULT 0,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events (id)
            )
        """)
        await db.commit()


# ====== Helpers ======
async def create_event(event_time, place):
    event_id = str(datetime.now().timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO events (id,time,place) VALUES (?,?,?)",
            (event_id, event_time.isoformat(), place),
        )
        await db.commit()
    return event_id


async def get_active_events():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, time, place FROM events WHERE is_active=1 ORDER BY time"
        ) as cursor:
            return await cursor.fetchall()


async def get_event_players(event_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, full_name, status, extra_count "
            "FROM players WHERE event_id=? ORDER BY joined_at",
            (event_id,),
        ) as cursor:
            return await cursor.fetchall()


async def set_player(event_id, user_id, username, full_name, status, extra_count=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM players WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        )
        await db.execute(
            "INSERT INTO players (event_id,user_id,username,full_name,status,extra_count) "
            "VALUES (?,?,?,?,?,?)",
            (event_id, user_id, username, full_name, status, extra_count),
        )
        await db.commit()


async def remove_player(event_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM players WHERE event_id=? AND user_id=?", (event_id, user_id))
        await db.commit()


async def get_event_info(event_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT time, place FROM events WHERE id=?", (event_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"time": datetime.fromisoformat(row[0]), "place": row[1]}
    return None


# ====== Keyboards ======
def create_join_keyboard(event_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton("✅ Going", callback_data=f"join_{event_id}_yes_0")],
            [InlineKeyboardButton("❌ Not going", callback_data=f"join_{event_id}_no_0")],
            [InlineKeyboardButton("+1", callback_data=f"extra_{event_id}_1"),
             InlineKeyboardButton("+2", callback_data=f"extra_{event_id}_2")],
        ]
    )


def create_extra_keyboard(event_id, extra_count):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton("Confirm + going", callback_data=f"join_{event_id}_yes_{extra_count}")],
            [InlineKeyboardButton("Back", callback_data=f"back_{event_id}")],
        ]
    )


# ====== Render ======
async def render_event(event_id):
    info = await get_event_info(event_id)
    if not info:
        return "Event not found"

    players = await get_event_players(event_id)
    going = [p for p in players if p[3] == "yes"]
    notgoing = [p for p in players if p[3] == "no"]

    text = (
        f"⚽ <b>Game:</b> {info['time'].strftime('%d.%m %H:%M')}\n"
        f"📍 <b>Place:</b> {info['place']}\n\n"
        f"✅ Going: {len(going)} / {PLAYER_LIMIT}\n"
        f"❌ Not going: {len(notgoing)}\n\n"
    )

    if going:
        text += "<b>Players:</b>\n"
        for _, username, full_name, _, extra in going:
            name = f"@{username}" if username else full_name
            extra_txt = f" +{extra}" if extra > 0 else ""
            text += f"✔️ {name}{extra_txt}\n"

    return text


# ====== Commands ======
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "⚽ <b>Hello! I'm your football bot.</b>\n\n"
        "<b>Commands:</b>\n"
        "/create - create a game manually (admin)\n"
        "/events - list active games\n"
        "/addevent - add custom event (admin)\n"
        "/delevent - delete an event by ID (admin)\n"
        "/myid - show your Telegram ID\n"
        "/chatid - show this chat ID"
    )


@router.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"Your Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("chatid"))
async def cmd_chatid(message: types.Message):
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(Command("events"))
async def cmd_events(message: types.Message):
    events = await get_active_events()
    if not events:
        await message.answer("No active events")
        return
    for eid, _, _ in events:
        txt = await render_event(eid)
        await message.answer(txt, reply_markup=create_join_keyboard(eid))


# Admin-only
@router.message(Command("addevent"))
async def cmd_addevent(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /addevent YYYY-MM-DD HH:MM Place")
        return
    dt_str, time_str, place = parts[1], parts[2].split()[0], " ".join(parts[2].split()[1:])
    dt = datetime.fromisoformat(f"{dt_str} {time_str}")
    eid = await create_event(dt, place or DEFAULT_PLACE)
    txt = await render_event(eid)
    await bot.send_message(MAIN_CHAT_ID, f"New custom game created!\n\n{txt}", reply_markup=create_join_keyboard(eid))


@router.message(Command("delevent"))
async def cmd_delevent(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Usage: /delevent EVENT_ID")
        return
    eid = parts[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET is_active=0 WHERE id=?", (eid,))
        await db.commit()
    await message.answer(f"Event {eid} deleted")


# ====== Callbacks ======
@router.callback_query()
async def callback_handler(callback: CallbackQuery):
    data = callback.data.split("_")
    action = data[0]
    eid = data[1]

    if action == "join":
        status, extra = data[2], int(data[3])
        full_name = f"{callback.from_user.first_name or ''} {callback.from_user.last_name or ''}".strip()
        await set_player(eid, callback.from_user.id, callback.from_user.username, full_name, status, extra)
        txt = await render_event(eid)
        await callback.message.edit_text(txt, reply_markup=create_join_keyboard(eid))
        await callback.answer("Updated!")

    elif action == "extra":
        extra = int(data[2])
        await callback.message.edit_reply_markup(reply_markup=create_extra_keyboard(eid, extra))
        await callback.answer()

    elif action == "back":
        await callback.message.edit_reply_markup(reply_markup=create_join_keyboard(eid))
        await callback.answer()


# ====== Scheduler ======
async def scheduled_create_48h():
    now = datetime.utcnow() + timedelta(hours=TIMEZONE_SHIFT)
    weekday = now.weekday()

    if weekday == 2:  # Wed → create Friday
        target = now + timedelta(days=(4 - weekday))
    elif weekday == 5:  # Sat → create Monday
        target = now + timedelta(days=(7 - weekday))
    else:
        return

    target = target.replace(hour=21, minute=0, second=0, microsecond=0)
    eid = await create_event(target, DEFAULT_PLACE)
    txt = await render_event(eid)
    await bot.send_message(MAIN_CHAT_ID, f"⚽ New game!\n\n{txt}", reply_markup=create_join_keyboard(eid))


# ====== Main ======
async def main():
    await init_db()
    scheduler.add_job(scheduled_create_48h, "cron", day_of_week="wed,sat", hour=21, timezone="Asia/Tbilisi")
    scheduler.start()
    logging.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())