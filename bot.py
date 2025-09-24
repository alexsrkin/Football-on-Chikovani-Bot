import os
import logging
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# ====== Config ======
API_TOKEN = os.getenv("API_TOKEN")  # токен берем из Render Environment
DB_PATH = "football_bot.db"

DEFAULT_GAME_DAYS = [0, 4]  # Monday=0, Friday=4
DEFAULT_GAME_TIME = "21:00"
DEFAULT_PLACE = "Chikovani St."
TIMEZONE_SHIFT = 4  # GMT+4

MAIN_CHAT_ID = -1001234567890   # заменить на реальный id чата
TEN_LARI_CHAT_ID = -1001234567891  # заменить на реальный id чата
ADMIN_IDS = [1969502668, 192472924]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()

# ====== DB ======
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                time DATETIME,
                place TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                ten_lari_chat_opened BOOLEAN DEFAULT 0
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                position TEXT,
                extra_count INTEGER DEFAULT 0,
                going BOOLEAN,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events (id)
            )
        ''')
        await db.commit()

# ====== Utilities ======
async def create_event(event_time, place):
    event_id = str(datetime.now().timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO events (id,time,place) VALUES (?,?,?)',
            (event_id, event_time.isoformat(), place)
        )
        await db.commit()
    return event_id

async def delete_event(event_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM events WHERE id=?', (event_id,))
        await db.execute('DELETE FROM players WHERE event_id=?', (event_id,))
        await db.commit()

async def get_event_info(event_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT time, place, ten_lari_chat_opened FROM events WHERE id=?',
            (event_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    'time': datetime.fromisoformat(row[0]),
                    'place': row[1],
                    'ten_lari_chat_opened': bool(row[2])
                }
            return None

async def get_event_players(event_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT username, full_name, position, extra_count, going '
            'FROM players WHERE event_id=? ORDER BY joined_at',
            (event_id,)
        ) as cursor:
            return await cursor.fetchall()

async def join_event(event_id, user_id, username, full_name, position, extra_count, going):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM players WHERE event_id=? AND user_id=?', (event_id, user_id))
        await db.execute(
            'INSERT INTO players (event_id,user_id,username,full_name,position,extra_count,going) '
            'VALUES (?,?,?,?,?,?,?)',
            (event_id, user_id, username, full_name, position, extra_count, going)
        )
        await db.commit()

# ====== Keyboards ======
def create_join_keyboard(event_id):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Going", callback_data=f"join_{event_id}_yes")],
        [InlineKeyboardButton("❌ Not going", callback_data=f"join_{event_id}_no")],
        [InlineKeyboardButton("➕1", callback_data=f"extra_{event_id}_1"),
         InlineKeyboardButton("➕2", callback_data=f"extra_{event_id}_2"),
         InlineKeyboardButton("➕3", callback_data=f"extra_{event_id}_3")]
    ])
    return kb

# ====== Render Event ======
async def render_event(event_id):
    event = await get_event_info(event_id)
    if not event:
        return "Event not found"
    players = await get_event_players(event_id)

    going = [p for p in players if p[4]]
    not_going = [p for p in players if p[4] == 0]

    text = f"⚽ <b>Game:</b> {event['time'].strftime('%d.%m %H:%M')}\n📍 <b>Place:</b> {event['place']}\n\n"
    text += f"<b>Going ({len(going)}/20):</b>\n"
    if going:
        for username, full_name, position, extra_count, going_flag in going:
            name = f"@{username}" if username else full_name
            extra_text = f" +{extra_count}" if extra_count else ""
            text += f"✅ {name}{extra_text}\n"
    else:
        text += "Nobody yet 👀\n"

    text += f"\n<b>Not going ({len(not_going)}):</b>\n"
    if not_going:
        for username, full_name, _, _, _ in not_going:
            name = f"@{username}" if username else full_name
            text += f"❌ {name}\n"
    else:
        text += "Nobody declined.\n"

    return text

# ====== Commands ======
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "⚽ <b>Hello! I'm your football bot.</b>\n\n"
        "<b>Commands:</b>\n"
        "/create - create a game manually\n"
        "/events - list active games\n"
        "/addevent - add custom event (admin)\n"
        "/delevent - delete an event by ID (admin)\n"
        "/set_place - change game place (admin)\n"
        "/set_time - change game time (admin)\n"
        "/open_10lari - open registration in 10 Lari chat (admin)\n"
        "/myid - show your Telegram ID\n"
        "/chatid - show this chat ID"
    )

@router.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"Your Telegram ID is: {message.from_user.id}")

@router.message(Command("chatid"))
async def cmd_chatid(message: types.Message):
    await message.answer(f"Chat ID is: {message.chat.id}")

@router.message(Command("addevent"))
async def cmd_addevent(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Usage: /addevent YYYY-MM-DD HH:MM")
        return
    dt_str = f"{parts[1]} {parts[2]}"
    try:
        event_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        event_id = await create_event(event_time, DEFAULT_PLACE)
        text = await render_event(event_id)
        await bot.send_message(MAIN_CHAT_ID, text, reply_markup=create_join_keyboard(event_id))
    except ValueError:
        await message.answer("Invalid format. Use YYYY-MM-DD HH:MM")

@router.message(Command("delevent"))
async def cmd_delevent(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /delevent EVENT_ID")
        return
    await delete_event(parts[1])
    await message.answer("Event deleted.")

# ====== Callbacks ======
@router.callback_query()
async def callback_handler(callback: CallbackQuery):
    data = callback.data.split('_')
    action, event_id = data[0], data[1]

    full_name = f"{callback.from_user.first_name or ''} {callback.from_user.last_name or ''}".strip()

    if action == "join":
        going = data[2] == "yes"
        await join_event(event_id, callback.from_user.id, callback.from_user.username, full_name, None, 0, going)
        text = await render_event(event_id)
        await callback.message.edit_text(text, reply_markup=create_join_keyboard(event_id))
        await callback.answer("Updated!")
    elif action == "extra":
        extra_count = int(data[2])
        await join_event(event_id, callback.from_user.id, callback.from_user.username, full_name, None, extra_count, True)
        text = await render_event(event_id)
        await callback.message.edit_text(text, reply_markup=create_join_keyboard(event_id))
        await callback.answer(f"Added +{extra_count}")

# ====== Scheduler ======
async def scheduled_create_48h():
    now = datetime.utcnow() + timedelta(hours=TIMEZONE_SHIFT)
    weekday = now.weekday()
    if weekday == 2:  # Wednesday
        target = now + timedelta(days=(4 - weekday))  # Friday
    elif weekday == 5:  # Saturday
        target = now + timedelta(days=(0 - weekday) % 7)  # Monday
    else:
        return

    event_datetime = target.replace(hour=21, minute=0, second=0, microsecond=0)
    event_id = await create_event(event_datetime, DEFAULT_PLACE)
    text = await render_event(event_id)
    await bot.send_message(MAIN_CHAT_ID, f"⚽ <b>New game created!</b>\n\n{text}",
                           reply_markup=create_join_keyboard(event_id))

    # reminder за 3 часа
    reminder_time = event_datetime - timedelta(hours=3)
    scheduler.add_job(send_reminder, "date", run_date=reminder_time, args=[event_id])

async def send_reminder(event_id):
    text = await render_event(event_id)
    await bot.send_message(MAIN_CHAT_ID, f"⏰ Reminder: Game soon!\n\n{text}")

# ====== Main ======
async def main():
    await init_db()
    scheduler.add_job(
        scheduled_create_48h,
        "cron",
        day_of_week="wed,sat",
        hour=21,
        minute=0,
        timezone="Asia/Tbilisi"
    )
    scheduler.start()
    logging.info("Bot polling started")
    await dp.start_polling(bot)

# ====== Web Server for Render ======
async def handle(request):
    return web.Response(text="Bot is running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main())          # запускаем бота
    loop.create_task(start_web_server())  # запускаем веб-сервер
    loop.run_forever()
