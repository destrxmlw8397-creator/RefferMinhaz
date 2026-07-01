import os
import time
import hmac
import hashlib
import json
from urllib.parse import parse_qs
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg

app = FastAPI()

# CORS (শুধু আপনার ডোমেইনের জন্য)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://yourdomain.com"],  # আপনার ডোমেইন বসান
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not DATABASE_URL or not BOT_TOKEN:
    raise RuntimeError("Missing DATABASE_URL or BOT_TOKEN")

# ----- ডেটাবেস পুল -----
db_pool = None

async def get_db():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return db_pool

# ----- initData ভেরিফাই -----
def verify_init_data(init_data: str) -> dict:
    parsed = parse_qs(init_data)
    if not parsed:
        return None
    sorted_items = sorted([(k, v[0]) for k, v in parsed.items() if k != 'hash'])
    data_check_string = '\n'.join([f"{k}={v}" for k, v in sorted_items])
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    hmac_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if hmac_hash == parsed['hash'][0]:
        try:
            user_data = json.loads(parsed['user'][0])
            return user_data
        except:
            return None
    return None

# ----- মডেল -----
class TaskVerifyRequest(BaseModel):
    task_id: int
    init_data: str

# ----- এন্ডপয়েন্টস -----
@app.get("/api/tasks")
async def get_tasks(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user['id']

    pool = await get_db()
    async with pool.acquire() as conn:
        # ইউজারের বর্তমান ব্যালেন্স
        balance = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id) or 0.0
        # সব অ্যাকটিভ টাস্ক
        tasks = await conn.fetch("SELECT id, title, link, reward, task_type FROM tasks WHERE status='active'")
        # ইউজারের সম্পন্ন/পেন্ডিং টাস্ক
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

@app.post("/api/verify-task")
async def verify_task(req: TaskVerifyRequest):
    user = verify_init_data(req.init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user['id']
    task_id = req.task_id

    pool = await get_db()
    async with pool.acquire() as conn:
        # টাস্ক আছে?
        task = await conn.fetchrow("SELECT id, reward, task_type, link FROM tasks WHERE id=$1 AND status='active'", task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # ইতিমধ্যে সম্পন্ন?
        done = await conn.fetchval("SELECT id FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status IN ('completed','claimed')", user_id, task_id)
        if done:
            return {"status": "already_claimed"}

        # পেন্ডিং চেক
        pending_row = await conn.fetchrow("SELECT id, started_at FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status='pending'", user_id, task_id)

        if not pending_row:
            # নতুন পেন্ডিং তৈরি
            await conn.execute(
                "INSERT INTO user_tasks (user_id, task_id, status, started_at) VALUES ($1, $2, 'pending', $3)",
                user_id, task_id, int(time.time())
            )
            return {"status": "pending", "message": "Task started, wait 30 seconds then verify."}

        # পেন্ডিং রেকর্ড আছে – সময় চেক
        started = pending_row['started_at'] or 0
        elapsed = int(time.time()) - started
        if elapsed < 30:
            return {"status": "pending", "message": f"Please wait {30 - elapsed} more seconds."}

        # ---- টাস্ক ভেরিফাই ----
        # Telegram Channel টাইপের জন্য বট দিয়ে চেক
        if task['task_type'] == 'telegram_channel' and task['link']:
            # link থেকে @username বের করুন
            channel = task['link'].replace('@', '').strip()
            # এখানে আপনার Telethon client ব্যবহার করুন – ধরে নিচ্ছি global client আছে
            # নিচের অংশটি আপনার বটের সাথে সংযোগ স্থাপন করবে
            try:
                from telethon import TelegramClient
                # আপনার client যদি global থাকে তাহলে সেটি ব্যবহার করুন, অন্যথায় নতুন কানেকশন
                # আমরা ধরে নিচ্ছি আপনার বটে `client` নামে একটি অবজেক্ট আছে
                # এখানে শুধু ডেমো দিচ্ছি
                client = TelegramClient('session_name', os.getenv('API_ID'), os.getenv('API_HASH'))
                await client.start(bot_token=BOT_TOKEN)
                participant = await client.get_participant(channel, user_id)
                is_member = True
            except Exception:
                is_member = False
            if not is_member:
                return {"status": "failed", "message": "You are not a member of the channel."}
            # সদস্য হলে এগিয়ে যান
        # অন্য টাইপের জন্য (website/social) আমরা স্বয়ংক্রিয়ভাবে অ্যাপ্রুভ করি (কারণ ইউজার ইতিমধ্যে সাইট ভিজিট করেছে)

        # সফল – ব্যালেন্স আপডেট
        reward = task['reward']
        await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", reward, user_id)
        # user_tasks আপডেট
        await conn.execute("UPDATE user_tasks SET status='completed', completed_at=$1 WHERE id=$2", int(time.time()), pending_row['id'])

        return {"status": "success", "reward": reward}
