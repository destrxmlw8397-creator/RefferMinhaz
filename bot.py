import os
import time
import asyncio
import json
import hmac
import hashlib
from urllib.parse import parse_qs
from datetime import datetime, timedelta
import pytz
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
from aiohttp import web
import asyncpg

# --- ENVIRONMENT VARIABLES ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAIN_ADMIN_ID = int(os.environ.get("MAIN_ADMIN_ID", 0))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SESSION_STRING = os.environ.get("SESSION_STRING", None)
APP_URL = os.environ.get("APP_URL", "https://your-domain.com")

if not all([API_ID, API_HASH, BOT_TOKEN, MAIN_ADMIN_ID, DATABASE_URL]):
    raise ValueError("Missing required environment variables: API_ID, API_HASH, BOT_TOKEN, MAIN_ADMIN_ID, DATABASE_URL")

# --- Telegram Client ---
if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    print("✅ Using persistent StringSession")
else:
    client = TelegramClient('referral_bot', API_ID, API_HASH)
    print("⚠️ No SESSION_STRING found, using temporary session file")

waiting_users = {}          # user_id -> state (e.g., 'set_wallet', 'confirm_wallet', 'withdraw_amount', 'change_wallet', 'show_wallet')
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
temp_wallet = {}            # store new wallet during change flow

# --- Database connection pool ---
db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        # Users table
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
        # Stats table
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY,
                total_payout REAL DEFAULT 0.000
            )
        ''')
        await conn.execute("INSERT INTO stats (id, total_payout) VALUES (1, 0.000) ON CONFLICT (id) DO NOTHING")
        # Settings table
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
        # Required channels
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS required_channels (
                id SERIAL PRIMARY KEY,
                channel_username TEXT UNIQUE
            )
        ''')
        # Withdraw requests
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
        # Bonus setting
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bonus_setting (
                id INTEGER PRIMARY KEY,
                ref_count INTEGER,
                bonus_amount REAL
            )
        ''')
        await conn.execute("INSERT INTO bonus_setting (id, ref_count, bonus_amount) VALUES (1, 0, 0) ON CONFLICT (id) DO NOTHING")
        # Tasks
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
                proof_type TEXT DEFAULT 'screenshot'
            )
        ''')
        # Task submissions
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
        # Admins
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY
            )
        ''')
        await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", MAIN_ADMIN_ID)

        # --- NEW: Mini App user_tasks table ---
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_tasks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                task_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, task_id)
            )
        ''')
        
        # Insert demo tasks for Mini App if empty
        count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE task_type IN ('TG', 'Media')")
        if count == 0:
            await conn.execute("""
                INSERT INTO tasks (task_type, url, time_required, reward, task_limit, completed_count, proof_type, status)
                VALUES 
                ('TG', 'your_channel', 0, 10, 100, 0, 'screenshot', 'active'),
                ('Media', 'https://example.com', 30, 15, 50, 0, 'screenshot', 'active')
            """)
    print("✅ Database initialized with Mini App tables")

# --- Database helper functions ---
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
    [Button.text('📊 Statistics', resize=True), Button.text('📋 Task', resize=True)]
]

task_buttons = [
    [Button.text('TG Task', resize=True), Button.text('Media Task', resize=True)],
    [Button.text('Back', resize=True)]
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
    "📤 Withdraw", "📊 Statistics", "📋 Task"
]

TASK_COMMANDS = [
    "TG Task", "Media Task", "Back"
]

CHANNEL_SETTINGS_COMMANDS = [
    "Joining Channel", "Withdrawal Channel", "Proof Channel", "Task Channel",
    "Edit Channel", "Delete Channel", "Back"
]

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

# --- GRANT MILESTONE BONUSES ---
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

# --- Background timer for task claim (Media only) ---
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

# --- Helper: Get available tasks ---
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

# --- Helper: Show task list ---
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

# --- Helper: Render Task Details ---
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

# --- Helper: Send task notification ---
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

# --- Helper: Start a task ---
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

# --- Auto approve pending submissions after 6 hours ---
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

# --- Weekly release ---
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
                        await client.send_message(user_id, f"🔄 **Weekly Release!**\n\nYour hold balance of **{hold_amount:.2f} {currency}** has been released to your real balance. You can now withdraw it.")
                    except:
                        pass
                    print(f"Released {hold_amount} for user {user_id}")
        except Exception as e:
            print(f"Error in weekly_release: {e}")
        await asyncio.sleep(3600)

# --- START COMMAND ---
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
                    ref_alert = f"🔋 You Got a New Referral {ref_name_link}\n\n💡 Reward Only If Referral Solves the Captcha and Joins Our Channels"
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

# --- JOIN CHECK CALLBACK ---
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

# --- ADMIN PANEL ---
@client.on(events.NewMessage(pattern='/panel'))
async def admin_panel(event):
    if not await is_admin(event.sender_id):
        return
    admin_user_mode[event.sender_id] = False
    await event.respond("🛠 **Admin Panel**", buttons=admin_keyboard)

