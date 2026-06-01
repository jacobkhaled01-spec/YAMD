import os, time, asyncio, logging, sqlite3, shutil, subprocess, math, re
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
import yt_dlp
from aiohttp import web

BOT_NAME      = "⚡ YAMD - Ultra Speed Downloader"
BOT_FULL_NAME = "YAAQOB ALMAHAJERI MEDIA DOWNLOADER | ULTRA SPEED EDITION"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

MAX_SIZE   = 48 * 1024 * 1024
DL_DIR     = Path.home() / "yamd_dl"
DL_DIR.mkdir(exist_ok=True)
HAS_FFMPEG = bool(shutil.which("ffmpeg"))

QUALITY = {
    "144":   ("🐢 144p", "bestvideo[height<=144]+bestaudio/best[height<=144]/worst"),
    "240":   ("🐢 240p", "bestvideo[height<=240]+bestaudio/best[height<=240]"),
    "360":   ("🎥 360p", "bestvideo[height<=360]+bestaudio/best[height<=360]"),
    "480":   ("🎥 480p", "bestvideo[height<=480]+bestaudio/best[height<=480]"),
    "best":  ("✨ أفضل جودة", "bestvideo+bestaudio/best"),
    "audio": ("🔊 MP3",  "bestaudio/best"),
}

