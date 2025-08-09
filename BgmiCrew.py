# bgmi_tournament_bot.py
# Single-file BGMI Tournament Bot (MongoDB backed)
# Requirements:
#   pip install pyrogram tgcrypto motor apscheduler python-dotenv pymongo
#
# Put a .env file in same folder with:
# BOT_TOKEN=123456:ABC-DEF...
# MONGO_URI=mongodb+srv://user:pass@cluster0.../dbname
# ADMIN_ID=7707903995
# TIMEZONE=Asia/Kolkata    # optional

import os
import logging
from datetime import datetime, timedelta
import asyncio

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import motor.motor_asyncio
from dotenv import load_dotenv
from bson import ObjectId

# --- Load config ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8274531701:AAF4mIvbc36WX-V6NYuJsGljphMbWtbaHJM")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://adsrunnerpro:adsrunnerpro@cluster0.2zzs40v.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_ID = int(os.getenv("7707903995") or 0)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

if not BOT_TOKEN or not MONGO_URI or ADMIN_ID == 0:
    print("Please set BOT_TOKEN, MONGO_URI and ADMIN_ID in .env before running.")
    exit(1)

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bgmi_tourn")

# --- Bot client ---
app = Client("bgmi_tourn_bot", bot_token=BOT_TOKEN)

# --- MongoDB ---
mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo.get_default_database()
tournaments_col = db["tournaments"]
registrations_col = db["registrations"]
access_col = db["access_tokens"]

# --- Scheduler ---
scheduler = AsyncIOScheduler()
scheduler.start()

# --- Helpers ---
def clickable_name(user):
    name = user.first_name or "Player"
    if user.last_name:
        name += f" {user.last_name}"
    return f"[{name}](tg://user?id={user.id})"

def start_keyboard(t_id=None):
    kb = []
    if t_id:
        kb.append([InlineKeyboardButton("View Tournament", callback_data=f"view_{t_id}")])
    kb.append([InlineKeyboardButton("Join Tournament", callback_data="join_tourn")])
    kb.append([InlineKeyboardButton("My Registrations", callback_data="my_regs"),
               InlineKeyboardButton("Help", callback_data="help_menu")])
    return InlineKeyboardMarkup(kb)

HELP_TEXT = (
    "*BGMI Crew Tournament Bot — Help*\n\n"
    "*Players:*\n"
    "- /start : Open menu\n"
    "- Use Join Tournament button and follow DM instructions to register (IGN + BGMI_ID)\n\n"
    "*Admin:*\n"
    "- /create_tournament (private to admin): interactive creation\n"
    "- /list_tournaments : list all tournaments\n"
    "- /list_players <tourn_id> : list players\n"
    "- /setroom <tourn_id> <room_id> <pass> <HH:MM> : set room and schedule announcement\n"
    "- /close_registration <tourn_id> : stop registrations\n"
    "- /announce_winner <tourn_id> <winner_ign> : announce winner & close\n"
)

HELP_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("Contact Admin", url=f"tg://user?id={ADMIN_ID}"),
     InlineKeyboardButton("Back", callback_data="back_to_start")]
])

# interactive admin state store (in-memory)
creating_states = {}

# --- Handlers ---

@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    user = message.from_user
    welcome = (
        f"Hello {clickable_name(user)}!\n\n"
        "Welcome to BGMI Crew Tournament Hub.\n"
        "Use the buttons below to join the active tournament or view your registrations.\n"
        "Tournament room details will be sent privately before match start."
    )
    upcoming = await tournaments_col.find_one({"status": "open"}, sort=[("start_at", 1)])
    kb = start_keyboard(str(upcoming["_id"])) if upcoming else start_keyboard()
    await message.reply_text(welcome, reply_markup=kb, disable_web_page_preview=True, parse_mode="markdown")

@app.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message):
    await message.reply_text(HELP_TEXT, reply_markup=HELP_KB, parse_mode="markdown")

