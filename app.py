import os
import re
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from sqladmin import Admin, ModelView
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from datetime import datetime
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================
# ১. কনফিগারেশন এবং ডাটাবেজ সেটআপ
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN") # Render Environment Variable এ সেট করবেন
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789")) # আপনার টেলিগ্রাম ইউজার আইডি

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

app = FastAPI(title="RefferMinhaz System")
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# ==========================================
# ২. ডাটাবেজ মডেলস (Models)
# ==========================================
class TGTask(Base):
    __tablename__ = "tg_tasks"

    id = Column(Integer, primary_key=True, index=True)
    channel_username = Column(String(255), nullable=False) # e.g., @mychannel
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
# ৪. টেলিগ্রাম বট লজিক (অ্যাডমিন পার্ট)
# ==========================================
# সাময়িক ডেটা স্টোর করার জন্য মেমোরি ডিকশনারি
admin_states = {}

def is_admin(chat_id):
    return chat_id == ADMIN_ID

@bot.message_handler(commands=['admin'])
def send_admin_keyboard(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "❌ আপনি এই বটের অ্যাডমিন নন।")
        return
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("➕ Add TG Task"), KeyboardButton("➕ Add Media Task"))
    bot.send_message(message.chat.id, "👋 অ্যাডমিন প্যানেলে স্বাগতম! নিচের যেকোনো একটি অপশন বেছে নিন:", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == "➕ Add TG Task")
def ask_channel_username(message):
    if not is_admin(message.chat.id): return
    
    admin_states[message.chat.id] = {"state": "AWAITING_USERNAME"}
    bot.send_message(message.chat.id, "📢 দয়া করে চ্যানেলের ইউজারনেম (যেমন: `@channelname`) অথবা ইনভাইট লিঙ্কটি দিন:")

@bot.message_handler(func=lambda message: admin_states.get(message.chat.id, {}).get("state") == "AWAITING_USERNAME")
def handle_channel_username(message):
    if not is_admin(message.chat.id): return
    
    username = message.text.strip()
    if not username.startswith("@") and "t.me/" not in username:
        bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট! ইউজারনেমটি অবশ্যই `@` দিয়ে শুরু হতে হবে অথবা একটি বৈধ টেলিগ্রাম লিঙ্ক হতে হবে। আবার চেষ্টা করুন:")
        return
    
    # ইউজারনেম এক্সট্রাক্ট করা (যদি লিঙ্ক দেয়)
    if "t.me/" in username:
        parsed = username.split("t.me/")[-1].replace("+", "")
        username = f"@{parsed}" if "/" not in parsed else username

    admin_states[message.chat.id]["username"] = username
    admin_states[message.chat.id]["state"] = "AWAITING_VERIFICATION"
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔄 Check Admin Status", callback_data="check_admin"),
               InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
    
    bot.send_message(message.chat.id, f"চ্যানেল: {username}\n\n⚠️ এই চ্যানেলে বটকে অবশ্যই 'Admin' হিসেবে যুক্ত করতে হবে। আপনি কি বটকে এডমিন করেছেন?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["check_admin", "back_to_admin", "confirm_task", "cancel_task"])
def callback_handler(call):
    chat_id = call.message.chat.id
    state_data = admin_states.get(chat_id, {})

    if call.data == "back_to_admin":
        admin_states.pop(chat_id, None)
        bot.delete_message(chat_id, call.message.message_id)
        send_admin_keyboard(call.message)
        return

    if call.data == "check_admin":
        username = state_data.get("username")
        try:
            # বট নিজে ঐ চ্যানেলের এডমিন কিনা চেক করার চেষ্টা করবে
            member = bot.get_chat_member(username, bot.get_me().id)
            if member.status in ['administrator', 'creator']:
                state_data["state"] = "AWAITING_REWARD"
                bot.edit_message_text(f"✅ বট সফলভাবে এডমিন হিসেবে ভেরিফাইড হয়েছে!\n\n💰 এই টাস্কটি কমপ্লিট করলে ইউজার কত রিওয়ার্ড পয়েন্ট পাবে? (শুধুমাত্র সংখ্যা লিখুন, যেমন: `50`):", chat_id, call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "❌ বট এখনো এই চ্যানেলের এডমিন নয়!", show_alert=True)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ চ্যানেলটি খুঁজে পাওয়া যায়নি বা বট এডমিন নয়। নিশ্চিত হয়ে আবার চেক করুন।", show_alert=True)

    elif call.data == "confirm_task":
        # ডাটাবেজে সেভ করার জন্য উইন্ডো ওপেন (FastAPI এর মাধ্যমে রানিং থাকায় সিঙ্ক ওয়েতে টাস্ক পুশ)
        username = state_data.get("username")
        reward = state_data.get("reward")
        
        # সিঙ্ক ইভেন্ট লুপের বাইরে থাকায় আলাদা মেকানিজম বা ডিরেক্ট এঞ্জিন এক্সিকিউশন
        import asyncio
        async def save_task():
            async with async_session() as session:
                async with session.begin():
                    new_task = TGTask(channel_username=username, reward_points=int(reward))
                    session.add(new_task)
                await session.commit()
        
        asyncio.run(save_task())
        bot.edit_message_text(f"🎉 টাস্কটি সফলভাবে যোগ করা হয়েছে!\n📢 চ্যানেল: {username}\n💰 রিওয়ার্ড: {reward} পয়েন্ট", chat_id, call.message.message_id)
        admin_states.pop(chat_id, None)

    elif call.data == "cancel_task":
        admin_states.pop(chat_id, None)
        bot.edit_message_text("❌ টাস্ক বাতিল করা হয়েছে।", chat_id, call.message.message_id)

@bot.message_handler(func=lambda message: admin_states.get(message.chat.id, {}).get("state") == "AWAITING_REWARD")
def handle_reward_input(message):
    if not is_admin(message.chat.id): return
    
    text = message.text.strip()
    if not text.isdigit():
        bot.send_message(message.chat.id, "❌ দয়া করে একটি সঠিক সংখ্যা দিন (যেমন: 50, 100):")
        return
    
    admin_states[message.chat.id]["reward"] = int(text)
    admin_states[message.chat.id]["state"] = "CONFIRMATION"
    
    username = admin_states[message.chat.id]["username"]
    reward = admin_states[message.chat.id]["reward"]
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Confirm & Save", callback_data="confirm_task"),
               InlineKeyboardButton("❌ Cancel", callback_data="cancel_task"))
    
    bot.send_message(message.chat.id, f"📝 **টাস্ক সামারি:**\n\n📢 চ্যানেল: {username}\n💰 রিওয়ার্ড: {reward} পয়েন্ট\n\nআপনি কি এটি সেভ করতে চান?", reply_markup=markup, parse_mode="Markdown")

