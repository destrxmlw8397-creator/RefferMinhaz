import os
import time
import asyncio
import hmac
import hashlib
import json
from urllib.parse import parse_qs
from datetime import datetime, timedelta
import pytz
from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError
from telethon.tl.types import (
    ChannelParticipantAdmin, ChannelParticipantCreator,
    KeyboardButtonSimpleWebView
)
from aiohttp import web
import asyncpg
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

# --- ENVIRONMENT VARIABLES ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAIN_ADMIN_ID = int(os.environ.get("MAIN_ADMIN_ID", 0))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
WEB_APP_URL = "https://refferminhaz.onrender.com"

if not all([API_ID, API_HASH, BOT_TOKEN, MAIN_ADMIN_ID, DATABASE_URL]):
    raise ValueError("Missing required environment variables")

client = TelegramClient('referral_bot', API_ID, API_HASH)

# --- State dictionaries ---
waiting_users = {}
admin_waiting = {}
admin_confirm = {}
task_waiting = {}
task_sessions = {}
screenshot_waiting = {}
processed_media = set()
screenshot_lock = asyncio.Lock()
task_list_msgs = {}
admin_edit_state = {}
admin_tg_task_state = {}
admin_channel_state = {}
admin_user_mode = {MAIN_ADMIN_ID: True}
temp_wallet = {}

# --- Database pool ---
db_pool = None

# --- FastAPI app ---
fastapi_app = FastAPI()
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Serve index.html ---
@fastapi_app.get("/")
async def root():
    return FileResponse("index.html")

# --- Pydantic models ---
class TaskVerifyRequest(BaseModel):
    task_id: int
    init_data: str

# --- initData verification (FIXED with debug logs) ---
def verify_init_data(init_data: str) -> dict:
    if not init_data:
        print("❌ init_data is empty")
        return None

    try:
        parsed = parse_qs(init_data)
        parsed = {k: v[0] for k, v in parsed.items()}
    except Exception as e:
        print(f"❌ Parse error: {e}")
        return None

    if 'hash' not in parsed:
        print("❌ No 'hash' field in init_data")
        return None

    received_hash = parsed.pop('hash')
    sorted_keys = sorted(parsed.keys())
    data_check_string = '\n'.join([f"{k}={parsed[k]}" for k in sorted_keys])

    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    expected_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        print(f"❌ Hash mismatch!\nExpected: {expected_hash}\nReceived: {received_hash}")
        return None

    print("✅ Hash verified successfully")

    if 'user' not in parsed:
        print("❌ No 'user' field in init_data")
        return None

    try:
        user_data = json.loads(parsed['user'])
        print(f"✅ User data: {user_data}")
        return user_data
    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
        return None

# --- FastAPI endpoints ---
@fastapi_app.get("/api/tasks")
async def get_tasks(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user['id']

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        balance = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id) or 0.0
        tasks = await conn.fetch("SELECT id, title, link, reward, task_type FROM tasks WHERE status='active'")
        done = await conn.fetch("SELECT task_id FROM user_tasks WHERE user_id=$1 AND status IN ('completed','claimed')", user_id)
        done_ids = {row['task_id'] for row in done}
        pending = await conn.fetch("SELECT task_id FROM user_tasks WHERE user_id=$1 AND status='pending'", user_id)
        pending_ids = {row['task_id'] for row in pending}

    result = []
    for t in tasks:
        status = 'available'
        if t['id'] in done_ids:
            status = 'claimed'
        elif t['id'] in pending_ids:
            status = 'pending'
        result.append({
            "id": t['id'],
            "title": t['title'],
            "link": t['link'],
            "reward": t['reward'],
            "type": t['task_type'],
            "status": status
        })
    return {"tasks": result, "balance": balance}

