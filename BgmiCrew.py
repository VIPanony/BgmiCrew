#!/usr/bin/env python3
# bgmi_tournament_final.py
# Cleaned + hardened one-file BGMI tournament bot (Pyrogram 2.x compatible)

import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
from functools import partial

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import motor.motor_asyncio
from bson import ObjectId

# ------------------ CONFIG (edit only these) ------------------
BOT_TOKEN = "8274531701:AAF4mIvbc36WX-V6NYuJsGljphMbWtbaHJM"
MONGO_URI = "mongodb+srv://adsrunnerpro:adsrunnerpro@cluster0.2zzs40v.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
ADMIN_ID = 7707903995
API_ID = 24585198
API_HASH = "199233760e0e538ba91613e478ef9cf0"
DB_NAME = "bgmi_tourn_db"
TIMEZONE = "Asia/Kolkata"
SESSION_NAME = "bgmi_tourn_bot"  # change if collisions with old user-session
# ----------------------------------------------------------------------------------

# Basic validation
if not BOT_TOKEN or not MONGO_URI or not ADMIN_ID:
    print("BOT_TOKEN, MONGO_URI or ADMIN_ID not set. Edit the top of this file and add credentials.")
    raise SystemExit(1)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("bgmi_final")

# Pyrogram client (bot)
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# MongoDB (motor async)
mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
tournaments_col = db["tournaments"]
registrations_col = db["registrations"]
access_col = db["access_tokens"]

# Scheduler (do NOT start at import time)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# ------------------ Helpers ------------------

def clickable_name(user):
    name = (user.first_name or "Player")
    if getattr(user, "last_name", None):
        name += f" {user.last_name}"
    return f"[{name}](tg://user?id={user.id})"

def start_keyboard(tourn_id: Optional[str] = None):
    kb = []
    if tourn_id:
        kb.append([InlineKeyboardButton("View Tournament", callback_data=f"view_{tourn_id}")])
    kb.append([InlineKeyboardButton("Join Tournament", callback_data="join_tourn")])
    kb.append([InlineKeyboardButton("My Registrations", callback_data="my_regs"),
               InlineKeyboardButton("Help", callback_data="help_menu")])
    return InlineKeyboardMarkup(kb)

HELP_TEXT = (
    "*BGMI Crew — Tournament Bot Help*\n\n"
    "Players:\n- Use /start to open the main menu.\n- Click 'Join Tournament' and follow DM instructions.\n\n"
    "Admin commands:\n- /create_tournament\n- /list_tournaments\n- /list_players <tourn_id>\n- /setroom <tourn_id> <room_id> <pass> <HH:MM>\n- /close_registration <tourn_id>\n- /announce_winner <tourn_id> <winner_ign>\n- /tokens\n- /access <username_or_id> <hours>\n"
)

HELP_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("Contact Admin", url=f"tg://user?id={ADMIN_ID}"),
     InlineKeyboardButton("Back", callback_data="back_to_start")]
])

creating_states: dict = {}

# Helper to schedule coroutine safely from APScheduler (which expects a callable)
def schedule_coroutine(run_date: datetime, coro_func: Callable[..., Any], *args, **kwargs):
    """
    Schedule an asyncio coroutine to be created at `run_date`.
    Use like: schedule_coroutine(ann_time, send_room_details, tourn_id)
    """
    def _job():
        try:
            asyncio.get_event_loop().create_task(coro_func(*args, **kwargs))
        except Exception as e:
            logger.exception("Failed to create task for scheduled job: %s", e)
    scheduler.add_job(_job, 'date', run_date=run_date)

# ------------------ Handlers ------------------

@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    user = message.from_user
    try:
        upcoming = await tournaments_col.find_one({"status": "open"}, sort=[("start_at", 1)])
    except Exception as e:
        logger.exception("DB read error in /start: %s", e)
        upcoming = None

    welcome = (
        f"Hello {clickable_name(user)}!\n\n"
        "Welcome to BGMI Crew Tournament Hub.\n"
        "Use the buttons below to join the active tournament or view your registrations."
    )
    kb = start_keyboard(tourn_id=str(upcoming["_id"])) if upcoming else start_keyboard()
    await message.reply_text(welcome, reply_markup=kb, disable_web_page_preview=True, parse_mode="markdown")

@app.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message):
    await message.reply_text(HELP_TEXT, reply_markup=HELP_KB, parse_mode="markdown")

