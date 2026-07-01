import os
import json
import hmac
import hashlib
from urllib.parse import parse_qs
from aiohttp import web
from telethon import Button

# এই ফাংশনগুলো bot.py থেকে কল হবে
async def serve_index(request):
    """Serve the Mini App HTML (with fallback)"""
    static_path = os.path.join(os.path.dirname(__file__), 'static', 'index.html')
    if os.path.exists(static_path):
        try:
            with open(static_path, 'r', encoding='utf-8') as f:
                return web.Response(text=f.read(), content_type='text/html')
        except Exception:
            pass

    # Fallback HTML (embedded)
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

def verify_init_data(init_data: str, bot_token: str) -> dict:
    parsed = parse_qs(init_data)
    data = {k: v[0] for k, v in parsed.items() if k != 'hash'}
    hash_str = parsed.get('hash', [''])[0]
    sorted_data = sorted(data.items())
    check_string = '\n'.join([f"{k}={v}" for k, v in sorted_data])
    secret = hashlib.sha256(bot_token.encode()).digest()
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
    bot_token = os.environ.get('BOT_TOKEN')
    try:
        user = verify_init_data(init_data, bot_token)
    except Exception:
        return web.json_response({'error': 'Invalid init data'}, status=403)
    user_id = user['id']
    db_pool = request.app['db_pool']
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
    bot_token = os.environ.get('BOT_TOKEN')
    try:
        user = verify_init_data(init_data, bot_token)
    except Exception:
        return web.json_response({'error': 'Invalid init data'}, status=403)
    user_id = user['id']
    try:
        data = await request.json()
        task_id = int(data.get('task_id'))
    except:
        return web.json_response({'error': 'Invalid request'}, status=400)
    
    db_pool = request.app['db_pool']
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
