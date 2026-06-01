import os, asyncio, logging
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from aiohttp import web
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN missing")
    exit(1)

COOKIES_TXT = os.environ.get("COOKIES_TXT", "")
if COOKIES_TXT:
    with open("/tmp/cookies.txt", "w") as f:
        f.write(COOKIES_TXT)
    logger.info("Cookies loaded")

DOWNLOAD_DIR = Path("/tmp/yamd")
DOWNLOAD_DIR.mkdir(exist_ok=True)

async def start(update: Update, context):
    await update.message.reply_text("أرسل رابط يوتيوب للتحميل")

async def download(update: Update, context):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("أرسل رابطاً صالحاً")
        return
    msg = await update.message.reply_text("⏳ جار التحميل...")
    opts = {
        'outtmpl': '/tmp/%(title)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
    }
    if COOKIES_TXT and Path("/tmp/cookies.txt").exists():
        opts['cookiefile'] = "/tmp/cookies.txt"
        logger.info("Using cookies")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        if not Path(filename).exists():
            # البحث عن ملف بامتداد مختلف
            for f in Path("/tmp").glob(f"{Path(filename).stem}.*"):
                filename = str(f)
                break
        await update.message.reply_video(video=open(filename, 'rb'), caption=f"✅ تم التحميل: {info.get('title','')[:100]}")
        Path(filename).unlink()
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {str(e)[:200]}")

async def setup_webhook():
    # إنشاء التطبيق
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))

    # تهيئة التطبيق (هذا هو الإصلاح)
    await application.initialize()
    await application.start()

    # إعداد خادم الويب
    port = int(os.environ.get("PORT", 8080))
    host = "0.0.0.0"
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/webhook"
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

    server = web.Application()
    server.router.add_post("/webhook", application.process_update)
    server.router.add_get("/", lambda r: web.Response(text="YAMD is running"))
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"HTTP server running on port {port}")
    # الانتظار إلى الأبد
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(setup_webhook())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
