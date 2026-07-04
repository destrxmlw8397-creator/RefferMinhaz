import os
from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime

# ১. ডাটাবেজ এবং অ্যাপ কনফিগারেশন
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

engine = create_async_engine(DATABASE_URL, echo=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

app = FastAPI(title="RefferMinhaz API")

# ২. ডাটাবেজ মডেল (Announcement)
class Announcement(Base):
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

# ৩. SQLAdmin ভিউ কনফিগারেশন
# এখানেই মূল ভুলটি ছিল। `model=Announcement` ক্লাস ডিক্লেয়ারেশনের ভেতরেই পাস করতে হবে।
class AnnouncementAdmin(ModelView, model=Announcement):
    column_list = [Announcement.id, Announcement.title, Announcement.created_at]
    form_columns = [Announcement.title, Announcement.content]
    icon = "fa-solid fa-bullhorn"

# ৪. অ্যাডমিন প্যানেল ইনিশিয়ালাইজেশন
admin = Admin(app, engine)

# সঠিকভাবে তৈরি করা ভিউটি অ্যাডমিনে যুক্ত করা হলো
admin.add_view(AnnouncementAdmin) 

# ৫. বেসিক রাউটস (প্রয়োজনীয়তা অনুযায়ী)
@app.get("/")
async def root():
    return {"message": "Server is running successfully!"}

# ডাটাবেজ টেবিল তৈরি করার জন্য (যদি আগে থেকে তৈরি করা না থাকে)
@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
