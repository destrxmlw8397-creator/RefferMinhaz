import os
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from sqladmin import Admin, ModelView
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, DateTime, Boolean, func
from datetime import datetime

# Aiogram 3.x Imports
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, Update, WebAppInfo
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==========================================
# ১. কনফিগারেশন এবং ডাটাবেজ সেটআপ
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789")) # আপনার টেলিগ্রাম আইডি
WEBAPP_URL = "https://refferminhaz.onrender.com" # আপনার Render ডোমেইন URL

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

app = FastAPI(title="RefferMinhaz System")

# Aiogram Initialization
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# ২. ডাটাবেজ মডেলস (Models)
# ==========================================
class TGTask(Base):
    __tablename__ = "tg_tasks"
    id = Column(Integer, primary_key=True, index=True)
    channel_username = Column(String(255), nullable=False)
    reward_points = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserProgress(Base):
    __tablename__ = "user_progress"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    task_id = Column(Integer)
    completed = Column(Boolean, default=False)

# ==========================================
# ৩. SQLAdmin ভিউ (Dashboard)
# ==========================================
class TGTaskAdmin(ModelView, model=TGTask):
    column_list = [TGTask.id, TGTask.channel_username, TGTask.reward_points, TGTask.created_at]
    form_columns = [TGTask.channel_username, TGTask.reward_points]
    icon = "fa-solid fa-tasks"

admin = Admin(app, engine)
admin.add_view(TGTaskAdmin)

# ==========================================
# ৪. FSM (States) এবং টেলিগ্রাম বট লজিক (অ্যাডমিন পার্ট)
# ==========================================
class AdminStates(StatesGroup):
    awaiting_username = State()
    awaiting_reward = State()

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@dp.message(Command("admin"))
async def send_admin_keyboard(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.reply("❌ আপনি এই বটের অ্যাডমিন নন।")
        return
    kb = [[KeyboardButton(text="➕ Add TG Task")], [KeyboardButton(text="➕ Add Media Task")]]
    markup = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("👋 অ্যাডমিন প্যানেলে স্বাগতম! নিচের যেকোনো একটি অপশন বেছে নিন:", reply_markup=markup)

@dp.message(F.text == "➕ Add TG Task")
async def ask_channel_username(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminStates.awaiting_username)
    await message.answer("📢 দয়া করে চ্যানেলের ইউজারনেম (যেমন: `@channelname`) অথবা ইনভাইট লিঙ্কটি দিন:")

@dp.message(AdminStates.awaiting_username)
async def handle_channel_username(message: types.Message, state: FSMContext):
    username = message.text.strip()
    if not username.startswith("@") and "t.me/" not in username:
        await message.answer("❌ ভুল ফরম্যাট! আবার চেষ্টা করুন:")
        return
    if "t.me/" in username:
        parsed = username.split("t.me/")[-1].replace("+", "")
        username = f"@{parsed}" if "/" not in parsed else username

    await state.update_data(username=username)
    inline_kb = [[InlineKeyboardButton(text="🔄 Check Admin Status", callback_data="check_admin"), InlineKeyboardButton(text="🔙 Back", callback_data="back_to_admin")]]
    await message.answer(f"চ্যানেল: {username}\n\n⚠️ বটকে এডমিন করেছেন?", reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_kb))

