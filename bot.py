import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from database import SessionLocal, Announcement

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_command(message: types.Message):
    await message.answer(
        "👋 স্বাগতম! আমাদের ওয়েব অ্যাপ দেখতে নিচের বাটনে ক্লিক করুন।",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="🌐 ওয়েব অ্যাপ খুলুন",
                    web_app=types.WebAppInfo(url=os.getenv("WEBAPP_URL"))
                )]
            ]
        )
    )

@dp.message(Command("announcements"))
async def announcements_command(message: types.Message):
    session = SessionLocal()
    announcements = session.query(Announcement).order_by(Announcement.created_at.desc()).limit(5).all()
    session.close()
    
    if not announcements:
        await message.answer("📭 কোনো ঘোষণা নেই।")
        return
    
    text = "📢 **সর্বশেষ ঘোষণা:**\n\n"
    for ann in announcements:
        text += f"🔹 *{ann.title}*\n{ann.content[:200]}...\n\n"
    await message.answer(text, parse_mode="Markdown")