# ==========================================
# ৫. WebApp এবং API রাউটস (User Part)
# ==========================================
@app.post("/webhook")
async def telegram_webhook(request: Request):
    json_str = await request.json()
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return {"status": "ok"}

# ইউজারদের জন্য ডেমো WebApp ইন্টারফেস (টাস্ক লিস্ট ও ক্লায়েন্ট সাইড ভেরিফিকেশন)
@app.get("/webapp", response_class=HTMLResponse)
async def webapp_ui(user_id: int = 123456):
    # ডাটাবেজ থেকে সমস্ত অ্যাক্টিভ টেলিগ্রাম টাস্ক তুলে আনা
    import asyncio
    async def get_tasks():
        async with async_session() as session:
            from sqlalchemy import select
            result = await session.execute(select(TGTask))
            return result.scalars().all()
    
    tasks = await asyncio.run(get_tasks())
    
    tasks_html = ""
    for task in tasks:
        # টেলিগ্রাম লিঙ্ক জেনারেট করা
        clean_name = task.channel_username.replace("@", "")
        link = f"https://t.me/{clean_name}"
        
        tasks_html += f"""
        <div style="border: 1px solid #ccc; padding: 15px; margin: 10px 0; border-radius: 8px; background: #fff;">
            <h4>📢 Join Channel: {task.channel_username}</h4>
            <p>💰 Reward: <b>{task.reward_points} Points</b></p>
            <a href="{link}" target="_blank" onclick="document.getElementById('btn-{task.id}').disabled=false;" 
               style="background: #0088cc; color: #fff; padding: 8px 15px; text-decoration: none; border-radius: 5px; display: inline-block;">
               👉 Click to Join
            </a>
            <button id="btn-{task.id}" disabled onclick="claimReward({task.id}, {user_id})"
               style="background: #28a745; color: #fff; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; margin-left: 10px;">
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
        <body style="font-family: Arial, sans-serif; background: #f4f4f9; padding: 20px;">
            <h2>🌟 Telegram Tasks</h2>
            <div id="tasks-container">{tasks_html if tasks_html else "<p>No active tasks available.</p>"}</div>
            
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
        # ১. টাস্ক ইনফো নেওয়া
        task_res = await session.execute(select(TGTask).where(TGTask.id == task_id))
        task = task_res.scalar_one_or_none()
        if not task:
            return {"success": False, "message": "Task not found!"}

        # ২. অলরেডি কমপ্লিট করেছে কিনা চেক
        prog_res = await session.execute(select(UserProgress).where(UserProgress.user_id == user_id, UserProgress.task_id == task_id))
        progress = prog_res.scalar_one_or_none()
        if progress and progress.completed:
            return {"success": False, "message": "You have already claimed this reward!"}

        # ৩. টেলিগ্রাম এপিআই দিয়ে রিয়েল-টাইম চেক করা (ইউজার সত্যি চ্যানেলে আছে কিনা)
        try:
            member = bot.get_chat_member(task.channel_username, user_id)
            if member.status in ['member', 'administrator', 'creator']:
                # ডাটাবেজে এন্ট্রি করা এবং পয়েন্ট যোগ করার লজিক (এখানে Progress টেবিলে সেভ হচ্ছে)
                if not progress:
                    progress = UserProgress(user_id=user_id, task_id=task_id, completed=True)
                    session.add(progress)
                else:
                    progress.completed = True
                
                await session.commit()
                return {"success": True, "message": f"Success! {task.reward_points} points added to your balance."}
            else:
                return {"success": False, "message": "You haven't joined the channel yet!"}
        except Exception:
            return {"success": False, "message": "Could not verify your membership. Make sure you joined!"}

@app.get("/")
async def root():
    return {"message": "Server is running! WebApp is available at /webapp"}

@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
