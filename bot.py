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

async def start(update, context):
    await update.message.reply_text("أرسل رابط يوتيوب")

async def download(update, context):
    url = update.message.text
    msg = await update.message.reply_text("جار التحميل...")
    opts = {
        'outtmpl': '/tmp/%(title)s.%(ext)s',
        'quiet': True,
    }
    if COOKIES_TXT and Path("/tmp/cookies.txt").exists():
        opts['cookiefile'] = "/tmp/cookies.txt"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        with open(filename, 'rb') as f:
            await update.message.reply_video(video=f, caption="✅ تم التحميل")
        Path(filename).unlink()
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")

async def webhook_setup(app):
    port = int(os.environ.get("PORT", 8080))
    host = "0.0.0.0"
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/webhook"
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

    server = web.Application()
    server.router.add_post("/webhook", app.process_update)
    server.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"HTTP server on port {port}")
    await asyncio.Event().wait()

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(webhook_setup(application))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

if __name__ == "__main__":
    main()
