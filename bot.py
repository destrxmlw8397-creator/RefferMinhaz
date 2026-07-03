import os
import time
import hmac
import hashlib
import json
from urllib.parse import parse_qs
from datetime import datetime
import pytz
import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError

# --- ENVIRONMENT VARIABLES ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not all([API_ID, API_HASH, BOT_TOKEN, DATABASE_URL]):
    raise ValueError("Missing required environment variables: API_ID, API_HASH, BOT_TOKEN, DATABASE_URL")

# --- Telegram Client (for channel membership verification) ---
client = TelegramClient('task_bot_session', API_ID, API_HASH)

# --- Database pool ---
db_pool = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return db_pool

# --- FastAPI App ---
app = FastAPI()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Serve index.html from root ---
@app.get("/")
async def read_index():
    return FileResponse("index.html")

# --- Pydantic Models ---
class TaskVerifyRequest(BaseModel):
    task_id: int
    init_data: str

# --- initData Verification ---
def verify_init_data(init_data: str) -> dict:
    """
    Verify Telegram WebApp initData and return user data if valid.
    """
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

    # Debug prints (visible in Render logs)
    print(f"🔍 data_check_string: {data_check_string}")

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

# --- API Endpoints ---
@app.get("/api/tasks")
async def get_tasks(init_data: str):
    """
    Get list of available tasks for the user.
    """
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user_id = user['id']

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Get user's balance
        balance = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id) or 0.0
        
        # Get all active tasks
        tasks = await conn.fetch(
            "SELECT id, title, reward, task_type, link FROM tasks WHERE status='active'"
        )
        
        # Get completed task IDs for this user
        completed = await conn.fetch(
            "SELECT task_id FROM user_tasks WHERE user_id=$1 AND status IN ('completed','claimed')",
            user_id
        )
        completed_ids = {row['task_id'] for row in completed}
        
        # Get pending task IDs for this user
        pending = await conn.fetch(
            "SELECT task_id FROM user_tasks WHERE user_id=$1 AND status='pending'",
            user_id
        )
        pending_ids = {row['task_id'] for row in pending}

    result = []
    for t in tasks:
        if t['id'] in completed_ids:
            status = 'claimed'
        elif t['id'] in pending_ids:
            status = 'pending'
        else:
            status = 'available'
        
        result.append({
            "id": t['id'],
            "title": t['title'],
            "reward": t['reward'],
            "type": t['task_type'],
            "link": t['link'],
            "status": status
        })
    
    return {"tasks": result, "balance": balance}

@app.post("/api/verify-task")
async def verify_task(req: TaskVerifyRequest):
    """
    Verify a task completion.
    """
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user_id = user['id']
    task_id = req.task_id

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Check if task exists and is active
        task = await conn.fetchrow(
            "SELECT id, reward, task_type, link FROM tasks WHERE id=$1 AND status='active'",
            task_id
        )
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Check if already completed
        completed = await conn.fetchval(
            "SELECT id FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status IN ('completed','claimed')",
            user_id, task_id
        )
        if completed:
            return {"status": "already_claimed", "message": "Task already completed."}

        # Check if already pending
        pending_row = await conn.fetchrow(
            "SELECT id, started_at FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status='pending'",
            user_id, task_id
        )

        if not pending_row:
            # New task start - create pending entry
            await conn.execute(
                "INSERT INTO user_tasks (user_id, task_id, status, started_at) VALUES ($1, $2, 'pending', $3)",
                user_id, task_id, int(time.time())
            )
            return {"status": "pending", "message": "Task started. Please wait 30 seconds and verify."}

        # Check if 30 seconds have passed
        started = pending_row['started_at'] or 0
        elapsed = int(time.time()) - started
        if elapsed < 30:
            remaining = 30 - elapsed
            return {"status": "pending", "message": f"Please wait {remaining} more seconds."}

        # --- Verification logic based on task type ---
        if task['task_type'] == 'telegram_channel' and task['link']:
            # Check if user is member of the channel
            channel = task['link'].replace('@', '').strip()
            try:
                # Ensure client is connected
                if not client.is_connected():
                    await client.connect()
                
                entity = await client.get_entity(f"@{channel}")
                await client(GetParticipantRequest(channel=entity, participant=user_id))
                is_member = True
            except UserNotParticipantError:
                is_member = False
            except Exception as e:
                print(f"Channel check error: {e}")
                is_member = False
            
            if not is_member:
                return {"status": "failed", "message": "You are not a member of the required channel."}
        
        # For other task types (website, social, etc.), auto-approve
        # (In production, you might want manual review)

        # --- Success: award reward ---
        reward = task['reward']
        await conn.execute(
            "UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2",
            reward, user_id
        )
        await conn.execute(
            "UPDATE user_tasks SET status='completed', completed_at=$1 WHERE id=$2",
            int(time.time()), pending_row['id']
        )

        return {"status": "success", "reward": reward, "message": f"You earned {reward} coins!"}

# --- Startup/Shutdown events ---
@app.on_event("startup")
async def startup():
    """Initialize database and Telegram client on startup."""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    await init_db()
    await client.start(bot_token=BOT_TOKEN)
    print("🤖 Telegram client started for task verification.")

@app.on_event("shutdown")
async def shutdown():
    """Close database pool and Telegram client."""
    if db_pool:
        await db_pool.close()
    await client.disconnect()
    print("🛑 Connections closed.")

async def init_db():
    """Create tables if they don't exist."""
    async with db_pool.acquire() as conn:
        # Users table (if not exists)
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
        
        # Tasks table
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                task_type TEXT,
                title TEXT,
                link TEXT,
                reward REAL,
                status TEXT DEFAULT 'active',
                time_required INTEGER DEFAULT 0
            )
        ''')
        
        # User tasks table
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
        
        # Insert a sample task if none exist
        count = await conn.fetchval("SELECT COUNT(*) FROM tasks")
        if count == 0:
            await conn.execute('''
                INSERT INTO tasks (task_type, title, link, reward, status)
                VALUES 
                ('telegram_channel', 'Join Our Channel', '@yourchannel', 10, 'active'),
                ('website', 'Visit Our Website', 'https://example.com', 5, 'active')
            ''')
            print("✅ Sample tasks inserted.")

# --- Run with Uvicorn ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
