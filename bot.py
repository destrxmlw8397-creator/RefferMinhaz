import os
import time
import asyncio
from datetime import datetime, timedelta
import pytz
from telethon import TelegramClient, events, Button
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

if not all([API_ID, API_HASH, BOT_TOKEN, MAIN_ADMIN_ID, DATABASE_URL]):
    raise ValueError("Missing required environment variables: API_ID, API_HASH, BOT_TOKEN, MAIN_ADMIN_ID, DATABASE_URL")

client = TelegramClient('referral_bot', API_ID, API_HASH)

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

# --- TEXT HANDLER ---
@client.on(events.NewMessage)
async def handle_text(event):
    text = event.message.message
    user_id = event.sender_id
    settings = await get_settings()
    is_user_admin = await is_admin(user_id)

    # --- Admin channel settings input handling (only in admin mode) ---
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

    # --- Admin edit state: awaiting input (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in admin_edit_state and admin_edit_state[user_id].get('stage') == 'awaiting_input':
        tid = admin_edit_state[user_id]['task_id']
        field = admin_edit_state[user_id].get('field')
        col = admin_edit_state[user_id].get('col')
        if not field or not col:
            await event.reply("❌ Invalid edit session.")
            return
        valid = True
        error_msg = None
        new_val = None

        if col == 'url':
            new_val = fix_url(text)
        elif col == 'time_required':
            try:
                new_val = int(text)
                if new_val <= 0:
                    valid = False
                    error_msg = "Time must be positive."
            except ValueError:
                valid = False
                error_msg = "Invalid number. Please enter a number."
        elif col == 'reward':
            try:
                new_val = float(text)
                if new_val <= 0:
                    valid = False
                    error_msg = "Reward must be positive."
            except ValueError:
                valid = False
                error_msg = "Invalid number. Please enter a number."
        elif col == 'task_limit':
            try:
                new_val = int(text)
                if new_val <= 0:
                    valid = False
                    error_msg = "Limit must be positive."
            except ValueError:
                valid = False
                error_msg = "Invalid number. Please enter a number."
        elif col == 'proof_type':
            val = text.strip().lower()
            if val not in ['screenshot', 'screen record', 'skip']:
                valid = False
                error_msg = "Invalid proof type. Please send `screenshot`, `screen record`, or `skip`."
            else:
                new_val = val
        else:
            valid = False
            error_msg = "Invalid field."

        if not valid:
            await event.reply(f"❌ {error_msg}\nPlease try again.")
            return

        admin_edit_state[user_id]['new_val'] = new_val
        admin_edit_state[user_id]['stage'] = 'confirm'

        async with db_pool.acquire() as conn:
            current = await conn.fetchval(f"SELECT {col} FROM tasks WHERE id=$1", tid)
        if col == 'time_required':
            current_str = f"{current}s"
            new_str = f"{new_val}s"
        elif col == 'reward':
            currency = settings['currency']
            current_str = f"{current} {currency}"
            new_str = f"{new_val} {currency}"
        else:
            current_str = str(current)
            new_str = str(new_val)

        display_names = {
            'url': 'URL',
            'time_required': 'Time',
            'reward': 'Reward',
            'task_limit': 'User Limit',
            'proof_type': 'Proof Type'
        }
        display = display_names.get(col, col)
        confirm_text = (f"⚠️ **Confirm {display} Update**\n\n"
                        f"Current: {current_str}\n"
                        f"New: {new_str}\n\n"
                        f"Are you sure?")
        buttons = [
            [Button.inline("✅ Yes", f"edit_confirm_{tid}_{field}_yes"),
             Button.inline("🔙 Back", f"edit_back_{tid}")]
        ]
        await event.reply(confirm_text, buttons=buttons)
        return

    # --- Admin TG Task creation input handling (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in admin_tg_task_state:
        state = admin_tg_task_state[user_id]
        step = state.get('step')
        if text in ADMIN_COMMANDS or text in TASK_SETTINGS_COMMANDS:
            del admin_tg_task_state[user_id]
        else:
            if step == 'channel':
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
                state['channel'] = channel_username
                state['step'] = 'confirm_channel'
                bot_username = (await client.get_me()).username
                msg = (f"📢 Target Channel: https://t.me/{channel_username}\n\n"
                       f"🤖 Bot Username: @{bot_username}\n\n"
                       f"⚠️ Instructions:\n"
                       f"1. Add this bot to your channel as an Admin.\n"
                       f"2. Ensure the bot has \"Post Messages\" permission.\n"
                       f"3. Click the \"Check ✅\" button below once done.")
                buttons = [
                    [Button.inline("✅ Check", f"tg_admin_check_{channel_username}")],
                    [Button.inline("✏️ Edit", f"tg_admin_edit_{channel_username}")],
                    [Button.inline("❌ Cancel", b"tg_admin_cancel")]
                ]
                await event.respond(msg, buttons=buttons)
                return
            elif step == 'limit':
                try:
                    limit = int(text)
                    if limit < 5:
                        await event.respond("❌ Minimum limit is 5. Please enter a number >= 5.")
                        return
                    state['limit'] = limit
                    state['step'] = 'reward'
                    currency = settings['currency']
                    msg = (f"💰 Step 2: Set Task Reward\n━━━━━━━━━━━━━━\n"
                           f"🎯 Target Channel: https://t.me/{state['channel']}\n"
                           f"📊 Task Limit: {limit}\n\n"
                           f"⚠️ Minimum per Task price: 0.005 {currency}\n"
                           f"Please send your per task reward 👇🏻")
                    await event.respond(msg)
                except ValueError:
                    await event.respond("❌ Invalid number. Please enter a valid integer.")
            elif step == 'reward':
                try:
                    reward = float(text)
                    if reward <= 0:
                        await event.respond("❌ Reward must be positive.")
                        return
                    state['reward'] = reward
                    state['step'] = 'confirm'
                    currency = settings['currency']
                    msg = (f"⚠️ **Confirm TG Task Creation**\n\n"
                           f"📢 Target Channel: https://t.me/{state['channel']}\n"
                           f"👥 Task Limit: {state['limit']}\n"
                           f"💰 Reward per task: {reward} {currency}\n\n"
                           f"Are you sure?")
                    buttons = [
                        [Button.inline("✅ Yes", f"tg_admin_confirm_yes")],
                        [Button.inline("❌ No", f"tg_admin_confirm_no")]
                    ]
                    await event.respond(msg, buttons=buttons)
                except ValueError:
                    await event.respond("❌ Invalid reward amount. Please enter a number.")
            return

    # --- Admin Task Input Handling (Add Media Task) (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in task_waiting:
        action = task_waiting[user_id]
        step = action['step']
        if text in ADMIN_COMMANDS or text in TASK_SETTINGS_COMMANDS:
            del task_waiting[user_id]
        else:
            if step == 'url':
                fixed = fix_url(text)
                action['task_data']['url'] = fixed
                action['step'] = 'time'
                await event.respond("⏱️ Enter time required in seconds (e.g., 30):")
                return
            elif step == 'time':
                try:
                    time_val = int(text)
                    if time_val <= 0:
                        await event.respond("❌ Time must be a positive number. Enter again:")
                        return
                    action['task_data']['time_required'] = time_val
                    action['step'] = 'reward'
                    currency = settings['currency']
                    await event.respond(f"💰 Enter reward amount (in {currency}) for completing this task:")
                    return
                except ValueError:
                    await event.respond("❌ Invalid number. Enter time in seconds (e.g., 30):")
                    return
            elif step == 'reward':
                try:
                    reward = float(text)
                    if reward <= 0:
                        await event.respond("❌ Reward must be positive. Enter again:")
                        return
                    action['task_data']['reward'] = reward
                    action['step'] = 'limit'
                    await event.respond("👥 How many users can complete this task? (Enter a number):")
                    return
                except ValueError:
                    await event.respond("❌ Invalid reward amount. Enter a number:")
                    return
            elif step == 'limit':
                try:
                    limit = int(text)
                    if limit <= 0:
                        await event.respond("❌ Limit must be positive. Enter again:")
                        return
                    action['task_data']['task_limit'] = limit
                    action['step'] = 'proof_type'
                    await event.respond("📎 Choose proof type:\nSend `screenshot`, `screen record`, or `skip`")
                    return
                except ValueError:
                    await event.respond("❌ Invalid limit. Enter a number:")
                    return
            elif step == 'proof_type':
                ptype = text.strip().lower()
                if ptype not in ['screenshot', 'screen record', 'skip']:
                    await event.respond("❌ Invalid choice. Please send `screenshot`, `screen record`, or `skip`")
                    return
                action['task_data']['proof_type'] = ptype
                task_data = action['task_data']
                currency = settings['currency']
                confirm_text = (f"⚠️ **Confirm Add Media Task**\n\n"
                                f"🔗 URL: {task_data['url']}\n"
                                f"⏱️ Time: {task_data['time_required']} seconds\n"
                                f"💰 Reward: {task_data['reward']} {currency}\n"
                                f"👥 User Limit: {task_data['task_limit']}\n"
                                f"📎 Proof Type: {task_data['proof_type']}\n\n"
                                f"Are you sure?")
                kb = [
                    [Button.inline("✅ Yes", b"confirm_task_yes")],
                    [Button.inline("❌ No", b"confirm_task_no")]
                ]
                await event.respond(confirm_text, buttons=kb)
                admin_confirm[user_id] = {'action': 'add_media_task', 'task_data': task_data}
                del task_waiting[user_id]
                return
        return

    # Admin input waiting (for setting proof channel, etc.) - kept for backward compatibility (only in admin mode)
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in admin_waiting and admin_waiting[user_id] == "set_proof_channel":
        if text in ADMIN_COMMANDS or text in TASK_SETTINGS_COMMANDS:
            del admin_waiting[user_id]
        else:
            channel_input = text.strip()
            try:
                entity = await client.get_entity(channel_input)
                me = await client.get_me()
                try:
                    participant = await client(GetParticipantRequest(channel=entity, participant=me))
                    if isinstance(participant.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                        if entity.username:
                            channel_str = entity.username
                        else:
                            channel_str = str(entity.id)
                        async with db_pool.acquire() as conn:
                            await conn.execute("UPDATE settings SET task_proof_channel = $1 WHERE id=1", channel_str)
                        await event.respond(f"✅ Task proof channel set to: {channel_str}")
                    else:
                        await event.respond("❌ Bot is not an admin of this channel. Please make the bot admin and try again.")
                except UserNotParticipantError:
                    await event.respond("❌ Bot is not a member of this channel. Please add the bot and make it admin.")
                except Exception as e:
                    await event.respond(f"❌ Error checking admin status: {e}")
            except Exception as e:
                await event.respond(f"❌ Could not resolve channel: {e}\nPlease send a valid channel username (with @) or channel ID.")
            del admin_waiting[user_id]
        return

    # Admin input waiting (other) (only in admin mode)
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in admin_waiting:
        action = admin_waiting[user_id]
        if isinstance(action, str):
            if action in ["wb", "rb", "db", "min_withdraw", "add_channel", "currency", "bc"]:
                if text in ADMIN_COMMANDS:
                    del admin_waiting[user_id]
                else:
                    async with db_pool.acquire() as conn:
                        if action == "wb":
                            await conn.execute("UPDATE settings SET welcome_bonus = $1 WHERE id=1", float(text))
                            msg = f"✅ Welcome Bonus set to {text}"
                        elif action == "rb":
                            await conn.execute("UPDATE settings SET ref_bonus = $1 WHERE id=1", float(text))
                            msg = f"✅ Referral Bonus set to {text}"
                        elif action == "db":
                            await conn.execute("UPDATE settings SET daily_bonus = $1 WHERE id=1", float(text))
                            msg = f"✅ Daily Bonus set to {text}"
                        elif action == "min_withdraw":
                            await conn.execute("UPDATE settings SET min_withdraw = $1 WHERE id=1", float(text))
                            msg = f"✅ Minimum Withdraw amount set to {text}"
                        elif action == "add_channel":
                            ch = text.replace("@", "").strip()
                            if ch:
                                try:
                                    await conn.execute("INSERT INTO required_channels (channel_username) VALUES ($1)", ch)
                                    msg = f"✅ Channel @{ch} added to join list."
                                except asyncpg.UniqueViolationError:
                                    msg = f"⚠️ Channel @{ch} already exists."
                            else:
                                msg = "❌ Invalid channel username."
                        elif action == "currency":
                            await conn.execute("UPDATE settings SET currency = $1 WHERE id=1", text)
                            msg = f"✅ Currency set to {text}"
                        elif action == "bc":
                            rows = await conn.fetch("SELECT user_id FROM users")
                            all_users = [r['user_id'] for r in rows]
                            await event.respond(f"🚀 Broadcasting to {len(all_users)} users...")
                            count = 0
                            for u in all_users:
                                try:
                                    await client.send_message(u, text)
                                    count += 1
                                    time.sleep(0.1)
                                except:
                                    pass
                            msg = f"✅ Broadcast finished. Sent to {count} users."
                    del admin_waiting[user_id]
                    await event.respond(msg)
                    return
        else:
            pass

    # --- ADMIN COMMANDS (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True):
        if text == "Set Welcome Bonus":
            admin_waiting[user_id] = "wb"
            await event.respond("Enter new Welcome Bonus amount:")
            return
        elif text == "Set Referral Bonus":
            admin_waiting[user_id] = "rb"
            await event.respond("Enter new Referral Bonus amount:")
            return
        elif text == "Set 24h Bonus":
            admin_waiting[user_id] = "db"
            await event.respond("Enter new Daily Bonus amount:")
            return
        elif text == "Set Min Withdraw":
            admin_waiting[user_id] = "min_withdraw"
            await event.respond("Enter new minimum withdrawal amount:")
            return
        elif text == "Currency":
            admin_waiting[user_id] = "currency"
            await event.respond("Send the new currency symbol (e.g., USD, BDT, ₹):")
            return
        elif text == "Withdraw ON":
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE settings SET withdraw_status = 1 WHERE id=1")
            await event.respond("✅ Withdrawal turned ON.")
            return
        elif text == "Withdraw OFF":
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE settings SET withdraw_status = 0 WHERE id=1")
            await event.respond("✅ Withdrawal turned OFF.")
            return
        elif text == "Add Balance":
            admin_waiting[user_id] = "add_balance"
            await event.respond("Send User ID and Amount like:\n`123456789 10.5`")
            return
        elif text == "Cut Balance":
            admin_waiting[user_id] = "cut_balance"
            await event.respond("Send User ID and Amount like:\n`123456789 10.5`")
            return
        elif text == "🎁 Set Bonus":
            admin_waiting[user_id] = "set_bonus"
            await event.respond("Send Referral count and Bonus amount like:\n`10 50`\n(This will set the bonus for reaching that many referrals)")
            return
        elif text == "📊 Statistics":
            async with db_pool.acquire() as conn:
                total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
                total_payout = await conn.fetchval("SELECT total_payout FROM stats WHERE id=1")
                pending_requests = await conn.fetchval("SELECT COUNT(*) FROM withdraw_requests WHERE status='pending'")
                pending_amount = await conn.fetchval("SELECT SUM(amount) FROM withdraw_requests WHERE status='pending'") or 0
                total_balance = await conn.fetchval("SELECT SUM(balance) FROM users") or 0
                total_hold = await conn.fetchval("SELECT SUM(hold_balance) FROM users") or 0
            currency = settings['currency']
            msg = (f"📊 **Bot Statistics**\n\n"
                   f"👥 Total Users: {total_users}\n"
                   f"💰 Total Payouts: {total_payout:.2f} {currency}\n"
                   f"⏳ Pending Withdrawals: {pending_requests} requests\n"
                   f"💵 Pending Amount: {pending_amount:.2f} {currency}\n"
                   f"💎 Total Real Balance: {total_balance:.2f} {currency}\n"
                   f"🔄 Total Hold Balance: {total_hold:.2f} {currency}")
            await event.respond(msg)
            return
        elif text == "Broadcast":
            admin_waiting[user_id] = "bc"
            await event.respond("Send the message to broadcast to all users:")
            return
        elif text == "Task Settings":
            await event.respond("⚙️ **Task Settings**\n\nSelect an option:", buttons=task_settings_keyboard)
            return
        elif text == "Channel Settings":
            await event.respond("🔧 **Channel Settings**\n\nSelect a category to manage channels:", buttons=channel_settings_keyboard)
            return
        elif text == "Add Admin":
            if user_id != MAIN_ADMIN_ID:
                await event.respond("❌ Only the main admin can manage admins.")
                return
            admin_waiting[user_id] = "add_admin"
            await event.respond("📢 Send the user ID or username (with @) of the new admin:")
            return
        elif text == "Delete Admin":
            if user_id != MAIN_ADMIN_ID:
                await event.respond("❌ Only the main admin can manage admins.")
                return
            async with db_pool.acquire() as conn:
                admins = await conn.fetch("SELECT user_id FROM admins WHERE user_id != $1", MAIN_ADMIN_ID)
            if not admins:
                await event.respond("ℹ️ No other admins to remove.")
                return
            rows = []
            for (admin_id,) in admins:
                try:
                    user = await client.get_entity(admin_id)
                    name = user.first_name
                    if user.username:
                        name += f" (@{user.username})"
                except:
                    name = str(admin_id)
                rows.append([Button.inline(f"❌ {name}", f"del_admin_{admin_id}")])
            rows.append([Button.inline("🔙 Cancel", b"cancel_del_admin")])
            await event.respond("Select an admin to remove:", buttons=rows)
            return

    # --- Admin waiting for add_admin input ---
    if is_user_admin and user_id == MAIN_ADMIN_ID and user_id in admin_waiting and admin_waiting[user_id] == "add_admin":
        if text in ADMIN_COMMANDS:
            del admin_waiting[user_id]
        else:
            input_text = text.strip()
            try:
                if input_text.startswith('@'):
                    entity = await client.get_entity(input_text)
                else:
                    entity = await client.get_entity(int(input_text))
                new_admin_id = entity.id
                if new_admin_id == MAIN_ADMIN_ID:
                    await event.respond("❌ The main admin is already an admin.")
                    del admin_waiting[user_id]
                    return
                async with db_pool.acquire() as conn:
                    existing = await conn.fetchval("SELECT 1 FROM admins WHERE user_id=$1", new_admin_id)
                    if existing:
                        await event.respond("⚠️ This user is already an admin.")
                    else:
                        await conn.execute("INSERT INTO admins (user_id) VALUES ($1)", new_admin_id)
                        await event.respond(f"✅ User {entity.first_name} (ID: {new_admin_id}) has been added as an admin.")
            except Exception as e:
                await event.respond(f"❌ Could not find user: {e}")
            del admin_waiting[user_id]
            return

    # CHANNEL SETTINGS COMMANDS (only in admin mode)
    if is_user_admin and not admin_user_mode.get(user_id, True) and text in CHANNEL_SETTINGS_COMMANDS:
        if text == "Back":
            await event.respond("🔙 Back to Admin Panel.", buttons=admin_keyboard)
            return
        elif text in ["Joining Channel", "Withdrawal Channel", "Proof Channel", "Task Channel"]:
            cat_map = {
                "Joining Channel": "joining",
                "Withdrawal Channel": "withdrawal",
                "Proof Channel": "proof",
                "Task Channel": "task"
            }
            category = cat_map[text]
            admin_channel_state[user_id] = {'category': category, 'step': 'add'}
            await event.respond(f"📢 Please send the channel username or link for **{text}**:")
            return
        elif text == "Edit Channel":
            buttons = [
                [Button.inline("Joining Channel", b"edit_cat_joining")],
                [Button.inline("Withdrawal Channel", b"edit_cat_withdrawal")],
                [Button.inline("Proof Channel", b"edit_cat_proof")],
                [Button.inline("Task Channel", b"edit_cat_task")],
                [Button.inline("❌ Cancel", b"edit_cat_cancel")]
            ]
            await event.respond("📝 Select a category to edit a channel:", buttons=buttons)
            return
        elif text == "Delete Channel":
            buttons = [
                [Button.inline("Joining Channel", b"del_cat_joining")],
                [Button.inline("Withdrawal Channel", b"del_cat_withdrawal")],
                [Button.inline("Proof Channel", b"del_cat_proof")],
                [Button.inline("Task Channel", b"del_cat_task")],
                [Button.inline("❌ Cancel", b"del_cat_cancel")]
            ]
            await event.respond("🗑️ Select a category to delete a channel:", buttons=buttons)
            return

    # Task Settings Commands (only in admin mode)
    if is_user_admin and not admin_user_mode.get(user_id, True) and text in TASK_SETTINGS_COMMANDS:
        if text == "Back":
            await event.respond("🔙 Back to Admin Panel.", buttons=admin_keyboard)
            return
        elif text == "Add Media Task":
            task_waiting[user_id] = {'step': 'url', 'task_data': {}}
            await event.respond("🔗 Enter the website URL (must include http:// or https://):")
            return
        elif text == "Add TG Task":
            admin_tg_task_state[user_id] = {'step': 'channel'}
            await event.respond("📢 Please send the channel username (e.g., @checkingreffer) or channel link (e.g., https://t.me/checkingreffer):")
            return
        elif text == "Edit TG Task" or text == "Edit Media Task":
            task_type = "Media" if "Media" in text else "TG"
            async with db_pool.acquire() as conn:
                tasks = await conn.fetch("SELECT id, url, time_required, reward, task_limit, completed_count, proof_type FROM tasks WHERE task_type=$1 AND status='active'", task_type)
            if not tasks:
                await event.respond(f"❌ No active {task_type} tasks found.")
                return
            rows = []
            for task in tasks:
                rows.append([Button.inline(f"✏️ {task['url'][:30]}...", f"edit_task_{task['id']}_{task_type}")])
            rows.append([Button.inline("🔙 Cancel", b"cancel_edit_task")])
            await event.respond(f"📋 Select a {task_type} task to edit:", buttons=rows)
            return
        elif text == "Delete TG Task" or text == "Delete Media Task":
            task_type = "Media" if "Media" in text else "TG"
            async with db_pool.acquire() as conn:
                tasks = await conn.fetch("SELECT id, url, time_required, reward, task_limit, completed_count, proof_type FROM tasks WHERE task_type=$1 AND status='active'", task_type)
            if not tasks:
                await event.respond(f"❌ No active {task_type} tasks found.")
                return
            rows = []
            for task in tasks:
                rows.append([Button.inline(f"🗑️ {task['url'][:30]}...", f"del_task_{task['id']}_{task_type}")])
            rows.append([Button.inline("🔙 Cancel", b"cancel_del_task")])
            await event.respond(f"📋 Select a {task_type} task to delete:", buttons=rows)
            return
        elif text == "Set Proof Channel":
            admin_waiting[user_id] = "set_proof_channel"
            await event.respond("📢 Send the channel username (with @) or channel ID (for private channels).\n\nMake sure the bot is an admin of that channel.")
            return

    # Admin waiting for add/cut balance or set bonus (only in admin mode)
    if is_user_admin and not admin_user_mode.get(user_id, True) and user_id in admin_waiting and admin_waiting[user_id] in ["add_balance", "cut_balance", "set_bonus"]:
        action = admin_waiting[user_id]
        parts = text.strip().split()
        if len(parts) != 2:
            await event.respond("❌ Invalid format. Please send two values separated by space.")
            return
        try:
            first_val = float(parts[0])
            second_val = float(parts[1])
            if action in ["add_balance", "cut_balance"]:
                target_user = int(first_val)
                amount = second_val
                if amount <= 0:
                    await event.respond("❌ Amount must be positive.")
                    return
                admin_confirm[user_id] = {'action': action, 'user_id': target_user, 'amount': amount}
            elif action == "set_bonus":
                ref_count = int(first_val)
                bonus_amount = second_val
                if ref_count <= 0 or bonus_amount <= 0:
                    await event.respond("❌ Both values must be positive.")
                    return
                admin_confirm[user_id] = {'action': action, 'ref_count': ref_count, 'bonus_amount': bonus_amount}
            del admin_waiting[user_id]
            currency = settings['currency']
            if action == "set_bonus":
                confirm_text = (f"⚠️ **Confirm Set Bonus**\n\n"
                                f"Referral count: `{ref_count}`\n"
                                f"Bonus amount: {bonus_amount:.2f} {currency}\n\n"
                                f"Are you sure?")
            else:
                confirm_text = (f"⚠️ **Confirm {action.replace('_', ' ').title()}**\n\n"
                                f"User ID: `{target_user}`\n"
                                f"Amount: {amount:.2f} {currency}\n\n"
                                f"Are you sure?")
            kb = [
                [Button.inline("✅ Yes", f"confirm_{action}_yes")],
                [Button.inline("❌ No", f"confirm_{action}_no")]
            ]
            await event.respond(confirm_text, buttons=kb)
        except ValueError:
            await event.respond("❌ Invalid numbers. Please enter numeric values.")
        return

    # --- PROOF SUBMISSION HANDLING (photo or video) ---
    if user_id in screenshot_waiting:
        proof_type = screenshot_waiting[user_id].get('proof_type', 'screenshot')
        if proof_type == 'screenshot' and event.message.photo:
            await process_proof(event, user_id, 'photo')
            return
        elif proof_type == 'screen record' and event.message.video:
            await process_proof(event, user_id, 'video')
            return
        elif proof_type == 'screenshot' and not event.message.photo:
            await event.reply("📸 Please send a **photo** (screenshot) of the website. Text messages are not accepted.")
            return
        elif proof_type == 'screen record' and not event.message.video:
            await event.reply("🎥 Please send a **video** (screen recording) of the website. Text messages are not accepted.")
            return
        else:
            screenshot_waiting.pop(user_id, None)

    # --- Ignore commands like /start, /panel ---
    if text.startswith('/'):
        return

    # --- User wallet/withdrawal handling ---
    if user_id in waiting_users:
        # If user sent a command from the user menu, cancel the waiting state
        if text in USER_COMMANDS:
            del waiting_users[user_id]
            return

        state = waiting_users[user_id]

        # State: set_wallet (no wallet set, user sends address)
        if state == 'set_wallet':
            # Store wallet, then ask for confirmation
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET wallet = $1 WHERE user_id=$2", text, user_id)
            kb = [[Button.inline("✅ Confirm Wallet", b"confirm_wallet")],
                  [Button.inline("❌ Change", b"change_wallet")]]
            await event.reply(f"🗂 Your wallet has been set to:\n`{text}`\n\nPlease confirm it is correct.", buttons=kb)
            waiting_users[user_id] = 'confirm_wallet'

        # State: confirm_wallet (after setting new wallet, user confirms)
        elif state == 'confirm_wallet':
            # This state is handled by callback, but text input here is ignored.
            pass

        # State: change_wallet (user wants to change wallet, send new address)
        elif state == 'change_wallet':
            # Store temporarily, ask for confirmation
            temp_wallet[user_id] = text
            kb = [[Button.inline("✅ Confirm", b"confirm_change_wallet")],
                  [Button.inline("❌ Cancel", b"cancel_change_wallet")]]
            await event.reply(f"📝 New wallet address:\n`{text}`\n\nPlease confirm.", buttons=kb)
            waiting_users[user_id] = 'confirm_change_wallet'

        # State: confirm_change_wallet (handled by callback)

        # State: withdraw_amount (user sends amount)
        elif state == 'withdraw_amount':
            try:
                amount = float(text)
                if amount <= 0:
                    await event.reply("❌ Amount must be positive. Please enter a valid number.")
                    return
                # Check balance and min
                async with db_pool.acquire() as conn:
                    user = await conn.fetchrow("SELECT balance, wallet FROM users WHERE user_id=$1", user_id)
                    if not user:
                        await event.reply("❌ User not found.")
                        del waiting_users[user_id]
                        return
                    balance = user['balance']
                    wallet = user['wallet']
                    if amount > balance:
                        await event.reply(f"❌ You only have {balance:.2f} {settings['currency']} in real balance. Please enter a lower amount.")
                        return
                    if amount < settings['min_withdraw']:
                        await event.reply(f"❌ Minimum withdrawal is {settings['min_withdraw']} {settings['currency']}.")
                        return
                # Calculate fee
                fee_percent = settings['withdraw_fee'] or 25.0
                fee = amount * (fee_percent / 100)
                net = amount - fee
                currency = settings['currency']
                confirm_text = (f"📤 **Withdrawal Confirmation**\n\n"
                                f"💰 Total Requested: {amount:.2f} {currency}\n"
                                f"📉 Transaction Fee ({fee_percent}%): -{fee:.2f} {currency}\n"
                                f"✅ Net Amount Credited: {net:.2f} {currency}\n"
                                f"💳 Wallet Address: `{wallet}`\n\n"
                                f"⚠️ Please confirm that the wallet address is correct.\n"
                                f"Click **Confirm** to proceed with withdrawal.")
                kb = [
                    [Button.inline("✅ Confirm & Withdraw", f"confirm_withdraw_{user_id}")],
                    [Button.inline("❌ Cancel", b"cancel_withdraw")]
                ]
                await event.reply(confirm_text, buttons=kb)
                # Store withdrawal data in admin_confirm
                admin_confirm[user_id] = {'action': 'withdraw', 'amount': amount, 'fee': fee, 'net': net, 'wallet': wallet, 'user_id': user_id}
                del waiting_users[user_id]  # clear state
            except ValueError:
                await event.reply("❌ Invalid amount. Please enter a number (e.g., 10.5).")
        else:
            # Unknown state, clear
            del waiting_users[user_id]
        return

    # --- Enforce channel join for non-admin ---
    if not await is_admin(user_id) and not await is_user_joined_all(user_id):
        await event.respond("⛔ You must join all required channels first. Use /start to check.")
        return

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if not user:
        return

    # --- USER COMMANDS ---
    if text == '💰 Balance':
        currency = settings['currency']
        real_bal = user['balance']
        hold_bal = user['hold_balance']
        total = real_bal + hold_bal
        msg = (f"👑 Name - {user['name']} \n🆔 UserID - `{user['user_id']}`\n"
               f"💰 **Real Balance (Withdrawable)**: {real_bal:.2f} {currency}\n"
               f"🔄 **Hold Balance (Staking)**: {hold_bal:.2f} {currency}\n"
               f"💎 **Total**: {total:.2f} {currency}\n\n"
               f"🎉 Refer and earn more!")
        await event.reply(msg)

    elif text == '📊 Statistics':
        total_ref = user['total_ref']
        total_earned = user['total_earned']
        total_withdrawn = user['total_withdrawn']
        join_timestamp = user['join_date']
        async with db_pool.acquire() as conn:
            pending_sum = await conn.fetchval("SELECT SUM(amount) FROM withdraw_requests WHERE user_id=$1 AND status='pending'", user_id) or 0
        total_released = user['total_released'] or 0
        if join_timestamp:
            join_date = datetime.fromtimestamp(join_timestamp).strftime("%d/%m/%Y")
        else:
            join_date = "Unknown"
        currency = settings['currency']
        msg = (f"📊 **Your Statistics**\n\n"
               f"👥 Total Referrals: {total_ref}\n"
               f"💰 Total Earned (Referrals+Tasks): {total_earned:.2f} {currency}\n"
               f"💸 Total Paid Out: {total_withdrawn:.2f} {currency}\n"
               f"🔄 Total Released from Hold: {total_released:.2f} {currency}\n"
               f"⏳ Pending Payment: {pending_sum:.2f} {currency}\n"
               f"📅 Joined: {join_date}")
        await event.reply(msg)

    elif text == '👫 Invite':
        bot_user = (await client.get_me()).username
        msg, kb = await get_invite_data(user_id, bot_user)
        await event.reply(msg, buttons=kb)

    elif text == '🌾 Staking':
        hold_bal = user['hold_balance']
        daily_bonus = settings['daily_bonus']
        currency = settings['currency']
        last_claim = user['last_bonus']
        now = int(time.time())
        next_claim_time = last_claim + 86400 if last_claim else now
        if now >= next_claim_time:
            can_claim = True
            remaining = "Available now"
        else:
            can_claim = False
            remaining = str(timedelta(seconds=next_claim_time - now))

        last_release = user['last_release_time'] or 0
        next_release = last_release + 7*86400 if last_release else now
        if now >= next_release:
            release_status = "Ready for release (will happen automatically)"
        else:
            release_status = f"In {str(timedelta(seconds=next_release - now))}"

        total_released = user['total_released'] or 0

        msg = (f"🌾 **Staking Dashboard**\n\n"
               f"🔄 **Current Hold Balance:** {hold_bal:.2f} {currency}\n"
               f"💰 **Daily Farming Reward:** {daily_bonus} {currency}\n"
               f"⏳ **Next Claim Time:** {remaining}\n"
               f"📅 **Weekly Release:** {release_status}\n"
               f"📈 **Total Released to Real Balance:** {total_released:.2f} {currency}\n\n"
               f"Claim your daily farming reward below.")
        buttons = [[Button.inline("🎁 Claim Daily Reward", b"claim_farming")],
                   [Button.inline("🔙 Back", b"back_main")]]
        await event.reply(msg, buttons=buttons)

    elif text == '📤 Withdraw':
        async with db_pool.acquire() as conn:
            pending = await conn.fetchval("SELECT id FROM withdraw_requests WHERE user_id=$1 AND status='pending'", user_id)
        if pending:
            await event.reply("⚠️ You already have a pending withdrawal request. Please wait for it to be processed.")
            return
        if settings['withdraw_status'] == 0:
            await event.reply("⚠️ Withdrawal is currently OFF by Admin.")
            return
        real_bal = user['balance']
        if real_bal < settings['min_withdraw']:
            await event.reply(f"⚠️ Your real balance ({real_bal:.2f} {settings['currency']}) is below the minimum withdrawal amount of {settings['min_withdraw']} {settings['currency']}.")
            return

        wallet = user['wallet']
        if wallet == 'Not Set':
            # No wallet set: ask to set one
            waiting_users[user_id] = 'set_wallet'
            await event.reply("🗂 You have not set your wallet address. Please send your Paytm/UPI/Bank number now.\n\nThis will be used for all future withdrawals.")
            return
        else:
            # Show current wallet with options
            kb = [
                [Button.inline("✅ Confirm", b"withdraw_confirm")],
                [Button.inline("✏️ Change", b"withdraw_change")],
                [Button.inline("❌ Cancel", b"withdraw_cancel")]
            ]
            await event.reply(f"💳 Your current wallet address:\n`{wallet}`\n\nDo you want to proceed with this wallet?", buttons=kb)
            # Set state to show wallet (handled by callback)
            waiting_users[user_id] = 'show_wallet'

    # --- TASK SYSTEM ---
    elif text == '📋 Task':
        await event.respond("📋 **Task Menu**\n\nSelect an option below:", buttons=task_buttons)

    elif text in TASK_COMMANDS:
        if text == 'Back':
            await event.respond("🔙 Back to main menu.", buttons=main_buttons)
        elif text == 'Media Task':
            await show_task_list(user_id, event.chat_id, task_type='Media')
        elif text == 'TG Task':
            await show_task_list(user_id, event.chat_id, task_type='TG')
        else:
            await event.reply(f"📌 **{text}**\n\nThis feature is under development. Stay tuned!")

    else:
        pass

# --- Helper function to process proof (photo or video) ---
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

# --- CALLBACK QUERY HANDLER ---
@client.on(events.CallbackQuery)
async def callback(event):
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    bot_obj = await client.get_me()
    is_user_admin = await is_admin(user_id)

    # Handle wallet confirmation (from set_wallet flow)
    if data == "confirm_wallet":
        if user_id in waiting_users and waiting_users[user_id] == 'confirm_wallet':
            del waiting_users[user_id]
            await event.edit("✅ Wallet confirmed and saved.")
            # Now ask for withdrawal amount
            settings = await get_settings()
            async with db_pool.acquire() as conn:
                user = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1", user_id)
                if user:
                    await event.respond(f"💰 Your current real balance is {user['balance']:.2f} {settings['currency']}.\n\nPlease enter the amount you wish to withdraw (minimum: {settings['min_withdraw']} {settings['currency']}):")
                    waiting_users[user_id] = 'withdraw_amount'
            await event.answer("Wallet confirmed.", alert=True)
        else:
            await event.answer("No pending wallet confirmation.", alert=True)
        return

    if data == "change_wallet":
        if user_id in waiting_users and waiting_users[user_id] == 'confirm_wallet':
            waiting_users[user_id] = 'change_wallet'
            await event.edit("✏️ Please send the new wallet address:")
            await event.answer("Send new wallet.", alert=True)
        else:
            await event.answer("No pending wallet change.", alert=True)
        return

    # Handle confirm change wallet
    if data == "confirm_change_wallet":
        if user_id in waiting_users and waiting_users[user_id] == 'confirm_change_wallet':
            new_wallet = temp_wallet.pop(user_id, None)
            if not new_wallet:
                await event.answer("No new wallet found.", alert=True)
                return
            # Save new wallet
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET wallet = $1 WHERE user_id=$2", new_wallet, user_id)
            del waiting_users[user_id]
            await event.edit("✅ Wallet updated successfully.")
            # Now ask for amount
            settings = await get_settings()
            async with db_pool.acquire() as conn:
                user = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1", user_id)
                if user:
                    await event.respond(f"💰 Your current real balance is {user['balance']:.2f} {settings['currency']}.\n\nPlease enter the amount you wish to withdraw (minimum: {settings['min_withdraw']} {settings['currency']}):")
                    waiting_users[user_id] = 'withdraw_amount'
            await event.answer("Wallet updated.", alert=True)
        else:
            await event.answer("No pending change.", alert=True)
        return

    if data == "cancel_change_wallet":
        if user_id in waiting_users:
            del waiting_users[user_id]
            temp_wallet.pop(user_id, None)
            await event.edit("❌ Wallet change cancelled.")
            await event.answer("Cancelled.", alert=True)
        else:
            await event.answer("No pending change.", alert=True)
        return

    # Handle withdraw show wallet buttons
    if data == "withdraw_confirm":
        if user_id in waiting_users and waiting_users[user_id] == 'show_wallet':
            del waiting_users[user_id]
            await event.edit("✅ Wallet confirmed. Now enter the amount you wish to withdraw.")
            settings = await get_settings()
            async with db_pool.acquire() as conn:
                user = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1", user_id)
                if user:
                    await event.respond(f"💰 Your current real balance is {user['balance']:.2f} {settings['currency']}.\n\nPlease enter the amount you wish to withdraw (minimum: {settings['min_withdraw']} {settings['currency']}):")
                    waiting_users[user_id] = 'withdraw_amount'
            await event.answer("Proceeding.", alert=True)
        else:
            await event.answer("No pending withdrawal.", alert=True)
        return

    if data == "withdraw_change":
        if user_id in waiting_users and waiting_users[user_id] == 'show_wallet':
            waiting_users[user_id] = 'change_wallet'
            await event.edit("✏️ Please send the new wallet address:")
            await event.answer("Send new wallet.", alert=True)
        else:
            await event.answer("No pending withdrawal.", alert=True)
        return

    if data == "withdraw_cancel":
        if user_id in waiting_users:
            del waiting_users[user_id]
            await event.edit("❌ Withdrawal cancelled.")
            await event.answer("Cancelled.", alert=True)
        else:
            await event.answer("No pending withdrawal.", alert=True)
        return

    # Handle withdrawal confirm/cancel (from amount confirmation)
    if data == "cancel_withdraw":
        if user_id in admin_confirm:
            del admin_confirm[user_id]
        await event.edit("❌ Withdrawal cancelled.")
        await event.answer("Cancelled.", alert=True)
        return

    if data.startswith("confirm_withdraw_"):
        if user_id not in admin_confirm or admin_confirm[user_id].get('action') != 'withdraw':
            await event.answer("❌ No pending withdrawal.", alert=True)
            return
        withdraw_data = admin_confirm[user_id]
        amount = withdraw_data['amount']
        fee = withdraw_data['fee']
        net = withdraw_data['net']
        wallet = withdraw_data['wallet']
        target_user = withdraw_data['user_id']
        if target_user != user_id:
            await event.answer("❌ Invalid user.", alert=True)
            return

        # Fetch settings here to avoid UnboundLocalError
        settings = await get_settings()

        async with db_pool.acquire() as conn:
            current_balance = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id)
            if current_balance < amount:
                await event.edit(f"❌ Insufficient balance. You have {current_balance:.2f} {settings['currency']}.")
                del admin_confirm[user_id]
                return

            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", amount, user_id)
            request_time = int(time.time())
            await conn.execute("INSERT INTO withdraw_requests (user_id, amount, fee_amount, net_amount, wallet, request_time, status) VALUES ($1, $2, $3, $4, $5, $6, 'pending')",
                               user_id, amount, fee, net, wallet, request_time)
            req_id = await conn.fetchval("SELECT id FROM withdraw_requests WHERE user_id=$1 AND request_time=$2 AND amount=$3", user_id, request_time, amount)

        del admin_confirm[user_id]
        await event.edit(f"✅ **Withdrawal request submitted!**\n\n"
                         f"Total Requested: {amount:.2f} {settings['currency']}\n"
                         f"Net Amount: {net:.2f} {settings['currency']}\n"
                         f"Wallet: `{wallet}`\n\n"
                         f"Your request is pending approval. You will be notified once processed.")
        await event.answer("Request submitted.", alert=True)

        # Send to withdrawal channel
        withdrawal_channel = settings['withdrawal_channel']
        if not withdrawal_channel:
            await event.respond("❌ Withdrawal channel not set. Please contact admin.")
            return
        try:
            withdrawal_entity = await client.get_entity(withdrawal_channel)
            user_info = await client.get_entity(user_id)
            user_name = user_info.first_name or "Unknown"
            currency = settings['currency']
            request_time_str = await get_now()
            msg = (f"🚀 NEW SUCCESSFUL WITHDRAWAL 🚀\n\n"
                   f"👤 User: ID: {user_id}\n"
                   f"💰 Total Requested: {amount:.2f} {currency}\n"
                   f"📉 Transaction Fee ({settings['withdraw_fee']}%): -{fee:.2f} {currency}\n"
                   f"✅ Net Amount Credited: {net:.2f} {currency}\n\n"
                   f"💳 Wallet Address:\n`{wallet}`")
            pay_button = [Button.inline("✅ Paid", f"p_{req_id}")]
            await client.send_message(withdrawal_entity, msg, buttons=pay_button)
        except Exception as e:
            print(f"Error sending withdrawal request: {e}")
            await event.respond("⚠️ Could not send withdrawal request to admin channel. Please contact admin.")
        return

    # --- Staking: Claim farming reward ---
    if data == "claim_farming":
        if not await is_user_joined_all(user_id) and not await is_admin(user_id):
            await event.answer("❌ You must join all required channels first.", alert=True)
            return
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT last_bonus, hold_balance FROM users WHERE user_id=$1", user_id)
            if not user:
                await event.answer("❌ User not found.", alert=True)
                return
            last_claim = user['last_bonus']
            now = int(time.time())
            if last_claim and now - last_claim < 86400:
                remaining = 86400 - (now - last_claim)
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                await event.answer(f"⏳ You can claim again in {hours}h {minutes}m.", alert=True)
                return
            settings = await get_settings()
            daily_bonus = settings['daily_bonus']
            if daily_bonus <= 0:
                await event.answer("❌ Daily bonus is not set by admin.", alert=True)
                return
            await conn.execute("UPDATE users SET hold_balance = hold_balance + $1, last_bonus = $2 WHERE user_id=$3", daily_bonus, now, user_id)
            currency = settings['currency']
            new_hold = await conn.fetchval("SELECT hold_balance FROM users WHERE user_id=$1", user_id)
            await event.edit(f"✅ **Claimed!**\n\nYou have received {daily_bonus:.2f} {currency} in your Hold Balance.\nNew Hold Balance: {new_hold:.2f} {currency}")
            await event.answer("Claimed!", alert=True)
        return

    if data == "back_main":
        await event.edit("🔙 Back to main menu.", buttons=main_buttons)
        return

    # --- Delete admin confirmation ---
    if data.startswith("del_admin_"):
        if user_id != MAIN_ADMIN_ID:
            await event.answer("❌ Only main admin can remove admins.", alert=True)
            return
        admin_to_delete = int(data.split("_")[2])
        if admin_to_delete == MAIN_ADMIN_ID:
            await event.answer("❌ Cannot remove main admin.", alert=True)
            return
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM admins WHERE user_id=$1", admin_to_delete)
        await event.edit("✅ Admin removed.")
        await event.answer("Removed.", alert=True)
        return

    if data == "cancel_del_admin":
        await event.edit("❌ Operation cancelled.")
        return

    # --- Channel Settings: Edit category selection (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("edit_cat_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        category = data.split("_")[2]
        if category == "cancel":
            await event.edit("❌ Edit cancelled.")
            return
        channels = []
        if category == "joining":
            async with db_pool.acquire() as conn:
                channels = await conn.fetch("SELECT id, channel_username FROM required_channels")
        elif category == "withdrawal":
            settings = await get_settings()
            ch = settings['withdrawal_channel']
            if ch:
                channels = [(0, ch)]
        elif category == "proof":
            settings = await get_settings()
            ch = settings['task_proof_channel']
            if ch:
                channels = [(0, ch)]
        elif category == "task":
            settings = await get_settings()
            ch = settings['task_channel']
            if ch:
                channels = [(0, ch)]
        if not channels:
            await event.edit(f"❌ No channels in {category} category.")
            return
        rows = []
        for cid, ch in channels:
            rows.append([Button.inline(f"✏️ @{ch}", f"edit_sel_{category}_{cid}")])
        rows.append([Button.inline("🔙 Cancel", b"edit_cat_cancel")])
        await event.edit(f"📝 Select a channel to edit in **{category}**:", buttons=rows)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("edit_sel_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        parts = data.split("_")
        category = parts[2]
        cid = int(parts[3]) if parts[3] != '0' else 0
        admin_channel_state[user_id] = {'category': category, 'step': 'edit_select', 'channel_id': cid}
        await event.edit(f"✏️ Please send the new channel username or link for the **{category}** channel:")
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data == "edit_cat_cancel":
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        await event.edit("❌ Edit cancelled.")
        return

    # --- Channel Settings: Delete category selection (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("del_cat_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        category = data.split("_")[2]
        if category == "cancel":
            await event.edit("❌ Delete cancelled.")
            return
        channels = []
        if category == "joining":
            async with db_pool.acquire() as conn:
                channels = await conn.fetch("SELECT id, channel_username FROM required_channels")
        elif category == "withdrawal":
            settings = await get_settings()
            ch = settings['withdrawal_channel']
            if ch:
                channels = [(0, ch)]
        elif category == "proof":
            settings = await get_settings()
            ch = settings['task_proof_channel']
            if ch:
                channels = [(0, ch)]
        elif category == "task":
            settings = await get_settings()
            ch = settings['task_channel']
            if ch:
                channels = [(0, ch)]
        if not channels:
            await event.edit(f"❌ No channels in {category} category.")
            return
        rows = []
        for cid, ch in channels:
            rows.append([Button.inline(f"🗑️ @{ch}", f"del_sel_{category}_{cid}")])
        rows.append([Button.inline("🔙 Cancel", b"del_cat_cancel")])
        await event.edit(f"🗑️ Select a channel to delete from **{category}**:", buttons=rows)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("del_sel_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        parts = data.split("_")
        category = parts[2]
        cid = int(parts[3]) if parts[3] != '0' else 0
        if category == "joining":
            async with db_pool.acquire() as conn:
                ch = await conn.fetchval("SELECT channel_username FROM required_channels WHERE id=$1", cid)
                ch_name = ch if ch else "Unknown"
        else:
            settings = await get_settings()
            if category == "withdrawal":
                ch_name = settings['withdrawal_channel'] or "Unknown"
            elif category == "proof":
                ch_name = settings['task_proof_channel'] or "Unknown"
            elif category == "task":
                ch_name = settings['task_channel'] or "Unknown"
            else:
                ch_name = "Unknown"
        buttons = [
            [Button.inline("✅ Yes, delete", f"confirm_del_ch_{category}_{cid}")],
            [Button.inline("❌ Cancel", f"del_cat_{category}")]
        ]
        await event.edit(f"⚠️ Are you sure you want to delete **@{ch_name}** from **{category}**?", buttons=buttons)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("confirm_del_ch_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        parts = data.split("_")
        category = parts[3]
        cid = int(parts[4]) if parts[4] != '0' else 0
        if category == "joining":
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM required_channels WHERE id=$1", cid)
            await event.edit("✅ Joining channel deleted.")
        elif category == "withdrawal":
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE settings SET withdrawal_channel = '' WHERE id=1")
            await event.edit("✅ Withdrawal channel cleared.")
        elif category == "proof":
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE settings SET task_proof_channel = '' WHERE id=1")
            await event.edit("✅ Proof channel cleared.")
        elif category == "task":
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE settings SET task_channel = '' WHERE id=1")
            await event.edit("✅ Task channel cleared.")
        else:
            await event.edit("❌ Unknown category.")
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data == "del_cat_cancel":
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        await event.edit("❌ Delete cancelled.")
        return

    # --- Admin TG Task creation callbacks (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("tg_admin_check_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        channel_username = data.split("_")[3]
        state = admin_tg_task_state.get(user_id)
        if not state or state.get('channel') != channel_username:
            await event.answer("❌ Invalid session.", alert=True)
            return
        entity = await is_bot_admin_in_channel(channel_username)
        if entity:
            state['step'] = 'limit'
            await event.edit(f"✅ Bot is admin of @{channel_username}.\n\n🚀 **Task Configuration Service**\n━━━━━━━━━━━━━━\n🎯 Target Channel: https://t.me/{channel_username}\n\nEnter the number of services you want to process.\n\n⚠️ Requirement: Minimum 5\n━━━━━━━━━━━━━━", 
                             buttons=[[Button.inline("🔙 Back", f"tg_admin_edit_{channel_username}")]])
            await event.answer("✅ Admin verified!", alert=True)
        else:
            await event.answer("❌ Bot is not admin or not found. Add bot as admin and try again.", alert=True)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("tg_admin_edit_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        channel_username = data.split("_")[3]
        state = admin_tg_task_state.get(user_id)
        if state:
            state['step'] = 'channel'
            await event.edit("📢 Please send the channel username or link again:")
        else:
            await event.answer("❌ Session expired.", alert=True)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data == "tg_admin_cancel":
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id in admin_tg_task_state:
            del admin_tg_task_state[user_id]
        await event.edit("❌ Task creation cancelled.")
        await event.answer("Cancelled.", alert=True)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and (data.startswith("tg_admin_confirm_yes") or data.startswith("tg_admin_confirm_no")):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        state = admin_tg_task_state.get(user_id)
        if not state:
            await event.answer("❌ No session.", alert=True)
            return
        if data.endswith("_no"):
            del admin_tg_task_state[user_id]
            await event.edit("❌ Task creation cancelled.")
            await event.answer("Cancelled.", alert=True)
            return
        channel = state['channel']
        limit = state['limit']
        reward = state['reward']
        async with db_pool.acquire() as conn:
            task_id = await conn.fetchval("INSERT INTO tasks (task_type, url, time_required, reward, task_limit, completed_count, proof_type, status) VALUES ('TG', $1, 0, $2, $3, 0, 'screenshot', 'active') RETURNING id",
                                          channel, reward, limit)
        del admin_tg_task_state[user_id]
        currency = (await get_settings())['currency']
        await send_task_notification(task_id, "TG", channel, reward, limit)
        await event.edit(f"✅ **TG Task Added Successfully!**\n\n"
                         f"📢 Channel: https://t.me/{channel}\n"
                         f"👥 Task Limit: {limit}\n"
                         f"💰 Reward per task: {reward} {currency}")
        await event.answer("Task added!", alert=True)
        return

    # --- Edit Task: selection from list (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("edit_task_"):
        parts = data.split("_")
        if len(parts) != 4:
            await event.answer("❌ Invalid data.", alert=True)
            return
        tid = int(parts[2])
        task_type = parts[3]
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        admin_edit_state[user_id] = {'task_id': tid, 'stage': 'menu'}
        keyboard = [
            [Button.inline("🔗 URL", f"edit_field_{tid}_url"),
             Button.inline("⏱️ Time", f"edit_field_{tid}_time")],
            [Button.inline("💰 Reward", f"edit_field_{tid}_reward"),
             Button.inline("👥 User Limit", f"edit_field_{tid}_limit")],
            [Button.inline("📎 Proof Type", f"edit_field_{tid}_proof"),
             Button.inline("❌ Cancel", f"edit_cancel_{tid}")]
        ]
        await event.edit("📝 **Edit Task**\n\nSelect what you want to change:", buttons=keyboard)
        return

    # --- Edit field: ask for new value (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("edit_field_"):
        parts = data.split("_")
        if len(parts) != 4:
            await event.answer("❌ Invalid data.", alert=True)
            return
        tid = int(parts[2])
        field = parts[3]
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id not in admin_edit_state or admin_edit_state[user_id]['task_id'] != tid:
            await event.answer("❌ No editing session.", alert=True)
            return

        col_map = {
            'url': 'url',
            'time': 'time_required',
            'reward': 'reward',
            'limit': 'task_limit',
            'proof': 'proof_type'
        }
        col = col_map.get(field)
        if not col:
            await event.answer("❌ Invalid field.", alert=True)
            return

        admin_edit_state[user_id]['stage'] = 'awaiting_input'
        admin_edit_state[user_id]['field'] = field
        admin_edit_state[user_id]['col'] = col
        prompt_map = {
            'url': "🔗 Enter new URL (including http:// or https://):",
            'time': "⏱️ Enter new time required in seconds (e.g., 30):",
            'reward': "💰 Enter new reward amount:",
            'limit': "👥 Enter new user limit (number):",
            'proof': "📎 Enter new proof type (send `screenshot`, `screen record`, or `skip`):"
        }
        prompt_msg = prompt_map.get(field, "Enter new value:")
        back_btn = Button.inline("🔙 Back", f"edit_back_{tid}")
        await event.edit(prompt_msg, buttons=[[back_btn]])
        return

    # --- Edit confirm: Yes (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("edit_confirm_"):
        parts = data.split("_")
        if len(parts) != 5:
            await event.answer("❌ Invalid data.", alert=True)
            return
        tid = int(parts[2])
        field = parts[3]
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id not in admin_edit_state or admin_edit_state[user_id]['task_id'] != tid:
            await event.answer("❌ No editing session.", alert=True)
            return
        new_val = admin_edit_state[user_id].get('new_val')
        if new_val is None:
            await event.answer("❌ No value to update.", alert=True)
            return

        col_map = {
            'url': 'url',
            'time': 'time_required',
            'reward': 'reward',
            'limit': 'task_limit',
            'proof': 'proof_type'
        }
        col = col_map.get(field)
        if not col:
            await event.answer("❌ Invalid field.", alert=True)
            return

        try:
            async with db_pool.acquire() as conn:
                await conn.execute(f"UPDATE tasks SET {col} = $1 WHERE id=$2", new_val, tid)
        except Exception as e:
            await event.answer(f"❌ Database error: {e}", alert=True)
            return
        admin_edit_state[user_id]['stage'] = 'updated'
        display_names = {
            'url': 'URL',
            'time_required': 'Time',
            'reward': 'Reward',
            'task_limit': 'User Limit',
            'proof_type': 'Proof Type'
        }
        display = display_names.get(col, col)
        success_text = f"✅ {display} updated successfully!\n\nNew value: {new_val}"
        buttons = [
            [Button.inline("🔙 Back", f"edit_back_{tid}"),
             Button.inline("❌ Cancel", f"edit_cancel_{tid}")]
        ]
        await event.edit(success_text, buttons=buttons)
        return

    # --- Edit Back: go back to edit menu (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("edit_back_"):
        parts = data.split("_")
        if len(parts) != 3:
            await event.answer("❌ Invalid data.", alert=True)
            return
        tid = int(parts[2])
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id not in admin_edit_state or admin_edit_state[user_id]['task_id'] != tid:
            await event.answer("❌ No editing session.", alert=True)
            return
        admin_edit_state[user_id].pop('new_val', None)
        admin_edit_state[user_id]['stage'] = 'menu'
        keyboard = [
            [Button.inline("🔗 URL", f"edit_field_{tid}_url"),
             Button.inline("⏱️ Time", f"edit_field_{tid}_time")],
            [Button.inline("💰 Reward", f"edit_field_{tid}_reward"),
             Button.inline("👥 User Limit", f"edit_field_{tid}_limit")],
            [Button.inline("📎 Proof Type", f"edit_field_{tid}_proof"),
             Button.inline("❌ Cancel", f"edit_cancel_{tid}")]
        ]
        await event.edit("📝 **Edit Task**\n\nSelect what you want to change:", buttons=keyboard)
        return

    # --- Edit Cancel: cancel editing (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("edit_cancel_"):
        parts = data.split("_")
        if len(parts) != 3:
            await event.answer("❌ Invalid data.", alert=True)
            return
        tid = int(parts[2])
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id in admin_edit_state:
            del admin_edit_state[user_id]
        await event.edit("❌ Editing cancelled.")
        return

    # --- Cancel from task list ---
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
        await event.respond("🔙 Back to Task Menu.", buttons=task_buttons)
        return

    # --- Back button: go to previous screen ---
    if data == "task_back":
        if user_id not in task_sessions:
            await show_task_list(user_id, event.chat_id)
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
            await show_task_list(user_id, chat_id, msg_id, task_type=task_type)
        elif screen == 'timer':
            await render_task_details(user_id, chat_id, msg_id)
        elif screen == 'timesup':
            await render_task_details(user_id, chat_id, msg_id)
        elif screen == 'proof':
            if user_id in screenshot_waiting:
                del screenshot_waiting[user_id]
            await render_task_details(user_id, chat_id, msg_id)
        else:
            await show_task_list(user_id, chat_id, msg_id, task_type=task_type)
        return

    # --- Start Task (both Media and TG) - edit the current list message ---
    if data.startswith("start_task_"):
        try:
            task_id = int(data.split("_")[2])
        except (IndexError, ValueError):
            await event.answer("❌ Invalid task.", alert=True)
            return

        async with db_pool.acquire() as conn:
            task = await conn.fetchrow("SELECT id, task_type, url, time_required, reward, task_limit, completed_count, proof_type, status FROM tasks WHERE id=$1", task_id)
            if not task or task['status'] != 'active':
                await event.answer("❌ This task is no longer available.", alert=True)
                await client.edit_message(event.chat_id, event.message_id, "❌ **Task Unavailable**\n\nThe task you selected has been removed or is no longer active.")
                return

            if task['completed_count'] >= task['task_limit']:
                await event.answer("❌ This task is already full.", alert=True)
                await client.edit_message(event.chat_id, event.message_id, f"❌ **Task Full**\n\nThe task has already been completed by the maximum number of users ({task['task_limit']}). Please try another task.")
                return

            existing = await conn.fetchval("SELECT status FROM task_submissions WHERE user_id=$1 AND task_id=$2 AND status IN ('pending', 'approved')", user_id, task_id)
            if existing:
                status_text = "pending" if existing == 'pending' else "completed"
                await event.answer(f"❌ You have already {status_text} this task.", alert=True)
                await client.edit_message(event.chat_id, event.message_id, f"❌ **Already {status_text.title()}**\n\nYou have already {status_text} this task. You cannot start it again.")
                return

        if user_id in screenshot_waiting:
            del screenshot_waiting[user_id]

        if user_id in task_sessions:
            if task_sessions[user_id].get('timer_task') and not task_sessions[user_id]['timer_task'].done():
                task_sessions[user_id]['timer_task'].cancel()
            del task_sessions[user_id]

        chat_id = event.chat_id
        msg_id = event.message_id

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
        await event.answer("✅ Task loaded.", alert=False)
        return

    # --- Media Task: User clicked "Check" (for Media) ---
    if data.startswith("task_visited_"):
        try:
            task_id = int(data.split("_")[2])
        except:
            await event.answer("❌ Invalid.", alert=True)
            return

        if user_id not in task_sessions:
            await event.answer("❌ No active session.", alert=True)
            return

        session = task_sessions[user_id]
        if session['task_id'] != task_id:
            await event.answer("❌ Task mismatch.", alert=True)
            return

        if session.get('visited', False):
            await event.answer("⏳ Timer already started. Please wait.", alert=True)
            return

        session['start_time'] = int(time.time())
        session['visited'] = True

        text = (f"⏳ **Timer Started!**\n\n"
                f"Please wait **{session['time_required']} seconds**.\n"
                f"After the timer ends, you will be able to claim reward.\n\n"
                f"⏱️ You can visit the website again if you wish.")
        buttons = [
            [Button.inline(f"⏳ Please wait {session['time_required']} seconds...", f"task_wait_{task_id}")],
            [Button.inline("🔙 Back", b"task_back")]
        ]
        await client.edit_message(session['chat_id'], session['message_id'], text, buttons=buttons)
        session['screen'] = 'timer'
        session['prev_screen'] = 'details'

        timer = asyncio.create_task(task_timer(user_id, task_id, session['message_id'], session['chat_id'], session['time_required']))
        session['timer_task'] = timer

        return

    # --- Task wait button ---
    if data.startswith("task_wait_"):
        try:
            task_id = int(data.split("_")[2])
        except:
            await event.answer("❌ Invalid.", alert=True)
            return

        if user_id not in task_sessions:
            await event.answer("❌ No active session.", alert=True)
            return

        session = task_sessions[user_id]
        if session['task_id'] != task_id:
            await event.answer("❌ Task mismatch.", alert=True)
            return

        elapsed = int(time.time()) - session['start_time']
        required = session['time_required']
        if elapsed < required:
            remaining = required - elapsed
            await event.answer(f"⏳ Please wait {remaining} more seconds.", alert=True)
        else:
            await event.answer("⏳ Please wait for the timer to finish.", alert=True)
        return

    # --- Claim Reward (for Media Task) ---
    if data.startswith("claim_task_"):
        try:
            task_id = int(data.split("_")[2])
        except:
            await event.answer("❌ Invalid task.", alert=True)
            return

        if user_id not in task_sessions:
            await event.answer("❌ No active session.", alert=True)
            return

        session = task_sessions[user_id]
        if session['task_id'] != task_id:
            await event.answer("❌ Task mismatch.", alert=True)
            return

        if session.get('proof_type') == 'skip':
            await event.answer("✅ This task does not require proof.", alert=True)
            return

        elapsed = int(time.time()) - session['start_time']
        required = session['time_required']
        if elapsed < required:
            remaining = required - elapsed
            await event.answer(f"⏳ Please wait {remaining} more seconds.", alert=True)
            return

        proof_type = session.get('proof_type', 'screenshot')
        proof_instruction = "📸 Please take a screenshot and send the photo here." if proof_type == 'screenshot' else "🎥 Please record your screen and send the video here."

        text = (f"📎 **Proof Required!**\n\n"
                f"{proof_instruction}\n\n"
                f"⚠️ Your proof will be reviewed by admin.")
        buttons = [
            [Button.inline("🔙 Back", b"task_back")]
        ]
        await client.edit_message(session['chat_id'], session['message_id'], text, buttons=buttons)
        session['screen'] = 'proof'
        session['prev_screen'] = 'timesup'
        screenshot_waiting[user_id] = {
            'task_id': task_id,
            'message_id': session['message_id'],
            'chat_id': session['chat_id'],
            'proof_type': proof_type,
            'task_data': {
                'url': session.get('url', ''),
                'reward': session.get('reward', 0)
            }
        }
        return

    # --- TG Task: User clicked "Check" ---
    if data.startswith("tg_check_"):
        try:
            task_id = int(data.split("_")[2])
        except:
            await event.answer("❌ Invalid.", alert=True)
            return

        if user_id not in task_sessions:
            await event.answer("❌ No active session.", alert=True)
            return

        session = task_sessions[user_id]
        if session['task_id'] != task_id:
            await event.answer("❌ Task mismatch.", alert=True)
            return

        channel_username = session['url']
        try:
            entity = await client.get_entity(f"@{channel_username}")
            try:
                await client(GetParticipantRequest(channel=entity, participant=user_id))
                is_member = True
            except UserNotParticipantError:
                is_member = False
        except Exception as e:
            await event.answer("❌ Could not resolve channel.", alert=True)
            return

        if is_member:
            text = (f"✅ **You are a member of the channel!**\n\n"
                    f"Click the **Claim Reward** button to get your reward.")
            buttons = [
                [Button.inline("🎁 Claim Reward", f"claim_tg_{task_id}")],
                [Button.inline("🔙 Back", b"task_back")]
            ]
            await client.edit_message(session['chat_id'], session['message_id'], text, buttons=buttons)
            session['screen'] = 'claim_ready'
            session['prev_screen'] = 'details'
            await event.answer("✅ Membership verified!", alert=True)
        else:
            await event.answer("❌ You are not a member. Please join the channel first.", alert=True)
        return

    # --- TG Task: Claim Reward ---
    if data.startswith("claim_tg_"):
        try:
            task_id = int(data.split("_")[2])
        except:
            await event.answer("❌ Invalid task.", alert=True)
            return

        if user_id not in task_sessions:
            await event.answer("❌ No active session.", alert=True)
            return

        session = task_sessions[user_id]
        if session['task_id'] != task_id:
            await event.answer("❌ Task mismatch.", alert=True)
            return

        async with db_pool.acquire() as conn:
            if await conn.fetchval("SELECT status FROM task_submissions WHERE user_id=$1 AND task_id=$2 AND status='approved'", user_id, task_id):
                await event.answer("❌ You have already claimed this task.", alert=True)
                return

            task = await conn.fetchrow("SELECT task_limit, completed_count, reward FROM tasks WHERE id=$1", task_id)
            if task['completed_count'] >= task['task_limit']:
                await event.answer("❌ This task is already full.", alert=True)
                return

            reward = task['reward']
            currency = (await get_settings())['currency']
            await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", reward, user_id)
            await conn.execute("UPDATE tasks SET completed_count = completed_count + 1 WHERE id=$1", task_id)
            await conn.execute("INSERT INTO task_submissions (user_id, task_id, reward, url, status, submitted_at, reviewed_at) VALUES ($1, $2, $3, $4, 'approved', $5, $5)",
                               user_id, task_id, reward, session['url'], int(time.time()))

        text = (f"🎉 **Task Completed!**\n\n"
                f"You have successfully completed the TG task and earned **{reward} {currency}** in your Real Balance.")
        buttons = [
            [Button.inline("🔙 Back", b"task_back")]
        ]
        await client.edit_message(session['chat_id'], session['message_id'], text, buttons=buttons)
        session['screen'] = 'completed'
        session['prev_screen'] = 'claim_ready'
        await event.answer("✅ Reward claimed!", alert=True)
        return

    # --- Admin approve/reject callbacks ---
    if data.startswith("approve_sub_") or data.startswith("reject_sub_"):
        settings = await get_settings()
        proof_channel_str = settings['task_proof_channel']
        if not proof_channel_str:
            await event.answer("❌ Proof channel not set.", alert=True)
            return
        try:
            proof_channel = await client.get_entity(proof_channel_str)
        except:
            await event.answer("❌ Proof channel not found.", alert=True)
            return

        is_channel_admin = await is_user_admin_in_channel(user_id, proof_channel)
        if not is_channel_admin:
            await event.answer("❌ You are not an admin of the proof channel.", alert=True)
            return

        try:
            sub_id = int(data.split("_")[2])
        except:
            await event.answer("Invalid submission ID.", alert=True)
            return

        async with db_pool.acquire() as conn:
            sub = await conn.fetchrow("SELECT id, user_id, task_id, reward, status FROM task_submissions WHERE id=$1 AND status='pending'", sub_id)
            if not sub:
                await event.answer("❌ Submission not found or already processed.", alert=True)
                return

            if data.startswith("approve_sub_"):
                await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", sub['reward'], sub['user_id'])
                await conn.execute("UPDATE task_submissions SET status='approved', reviewed_at=$1 WHERE id=$2", int(time.time()), sub_id)
                msg_obj = await event.get_message()
                new_text = msg_obj.text + f"\n\n🟢 **Status: Approved by Admin**"
                await event.edit(new_text, buttons=None)
                currency = settings['currency']
                try:
                    await client.send_message(sub['user_id'], f"🎉 **Task Approved!**\n\nYour task submission has been approved and you have earned **{sub['reward']} {currency}** in your Real Balance!")
                except:
                    pass
                await event.answer("✅ Approved and reward added.", alert=True)
            else:
                await conn.execute("UPDATE tasks SET completed_count = completed_count - 1 WHERE id=$1", sub['task_id'])
                await conn.execute("UPDATE task_submissions SET status='rejected', reviewed_at=$1 WHERE id=$2", int(time.time()), sub_id)
                msg_obj = await event.get_message()
                new_text = msg_obj.text + f"\n\n🔴 **Status: Rejected by Admin**"
                await event.edit(new_text, buttons=None)
                try:
                    await client.send_message(sub['user_id'], f"❌ **Task Rejected**\n\nYour task submission has been rejected. Please ensure you visited the website correctly and submitted valid proof.")
                except:
                    pass
                await event.answer("❌ Rejected.", alert=True)
        return

    # --- Delete task callbacks (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("del_task_"):
        parts = data.split("_")
        if len(parts) != 4:
            await event.answer("❌ Invalid data.", alert=True)
            return
        task_id = int(parts[2])
        task_type = parts[3]
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        kb = [
            [Button.inline("✅ Yes, delete", f"confirm_del_task_{task_id}")],
            [Button.inline("❌ Cancel", b"cancel_del_task")]
        ]
        await event.edit(f"⚠️ Are you sure you want to delete this {task_type} task?", buttons=kb)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("confirm_del_task_"):
        try:
            task_id = int(data.split("_")[3])
        except (IndexError, ValueError):
            await event.answer("❌ Invalid task ID.", alert=True)
            return
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE tasks SET status='deleted' WHERE id=$1", task_id)
        await event.edit("✅ Task deleted successfully.")
        await event.answer("Deleted.", alert=True)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data == "cancel_del_task":
        await event.edit("❌ Deletion cancelled.")
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data == "cancel_edit_task":
        await event.edit("❌ Cancelled.")
        return

    # --- Confirm Add Media Task (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and (data.startswith("confirm_task_yes") or data.startswith("confirm_task_no")):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id not in admin_confirm:
            await event.answer("No pending task.", alert=True)
            return
        confirm_data = admin_confirm[user_id]
        if confirm_data['action'] != "add_media_task":
            await event.answer("Invalid action.", alert=True)
            return
        if data.endswith("_no"):
            del admin_confirm[user_id]
            await event.edit("❌ Task creation cancelled.")
            await event.answer("Cancelled.", alert=True)
            return
        task_data = confirm_data['task_data']
        async with db_pool.acquire() as conn:
            task_id = await conn.fetchval("INSERT INTO tasks (task_type, url, time_required, reward, task_limit, completed_count, proof_type, status) VALUES ('Media', $1, $2, $3, $4, 0, $5, 'active') RETURNING id",
                                          task_data['url'], task_data['time_required'], task_data['reward'], task_data['task_limit'], task_data['proof_type'])
        del admin_confirm[user_id]
        currency = (await get_settings())['currency']
        await send_task_notification(task_id, "Media", task_data['url'], task_data['reward'], task_data['task_limit'], proof_type=task_data['proof_type'], time_required=task_data['time_required'])
        await event.edit(f"✅ **Media Task Added Successfully!**\n\n"
                         f"🔗 URL: {task_data['url']}\n"
                         f"⏱️ Time: {task_data['time_required']} seconds\n"
                         f"💰 Reward: {task_data['reward']} {currency}\n"
                         f"👥 User Limit: {task_data['task_limit']}\n"
                         f"📎 Proof Type: {task_data['proof_type']}")
        await event.answer("Task added!", alert=True)
        return

    # --- Confirm Add Balance, Cut Balance, Set Bonus (only in admin mode) ---
    if is_user_admin and not admin_user_mode.get(user_id, True) and (data.startswith("confirm_add_balance_yes") or data.startswith("confirm_add_balance_no")):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id not in admin_confirm:
            await event.answer("No pending confirmation.", alert=True)
            return
        confirm_data = admin_confirm[user_id]
        if confirm_data['action'] != "add_balance":
            await event.answer("Invalid action.", alert=True)
            return
        if data.endswith("_no"):
            del admin_confirm[user_id]
            await event.edit("❌ Operation cancelled.")
            await event.answer("Cancelled.", alert=True)
            return
        target_user = confirm_data['user_id']
        amount = confirm_data['amount']
        currency = (await get_settings())['currency']
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, target_user)
        del admin_confirm[user_id]
        await event.edit(f"✅ Added {amount:.2f} {currency} to user {target_user} (Real Balance).")
        try:
            await client.send_message(target_user, f"🎁 **Admin added {amount:.2f} {currency} to your Real Balance!**")
        except:
            pass
        await event.answer("Done!", alert=True)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and (data.startswith("confirm_cut_balance_yes") or data.startswith("confirm_cut_balance_no")):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id not in admin_confirm:
            await event.answer("No pending confirmation.", alert=True)
            return
        confirm_data = admin_confirm[user_id]
        if confirm_data['action'] != "cut_balance":
            await event.answer("Invalid action.", alert=True)
            return
        if data.endswith("_no"):
            del admin_confirm[user_id]
            await event.edit("❌ Operation cancelled.")
            await event.answer("Cancelled.", alert=True)
            return
        target_user = confirm_data['user_id']
        amount = confirm_data['amount']
        currency = (await get_settings())['currency']
        async with db_pool.acquire() as conn:
            balance = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", target_user)
            if balance < amount:
                await event.edit(f"❌ Insufficient balance. User has {balance:.2f} {currency}.")
                del admin_confirm[user_id]
                return
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", amount, target_user)
        del admin_confirm[user_id]
        await event.edit(f"✅ Cut {amount:.2f} {currency} from user {target_user} (Real Balance).")
        try:
            await client.send_message(target_user, f"⚠️ **Admin deducted {amount:.2f} {currency} from your Real Balance.**")
        except:
            pass
        await event.answer("Done!", alert=True)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and (data.startswith("confirm_set_bonus_yes") or data.startswith("confirm_set_bonus_no")):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        if user_id not in admin_confirm:
            await event.answer("No pending confirmation.", alert=True)
            return
        confirm_data = admin_confirm[user_id]
        if confirm_data['action'] != "set_bonus":
            await event.answer("Invalid action.", alert=True)
            return
        if data.endswith("_no"):
            del admin_confirm[user_id]
            await event.edit("❌ Operation cancelled.")
            await event.answer("Cancelled.", alert=True)
            return
        ref_count = confirm_data['ref_count']
        bonus_amount = confirm_data['bonus_amount']
        currency = (await get_settings())['currency']
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE bonus_setting SET ref_count = $1, bonus_amount = $2 WHERE id = 1", ref_count, bonus_amount)
        del admin_confirm[user_id]
        await event.edit(f"✅ Bonus set: {bonus_amount:.2f} {currency} for {ref_count} referrals.\n\nUsers will get this bonus for every multiple of {ref_count} referrals.")
        await event.answer("Done!", alert=True)
        return

    # Delete channel (old - kept for safety) (only in admin mode)
    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("delch_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        ch_id = int(data.split("_")[1])
        async with db_pool.acquire() as conn:
            ch = await conn.fetchval("SELECT channel_username FROM required_channels WHERE id=$1", ch_id)
            if not ch:
                await event.answer("Channel not found.", alert=True)
                return
        kb = [
            [Button.inline("✅ Yes, delete", f"confirm_del_{ch_id}")],
            [Button.inline("❌ Cancel", b"cancel_del")]
        ]
        await event.edit(f"Are you sure you want to delete @{ch} from required channels?", buttons=kb)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data.startswith("confirm_del_"):
        if not is_user_admin:
            await event.answer("❌ Admin only.", alert=True)
            return
        ch_id = int(data.split("_")[2])
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM required_channels WHERE id=$1", ch_id)
        await event.edit("✅ Channel removed from join list.")
        async with db_pool.acquire() as conn:
            remaining = await conn.fetch("SELECT id, channel_username FROM required_channels")
        if remaining:
            rows = []
            row = []
            for cid, ch in remaining:
                row.append(Button.inline(f"Delete @{ch}", f"delch_{cid}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([Button.inline("🔙 Done", b"cancel_del")])
            await event.respond("Remaining channels:", buttons=rows)
        return

    if is_user_admin and not admin_user_mode.get(user_id, True) and data == "cancel_del":
        await event.edit("Operation cancelled.")
        return

    # Payment confirmation (only for admin)
    if data.startswith("p_"):
        if not await is_admin(user_id):
            await event.answer("❌ Admin only.", alert=True)
            return
        req_id = int(data.split("_")[1])
        async with db_pool.acquire() as conn:
            req = await conn.fetchrow("SELECT user_id, amount, fee_amount, net_amount, wallet, request_time FROM withdraw_requests WHERE id=$1 AND status='pending'", req_id)
            if not req:
                await event.answer("Request not found or already processed.", alert=True)
                return
            target_user, total, fee, net, wallet, req_time = req['user_id'], req['amount'], req['fee_amount'], req['net_amount'], req['wallet'], req['request_time']
            request_time_str = datetime.fromtimestamp(req_time).strftime("%d/%m/%Y %I:%M %p")
            settings = await get_settings()
            currency = settings['currency']

            await conn.execute("UPDATE withdraw_requests SET status='paid', paid_time=$1 WHERE id=$2", int(time.time()), req_id)
            await conn.execute("UPDATE users SET total_withdrawn = total_withdrawn + $1 WHERE user_id=$2", net, target_user)
            await conn.execute("UPDATE stats SET total_payout = total_payout + $1 WHERE id=1", net)

        msg_obj = await event.get_message()
        new_text = msg_obj.text + f"\n\nAdd transactions link\n🛡 Status: 100% Verified & Paid"
        await event.edit(new_text, buttons=None)

        success_text = (
            f"🎁 **Congratulations!**\n\n"
            f"✅ **Your withdrawal of {net:.2f} {currency} (after fee) has been approved and sent!**\n\n"
            f"🕒 **Your Withdraw Date & Time was:** {request_time_str}\n\n"
            f"💼 Check your wallet now."
        )
        try:
            await client.send_message(target_user, success_text)
            await event.answer("✅ Success! User notified.", alert=True)
        except:
            await event.answer("⚠️ Could not notify user.", alert=True)
        return

    # My referrals
    if data == "my_ref":
        async with db_pool.acquire() as conn:
            refs = await conn.fetch("SELECT username, user_id FROM users WHERE ref_by=$1", user_id)
        ref_list = "\n".join([f"👤 @{r['username']}" if r['username'] != "No Username" else f"👤 {r['user_id']}" for r in refs]) or "No referrals yet."
        msg = f"➡️ Your Total Referrals: {len(refs)}\n\n👨‍👨‍👦 Your Referred Users ⬇️\n\n{ref_list}"
        await event.edit(msg, buttons=[Button.inline("🔙 Back", b"back_inv")])
        return

    if data == "top_list":
        settings = await get_settings()
        currency = settings['currency']
        # Get top 25 users by total_ref descending, fetch name, username, total_ref, total_earned
        async with db_pool.acquire() as conn:
            tops = await conn.fetch("SELECT user_id, name, username, total_ref, total_earned FROM users ORDER BY total_ref DESC LIMIT 25")
        if not tops:
            await event.edit("❌ No users found.", buttons=[Button.inline("🔙 Back", b"back_inv")])
            return

        header = f"🏆 Top 25 Referral Leaders\n\n📊 Ranking by Referrals\n\n"
        lines = []
        for i, row in enumerate(tops, start=1):
            user_id = row['user_id']
            name = row['name']
            username = row['username']
            total_ref = row['total_ref']
            total_earned = row['total_earned']

            # Determine display name
            if username and username != "No Username":
                display = f"@{username}"
            elif name and name != "Unknown":
                display = name
            else:
                display = str(user_id)

            # Rank display
            if i == 1:
                rank_str = "🥇"
            elif i == 2:
                rank_str = "🥈"
            elif i == 3:
                rank_str = "🥉"
            else:
                rank_str = f"{i}."

            lines.append(f"{rank_str} {display} — {total_ref} refs · {total_earned:.2f} {currency}")

        top_msg = header + "\n".join(lines) + "\n\n📊 Rankings update in real-time. Keep inviting to climb the leaderboard! 🚀"
        await event.edit(top_msg, buttons=[Button.inline("🔙 Back", b"back_inv")])
        return

    if data == "back_inv":
        msg, kb = await get_invite_data(user_id, bot_obj.username)
        await event.edit(msg, buttons=kb)
        return

# --- Web server for Render health checks ---
async def health(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
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
