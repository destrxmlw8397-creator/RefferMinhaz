import os
from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse
from starlette_admin.contrib.sqla import Admin, ModelView
from sqlalchemy.orm import Session
from database import SessionLocal, Announcement, BotConfig
from bot import dp, bot

app = FastAPI(title="Telegram Bot WebApp")

# ========== অ্যাডমিন প্যানেল ==========
admin = Admin(
    engine=SessionLocal().get_bind(),
    title="বট অ্যাডমিন প্যানেল",
    base_url="/admin",
)

class AnnouncementAdmin(ModelView):
    model = Announcement          # ← মডেল এখানে সংযুক্ত করুন
    fields = ["title", "content"]
    search_fields = ["title", "content"]
    ordering = ["-created_at"]

class BotConfigAdmin(ModelView):
    model = BotConfig
    fields = ["key", "value"]

admin.register(AnnouncementAdmin)   # ← শুধু অ্যাডমিন ক্লাস দিতে হবে
admin.register(BotConfigAdmin)
admin.mount_to(app)

# ========== ডাটাবেস ডিপেন্ডেন্সি ==========
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ========== ওয়েব অ্যাপ (পাবলিক ভিউ) ==========
@app.get("/", response_class=HTMLResponse)
async def webapp_home(db: Session = Depends(get_db)):
    announcements = db.query(Announcement).order_by(Announcement.created_at.desc()).limit(10).all()
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>📢 ঘোষণা বোর্ড</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 20px auto; padding: 0 15px; background: #f5f5f5; }
            .card { background: white; border-radius: 12px; padding: 20px; margin-bottom: 15px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            .title { font-size: 20px; font-weight: bold; color: #333; }
            .date { color: #888; font-size: 14px; }
            .content { margin-top: 10px; line-height: 1.6; }
            .empty { text-align: center; color: #888; padding: 40px; }
            h1 { text-align: center; color: #2c3e50; }
        </style>
    </head>
    <body>
        <h1>📢 ঘোষণা বোর্ড</h1>
    """
    
    if announcements:
        for ann in announcements:
            html += f"""
            <div class="card">
                <div class="title">{ann.title}</div>
                <div class="date">{ann.created_at.strftime('%d %b %Y, %I:%M %p')}</div>
                <div class="content">{ann.content}</div>
            </div>
            """
    else:
        html += '<div class="empty">📭 এখনো কোনো ঘোষণা নেই</div>'
    
    html += """
    </body>
    </html>
    """
    return html

# ========== ওয়েবহুক এন্ডপয়েন্ট ==========
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_update(bot, update)
    return {"ok": True}

# ========== বুটস্ট্র্যাপ ==========
@app.on_event("startup")
async def startup():
    webhook_url = os.getenv("RENDER_EXTERNAL_URL") + "/webhook"
    await bot.set_webhook(webhook_url)
    print(f"✅ Webhook set to: {webhook_url}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