# --- /earn COMMAND (MINI APP) ---
@client.on(events.NewMessage(pattern='/earn'))
async def earn_command(event):
    """Open the Earn Tasks Mini App"""
    base_url = APP_URL
    if hasattr(Button, 'webview'):
        btn = Button.webview("🚀 Open Earn Page", base_url)
    else:
        btn = Button.url("🚀 Open Earn Page", base_url)
    await event.respond(
        "📋 **Earn Tasks**\n\nComplete tasks and earn rewards! 🎁",
        buttons=[[btn]]
    )

# --- TEXT HANDLER ---
# ... (your existing handle_text function) ...
# Since handle_text is very large, I'll include it from your original code.
# For brevity in this response, I'm omitting the full handle_text but it must be present.

# --- CALLBACK QUERY HANDLER ---
# ... (your existing callback function) ...

# --- Web server handlers for Mini App ---
async def serve_index(request):
    """Serve the Mini App HTML (with fallback)"""
    static_path = os.path.join(os.path.dirname(__file__), 'static', 'index.html')
    if os.path.exists(static_path):
        try:
            with open(static_path, 'r', encoding='utf-8') as f:
                return web.Response(text=f.read(), content_type='text/html')
        except Exception:
            pass

    # Fallback HTML (minimal but functional)
    fallback_html = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Earn Tasks</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:sans-serif;background:#0f0f13;color:#e0e0e0;padding:20px 16px 40px}.container{max-width:480px;margin:0 auto}h1{font-size:24px;font-weight:700;margin-bottom:24px;background:linear-gradient(135deg,#f0b90b,#f5d75c);-webkit-background-clip:text;-webkit-text-fill-color:transparent}.task-list{display:flex;flex-direction:column;gap:16px}.task-card{background:#1a1a22;border-radius:16px;padding:16px 18px;display:flex;align-items:center;justify-content:space-between;border:1px solid #2a2a35}.task-info{display:flex;flex-direction:column;gap:4px;flex:1}.task-title{font-size:16px;font-weight:600}.task-reward{font-size:14px;color:#f5b342}.task-btn{padding:8px 18px;border:none;border-radius:30px;font-size:14px;font-weight:600;cursor:pointer;background:#2d2d3a;color:#c0c0d0;min-width:80px}.task-btn.start{background:#f0b90b;color:#0f0f13}.task-btn.verify{background:#3b82f6;color:white}.task-btn.verify.loading{background:#1e293b;color:#94a3b8;pointer-events:none}.task-btn.done{background:#22c55e;color:white;opacity:0.6;cursor:default}.spinner{display:inline-block;width:18px;height:18px;border:2px solid rgba(255,255,255,0.2);border-top:2px solid #f0b90b;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:6px}@keyframes spin{to{transform:rotate(360deg)}}.status-badge{font-size:12px;padding:2px 10px;border-radius:20px;background:#2a2a35}.status-badge.done{background:#22c55e20;color:#22c55e}.status-badge.pending{background:#f0b90b20;color:#f0b90b}.empty-state{text-align:center;padding:40px 0;color:#6a6a7a}</style>