@fastapi_app.post("/api/verify-task")
async def verify_task(req: TaskVerifyRequest):
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user['id']
    task_id = req.task_id

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        task = await conn.fetchrow("SELECT id, reward, task_type, link FROM tasks WHERE id=$1 AND status='active'", task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        done = await conn.fetchval("SELECT id FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status IN ('completed','claimed')", user_id, task_id)
        if done:
            return {"status": "already_claimed"}

        pending_row = await conn.fetchrow("SELECT id, started_at FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status='pending'", user_id, task_id)

        if not pending_row:
            await conn.execute(
                "INSERT INTO user_tasks (user_id, task_id, status, started_at) VALUES ($1, $2, 'pending', $3)",
                user_id, task_id, int(time.time())
            )
            return {"status": "pending", "message": "Task started, wait 30 seconds then verify."}

        started = pending_row['started_at'] or 0
        elapsed = int(time.time()) - started
        if elapsed < 30:
            return {"status": "pending", "message": f"Please wait {30 - elapsed} more seconds."}

        # Telegram Channel verification
        if task['task_type'] == 'telegram_channel' and task['link']:
            channel = task['link'].replace('@', '').strip()
            try:
                entity = await client.get_entity(f"@{channel}")
                await client(GetParticipantRequest(channel=entity, participant=user_id))
                is_member = True
            except UserNotParticipantError:
                is_member = False
            except Exception:
                is_member = False
            if not is_member:
                return {"status": "failed", "message": "You are not a member of the channel."}

        # Success - update balance
        reward = task['reward']
        await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", reward, user_id)
        await conn.execute("UPDATE user_tasks SET status='completed', completed_at=$1 WHERE id=$2", int(time.time()), pending_row['id'])

        return {"status": "success", "reward": reward}

# --- Database helper functions ---
async def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return db_pool

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name TEXT,
                username TEXT,
                balance REAL DEFAULT 0.0,
                hold_balance REAL DEFAULT 0.0,
                ref_by BIGINT,
                wallet TEXT DEFAULT 'Not Set',
                total_ref INTEGER DEFAULT 0,
                last_bonus INTEGER DEFAULT 0,
                is_joined INTEGER DEFAULT 0,
                total_earned REAL DEFAULT 0,
                total_withdrawn REAL DEFAULT 0,
                join_date INTEGER DEFAULT 0,
                claimed_milestones TEXT DEFAULT '',
                last_release_time INTEGER DEFAULT 0,
                total_released REAL DEFAULT 0.0
            )
        ''')
        await conn.execute('''
            DO $$ 
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='hold_balance') THEN
                    ALTER TABLE users ADD COLUMN hold_balance REAL DEFAULT 0.0;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='last_release_time') THEN
                    ALTER TABLE users ADD COLUMN last_release_time INTEGER DEFAULT 0;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='total_released') THEN
                    ALTER TABLE users ADD COLUMN total_released REAL DEFAULT 0.0;
                END IF;
            END $$;
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY,
                total_payout REAL DEFAULT 0.000
            )
        ''')
        await conn.execute("INSERT INTO stats (id, total_payout) VALUES (1, 0.000) ON CONFLICT (id) DO NOTHING")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY,
                welcome_bonus REAL DEFAULT 0,
                ref_bonus REAL DEFAULT 0,
                daily_bonus REAL DEFAULT 0,
                withdraw_status INTEGER DEFAULT 1,
                currency TEXT DEFAULT 'BDT',
                min_withdraw REAL DEFAULT 20,
                task_proof_channel TEXT DEFAULT '',
                withdrawal_channel TEXT DEFAULT '',
                task_channel TEXT DEFAULT '',
                withdraw_fee REAL DEFAULT 25.0
            )
        ''')
        await conn.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='settings' AND column_name='withdraw_fee') THEN
                    ALTER TABLE settings ADD COLUMN withdraw_fee REAL DEFAULT 25.0;
                END IF;
            END $$;
        ''')
        await conn.execute("INSERT INTO settings (id, welcome_bonus, ref_bonus, daily_bonus, currency, min_withdraw, task_proof_channel, withdrawal_channel, task_channel, withdraw_fee) VALUES (1, 0, 0, 0, 'BDT', 20, '', '', '', 25.0) ON CONFLICT (id) DO NOTHING")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS required_channels (
                id SERIAL PRIMARY KEY,
                channel_username TEXT UNIQUE
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdraw_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount REAL,
                fee_amount REAL DEFAULT 0,
                net_amount REAL DEFAULT 0,
                wallet TEXT,
                request_time INTEGER,
                status TEXT DEFAULT 'pending',
                paid_time INTEGER
            )
        ''')
        await conn.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='withdraw_requests' AND column_name='fee_amount') THEN
                    ALTER TABLE withdraw_requests ADD COLUMN fee_amount REAL DEFAULT 0;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='withdraw_requests' AND column_name='net_amount') THEN
                    ALTER TABLE withdraw_requests ADD COLUMN net_amount REAL DEFAULT 0;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='withdraw_requests' AND column_name='wallet') THEN
                    ALTER TABLE withdraw_requests ADD COLUMN wallet TEXT;
                END IF;
            END $$;
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bonus_setting (
                id INTEGER PRIMARY KEY,
                ref_count INTEGER,
                bonus_amount REAL
            )
        ''')
        await conn.execute("INSERT INTO bonus_setting (id, ref_count, bonus_amount) VALUES (1, 0, 0) ON CONFLICT (id) DO NOTHING")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                task_type TEXT,
                url TEXT,
                time_required INTEGER,
                reward REAL,
                status TEXT DEFAULT 'active',
                task_limit INTEGER DEFAULT 1,
                completed_count INTEGER DEFAULT 0,
                proof_type TEXT DEFAULT 'screenshot',
                title TEXT,
                link TEXT
            )
        ''')
        await conn.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='tasks' AND column_name='title') THEN
                    ALTER TABLE tasks ADD COLUMN title TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='tasks' AND column_name='link') THEN
                    ALTER TABLE tasks ADD COLUMN link TEXT;
                END IF;
            END $$;
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS task_submissions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                task_id INTEGER,
                reward REAL,
                url TEXT,
                status TEXT DEFAULT 'pending',
                submitted_at INTEGER,
                reviewed_at INTEGER
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_tasks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                status TEXT DEFAULT 'pending',
                started_at INTEGER,
                completed_at INTEGER,
                UNIQUE(user_id, task_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY
            )
        ''')
        await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", MAIN_ADMIN_ID)

async def is_admin(user_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchval("SELECT 1 FROM admins WHERE user_id=$1", user_id)
        return row is not None

async def get_settings():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM settings WHERE id=1")

async def get_bonus_setting():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT ref_count, bonus_amount FROM bonus_setting WHERE id=1")

def fix_url(url):
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        return 'https://' + url
    return url

# --- KEYBOARDS ---
main_buttons = [
    [Button.text('💰 Balance', resize=True), Button.text('👫 Invite', resize=True)],
    [Button.text('🌾 Staking', resize=True), Button.text('📤 Withdraw', resize=True)],
    [Button.text('📊 Statistics', resize=True)],
    [KeyboardButtonSimpleWebView("🎯 Earn", WEB_APP_URL)]
]

admin_keyboard = [
    [Button.text('Set Welcome Bonus'), Button.text('Set Referral Bonus')],
    [Button.text('Set 24h Bonus'), Button.text('Set Min Withdraw')],
    [Button.text('Currency'), Button.text('Withdraw ON'), Button.text('Withdraw OFF')],
    [Button.text('Add Balance'), Button.text('Cut Balance')],
    [Button.text('🎁 Set Bonus'), Button.text('📊 Statistics'), Button.text('Broadcast')],
    [Button.text('Task Settings')],
    [Button.text('Channel Settings')],
    [Button.text('Add Admin'), Button.text('Delete Admin')]
]

task_settings_keyboard = [
    [Button.text('Add TG Task', resize=True), Button.text('Add Media Task', resize=True)],
    [Button.text('Edit TG Task', resize=True), Button.text('Edit Media Task', resize=True)],
    [Button.text('Delete TG Task', resize=True), Button.text('Delete Media Task', resize=True)],
    [Button.text('Set Proof Channel', resize=True)],
    [Button.text('Back', resize=True)]
]

channel_settings_keyboard = [
    [Button.text('Joining Channel', resize=True), Button.text('Withdrawal Channel', resize=True)],
    [Button.text('Proof Channel', resize=True), Button.text('Task Channel', resize=True)],
    [Button.text('Edit Channel', resize=True), Button.text('Delete Channel', resize=True)],
    [Button.text('Back', resize=True)]
]

ADMIN_COMMANDS = [
    "Set Welcome Bonus", "Set Referral Bonus", "Set 24h Bonus",
    "Set Min Withdraw", "Currency", "Withdraw ON", "Withdraw OFF",
    "Add Balance", "Cut Balance", "🎁 Set Bonus", "📊 Statistics", "Broadcast",
    "Task Settings", "Channel Settings", "Add Admin", "Delete Admin"
]

TASK_SETTINGS_COMMANDS = [
    "Add TG Task", "Add Media Task",
    "Edit TG Task", "Edit Media Task",
    "Delete TG Task", "Delete Media Task",
    "Set Proof Channel", "Back"
]

USER_COMMANDS = [
    "💰 Balance", "👫 Invite", "🌾 Staking",
    "📤 Withdraw", "📊 Statistics"
]

CHANNEL_SETTINGS_COMMANDS = [
    "Joining Channel", "Withdrawal Channel", "Proof Channel", "Task Channel",
    "Edit Channel", "Delete Channel", "Back"
]

# --- Helper functions ---
async def get_now():
    tz = pytz.timezone('Asia/Dhaka')
    return datetime.now(tz).strftime("%d/%m/%Y %I:%M %p")

async def is_bot_admin_in_channel(channel_identifier):
    try:
        entity = await client.get_entity(channel_identifier)
        me = await client.get_me()
        participant = await client(GetParticipantRequest(channel=entity, participant=me))
        if isinstance(participant.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
            return entity
        return None
    except UserNotParticipantError:
        return None
    except Exception as e:
        print(f"Error checking bot admin: {e}")
        return None

async def is_user_admin_in_channel(user_id, channel_entity):
    try:
        participant = await client(GetParticipantRequest(channel=channel_entity, participant=user_id))
        if isinstance(participant.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
            return True
        return False
    except UserNotParticipantError:
        return False
    except Exception as e:
        print(f"Error checking user admin: {e}")
        return False

async def is_user_joined_all(user_id):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT channel_username FROM required_channels")
    if not rows:
        return True
    for row in rows:
        ch = row['channel_username']
        try:
            p = await client.get_permissions(ch, user_id)
            if not p:
                return False
        except UserNotParticipantError:
            return False
        except Exception as e:
            print(f"Join check error: {e}")
            return False
    return True

async def get_invite_data(user_id, bot_username):
    async with db_pool.acquire() as conn:
        res = await conn.fetchval("SELECT total_ref FROM users WHERE user_id=$1", user_id)
    total_ref = res if res else 0
    link = f"https://t.me/{bot_username}?start={user_id}"
    settings = await get_settings()
    ref_bonus = settings['ref_bonus']
    currency = settings['currency']
    msg = (f"🙌 **Total Refers = {total_ref} User(s)**\n\n"
           f"🙌 **Your Invite Link =** {link}\n\n"
           f"🧶 **Invite to Earn {ref_bonus} {currency} Per Invite**")
    kb = [[Button.inline("🔍 My Refers", b"my_ref"), Button.inline("🔥 Top List", b"top_list")]]
    return msg, kb

def build_channel_buttons(channels):
    rows = []
    row = []
    for idx, (ch,) in enumerate(channels, start=1):
        row.append(Button.url(f"Join Channel {idx}", f"https://t.me/{ch}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Button.inline("🟢 Joined", b"check_join")])
    return rows

async def grant_milestone_bonuses(user_id, sets):
    bonus_set = await get_bonus_setting()
    if not bonus_set:
        return
    ref_count = bonus_set['ref_count']
    bonus_amt = bonus_set['bonus_amount']
    if ref_count <= 0 or bonus_amt <= 0:
        return

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT total_ref, claimed_milestones FROM users WHERE user_id=$1", user_id)
        if not row:
            return
        total_ref = row['total_ref']
        claimed_str = row['claimed_milestones'] or ''
        claimed_list = [int(x) for x in claimed_str.split(',') if x.isdigit()] if claimed_str else []

        multiples = []
        m = ref_count
        while m <= total_ref:
            multiples.append(m)
            m += ref_count

        new_milestones = [m for m in multiples if m not in claimed_list]
        if not new_milestones:
            return

        total_bonus = len(new_milestones) * bonus_amt
        await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", total_bonus, user_id)
        all_claimed = claimed_list + new_milestones
        new_claimed_str = ','.join(map(str, all_claimed))
        await conn.execute("UPDATE users SET claimed_milestones = $1 WHERE user_id=$2", new_claimed_str, user_id)

    if len(new_milestones) == 1:
        msg = f"🎉 **Congratulations!** You have reached **{new_milestones[0]}** referrals and earned a bonus of **{bonus_amt} {sets['currency']}**!"
    else:
        milestones_str = ', '.join(map(str, new_milestones))
        msg = f"🎉 **Congratulations!** You have reached milestones {milestones_str} and earned a total of **{total_bonus} {sets['currency']}**!"
    try:
        await client.send_message(user_id, msg)
    except:
        pass

# --- Task system functions ---
async def task_timer(user_id, task_id, message_id, chat_id, required_time):
    await asyncio.sleep(required_time)
    try:
        if user_id in task_sessions and task_sessions[user_id]['task_id'] == task_id:
            session = task_sessions[user_id]
            if session.get('task_type') == 'Media':
                proof_type = session.get('proof_type', 'screenshot')
                if proof_type == 'skip':
                    reward = session['reward']
                    async with db_pool.acquire() as conn:
                        existing = await conn.fetchval("SELECT status FROM task_submissions WHERE user_id=$1 AND task_id=$2 AND status='approved'", user_id, task_id)
                        if existing:
                            await client.edit_message(chat_id, message_id, "✅ You have already claimed this task.")
                            return
                        task = await conn.fetchrow("SELECT task_limit, completed_count FROM tasks WHERE id=$1", task_id)
                        if task['completed_count'] >= task['task_limit']:
                            await client.edit_message(chat_id, message_id, "❌ This task is already full.")
                            return
                        await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", reward, user_id)
                        await conn.execute("UPDATE tasks SET completed_count = completed_count + 1 WHERE id=$1", task_id)
                        await conn.execute("INSERT INTO task_submissions (user_id, task_id, reward, url, status, submitted_at, reviewed_at) VALUES ($1, $2, $3, $4, 'approved', $5, $5)",
                                           user_id, task_id, reward, session['url'], int(time.time()))
                    currency = (await get_settings())['currency']
                    text = (f"🎉 **Task Completed!**\n\n"
                            f"You have successfully completed the task and earned **{reward} {currency}** in your Real Balance.\n\n"
                            f"No proof was required for this task.")
                    buttons = [[Button.inline("🔙 Back", b"task_back")]]
                    await client.edit_message(chat_id, message_id, text, buttons=buttons)
                    session['screen'] = 'completed'
                else:
                    text = (f"✅ **Time's up!**\n\n"
                            f"Click the **Claim Reward** button to submit your proof.")
                    buttons = [
                        [Button.inline("🎁 Claim Reward", f"claim_task_{task_id}")],
                        [Button.inline("🔙 Back", b"task_back")]
                    ]
                    await client.edit_message(chat_id, message_id, text, buttons=buttons)
                    session['screen'] = 'timesup'
                    session['prev_screen'] = 'details'
    except Exception as e:
        print(f"Error editing task message: {e}")

async def get_user_available_tasks(user_id):
    async with db_pool.acquire() as conn:
        tasks = await conn.fetch("SELECT id, task_type, url, time_required, reward, task_limit, completed_count, proof_type FROM tasks WHERE status='active'")
    available = []
    for task in tasks:
        if task['completed_count'] >= task['task_limit']:
            continue
        async with db_pool.acquire() as conn:
            sub = await conn.fetchval("SELECT status FROM task_submissions WHERE user_id=$1 AND task_id=$2 ORDER BY id DESC LIMIT 1", user_id, task['id'])
        if sub and sub in ('pending', 'approved'):
            continue
        available.append((task['id'], task['task_type'], task['url'], task['time_required'], task['reward'], task['task_limit'], task['completed_count'], task['proof_type']))
    return available

async def show_task_list(user_id, chat_id, msg_id=None, task_type=None):
    available = await get_user_available_tasks(user_id)
    if task_type:
        available = [t for t in available if t[1] == task_type]
    if available:
        rows = []
        for idx, (tid, ttype, url, ttime, reward, tlimit, ccount, ptype) in enumerate(available, start=1):
            label = f"📌 {ttype} Task {idx}"
            rows.append([Button.inline(label, f"start_task_{tid}")])
        rows.append([Button.inline("❌ Cancel", b"task_cancel_list")])
        text = f"📋 **Available {task_type if task_type else ''} Tasks**\n\nClick a task to start:"
        if msg_id:
            await client.edit_message(chat_id, msg_id, text, buttons=rows)
        else:
            msg = await client.send_message(chat_id, text, buttons=rows)
            task_list_msgs[user_id] = {'msg_id': msg.id, 'chat_id': chat_id}
    else:
        if msg_id:
            await client.edit_message(chat_id, msg_id, f"❌ No active {task_type if task_type else ''} tasks available.")
        else:
            await client.send_message(chat_id, f"❌ No active {task_type if task_type else ''} tasks available.")

async def render_task_details(user_id, chat_id, msg_id):
    session = task_sessions[user_id]
    task_id = session['task_id']
    task_type = session.get('task_type', 'Media')
    url = session['url']
    fixed_url = fix_url(url) if task_type == 'Media' else f"https://t.me/{url}"
    ttime = session.get('time_required', 0)
    reward = session['reward']
    async with db_pool.acquire() as conn:
        res = await conn.fetchrow("SELECT task_limit, completed_count, proof_type FROM tasks WHERE id=$1", task_id)
        if res:
            tlimit, ccount, ptype = res['task_limit'], res['completed_count'], res['proof_type']
            session['task_limit'] = tlimit
            session['completed_count'] = ccount
            session['proof_type'] = ptype
        else:
            tlimit = session.get('task_limit', 1)
            ccount = session.get('completed_count', 0)
            ptype = session.get('proof_type', 'screenshot')
    settings = await get_settings()
    currency = settings['currency']
    progress_text = f"📊 **Task Progress:** {ccount}/{tlimit}"
    if ptype == 'skip':
        proof_display = "📎 No proof required"
    else:
        proof_display = "📸 Screenshot" if ptype == "screenshot" else "🎥 Screen Record"
    if task_type == 'Media':
        text = (f"📌 **Task: Media Task {task_id}**\n\n"
                f"⏱️ **Time Required:** {ttime} seconds\n"
                f"💰 **Reward:** {reward} {currency}\n"
                f"{progress_text}\n"
                f"{proof_display}\n\n")
        if ptype == 'skip':
            text += (f"1️⃣ Click the **Visit Website** button below.\n"
                     f"2️⃣ After visiting, click **Check** to start the timer.\n"
                     f"3️⃣ Wait {ttime} seconds for the timer to finish.\n"
                     f"4️⃣ After timer, reward will be automatically added to your balance.\n\n"
                     f"⚠️ **Note:** You must actually visit the website to get the reward.")
        else:
            text += (f"1️⃣ Click the **Visit Website** button below.\n"
                     f"2️⃣ After visiting, click **Check** to start the timer.\n"
                     f"3️⃣ Wait {ttime} seconds for the timer to finish.\n"
                     f"4️⃣ After timer, click **Claim Reward** and submit your proof.\n\n"
                     f"⚠️ **Note:** You must actually visit the website and submit a valid proof to get the reward.")
        buttons = [
            [Button.url("🔗 Visit Website", fixed_url)],
            [Button.inline("✅ Check", f"task_visited_{task_id}")],
            [Button.inline("🔙 Back", b"task_back")]
        ]
    else:  # TG Task
        text = (f"📌 **Task: TG Task {task_id}**\n\n"
                f"💰 **Reward:** {reward} {currency}\n"
                f"{progress_text}\n\n"
                f"1️⃣ Click the **Join Channel** button below.\n"
                f"2️⃣ After joining, click **Check** to verify membership.\n"
                f"3️⃣ If you are a member, you can **Claim Reward**.\n\n"
                f"⚠️ **Note:** You must join the channel to get the reward.")
        buttons = [
            [Button.url("🔗 Join Channel", fixed_url)],
            [Button.inline("✅ Check", f"tg_check_{task_id}")],
            [Button.inline("🔙 Back", b"task_back")]
        ]
    await client.edit_message(chat_id, msg_id, text, buttons=buttons)
    session['screen'] = 'details'
    session['prev_screen'] = None

async def send_task_notification(task_id, task_type, url, reward, task_limit, proof_type=None, time_required=None):
    settings = await get_settings()
    task_channel = settings['task_channel']
    if not task_channel:
        return
    try:
        channel_entity = await client.get_entity(task_channel)
        currency = settings['currency']
        bot_username = (await client.get_me()).username
        if task_type == "Media":
            proof_text = f"📎 Proof Type: {proof_type}\n" if proof_type else ""
            time_text = f"⏱️ Time Required: {time_required} seconds\n" if time_required else ""
            detail_text = (
                f"📌 **New Media Task Added!**\n\n"
                f"🔗 URL: {url}\n"
                f"{time_text}"
                f"💰 Reward: {reward} {currency}\n"
                f"👥 User Limit: {task_limit}\n"
                f"{proof_text}"
            )
        else:
            detail_text = (
                f"📌 **New TG Task Added!**\n\n"
                f"📢 Channel: https://t.me/{url}\n"
                f"💰 Reward: {reward} {currency}\n"
                f"👥 User Limit: {task_limit}\n"
            )
        start_url = f"https://t.me/{bot_username}?start=task_{task_id}"
        buttons = [
            [Button.url("🚀 Start Task", start_url)]
        ]
        await client.send_message(channel_entity, detail_text, buttons=buttons)
    except Exception as e:
        print(f"Error sending task notification: {e}")

async def start_task_for_user(user_id, task_id):
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow("SELECT id, task_type, url, time_required, reward, task_limit, completed_count, proof_type, status FROM tasks WHERE id=$1", task_id)
        if not task or task['status'] != 'active':
            await client.send_message(user_id, "❌ **Task Unavailable**\n\nThe task you tried to start has been removed or is no longer active.")
            return False

        if task['completed_count'] >= task['task_limit']:
            await client.send_message(user_id, f"❌ **Task Full**\n\nThe task has already been completed by the maximum number of users ({task['task_limit']}). Please try another task.")
            return False

        existing = await conn.fetchval("SELECT status FROM task_submissions WHERE user_id=$1 AND task_id=$2 AND status IN ('pending', 'approved')", user_id, task_id)
        if existing:
            status_text = "pending" if existing == 'pending' else "completed"
            await client.send_message(user_id, f"❌ **Already {status_text.title()}**\n\nYou have already {status_text} this task. You cannot start it again.")
            return False

    if user_id in screenshot_waiting:
        del screenshot_waiting[user_id]

    if user_id in task_sessions:
        if task_sessions[user_id].get('timer_task') and not task_sessions[user_id]['timer_task'].done():
            task_sessions[user_id]['timer_task'].cancel()
        del task_sessions[user_id]

    chat_id = user_id
    msg = await client.send_message(user_id, "📌 **Task Details**\n\nLoading...")
    msg_id = msg.id

    task_sessions[user_id] = {
        'task_id': task['id'],
        'task_type': task['task_type'],
        'url': task['url'],
        'time_required': task['time_required'],
        'reward': task['reward'],
        'task_limit': task['task_limit'],
        'completed_count': task['completed_count'],
        'proof_type': task['proof_type'],
        'message_id': msg_id,
        'chat_id': chat_id,
        'start_time': None,
        'timer_task': None,
        'visited': False,
        'screen': 'details',
        'prev_screen': None
    }

    await render_task_details(user_id, chat_id, msg_id)
    return True

async def auto_approve_pending_submissions():
    while True:
        try:
            current_time = int(time.time())
            async with db_pool.acquire() as conn:
                pending = await conn.fetch("SELECT id, user_id, task_id, reward, url FROM task_submissions WHERE status='pending' AND submitted_at <= $1", current_time - 21600)
                for sub in pending:
                    await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", sub['reward'], sub['user_id'])
                    await conn.execute("UPDATE task_submissions SET status='approved', reviewed_at=$1 WHERE id=$2", current_time, sub['id'])
                    currency = (await get_settings())['currency']
                    try:
                        await client.send_message(sub['user_id'], f"🎉 **Task Auto-Approved!**\n\nYour task submission for Task ID {sub['task_id']} has been automatically approved after 6 hours. You have earned **{sub['reward']} {currency}**!")
                    except:
                        pass
                    print(f"Auto-approved submission {sub['id']} for user {sub['user_id']}")
        except Exception as e:
            print(f"Error in auto_approve_pending_submissions: {e}")
        await asyncio.sleep(300)

async def weekly_release():
    while True:
        try:
            now = int(time.time())
            one_week_seconds = 7 * 24 * 3600
            async with db_pool.acquire() as conn:
                rows = await conn.fetch("SELECT user_id, hold_balance FROM users WHERE hold_balance > 0 AND (last_release_time = 0 OR now - last_release_time >= $1)", one_week_seconds)
                for row in rows:
                    user_id = row['user_id']
                    hold_amount = row['hold_balance']
                    if hold_amount <= 0:
                        continue
                    await conn.execute("UPDATE users SET balance = balance + $1, hold_balance = 0, total_released = total_released + $1, last_release_time = $2 WHERE user_id = $3",
                                       hold_amount, now, user_id)
                    currency = (await get_settings())['currency']
                    try:
                        await client.send_message(user_id, f"🔓 **Weekly Release!**\n\nYour hold balance of **{hold_amount:.2f} {currency}** has been released to your real balance. You can now withdraw it.")
                    except:
                        pass
                    print(f"Released {hold_amount} for user {user_id}")
        except Exception as e:
            print(f"Error in weekly_release: {e}")
        await asyncio.sleep(3600)

async def process_proof(event, user_id, media_type):
    async with screenshot_lock:
        msg_id = event.message.id
        if msg_id in processed_media:
            return
        processed_media.add(msg_id)

        submission_data = screenshot_waiting.pop(user_id, None)
        if not submission_data:
            return

        task_id = submission_data['task_id']
        task_data = submission_data['task_data']
        reward = task_data.get('reward', 0)
        url = task_data.get('url', 'Unknown URL')
        currency = (await get_settings())['currency']

        async with db_pool.acquire() as conn:
            existing = await conn.fetchval("SELECT id FROM task_submissions WHERE user_id=$1 AND task_id=$2 AND status='pending'", user_id, task_id)
            if existing:
                await event.reply("⚠️ You already have a pending submission for this task. Please wait for admin review.")
                return

            sub_id = await conn.fetchval("INSERT INTO task_submissions (user_id, task_id, reward, url, submitted_at) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                                         user_id, task_id, reward, url, int(time.time()))
            await conn.execute("UPDATE tasks SET completed_count = completed_count + 1 WHERE id=$1", task_id)

            user_info = await conn.fetchrow("SELECT name, username FROM users WHERE user_id=$1", user_id)
            name = user_info['name'] if user_info else "Unknown"

        settings = await get_settings()
        proof_channel_str = settings['task_proof_channel']
        if not proof_channel_str:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE tasks SET completed_count = completed_count - 1 WHERE id=$1", task_id)
                await conn.execute("DELETE FROM task_submissions WHERE id=$1", sub_id)
            await event.reply("❌ Task proof channel not set. Please contact admin. You can try again later.")
            if user_id in task_sessions:
                del task_sessions[user_id]
            return

        try:
            proof_channel = await client.get_entity(proof_channel_str)
        except Exception as e:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE tasks SET completed_count = completed_count - 1 WHERE id=$1", task_id)
                await conn.execute("DELETE FROM task_submissions WHERE id=$1", sub_id)
            await event.reply(f"❌ Could not resolve proof channel. Error: {e}\nPlease contact admin.")
            if user_id in task_sessions:
                del task_sessions[user_id]
            return

        proof_type_display = "📸 Screenshot" if media_type == 'photo' else "🎥 Screen Record"

        caption = (f"📸 **New Task Submission** ({proof_type_display})\n\n"
                   f"👤 **User:** {name}\n"
                   f"🆔 **User ID:** `{user_id}`\n"
                   f"📌 **Task ID:** {task_id}\n"
                   f"🔗 **URL:** {url}\n"
                   f"💰 **Reward:** {reward} {currency}\n"
                   f"📅 **Submitted:** {await get_now()}\n\n"
                   f"⏳ **Status:** Pending Review")

        approve_data = f"approve_sub_{sub_id}"
        reject_data = f"reject_sub_{sub_id}"
        buttons = [
            [Button.inline("✅ Approve", approve_data.encode()),
             Button.inline("❌ Reject", reject_data.encode())]
        ]

        try:
            if media_type == 'photo':
                await client.send_message(proof_channel, file=event.message.photo, message=caption, buttons=buttons)
            else:
                await client.send_message(proof_channel, file=event.message.video, message=caption, buttons=buttons)
            await event.reply("✅ **Proof submitted successfully!**\n\nYour submission has been sent for review. You will be notified once admin approves or rejects it.")
            if user_id in task_sessions:
                del task_sessions[user_id]
        except Exception as e:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE tasks SET completed_count = completed_count - 1 WHERE id=$1", task_id)
                await conn.execute("DELETE FROM task_submissions WHERE id=$1", sub_id)
            await event.reply(f"❌ Failed to submit proof. Please try again later. Error: {e}")
            if user_id in task_sessions:
                del task_sessions[user_id]

# --- BOT HANDLERS ---
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    user_id = event.sender_id
    name = event.sender.first_name
    username = event.sender.username or "No Username"
    settings = await get_settings()
    now = int(time.time())

    if await is_admin(user_id):
        admin_user_mode[user_id] = True

    msg_text = event.message.message
    if msg_text.startswith('/start task_'):
        try:
            task_id = int(msg_text.split('_')[1])
        except:
            await event.respond("❌ Invalid task link.")
            return
        async with db_pool.acquire() as conn:
            existing = await conn.fetchval("SELECT user_id FROM users WHERE user_id=$1", user_id)
            if not existing:
                await conn.execute("INSERT INTO users (user_id, name, username, balance, hold_balance, total_earned, join_date) VALUES ($1, $2, $3, $4, $5, $4, $6)",
                                   user_id, name, username, 0, settings['welcome_bonus'], now)
        if not await is_user_joined_all(user_id):
            async with db_pool.acquire() as conn:
                channels = await conn.fetch("SELECT channel_username FROM required_channels")
            rows = build_channel_buttons([(c['channel_username'],) for c in channels])
            join_msg = "⛔ **Please join all required channels first.**\n\nClick each button below to join, then press 'Joined'.\nAfter joining, click the Start Task button again."
            await event.respond(join_msg, buttons=rows)
            return
        await start_task_for_user(user_id, task_id)
        return

    async with db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT user_id FROM users WHERE user_id=$1", user_id)
        is_new_user = False

        if not existing and msg_text.startswith('/start '):
            parts = msg_text.split(' ')
            if len(parts) > 1:
                potential_ref = int(parts[1])
                if potential_ref != user_id:
                    ref_name_link = f"[{name}](tg://user?id={user_id})"
                    ref_alert = f"🔔 You Got a New Referral {ref_name_link}\n\n💡 Reward Only If Referral Solves the Captcha and Joins Our Channels"
                    try:
                        await client.send_message(potential_ref, ref_alert, parse_mode='md')
                    except:
                        pass
                    await conn.execute("INSERT INTO users (user_id, name, username, ref_by, balance, hold_balance, total_earned, join_date) VALUES ($1, $2, $3, $4, $5, $6, $5, $7)",
                                       user_id, name, username, potential_ref, 0, settings['welcome_bonus'], now)
                    is_new_user = True
        if not existing and not is_new_user:
            await conn.execute("INSERT INTO users (user_id, name, username, balance, hold_balance, total_earned, join_date) VALUES ($1, $2, $3, $4, $5, $4, $6)",
                               user_id, name, username, 0, settings['welcome_bonus'], now)
            is_new_user = True
        elif existing:
            is_new_user = False
            await conn.execute("UPDATE users SET name = $1, username = $2 WHERE user_id = $3", name, username, user_id)

    welcome_bonus = settings['welcome_bonus']
    currency = settings['currency']

    if is_new_user and welcome_bonus > 0:
        welcome_msg = (f"👋 Welcome {name}!\n\n"
                       f"🎉 **Congratulations!** You have received a Welcome Bonus of **{welcome_bonus:.2f} {currency}** in your Hold Balance. It will be released weekly.")
    else:
        welcome_msg = f"👋 Welcome All Forwarder!"

    if not await is_admin(user_id) and not await is_user_joined_all(user_id):
        async with db_pool.acquire() as conn:
            channels = await conn.fetch("SELECT channel_username FROM required_channels")
        rows = build_channel_buttons([(c['channel_username'],) for c in channels])
        join_msg = "⛔ Must Join All Required Channels\n\nClick each button below to join, then press 'Joined'."
        await event.respond(join_msg, buttons=rows)
    else:
        await event.respond(welcome_msg, buttons=main_buttons)

@client.on(events.CallbackQuery(data=b"check_join"))
async def check_join_callback(event):
    user_id = event.sender_id
    if await is_user_joined_all(user_id):
        async with db_pool.acquire() as conn:
            user_info = await conn.fetchrow("SELECT is_joined, ref_by, name FROM users WHERE user_id=$1", user_id)
            if user_info and user_info['is_joined'] == 0:
                await conn.execute("UPDATE users SET is_joined = 1 WHERE user_id=$1", user_id)
                ref_id = user_info['ref_by']
                joining_user_name = user_info['name']
                if ref_id:
                    settings = await get_settings()
                    ref_bonus = settings['ref_bonus']
                    await conn.execute("UPDATE users SET balance = balance + $1, total_ref = total_ref + 1, total_earned = total_earned + $1 WHERE user_id=$2", ref_bonus, ref_id)
                    await grant_milestone_bonuses(ref_id, settings)
                    try:
                        await client.send_message(ref_id, f"You have received {ref_bonus} Points from {joining_user_name}.")
                    except:
                        pass
        await event.delete()
        settings = await get_settings()
        welcome_bonus = settings['welcome_bonus']
        currency = settings['currency']
        welcome_msg = f"👋 Welcome {event.sender.first_name}!"
        if welcome_bonus > 0:
            welcome_msg += f"\n\n🎉 **Congratulations!** You have received a Welcome Bonus of **{welcome_bonus:.2f} {currency}** in your Hold Balance. It will be released weekly."
        await event.respond(welcome_msg, buttons=main_buttons)
    else:
        async with db_pool.acquire() as conn:
            channels = await conn.fetch("SELECT channel_username FROM required_channels")
        rows = build_channel_buttons([(c['channel_username'],) for c in channels])
        await event.edit("⛔ You haven't joined all channels yet.\nClick each button below to join, then press 'Joined'.", buttons=rows)
        await event.answer("Please join all required channels.", alert=True)

@client.on(events.NewMessage(pattern='/panel'))
async def admin_panel(event):
    if not await is_admin(event.sender_id):
        return
    admin_user_mode[event.sender_id] = False
    await event.respond("🛠️ **Admin Panel**", buttons=admin_keyboard)

@client.on(events.NewMessage)
async def handle_text(event):
    text = event.message.message
    user_id = event.sender_id
    settings = await get_settings()
    is_user_admin = await is_admin(user_id)

    # Admin channel settings input handling (kept as before)
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in admin_channel_state:
        state = admin_channel_state[user_id]
        step = state.get('step')
        category = state.get('category')
        if text in ADMIN_COMMANDS or text in CHANNEL_SETTINGS_COMMANDS:
            del admin_channel_state[user_id]
        else:
            if step == 'add':
                channel_input = text.strip()
                if 't.me/' in channel_input:
                    parts = channel_input.split('t.me/')
                    if len(parts) > 1:
                        channel_username = parts[-1].split('/')[0].split('?')[0]
                    else:
                        channel_username = channel_input
                else:
                    channel_username = channel_input.replace('@', '').strip()
                if not channel_username:
                    await event.respond("❌ Invalid channel. Please provide a valid channel username or link.")
                    return
                async with db_pool.acquire() as conn:
                    if category == 'joining':
                        try:
                            await conn.execute("INSERT INTO required_channels (channel_username) VALUES ($1)", channel_username)
                            await event.respond(f"✅ Channel @{channel_username} added to joining list.")
                        except asyncpg.UniqueViolationError:
                            await event.respond(f"⚠️ Channel @{channel_username} already exists in joining list.")
                    elif category == 'withdrawal':
                        await conn.execute("UPDATE settings SET withdrawal_channel = $1 WHERE id=1", channel_username)
                        await event.respond(f"✅ Withdrawal channel set to @{channel_username}.")
                    elif category == 'proof':
                        await conn.execute("UPDATE settings SET task_proof_channel = $1 WHERE id=1", channel_username)
                        await event.respond(f"✅ Proof channel set to @{channel_username}.")
                    elif category == 'task':
                        await conn.execute("UPDATE settings SET task_channel = $1 WHERE id=1", channel_username)
                        await event.respond(f"✅ Task channel set to @{channel_username}.")
                    else:
                        await event.respond("❌ Unknown category.")
                del admin_channel_state[user_id]
                await event.respond("Channel Settings", buttons=channel_settings_keyboard)
                return
            elif step == 'edit_select':
                channel_input = text.strip()
                if 't.me/' in channel_input:
                    parts = channel_input.split('t.me/')
                    if len(parts) > 1:
                        channel_username = parts[-1].split('/')[0].split('?')[0]
                    else:
                        channel_username = channel_input
                else:
                    channel_username = channel_input.replace('@', '').strip()
                if not channel_username:
                    await event.respond("❌ Invalid channel. Please provide a valid channel username or link.")
                    return
                channel_id = state.get('channel_id')
                async with db_pool.acquire() as conn:
                    if category == 'joining':
                        try:
                            await conn.execute("UPDATE required_channels SET channel_username = $1 WHERE id = $2", channel_username, channel_id)
                            await event.respond(f"✅ Joining channel updated to @{channel_username}.")
                        except asyncpg.UniqueViolationError:
                            await event.respond(f"⚠️ Channel @{channel_username} already exists.")
                    elif category == 'withdrawal':
                        await conn.execute("UPDATE settings SET withdrawal_channel = $1 WHERE id=1", channel_username)
                        await event.respond(f"✅ Withdrawal channel updated to @{channel_username}.")
                    elif category == 'proof':
                        await conn.execute("UPDATE settings SET task_proof_channel = $1 WHERE id=1", channel_username)
                        await event.respond(f"✅ Proof channel updated to @{channel_username}.")
                    elif category == 'task':
                        await conn.execute("UPDATE settings SET task_channel = $1 WHERE id=1", channel_username)
                        await event.respond(f"✅ Task channel updated to @{channel_username}.")
                    else:
                        await event.respond("❌ Unknown category.")
                del admin_channel_state[user_id]
                await event.respond("Channel Settings", buttons=channel_settings_keyboard)
                return
            else:
                del admin_channel_state[user_id]
                await event.respond("Channel Settings", buttons=channel_settings_keyboard)
                return

    # Admin edit state handling (unchanged)
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in admin_edit_state and admin_edit_state[user_id].get('stage') == 'awaiting_input':
        # ... (this is the same as before, omitted for brevity but present in full code)
        pass

    # Other admin commands handling (same as before)
    if is_user_admin and not admin_user_mode.get(user_id, True):
        # ... (all admin commands)

    # User commands: Balance, Invite, Staking, Withdraw, Statistics (no Task menu)
    if text in USER_COMMANDS:
        # ... (user command handlers)

    # Task commands removed: "📋 Task", "TG Task", "Media Task" are no longer in main menu.

# --- CALLBACK QUERY HANDLER ---
@client.on(events.CallbackQuery)
async def callback(event):
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    bot_obj = await client.get_me()
    is_user_admin = await is_admin(user_id)

    # Wallet confirmations, withdrawal, farming, etc. (unchanged)
    # ...

    # Task system callbacks (with the two fixes)
    if data == "task_cancel_list":
        if user_id in task_sessions:
            session = task_sessions[user_id]
            if session.get('timer_task') and not session['timer_task'].done():
                session['timer_task'].cancel()
            del task_sessions[user_id]
        if user_id in screenshot_waiting:
            del screenshot_waiting[user_id]
        if user_id in task_list_msgs:
            list_info = task_list_msgs[user_id]
            try:
                await client.edit_message(list_info['chat_id'], list_info['msg_id'], "❌ Cancelled.")
            except:
                pass
            del task_list_msgs[user_id]
        # FIX: use event.edit instead of event.respond
        await event.edit("🔙 Back to main menu.", buttons=main_buttons)
        return

    if data == "task_back":
        if user_id not in task_sessions:
            await event.edit("🔙 Back to main menu.", buttons=main_buttons)
            return

        session = task_sessions[user_id]
        screen = session.get('screen', 'details')
        chat_id = session['chat_id']
        msg_id = session['message_id']
        task_type = session.get('task_type')

        if screen == 'details':
            if session.get('timer_task') and not session['timer_task'].done():
                session['timer_task'].cancel()
            del task_sessions[user_id]
            if user_id in screenshot_waiting:
                del screenshot_waiting[user_id]
            await event.edit("🔙 Back to main menu.", buttons=main_buttons)
        elif screen == 'timer':
            await render_task_details(user_id, chat_id, msg_id)
        elif screen == 'timesup':
            await render_task_details(user_id, chat_id, msg_id)
        elif screen == 'proof':
            if user_id in screenshot_waiting:
                del screenshot_waiting[user_id]
            await render_task_details(user_id, chat_id, msg_id)
        else:
            await event.edit("🔙 Back to main menu.", buttons=main_buttons)
        return

    # Other callbacks (start_task, task_visited, claim_task, tg_check, claim_tg, approve_sub, reject_sub, edit_task, etc.)
    # ... (unchanged)

# --- Main entry ---
async def main():
    await init_db()
    await client.start(bot_token=BOT_TOKEN)
    print("🤖 Bot started!")
    asyncio.create_task(auto_approve_pending_submissions())
    asyncio.create_task(weekly_release())

    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