@app.on_callback_query(filters.regex("^help_menu$"))
async def cb_help_menu(client: Client, callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(HELP_TEXT, reply_markup=HELP_KB, parse_mode="markdown")

@app.on_callback_query(filters.regex("^back_to_start$"))
async def cb_back_to_start(client: Client, callback: CallbackQuery):
    await callback.answer()
    upcoming = await tournaments_col.find_one({"status": "open"}, sort=[("start_at", 1)])
    kb = start_keyboard(str(upcoming["_id"])) if upcoming else start_keyboard()
    text = "Main menu — use /start to open full interface."
    await callback.message.edit_text(text, reply_markup=kb)

# Admin: start interactive creation (private chat recommended)
@app.on_message(filters.command("create_tournament") & filters.user(ADMIN_ID))
async def cmd_create_tourn(client: Client, message: Message):
    chat_id = message.chat.id
    creating_states[chat_id] = {"step": "name"}
    await message.reply_text("Tournament creation started. Send tournament name:")

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
        await message.reply_text("Send format (1v1 / 2v2 / 4v4):")
        return
    if state["step"] == "format":
        state["format"] = text
        state["step"] = "start"
        await message.reply_text("Send start datetime in YYYY-MM-DD HH:MM (server timezone):")
        return
    if state["step"] == "start":
        try:
            start_at = datetime.strptime(text, "%Y-%m-%d %H:%M")
            state["start_at"] = start_at
            state["step"] = "slots"
            await message.reply_text("Send max slots (integer):")
        except Exception:
            await message.reply_text("Invalid datetime format. Use YYYY-MM-DD HH:MM")
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
            await message.reply_text(f"Tournament created with id: {res.inserted_id}")
            creating_states.pop(chat_id, None)
        except Exception:
            await message.reply_text("Slots must be integer. Try again.")
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

# Player presses Join button -> bot instructs to DM IGN BGMI_ID
@app.on_callback_query(filters.regex("^join_tourn$"))
async def cb_join_tourn(client: Client, callback: CallbackQuery):
    await callback.answer("Please follow DM instructions to complete registration.")
    user = callback.from_user
    upcoming = await tournaments_col.find_one({"status": "open"}, sort=[("start_at", 1)])
    if not upcoming:
        await callback.message.reply_text("No open tournament right now.")
        return
    try:
        await client.send_message(user.id, "Send your IGN and BGMI numeric ID in one message like:\n`IGN123 1234567890`", parse_mode="markdown")
    except Exception:
        await callback.message.reply_text("I couldn't DM you. Start the bot in private and try again.")

@app.on_message(filters.private & filters.text)
async def handle_private_registration(client: Client, message: Message):
    # Expect: IGN BGMI_ID
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
        await message.reply_text("Registration full for this tournament.")
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
    await message.reply_text(f"✅ Registration complete for tournament *{upcoming.get('name')}* ! Good luck.", parse_mode="markdown")
    try:
        await client.send_message(ADMIN_ID, f"New registration:\nTournament: {upcoming.get('name')}\nUser: {clickable_name(message.from_user)}\nIGN: {ign} | BGMI ID: {bgmi_id}", parse_mode="markdown")
    except Exception:
        logger.info("Could not notify admin.")

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
    except Exception:
        await message.reply_text("Error closing registration. Ensure tourn_id is valid.")

# Set room & schedule announcement (admin)
@app.on_message(filters.command("setroom") & filters.user(ADMIN_ID))
async def cmd_setroom(client: Client, message: Message):
    # /setroom <tourn_id> <room_id> <pass> <HH:MM>
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
        await message.reply_text("Invalid time. Use HH:MM 24-hour.")
        return
    try:
        await tournaments_col.update_one({"_id": ObjectId(tourn_id)}, {"$set": {"room": {"id": room_id, "pass": room_pass, "announce_at": ann_time}, "status": "scheduled"}})
        await message.reply_text(f"Room set. Announcement scheduled at {ann_time.strftime('%Y-%m-%d %H:%M')}")
        # schedule announcement job
        scheduler.add_job(send_room_details_job, 'date', run_date=ann_time, args=[tourn_id])
    except Exception as e:
        logger.exception(e)
        await message.reply_text("Error setting room. Check tourn_id.")

async def send_room_details_job(tourn_id: str):
    tourn = await tournaments_col.find_one({"_id": ObjectId(tourn_id)})
    if not tourn:
        return
    regs_cursor = registrations_col.find({"tourn_id": tourn_id})
    text = f"🔔 *Room Details* for *{tourn.get('name')}*\nRoom ID: `{tourn['room']['id']}`\nPassword: `{tourn['room']['pass']}`\nStart Time: {tourn.get('start_at')}"
    # DM all regs
    async for r in regs_cursor:
        try:
            await app.send_message(r["user_id"], text, parse_mode="markdown")
            # schedule deletion of this DM after 30 minutes
            msg = await app.send_message(r["user_id"], "This message will auto-delete in 30 minutes (room details above).")
            # schedule deletion
            scheduler.add_job(lambda mid=msg.message_id, uid=r["user_id"]: asyncio.create_task(delete_message_after(uid, mid, 30*60)), 'date', run_date=datetime.now() + timedelta(seconds=1))
        except Exception:
            logger.info(f"Could not DM user {r['user_id']}")
    # schedule reminders 10,5,1 mins before announcement time
    ann_time = tourn['room'].get('announce_at')
    if isinstance(ann_time, datetime):
        for mins in (10,5,1):
            dt = ann_time - timedelta(minutes=mins)
            if dt > datetime.now():
                scheduler.add_job(send_reminder_job, 'date', run_date=dt, args=[tourn_id, mins])

async def delete_message_after(chat_id: int, message_id: int, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
        pass

async def send_reminder_job(tourn_id: str, mins_before: int):
    tourn = await tournaments_col.find_one({"_id": ObjectId(tourn_id)})
    if not tourn:
        return
    regs_cursor = registrations_col.find({"tourn_id": tourn_id})
    text = f"Reminder: match *{tourn.get('name')}* starts in {mins_before} minutes."
    async for r in regs_cursor:
        try:
            await app.send_message(r["user_id"], text, parse_mode="markdown")
        except Exception:
            logger.info(f"Could not send reminder to {r['user_id']}")

@app.on_message(filters.command("announce_winner") & filters.user(ADMIN_ID))
async def cmd_announce_winner(client: Client, message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply_text("Usage: /announce_winner <tourn_id> <winner_ign>")
        return
    tourn_id = args[1]; winner = args[2]
    # announce to admin channel (or to admin only)
    await message.reply_text(f"Winner for {tourn_id}: {winner}")
    await tournaments_col.update_one({"_id": ObjectId(tourn_id)}, {"$set": {"status": "finished", "winner": winner}})

# Access tokens / premium (simple admin commands)
@app.on_message(filters.command("tokens") & filters.user(ADMIN_ID))
async def cmd_tokens(client: Client, message: Message):
    cursor = access_col.find({})
    out = "Active access tokens (temporary premium):\n"
    async for a in cursor:
        out += f"- user_id: {a.get('user_id')} | until: {a.get('expires_at')}\n"
    await message.reply_text(out)

@app.on_message(filters.command("access") & filters.user(ADMIN_ID))
async def cmd_access(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.reply_text("Usage: /access <username_or_id> <hours>")
        return
    target = args[1]; hours = int(args[2])
    # resolve username to id if startswith @
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

# Simple /participants (player can view their regs)
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

# Graceful start
if __name__ == "__main__":
    logger.info("Starting BGMI Tournament Bot...")
    app.run()