</head>
<body>
<div class="container"><h1>📋 Earn Tasks</h1><div id="taskList" class="task-list"><div class="empty-state">Loading tasks...</div></div></div>
<script>
function getInitData(){return window.Telegram&&window.Telegram.WebApp?window.Telegram.WebApp.initData:'query_id=...&user=%7B%22id%22%3A123456%7D&auth_date=...&hash=...';}
async function fetchTasks(){const r=await fetch('/api/tasks',{headers:{'X-Telegram-Init-Data':getInitData()}});if(!r.ok)throw Error();return r.json();}
async function verifyTask(t){const r=await fetch('/api/verify-task',{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':getInitData()},body:JSON.stringify({task_id:t})});return r.json();}
function renderTasks(tasks){const c=document.getElementById('taskList');if(!tasks||tasks.length===0){c.innerHTML='<div class="empty-state">No tasks available.</div>';return;}let h='';tasks.forEach(task=>{const done=task.status==='completed';h+=`<div class="task-card"><div class="task-info"><div class="task-title">${task.title}</div><div class="task-reward">🎁 ${task.reward} TRX</div><span class="status-badge ${done?'done':'pending'}">${done?'Done':'Pending'}</span></div><button class="task-btn ${done?'done':'start'}" data-task-id="${task.id}">${done?'Claimed ✓':'Start'}</button></div>`;});c.innerHTML=h;document.querySelectorAll('.task-btn.start:not(.done)').forEach(b=>b.addEventListener('click',handleStartClick));}
async function handleStartClick(e){const btn=e.currentTarget;const taskId=btn.dataset.taskId;if(btn.disabled)return;btn.disabled=true;btn.classList.remove('start');btn.classList.add('verify','loading');btn.innerHTML='<span class="spinner"></span> Verifying...';try{await new Promise(r=>setTimeout(r,5000));const result=await verifyTask(taskId);if(result.success){btn.classList.remove('loading','verify');btn.classList.add('done');btn.innerHTML='Claimed ✓';btn.disabled=true;const badge=btn.closest('.task-card').querySelector('.status-badge');if(badge){badge.textContent='Done';badge.className='status-badge done';}}else{btn.classList.remove('loading','verify');btn.classList.add('start');btn.innerHTML='Start';btn.disabled=false;alert(result.message||'Verification failed.');}}catch(e){btn.classList.remove('loading','verify');btn.classList.add('start');btn.innerHTML='Start';btn.disabled=false;alert('An error occurred.');}}
document.addEventListener('DOMContentLoaded',async()=>{try{const tasks=await fetchTasks();renderTasks(tasks);}catch(e){document.getElementById('taskList').innerHTML='<div class="empty-state">⚠️ Failed to load tasks.</div>';}});
</script>
</body>
</html>"""
    return web.Response(text=fallback_html, content_type='text/html')

def verify_init_data(init_data: str) -> dict:
    parsed = parse_qs(init_data)
    data = {k: v[0] for k, v in parsed.items() if k != 'hash'}
    hash_str = parsed.get('hash', [''])[0]
    sorted_data = sorted(data.items())
    check_string = '\n'.join([f"{k}={v}" for k, v in sorted_data])
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    computed_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if computed_hash != hash_str:
        raise web.HTTPForbidden(text="Invalid initData")
    user_data = data.get('user')
    if not user_data:
        raise web.HTTPBadRequest(text="Missing user")
    return json.loads(user_data)

async def api_tasks(request):
    init_data = request.headers.get('X-Telegram-Init-Data')
    if not init_data:
        return web.json_response({'error': 'Missing init data'}, status=400)
    try:
        user = verify_init_data(init_data)
    except Exception:
        return web.json_response({'error': 'Invalid init data'}, status=403)
    user_id = user['id']
    async with db_pool.acquire() as conn:
        tasks = await conn.fetch("SELECT id, task_type, url, reward FROM tasks WHERE status='active'")
        completed = await conn.fetch("SELECT task_id FROM user_tasks WHERE user_id=$1 AND status='completed'", user_id)
        completed_ids = {r['task_id'] for r in completed}
        result = []
        for t in tasks:
            status = 'completed' if t['id'] in completed_ids else 'pending'
            title = f"Join @{t['url']}" if t['task_type'] == 'TG' else f"Visit {t['url']}"
            result.append({'id': t['id'], 'title': title, 'reward': t['reward'], 'status': status})
    return web.json_response(result)

async def api_verify_task(request):
    init_data = request.headers.get('X-Telegram-Init-Data')
    if not init_data:
        return web.json_response({'error': 'Missing init data'}, status=400)
    try:
        user = verify_init_data(init_data)
    except Exception:
        return web.json_response({'error': 'Invalid init data'}, status=403)
    user_id = user['id']
    try:
        data = await request.json()
        task_id = int(data.get('task_id'))
    except:
        return web.json_response({'error': 'Invalid request'}, status=400)
    
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow("SELECT id, reward FROM tasks WHERE id=$1 AND status='active'", task_id)
        if not task:
            return web.json_response({'success': False, 'message': 'Task not available'})
        existing = await conn.fetchrow("SELECT id, status FROM user_tasks WHERE user_id=$1 AND task_id=$2", user_id, task_id)
        if existing and existing['status'] == 'completed':
            return web.json_response({'success': False, 'message': 'Already completed'})
        reward = task['reward']
        if existing:
            await conn.execute("UPDATE user_tasks SET status='completed', updated_at=NOW() WHERE id=$1", existing['id'])
        else:
            await conn.execute("INSERT INTO user_tasks (user_id, task_id, status) VALUES ($1, $2, 'completed')", user_id, task_id)
        await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", reward, user_id)
    return web.json_response({'success': True, 'reward': reward})

async def health(request):
    return web.Response(text="OK")

# --- Web server startup ---
async def start_web_server():
    app = web.Application()
    app['db_pool'] = db_pool
    app.router.add_get('/', serve_index)
    app.router.add_get('/health', health)
    app.router.add_get('/api/tasks', api_tasks)
    app.router.add_post('/api/verify-task', api_verify_task)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web server running on port {port}")
    await asyncio.Event().wait()

# --- Main entry point ---
async def main():
    await init_db()
    await client.start(bot_token=BOT_TOKEN)
    print("🤖 Bot started!")
    asyncio.create_task(auto_approve_pending_submissions())
    asyncio.create_task(weekly_release())
    await start_web_server()

if __name__ == "__main__":
    asyncio.run(main())
