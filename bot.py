# ... (الكود كما هو بالضبط مع إضافة السطرين قبل loop.run_until_complete) ...

async def main():
    try:
        logger.info(f"ffmpeg: {'✅' if HAS_FFMPEG else '❌'}")
        logger.info(f"🍪 Cookies: {'✅ موجود' if COOKIE_FILE else '❌ غير موجود'}")
        logger.info(f"🚀 {BOT_NAME} يبدأ التشغيل...")

        app = (Application.builder().token(BOT_TOKEN)
               .connect_timeout(30).read_timeout(600).write_timeout(600)
               .pool_timeout(120).concurrent_updates(True).build())

        # 🔁 إضافة هذا السطر قبل أي شيء:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted (if any)")

        app.add_handler(CommandHandler("start", cmd_start))
        # ... باقي الهاندلرز ...
# ...