@app.on_callback_query(filters.regex("^help_menu$"))
async def cb_help_menu(client: Client, callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_text(HELP_TEXT, reply_markup=HELP_KB, parse_mode="markdown")
    except Exception:
        await callback.message.reply_text(HELP_TEXT, reply_markup=HELP_KB, parse_mode="markdown")

@app.on_callback_query(filters.regex("^back_to_start$"))
async def cb_back_to_start(client: Client, callback: CallbackQuery):
    await callback.answer()
    upcoming = await tournaments_col.find_one({"status": "open"}, sort=[("start_at", 1)])
    kb = start_keyboard(tourn_id=str(upcoming["_id"])) if upcoming else start_keyboard()
    text = "Main menu — use /start to open full interface."
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.reply_text(text, reply_markup=kb)

# Admin creation flows
@app.on_message(filters.command("create_tournament") & filters.user(ADMIN_ID))
async def cmd_create_tourn(client: Client, message: Message):
    chat_id = message.chat.id
    creating_states[chat_id] = {"step": "name"}
    await message.reply_text("Tournament creation started. Please send the tournament NAME (example: 'Arena Evening').")

@app.on_message(filters.private & filters.user(ADMIN_ID) & filters.text)
async def admin_private_flow(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in creating_states:
        return
    state = creating_states[chat_id]
    text = message.text.strip()
    if state["step"] == "name":
        state["name"] = text
        state["step"] = "format"
        await message.reply_text("Send FORMAT (1v1 / 2v2 / 4v4).")
        return
    if state["step"] == "format":
        state["format"] = text
        state["step"] = "start"
        await message.reply_text("Send START DATETIME in format YYYY-MM-DD HH:MM (server timezone).")
        return
    if state["step"] == "start":
        try:
            start_at = datetime.strptime(text, "%Y-%m-%d %H:%M")
            state["start_at"] = start_at
            state["step"] = "slots"
            await message.reply_text("Send MAX SLOTS (integer).")
        except Exception:
            await message.reply_text("Invalid datetime. Use YYYY-MM-DD HH:MM.")
        return
    if state["step"] == "slots":
        try:
            slots = int(text)
            doc = {
                "name": state["name"],
                "format": state["format"],
                "start_at": state["start_at"],
                "max_slots": slots,
                "status": "open",
                "created_by": chat_id,
                "created_at": datetime.utcnow()
            }
            res = await tournaments_col.insert_one(doc)
            await message.reply_text(f"Tournament created with id: {res.inserted_id}\nUse /list_tournaments to view.")
            creating_states.pop(chat_id, None)
        except Exception:
            await message.reply_text("Slots must be an integer. Try again.")
        return

@app.on_message(filters.command("list_tournaments") & filters.user(ADMIN_ID))
async def cmd_list_tournaments(client: Client, message: Message):
    cursor = tournaments_col.find({})
    out = "*Tournaments:*\n"
    async for t in cursor:
        tid = str(t["_id"])
        start = t.get("start_at")
        start_str = start.strftime("%Y-%m-%d %H:%M") if isinstance(start, datetime) else str(start)
        out += f"\nID: {tid}\nName: {t.get('name')}\nFormat: {t.get('format')}\nStart: {start_str}\nSlots: {t.get('max_slots')}\nStatus: {t.get('status')}\n"
    await message.reply_text(out, parse_mode="markdown")

@app.on_callback_query(filters.regex("^join_tourn$"))
async def cb_join_tourn(client: Client, callback: CallbackQuery):
    await callback.answer("Follow DM instructions to complete registration.")
    user = callback.from_user
    upcoming = await tournaments_col.find_one({"status": "open"}, sort=[("start_at", 1)])
    if not upcoming:
        try:
            await callback.message.reply_text("No open tournament right now.")
        except:
            pass
        return
    try:
        await client.send_message(user.id, "Send your IGN and BGMI numeric ID in one message like:\n`IGN123 1234567890`", parse_mode="markdown")
    except Exception:
        await callback.message.reply_text("I couldn't DM you. Start the bot in private chat and try again.")

@app.on_message(filters.private & filters.text)
async def handle_private_registration(client: Client, message: Message):
    # Expect two parts: IGN BGMI_ID
    parts = message.text.strip().split()
    if len(parts) < 2:
        return
    ign = parts[0]
    bgmi_id = parts[1]
    upcoming = await tournaments_col.find_one({"status": "open"}, sort=[("start_at", 1)])
    if not upcoming:
        await message.reply_text("No open tournament to register currently.")
        return
    tourn_id = str(upcoming["_id"])
    existing = await registrations_col.find_one({"tourn_id": tourn_id, "user_id": message.from_user.id})
    if existing:
        await message.reply_text("You are already registered for this tournament.")
        return
    count = await registrations_col.count_documents({"tourn_id": tourn_id})
    if count >= upcoming.get("max_slots", 9999):
        await message.reply_text("Registration is full for this tournament.")
        return
    doc = {
        "tourn_id": tourn_id,
        "user_id": message.from_user.id,
        "username": message.from_user.username,
        "ign": ign,
        "bgmi_id": bgmi_id,
        "registered_at": datetime.utcnow()
    }
    await registrations_col.insert_one(doc)
    await message.reply_text(f"✅ Registration complete for tournament *{upcoming.get('name')}*! Good luck.", parse_mode="markdown")
    try:
        await client.send_message(ADMIN_ID, f"New registration:\nTournament: {upcoming.get('name')}\nUser: {clickable_name(message.from_user)}\nIGN: {ign} | BGMI ID: {bgmi_id}", parse_mode="markdown")
    except Exception:
        logger.info("Could not notify admin DM.")

@app.on_message(filters.command("list_players") & filters.user(ADMIN_ID))
async def cmd_list_players(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("Usage: /list_players <tourn_id>")
        return
    tourn_id = args[1]
    cursor = registrations_col.find({"tourn_id": tourn_id})
    out = f"Players for tournament {tourn_id}:\n"
    async for r in cursor:
        out += f"- {r.get('ign')} (tg: @{r.get('username') or 'N/A'})\n"
    await message.reply_text(out)

@app.on_message(filters.command("close_registration") & filters.user(ADMIN_ID))
async def cmd_close_registration(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("Usage: /close_registration <tourn_id>")
        return
    tourn_id = args[1]
    try:
        await tournaments_col.update_one({"_id": ObjectId(tourn_id)}, {"$set": {"status": "closed"}})
        await message.reply_text("Registration closed.")
    except Exception as e:
        logger.exception(e)
        await message.reply_text("Error closing registration. Check tourn_id.")

@app.on_message(filters.command("setroom") & filters.user(ADMIN_ID))
async def cmd_setroom(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 5:
        await message.reply_text("Usage: /setroom <tourn_id> <room_id> <pass> <HH:MM>")
        return
    tourn_id, room_id, room_pass, time_str = args[1], args[2], args[3], args[4]
    try:
        hh, mm = map(int, time_str.split(":"))
        now = datetime.now()
        ann_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if ann_time < now:
            ann_time += timedelta(days=1)
    except Exception:
        await message.reply_text("Invalid time format. Use HH:MM (24h).")
        return
    try:
        await tournaments_col.update_one({"_id": ObjectId(tourn_id)}, {"$set": {"room": {"id": room_id, "pass": room_pass, "announce_at": ann_time}, "status": "scheduled"}})
        await message.reply_text(f"Room set. Announcement scheduled at {ann_time.strftime('%Y-%m-%d %H:%M')}")
        # schedule announcement job safely
        schedule_coroutine(ann_time, send_room_details, tourn_id)
    except Exception as e:
        logger.exception(e)
        await message.reply_text("Error setting room. Check tourn_id.")

async def send_room_details(tourn_id: str):
    tourn = await tournaments_col.find_one({"_id": ObjectId(tourn_id)})
    if not tourn or "room" not in tourn:
        return
    room = tourn["room"]
    regs_cursor = registrations_col.find({"tourn_id": tourn_id})
    start_at = tourn.get("start_at")
    start_str = start_at.strftime("%Y-%m-%d %H:%M") if isinstance(start_at, datetime) else str(start_at)
    text = f"🔔 *Room Details* for *{tourn.get('name')}*\n\nRoom ID: `{room.get('id')}`\nPassword: `{room.get('pass')}`\nStart Time: {start_str}\n\nJoin on time. Good luck!"
    async for r in regs_cursor:
        uid = r["user_id"]
        try:
            sent = await app.send_message(uid, text, parse_mode="markdown")
            delete_time = datetime.utcnow() + timedelta(minutes=30)
            # schedule delete at delete_time
            schedule_coroutine(delete_time, delete_dm_message, uid, sent.message_id)
        except Exception:
            logger.info(f"Could not DM user {uid} (maybe blocked bot).")
    ann_time = room.get("announce_at")
    if isinstance(ann_time, datetime):
        for mins in (10, 5, 1):
            dt = ann_time - timedelta(minutes=mins)
            if dt > datetime.utcnow():
                schedule_coroutine(dt, send_reminder, tourn_id, mins)

async def send_reminder(tourn_id: str, mins_before: int):
    tourn = await tournaments_col.find_one({"_id": ObjectId(tourn_id)})
    if not tourn:
        return
    regs_cursor = registrations_col.find({"tourn_id": tourn_id})
    text = f"⏰ Reminder: match *{tourn.get('name')}* starts in {mins_before} minutes. Get ready!"
    async for r in regs_cursor:
        uid = r["user_id"]
        try:
            await app.send_message(uid, text, parse_mode="markdown")
        except Exception:
            logger.info(f"Could not send reminder to {uid}")

async def delete_dm_message(chat_id: int, message_id: int):
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
        pass

@app.on_message(filters.command("announce_winner") & filters.user(ADMIN_ID))
async def cmd_announce_winner(client: Client, message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply_text("Usage: /announce_winner <tourn_id> <winner_ign>")
        return
    tourn_id = args[1]; winner = args[2]
    try:
        await tournaments_col.update_one({"_id": ObjectId(tourn_id)}, {"$set": {"status": "finished", "winner": winner}})
        await message.reply_text(f"Marked tournament {tourn_id} as finished. Winner: {winner}")
    except Exception:
        await message.reply_text("Error updating tournament. Check tourn_id.")

@app.on_message(filters.command("tokens") & filters.user(ADMIN_ID))
async def cmd_tokens(client: Client, message: Message):
    cursor = access_col.find({})
    out = "Active access tokens (temporary premium):\n"
    async for a in cursor:
        expires = a.get("expires_at")
        out += f"- user_id: {a.get('user_id')} | until: {expires}\n"
    await message.reply_text(out)

@app.on_message(filters.command("access") & filters.user(ADMIN_ID))
async def cmd_access(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.reply_text("Usage: /access <username_or_id> <hours>")
        return
    target = args[1]; hours = int(args[2])
    if target.startswith("@"):
        try:
            user_obj = await app.get_users(target)
            user_id = user_obj.id
        except Exception:
            await message.reply_text("Could not resolve username.")
            return
    else:
        try:
            user_id = int(target)
        except:
            await message.reply_text("Provide numeric user id or @username.")
            return
    expires = datetime.utcnow() + timedelta(hours=hours)
    await access_col.insert_one({"user_id": user_id, "expires_at": expires})
    await message.reply_text(f"Access granted to {user_id} until {expires} UTC.")

@app.on_callback_query(filters.regex("^my_regs$"))
async def cb_my_regs(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    cursor = registrations_col.find({"user_id": uid})
    out = "Your registrations:\n"
    found = False
    async for r in cursor:
        found = True
        tourn = await tournaments_col.find_one({"_id": ObjectId(r["tourn_id"])})
        out += f"- {tourn.get('name')} (id: {r['tourn_id']})\n"
    if not found:
        out = "You have no registrations."
    await callback.answer()
    await callback.message.reply_text(out)

# ------------------ Start/Stop ------------------

async def main():
    logger.info("Starting BGMI Tournament Bot (final start)...")
    try:
        await app.start()
    except Exception as e:
        logger.exception("App failed to start: %s", e)
        return

    # debug: who are we running as?
    try:
        me = await app.get_me()
        logger.info(f"Running as: @{getattr(me, 'username', 'N/A')} ({me.id}) is_bot={getattr(me, 'is_bot', None)}")
        if not getattr(me, 'is_bot', False):
            logger.error("Client is not a bot user! Likely using a user-session. STOP and remove old session files.")
            await app.stop()
            return
    except Exception:
        logger.exception("Could not fetch bot info via get_me().")

    # start scheduler after app started
    scheduler.start()
    logger.info("Scheduler started.")

    try:
        await idle()
    finally:
        logger.info("Stopping...")
        await app.stop()
        scheduler.shutdown(wait=False)
        logger.info("Stopped cleanly.")

if __name__ == "__main__":
    asyncio.run(main())