logging.basicConfig(level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("YAMD")
logger.setLevel(logging.INFO)
for _lib in ("httpx","httpcore","telegram","hpack","asyncio"):
    logging.getLogger(_lib).setLevel(logging.ERROR)

db = sqlite3.connect("yamd.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
_setup_db()

def db_user(u):
    now = datetime.now().isoformat()
    db.execute("INSERT INTO users(uid,username,fname,seen,active) VALUES(?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET active=excluded.active, username=COALESCE(excluded.username,username), fname=COALESCE(excluded.fname,fname)", (u.id, u.username or "", u.first_name or "", now, now))
    db.commit()

def db_log(uid, url, title, qkey, size, speed):
    db.execute("INSERT INTO downloads(uid,url,title,quality,size,speed,ts) VALUES(?,?,?,?,?,?,?)", (uid, url, title, qkey, size, round(speed,1), datetime.now().isoformat()))
    db.execute("UPDATE users SET cnt=cnt+1 WHERE uid=?", (uid,))
    db.commit()

def fmt_size(b):
    if b >= 1024*1024*1024: return f"{b/1024/1024/1024:.1f}GB"
    return f"{b/1024/1024:.1f}MB"

def fmt_speed(kbs):
    if kbs >= 1024: return f"{kbs/1024:.1f}MB/s"
    return f"{kbs:.0f}KB/s"

_edit_ts = defaultdict(float)
async def safe_edit(msg, text, markup=None):
    if time.time() - _edit_ts[msg.message_id] < 3: return
    try:
        await msg.edit_text(text, reply_markup=markup)
        _edit_ts[msg.message_id] = time.time()
    except: pass

def find_file(vid_id):
    cands = sorted([f for f in DL_DIR.glob(f"{vid_id}*") if f.is_file()], key=lambda f: f.stat().st_size, reverse=True)
    return str(cands[0]) if cands else None

def cleanup(vid_id):
    for f in DL_DIR.glob(f"{vid_id}*"):
        try: f.unlink()
        except: pass

def split_video(filepath, max_part_size=MAX_SIZE):
    if not HAS_FFMPEG: return None
    base = os.path.splitext(filepath)[0]
    part_pattern = f"{base}_part%03d.mp4"
    try:
        result = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", filepath], capture_output=True, text=True)
        duration = float(result.stdout.strip())
    except: return None
    if not duration: return None
    total_size = os.path.getsize(filepath)
    num_parts = math.ceil(total_size / max_part_size)
    segment_duration = duration / num_parts
    segment_duration = math.ceil(segment_duration) + 1
    cmd = ["ffmpeg", "-i", filepath, "-c", "copy", "-map", "0", "-f", "segment", "-segment_time", str(segment_duration), "-reset_timestamps", "1", "-segment_format", "mp4", part_pattern]
    subprocess.run(cmd, check=True, capture_output=True)
    parts = sorted(Path(base).parent.glob(f"{Path(filepath).stem}_part*.mp4"))
    return [str(p) for p in parts]

# الأوامر
async def cmd_start(update, ctx):
    db_user(update.effective_user)
    msg = (f"أهلاً بك في <b>{BOT_NAME}</b>! 🚀\n\n"
           "أرسل رابط الفيديو من أي منصة وسأقوم بالباقي.\n"
           "⚡ تجاوز ذكي لحظر يوتيوب\n"
           "/about للمزيد")
    if update.effective_user.id == ADMIN_ID: msg += "\n🛡️ /admin"
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_about(update, ctx):
    await update.message.reply_text(f"🌟 <b>{BOT_NAME}</b>\n<b>{BOT_FULL_NAME}</b>\n\nأسرع بوت تحميل مع تجاوز ذكي لحظر يوتيوب.", parse_mode="HTML")

async def on_link(update, ctx):
    db_user(update.effective_user)
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ أرسل رابطاً صالحاً.")
        return
    fmt = "bestvideo+bestaudio/best"
    status = await update.message.reply_text("⬇️ جارٍ التحميل...")
    asyncio.create_task(do_download(ctx, update.message.chat_id, url, fmt, "auto", status, update.effective_user.id))

async def do_download(ctx, chat_id, url, fmt, qkey, status_msg, uid):
    loop = asyncio.get_running_loop()
    t0 = time.time()
    vid_id = f"v{uid}{int(t0)}"
    spd = {"kbs":0.0,"bytes":0,"last_b":0,"last_t":t0}
    def hook(d):
        if d.get("status")!="downloading": return
        got = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        now = time.time()
        dt = now - spd["last_t"]
        if dt >= 1:
            spd["kbs"] = max(0, (got - spd["last_b"]) / dt / 1024)
            spd["last_b"] = got; spd["last_t"] = now
        spd["bytes"] = got
        kbs = spd["kbs"]
        pct = f"{got/total*100:.0f}%" if total else "…"
        eta = "?"
        if kbs>0 and total:
            s = (total - got) / 1024 / kbs
            eta = f"{int(s//60)}د{int(s%60)}ث" if s>=60 else f"{int(s)}ث"
        asyncio.run_coroutine_threadsafe(
            safe_edit(status_msg, f"⬇️ جارٍ التحميل...\n⚡ {fmt_speed(kbs)}\n📦 {fmt_size(got)}/{fmt_size(total)} [{pct}]  ETA: {eta}"),
            loop)

    is_pin = "pin.it" in url or "pinterest" in url.lower()
    is_youtube = "youtube.com" in url or "youtu.be" in url

    opts = {
        "outtmpl": str(DL_DIR / f"{vid_id}.%(ext)s"),
        "quiet": True, "no_warnings": True, "noprogress": True,
        "socket_timeout": 60,
        "retries": 10, "fragment_retries": 10,
        "no_check_certificate": True,
        "continuedl": False if is_pin else True,
        "concurrent_fragment_downloads": 0 if is_pin else 16,
        "http_chunk_size": 4 * 1024 * 1024,
        "buffersize": 1024 * 1024,
        "no_mtime": True, "no_playlist": True,
        "prefer_ffmpeg": True,
        "merge_output_format": "mp4",
        "format": fmt,
        "progress_hooks": [hook],
    }

    # ✨ الحل الجذري النهائي: محاكاة تطبيقات يوتيوب الرسمية
    if is_youtube:
        opts["user_agent"] = "com.google.android.youtube/19.29.36 (Linux; U; Android 14; en_US) gzip"
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "ios", "web"],
                "skip": ["hls", "dash"]
            }
        }
        opts["headers"] = {
            "User-Agent": opts["user_agent"],
            "Accept-Language": "en-US,en;q=0.9",
        }
        logger.info("🍃 استخدام محاكاة تطبيق YouTube VR/iOS")

    if qkey == "audio":
        del opts["merge_output_format"]
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            raw = ydl.prepare_filename(info)
        filename = _locate_file(raw, info.get("id", vid_id), qkey)
    except Exception as e:
        await status_msg.edit_text(f"❌ {str(e)[:200]}")
        cleanup(vid_id)
        return

    if not filename:
        await status_msg.edit_text("❌ الملف غير موجود.")
        return

    size = os.path.getsize(filename)
    speed = spd["kbs"]
    title = (info.get("title") or "")[:200]

    if size > MAX_SIZE and qkey != "audio":
        await safe_edit(status_msg, "📦 تقسيم...")
        parts = await loop.run_in_executor(None, lambda: split_video(filename, MAX_SIZE))
        if not parts:
            await status_msg.edit_text("❌ فشل التقسيم.")
            cleanup(vid_id)
            return
        total = len(parts)
        base_cap = f"<b>{title}</b>\n👤 {info.get('uploader','')}\n📥 {BOT_NAME}\n"
        for i, p in enumerate(parts, 1):
            ps = os.path.getsize(p)
            await ctx.bot.send_video(chat_id=chat_id, video=open(p, 'rb'),
                                     caption=f"{base_cap}📦 جزء {i}/{total} | {fmt_size(ps)}",
                                     parse_mode="HTML", read_timeout=600, write_timeout=600,
                                     supports_streaming=True)
            os.remove(p)
        await safe_edit(status_msg, "✅ تم.")
        db_log(uid, url, title, qkey, size, speed)
        await asyncio.sleep(2)
        await status_msg.delete()
    else:
        elapsed = int(time.time() - t0)
        await safe_edit(status_msg, f"📤 رفع {fmt_size(size)}... ⏱ {elapsed}ث")
        caption = f"<b>{title}</b>\n👤 {info.get('uploader','')}\n⚡ {fmt_speed(speed)} | ⏱ {elapsed}ث\n📥 {BOT_NAME}"
        kw = dict(chat_id=chat_id, caption=caption, parse_mode="HTML", read_timeout=600, write_timeout=600)
        with open(filename, 'rb') as f:
            if qkey == "audio":
                await ctx.bot.send_audio(audio=f, title=title[:64], performer=info.get('uploader','YAMD'), **kw)
            else:
                await ctx.bot.send_video(video=f, supports_streaming=True, **kw)
        db_log(uid, url, title, qkey, size, speed)
        await status_msg.delete()

