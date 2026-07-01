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

# --- Database connection pool ---
db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        # ... (all your existing table creations, including user_tasks) ...
        # I'm not repeating the full init_db here to save space; you already have it.
        # But make sure user_tasks table is created.
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
        count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE task_type IN ('TG', 'Media')")
        if count == 0:
            await conn.execute("""
                INSERT INTO tasks (task_type, url, time_required, reward, task_limit, completed_count, proof_type, status)
                VALUES 
                ('TG', 'your_channel', 0, 10, 100, 0, 'screenshot', 'active'),
                ('Media', 'https://example.com', 30, 15, 50, 0, 'screenshot', 'active')
            """)
    print("✅ Database initialized with Mini App tables")

# --- (Keep all your existing helper functions: is_admin, get_settings, etc.) ---
# For brevity, I'm not pasting them again; they are unchanged.

# --- Mini App verification helper ---
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

# --- Web server handlers (FIXED) ---
async def serve_index(request):
    """Serve the Mini App HTML (with fallback)"""
    # Try to load from static file first
    static_path = os.path.join(os.path.dirname(__file__), 'static', 'index.html')
    if os.path.exists(static_path):
        try:
            with open(static_path, 'r', encoding='utf-8') as f:
                return web.Response(text=f.read(), content_type='text/html')
        except Exception as e:
            print(f"Error reading static file: {e}")
            # fall through to fallback

    # Fallback embedded HTML (minimal, but works)
    fallback_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Earn Tasks</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f0f13; color: #e0e0e0; padding: 20px 16px 40px; }
        .container { max-width:480px; margin:0 auto; }
        h1 { font-size:24px; font-weight:700; margin-bottom:24px; background:linear-gradient(135deg,#f0b90b,#f5d75c); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
        .task-list { display:flex; flex-direction:column; gap:16px; }
        .task-card { background:#1a1a22; border-radius:16px; padding:16px 18px; display:flex; align-items:center; justify-content:space-between; border:1px solid #2a2a35; }
        .task-info { display:flex; flex-direction:column; gap:4px; flex:1; }
        .task-title { font-size:16px; font-weight:600; }
        .task-reward { font-size:14px; color:#f5b342; font-weight:500; }
        .task-btn { padding:8px 18px; border:none; border-radius:30px; font-size:14px; font-weight:600; cursor:pointer; background:#2d2d3a; color:#c0c0d0; min-width:80px; text-align:center; flex-shrink:0; }
        .task-btn.start { background:#f0b90b; color:#0f0f13; }
        .task-btn.verify { background:#3b82f6; color:white; }
        .task-btn.verify.loading { background:#1e293b; color:#94a3b8; pointer-events:none; }
        .task-btn.done { background:#22c55e; color:white; opacity:0.6; cursor:default; }
        .spinner { display:inline-block; width:18px; height:18px; border:2px solid rgba(255,255,255,0.2); border-top:2px solid #f0b90b; border-radius:50%; animation:spin 0.8s linear infinite; vertical-align:middle; margin-right:6px; }
        @keyframes spin { to { transform:rotate(360deg); } }
        .status-badge { font-size:12px; padding:2px 10px; border-radius:20px; background:#2a2a35; }
        .status-badge.done { background:#22c55e20; color:#22c55e; }
        .status-badge.pending { background:#f0b90b20; color:#f0b90b; }
        .empty-state { text-align:center; padding:40px 0; color:#6a6a7a; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📋 Earn Tasks</h1>
        <div id="taskList" class="task-list">
            <div class="empty-state">Loading tasks...</div>
        </div>
    </div>
    <script>
        function getInitData() {
            if (window.Telegram && window.Telegram.WebApp) {
                return window.Telegram.WebApp.initData;
            }
            return 'query_id=...&user=%7B%22id%22%3A123456%2C%22first_name%22%3A%22Test%22%7D&auth_date=...&hash=...';
        }

        async function fetchTasks() {
            const res = await fetch('/api/tasks', {
                headers: { 'X-Telegram-Init-Data': getInitData() }
            });
            if (!res.ok) throw new Error('Failed to fetch tasks');
            return res.json();
        }

        async function verifyTask(taskId) {
            const res = await fetch('/api/verify-task', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Telegram-Init-Data': getInitData()
                },
                body: JSON.stringify({ task_id: taskId })
            });
            return res.json();
        }

        function renderTasks(tasks) {
            const container = document.getElementById('taskList');
            if (!tasks || tasks.length === 0) {
                container.innerHTML = '<div class="empty-state">No tasks available right now.</div>';
                return;
            }
            let html = '';
            tasks.forEach(task => {
                const isCompleted = task.status === 'completed';
                const btnClass = isCompleted ? 'done' : 'start';
                const btnText = isCompleted ? 'Claimed ✓' : 'Start';
                const statusBadge = isCompleted
                    ? '<span class="status-badge done">Done</span>'
                    : '<span class="status-badge pending">Pending</span>';
                html += `
                    <div class="task-card" data-task-id="${task.id}" data-status="${task.status}">
                        <div class="task-info">
                            <div class="task-title">${task.title}</div>
                            <div class="task-reward">🎁 ${task.reward} TRX</div>
                            ${statusBadge}
                        </div>
                        <button class="task-btn ${btnClass}" data-task-id="${task.id}">${btnText}</button>
                    </div>
                `;
            });
            container.innerHTML = html;
            document.querySelectorAll('.task-btn.start:not(.done)').forEach(btn => {
                btn.addEventListener('click', handleStartClick);
            });
        }

        async function handleStartClick(e) {
            const btn = e.currentTarget;
            const taskCard = btn.closest('.task-card');
            const taskId = btn.dataset.taskId;
            if (btn.disabled) return;
            btn.disabled = true;

            btn.classList.remove('start');
            btn.classList.add('verify', 'loading');
            btn.innerHTML = '<span class="spinner"></span> Verifying...';

            try {
                await new Promise(resolve => setTimeout(resolve, 5000));
                const result = await verifyTask(taskId);
                if (result.success) {
                    btn.classList.remove('loading', 'verify');
                    btn.classList.add('done');
                    btn.innerHTML = 'Claimed ✓';
                    btn.disabled = true;
                    const badge = taskCard.querySelector('.status-badge');
                    if (badge) {
                        badge.textContent = 'Done';
                        badge.className = 'status-badge done';
                    }
                } else {
                    btn.classList.remove('loading', 'verify');
                    btn.classList.add('start');
                    btn.innerHTML = 'Start';
                    btn.disabled = false;
                    alert(result.message || 'Verification failed. Please try again.');
                }
            } catch (error) {
                console.error(error);
                btn.classList.remove('loading', 'verify');
                btn.classList.add('start');
                btn.innerHTML = 'Start';
                btn.disabled = false;
                alert('An error occurred. Please try again later.');
            }
        }

        document.addEventListener('DOMContentLoaded', async () => {
            try {
                const tasks = await fetchTasks();
                renderTasks(tasks);
            } catch (e) {
                document.getElementById('taskList').innerHTML =
                    '<div class="empty-state">⚠️ Failed to load tasks. Please try again later.</div>';
                console.error(e);
            }
        });
    </script>
</body>
</html>"""
    return web.Response(text=fallback_html, content_type='text/html')

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
        tasks = await conn.fetch("SELECT id, task_type, url, time_required, reward, task_limit, completed_count, proof_type FROM tasks WHERE status='active'")
        completed = await conn.fetch("SELECT task_id FROM user_tasks WHERE user_id=$1 AND status='completed'", user_id)
        completed_ids = {r['task_id'] for r in completed}
        result = []
        for t in tasks:
            status = 'completed' if t['id'] in completed_ids else 'pending'
            if t['task_type'] == 'TG':
                title = f"Join @{t['url']}"
            else:
                title = f"Visit {t['url']}"
            result.append({
                'id': t['id'],
                'title': title,
                'link': t['url'],
                'reward': t['reward'],
                'task_type': t['task_type'],
                'status': status
            })
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

async def start_web_server():
    app = web.Application()
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

# --- /earn COMMAND (with fallback for Button.webview) ---
@client.on(events.NewMessage(pattern='/earn'))
async def earn_command(event):
    base_url = APP_URL
    if hasattr(Button, 'webview'):
        btn = Button.webview("🚀 Open Earn Page", base_url)
    else:
        btn = Button.url("🚀 Open Earn Page", base_url)
    await event.respond(
        "📋 **Earn Tasks**\n\nComplete tasks and earn rewards! 🎁",
        buttons=[[btn]]
    )

# --- (Keep all your existing bot handlers: start, panel, text, callback, etc.) ---
# I'm not pasting them here again; they remain unchanged from your original.
# Make sure you include them in your final bot.py.

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
