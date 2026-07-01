from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
import os
import time
import hmac
import hashlib
import json
from urllib.parse import parse_qs

app = FastAPI()

# CORS (শুধু আপনার ডোমেইনের জন্য)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://yourdomain.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ডেটাবেস কানেকশন পুল (আপনার existing pool ব্যবহার করতে পারেন)
DATABASE_URL = os.environ.get("DATABASE_URL")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

async def get_db():
    return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

# initData ভেরিফাই
def verify_init_data(init_data: str, bot_token: str) -> dict:
    """
    Telegram initData চেক করে। True হলে ডিকোডেড ডেটা ফেরত দেয়, না হলে None
    """
    parsed = parse_qs(init_data)
    # 'hash' বাদ দিয়ে বাকি key-value জোড়া lexicographically সাজাই
    sorted_items = sorted([(k, v[0]) for k, v in parsed.items() if k != 'hash'])
    data_check_string = '\n'.join([f"{k}={v}" for k, v in sorted_items])
    secret = hashlib.sha256(bot_token.encode()).digest()
    hmac_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if hmac_hash == parsed['hash'][0]:
        # user ডেটা বের করি
        user_data = json.loads(parsed['user'][0])
        return user_data
    return None

# মডেল
class TaskVerifyRequest(BaseModel):
    task_id: int
    init_data: str

# টাস্ক লিস্ট এন্ডপয়েন্ট
@app.get("/api/tasks")
async def get_tasks(init_data: str):
    user = verify_init_data(init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid init data")
    user_id = user['id']

    pool = await get_db()
    async with pool.acquire() as conn:
        # সব অ্যাকটিভ টাস্ক
        tasks = await conn.fetch("SELECT id, title, link, reward, task_type FROM tasks WHERE status='active'")
        # ইউজারের সম্পন্ন টাস্কের আইডি
        done = await conn.fetch("SELECT task_id FROM user_tasks WHERE user_id=$1 AND status IN ('completed','claimed')", user_id)
        done_ids = {row['task_id'] for row in done}
        # ইউজারের pending টাস্ক (যেগুলো স্টার্ট করা হয়েছে কিন্তু কমপ্লিট হয়নি)
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
    return {"tasks": result}

# টাস্ক ভেরিফাই এন্ডপয়েন্ট
@app.post("/api/verify-task")
async def verify_task(req: TaskVerifyRequest):
    user = verify_init_data(req.init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid init data")
    user_id = user['id']
    task_id = req.task_id

    pool = await get_db()
    async with pool.acquire() as conn:
        # টাস্ক আছে কি না
        task = await conn.fetchrow("SELECT id, reward, task_type, link FROM tasks WHERE id=$1 AND status='active'", task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # ইতিমধ্যে সম্পন্ন?
        done = await conn.fetchval("SELECT id FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status IN ('completed','claimed')", user_id, task_id)
        if done:
            return {"status": "already_claimed"}

        # pending চেক
        pending = await conn.fetchval("SELECT id FROM user_tasks WHERE user_id=$1 AND task_id=$2 AND status='pending'", user_id, task_id)
        if not pending:
            # নতুন pending এন্ট্রি তৈরি করি
            await conn.execute(
                "INSERT INTO user_tasks (user_id, task_id, status, started_at) VALUES ($1, $2, 'pending', $3)",
                user_id, task_id, int(time.time())
            )
            # pending সময় শেষে চেক করার জন্য আলাদা মেকানিজম লাগতে পারে (যেমন ব্যাকগ্রাউন্ড টাস্ক)
            # আমরা এখানে সিম্পল: ক্লায়েন্ট কিছু সময় পর আবার ভেরিফাই করবে
            return {"status": "pending", "message": "Task started, please wait for verification."}
        else:
            # pending টাস্ক ইতিমধ্যে আছে – কিন্তু আমরা চেক করব যে সময় পেরিয়েছে কি না
            start_time = await conn.fetchval("SELECT started_at FROM user_tasks WHERE id=$1", pending)
            if not start_time:
                start_time = 0
            elapsed = int(time.time()) - start_time
            # ধরি ৩০ সেকেন্ড অপেক্ষা করতে হবে (ক্লায়েন্ট টাইমার রেখেছে)
            if elapsed < 30:
                return {"status": "pending", "message": f"Please wait {30 - elapsed} more seconds."}

            # এখানে টাস্ক টাইপ অনুযায়ী চেক করা যায়। Telegram Channel হলে বট দিয়ে চেক করুন।
            if task['task_type'] == 'telegram_channel' and task['link']:
                # link থেকে channel username বের করুন (ধরে নিচ্ছি link = @channelusername)
                channel = task['link'].replace('@', '').strip()
                try:
                    # আপনার Telethon ক্লায়েন্ট ব্যবহার করুন (আমরা এখানে কল্পনা করছি)
                    from telethon import TelegramClient
                    # আপনার existing client অবজেক্টটি ইম্পোর্ট করুন
                    client = TelegramClient(...)
                    await client.start(bot_token=BOT_TOKEN)
                    participant = await client.get_participant(channel, user_id)
                    is_member = True
                except Exception:
                    is_member = False
                if not is_member:
                    return {"status": "failed", "message": "You are not a member of the channel."}
            # অন্য টাইপের জন্য (website/social) আমরা স্বয়ংক্রিয়ভাবে অ্যাপ্রুভ করে দিতে পারি
            # কারণ ইউজার ক্লিক করলেই হয়তো কাজ শেষ

            # সফল হলে ব্যালেন্স আপডেট ও স্ট্যাটাস কমপ্লিট
            reward = task['reward']
            # আপনার users টেবিলে balance আপডেট করুন
            await conn.execute("UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE user_id=$2", reward, user_id)
            # user_tasks আপডেট
            await conn.execute("UPDATE user_tasks SET status='completed', completed_at=$1 WHERE id=$2", int(time.time()), pending)
            return {"status": "success", "reward": reward}
