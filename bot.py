import os, asyncio, logging
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from aiohttp import web
import yt_dlp

# إعدادات بسيطة
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# قراءة المتغيرات
TOKEN = os.environ.get("BOT_TOKEN")
COOKIES = os.environ.get("COOKIES_TXT", "")

if not TOKEN:
    logger.error("BOT_TOKEN not set")
    exit(1)

# حفظ الكوكيز في ملف
if COOKIES:
    Path("/tmp/cookies.txt").write_text(COOKIES)
    logger.info("✅ Cookies loaded")

# مجلد مؤقت
Path("/tmp/dl").mkdir(exist_ok=True)

# أمر /start
async def start(update: Update, context):
    await update.message.reply_text("🚀 أرسل رابط يوتيوب للتحميل")

# تحميل الفيديو
async def download(update: Update, context):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ أرسل رابط صالح")
        return
    
    msg = await update.message.reply_text("⏳ جاري التحميل...")
    
    opts = {
        "outtmpl": "/tmp/dl/%(title)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
    }
    if COOKIES:
        opts["cookiefile"] = "/tmp/cookies.txt"
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # البحث عن الملف إذا كان امتداده مختلفاً
            if not Path(filename).exists():
                for f in Path("/tmp/dl").glob(f"{Path(filename).stem}.*"):
                    filename = str(f)
                    break
        
        # إرسال الفيديو
        with open(filename, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption=f"✅ {info.get('title', 'فيديو')[:100]}",
                read_timeout=120,
                write_timeout=120
            )
        Path(filename).unlink()
        await msg.delete()
        
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {str(e)[:200]}")

# إعداد webhook
async def main():
    # بناء التطبيق
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))
    
    # تهيئة مهمة
    await app.initialize()
    await app.start()
    
    # إعداد webhook
    port = int(os.environ.get("PORT", 8080))
    host = "0.0.0.0"
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/webhook"
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_webhook(webhook_url)
    logger.info(f"✅ Webhook: {webhook_url}")
    
    # خادم HTTP
    server = web.Application()
    server.router.add_post("/webhook", app.process_update)
    server.router.add_get("/", lambda r: web.Response(text="YAMD Alive"))
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"🚀 Server on port {port}")
    
    # البقاء حياً
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