def _locate_file(raw, vid_id, qkey):
    ext_list = (["mp3"] if qkey=="audio" else ["mp4","mkv","webm",""])
    for ext in ext_list:
        candidate = os.path.splitext(raw)[0] + (f".{ext}" if ext else "")
        if os.path.exists(candidate):
            return candidate
    cands = sorted([f for f in DL_DIR.glob(f"{vid_id}*") if f.is_file()],
                   key=lambda f: f.stat().st_size, reverse=True)
    return str(cands[0]) if cands else None

# خادم HTTP
async def health(request):
    return web.Response(text="YAMD is running!")

async def run_web_server():
    app = web.Application()
    app.router.add_get('/', health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Health server on port {port}")

def main():
    logger.info(f"ffmpeg: {'✅' if HAS_FFMPEG else '❌'}")
    logger.info(f"🚀 {BOT_NAME} مع أحدث تجاوز لحظر يوتيوب!")
    app = (Application.builder().token(BOT_TOKEN)
           .connect_timeout(30).read_timeout(600).write_timeout(600)
           .pool_timeout(120).concurrent_updates(True).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(CallbackQueryHandler(on_admin, pattern=r"^a\|"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q\|"))
    app.add_error_handler(on_error)
    loop = asyncio.get_event_loop()
    loop.create_task(run_web_server())
    app.run_polling(allowed_updates=["message","callback_query"], drop_pending_updates=True)

# الدوال الإدارية المختصرة (موجودة)
def _setup_db():
    db.execute("""CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, username TEXT, fname TEXT, seen TEXT, active TEXT, cnt INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS downloads(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, url TEXT, title TEXT, quality TEXT, size INTEGER, speed REAL, ts TEXT)""")
    db.commit()

async def on_quality(update, ctx): pass  # مختصر
async def cmd_admin(update, ctx): pass
async def on_admin(update, ctx): pass
async def on_error(update, ctx): pass

if __name__ == "__main__":
    main()