@dp.callback_query(F.data == "check_admin")
async def check_admin_status(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    username = data.get("username")
    try:
        bot_info = await bot.get_me()
        member = await bot.get_chat_member(chat_id=username, user_id=bot_info.id)
        if member.status in ['administrator', 'creator']:
            await state.set_state(AdminStates.awaiting_reward)
            await call.message.edit_text(f"✅ বট সফলভাবে এডমিন ভেরিফাইড!\n\n💰 রিওয়ার্ড পয়েন্ট কত দিবেন? (শুধুমাত্র সংখ্যা লিখুন):")
        else:
            await call.answer("❌ বট এখনো এই চ্যানেলের এডমিন নয়!", show_alert=True)
    except Exception:
        await call.answer("❌ চ্যানেলটি খুঁজে পাওয়া যায়নি বা বট এডমিন নয়।", show_alert=True)

@dp.message(AdminStates.awaiting_reward)
async def handle_reward_input(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("❌ দয়া করে একটি সঠিক সংখ্যা দিন:")
        return
    await state.update_data(reward=int(text))
    data = await state.get_data()
    inline_kb = [[InlineKeyboardButton(text="✅ Confirm & Save", callback_data="confirm_task"), InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_task")]]
    await message.answer(f"📢 চ্যানেল: {data['username']}\n💰 রিওয়ার্ড: {data['reward']} পয়েন্ট\n\nসেভ করবেন?", reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_kb))

@dp.callback_query(F.data == "confirm_task")
async def confirm_task(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with async_session() as session:
        async with session.begin():
            session.add(TGTask(channel_username=data.get("username"), reward_points=data.get("reward")))
        await session.commit()
    await call.message.edit_text("🎉 টাস্কটি সফলভাবে যোগ করা হয়েছে!")
    await state.clear()

@dp.callback_query(F.data == "cancel_task")
async def cancel_task(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ টাস্ক বাতিল করা হয়েছে।")

# ==========================================
# 🆕 ৬. ইউজার মেনু লজিক (/start, Balance, Task)
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = [
        [KeyboardButton(text="💰 Balance"), KeyboardButton(text="🎯 Task")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(f"👋 স্বাগতম {message.from_user.first_name}!\nআমাদের বটের মাধ্যমে বিভিন্ন টাস্ক পূরণ করে সহজে কয়েন/পয়েন্ট আর্ন করুন।", reply_markup=markup)

@dp.message(F.text == "💰 Balance")
async def check_user_balance(message: types.Message):
    user_id = message.from_user.id
    async with async_session() as session:
        from sqlalchemy import select
        # ইউজারের কমপ্লিট করা টাস্কগুলোর পয়েন্ট যোগ করা হচ্ছে
        query = select(func.sum(TGTask.reward_points)).join(
            UserProgress, TGTask.id == UserProgress.task_id
        ).where(UserProgress.user_id == user_id, UserProgress.completed == True)
        
        result = await session.execute(query)
        total_balance = result.scalar() or 0
        
    await message.answer(f"👤 **ইউজার:** {message.from_user.first_name}\n💵 **আপনার মোট ব্যালেন্স:** {total_balance} পয়েন্ট")

@dp.message(F.text == "🎯 Task")
async def show_tasks_menu(message: types.Message):
    user_id = message.from_user.id
    # WebApp এ ডিরেক্ট ইউজার আইডি প্যারামিটার হিসেবে পাস করা হচ্ছে
    webapp_link = f"{WEBAPP_URL}/webapp?user_id={user_id}"
    
    inline_kb = [
        [InlineKeyboardButton(text="📢 TG Task (Open WebApp)", web_app=WebAppInfo(url=webapp_link))],
        [InlineKeyboardButton(text="🎬 Media Task", callback_data="media_task_clicked")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=inline_kb)
    await message.answer("👇 নিচে দেওয়া টাস্কগুলো পূরণ করে আর্নিং শুরু করুন:", reply_markup=markup)

@dp.callback_query(F.data == "media_task_clicked")
async def media_task_msg(call: types.CallbackQuery):
    await call.answer("⏳ Media Task বর্তমানে খালি আছে। খুব শীঘ্রই আসবে!", show_alert=True)

# ==========================================
# ৭. WebApp এবং API রাউটস (FastAPI)
# ==========================================
@app.post("/webhook")
async def telegram_webhook(request: Request):
    json_str = await request.json()
    update = Update.model_validate(json_str, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/webapp", response_class=HTMLResponse)
async def webapp_ui(user_id: int):
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(TGTask))
        tasks = result.scalars().all()
    
    tasks_html = ""
    for task in tasks:
        clean_name = task.channel_username.replace("@", "")
        link = f"https://t.me/{clean_name}"
        
        tasks_html += f"""
        <div style="border: 1px solid #eee; padding: 15px; margin: 12px 0; border-radius: 10px; background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <h4 style="margin: 0 0 5px 0; color: #333;">📢 Join: {task.channel_username}</h4>
            <p style="margin: 0 0 10px 0; color: #28a745; font-weight: bold;">💰 +{task.reward_points} Points</p>
            <a href="{link}" target="_blank" onclick="document.getElementById('btn-{task.id}').disabled=false;" 
               style="background: #0088cc; color: #fff; padding: 8px 16px; text-decoration: none; border-radius: 6px; display: inline-block; font-size: 14px;">
               👉 Join Channel
            </a>
            <button id="btn-{task.id}" disabled onclick="claimReward({task.id}, {user_id})"
               style="background: #28a745; color: #fff; padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; margin-left: 8px; font-size: 14px;">
               ✅ Joined (Claim)
            </button>
        </div>
        """

    return f"""
    <html>
        <head>
            <title>TG Tasks WebApp</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <script src="https://telegram.org/js/telegram-web-app.js"></script>
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #f9f9f9; padding: 15px; margin: 0;">
            <h3 style="color: #222; border-bottom: 2px solid #0088cc; padding-bottom: 8px;">🌟 Available Channels</h3>
            <div id="tasks-container">{tasks_html if tasks_html else "<p style='color:#777;'>No active tasks available right now.</p>"}</div>
            
            <script>
                const tg = window.Telegram.WebApp;
                tg.expand();

                async function claimReward(taskId, userId) {{
                    const response = await fetch(`/api/claim-reward?task_id=${{taskId}}&user_id=${{userId}}`, {{ method: 'POST' }});
                    const data = await response.json();
                    alert(data.message);
                    if (data.success) {{
                        document.getElementById(`btn-${{taskId}}`).innerText = "💰 Claimed";
                        document.getElementById(`btn-${{taskId}}`).disabled = true;
                        document.getElementById(`btn-${{taskId}}`).style.background = "#6c757d";
                    }}
                }}
            </script>
        </body>
    </html>
    """

@app.post("/api/claim-reward")
async def claim_reward(task_id: int, user_id: int):
    async with async_session() as session:
        from sqlalchemy import select
        
        task_res = await session.execute(select(TGTask).where(TGTask.id == task_id))
        task = task_res.scalar_one_or_none()
        if not task:
            return {"success": False, "message": "Task not found!"}

        prog_res = await session.execute(select(UserProgress).where(UserProgress.user_id == user_id, UserProgress.task_id == task_id))
        progress = prog_res.scalar_one_or_none()
        if progress and progress.completed:
            return {"success": False, "message": "You have already claimed this reward!"}

        try:
            member = await bot.get_chat_member(chat_id=task.channel_username, user_id=user_id)
            if member.status in ['member', 'administrator', 'creator']:
                if not progress:
                    progress = UserProgress(user_id=user_id, task_id=task_id, completed=True)
                    session.add(progress)
                else:
                    progress.completed = True
                await session.commit()
                return {"success": True, "message": f"Success! {task.reward_points} points added!"}
            else:
                return {"success": False, "message": "You haven't joined the channel yet!"}
        except Exception:
            return {"success": False, "message": "Could not verify membership. Ensure bot is admin in the channel!"}

@app.get("/")
async def root():
    return {"message": "Server running! WebApp at /webapp"}

@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
