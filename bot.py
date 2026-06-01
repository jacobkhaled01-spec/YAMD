import os, time, asyncio, logging, sqlite3, shutil, subprocess, math, re, json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import HTTPError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
import yt_dlp

BOT_NAME      = "⚡ YAMD - Ultra Speed Downloader"
BOT_FULL_NAME = "YAAQOB ALMAHAJERI MEDIA DOWNLOADER | ULTRA SPEED EDITION"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

MAX_SIZE   = 48 * 1024 * 1024
DL_DIR     = Path.home() / "yamd_dl"
DL_DIR.mkdir(exist_ok=True)
HAS_FFMPEG = bool(shutil.which("ffmpeg"))

# Invidious instances (يمكن تغييرها)
INVIDIOUS_INSTANCES = [
    "https://invidious.snopyta.org",
    "https://invidious.weblibre.org",
    "https://yewtu.be",
]

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

def _setup_db():
    cols = {r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    if not cols:
        db.execute("""CREATE TABLE users(
            uid INTEGER PRIMARY KEY, username TEXT, fname TEXT,
            seen TEXT, active TEXT, cnt INTEGER DEFAULT 0)""")
    elif "user_id" in cols:
        db.execute("""CREATE TABLE u2(uid INTEGER PRIMARY KEY,
            username TEXT,fname TEXT,seen TEXT,active TEXT,cnt INTEGER DEFAULT 0)""")
        db.execute("""INSERT INTO u2 SELECT user_id,
            COALESCE(username,''),COALESCE(first_name,''),
            COALESCE(first_seen,datetime('now')),
            COALESCE(last_active,datetime('now')),
            COALESCE(downloads_count,0) FROM users""")
        db.execute("DROP TABLE users")
        db.execute("ALTER TABLE u2 RENAME TO users")

    dcols = {r[1] for r in db.execute("PRAGMA table_info(downloads)").fetchall()}
    if not dcols:
        db.execute("""CREATE TABLE downloads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER, url TEXT, title TEXT,
            quality TEXT, size INTEGER, speed REAL, ts TEXT)""")
    elif "user_id" in dcols:
        db.execute("""CREATE TABLE d2(id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER,url TEXT,title TEXT,quality TEXT,
            size INTEGER,speed REAL,ts TEXT)""")
        db.execute("""INSERT INTO d2(id,uid,url,title,quality,size,ts)
            SELECT id,user_id,url,title,quality,
            COALESCE(file_size,0),COALESCE(downloaded_at,datetime('now')) FROM downloads""")
        db.execute("DROP TABLE downloads")
        db.execute("ALTER TABLE d2 RENAME TO downloads")
    db.commit()
_setup_db()

def db_user(u):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO users(uid,username,fname,seen,active) VALUES(?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET active=excluded.active,"
        "username=COALESCE(excluded.username,username),"
        "fname=COALESCE(excluded.fname,fname)",
        (u.id, u.username or "", u.first_name or "", now, now))
    db.commit()

def db_log(uid, url, title, qkey, size, speed):
    db.execute(
        "INSERT INTO downloads(uid,url,title,quality,size,speed,ts) VALUES(?,?,?,?,?,?,?)",
        (uid, url, title, qkey, size, round(speed,1), datetime.now().isoformat()))
    db.execute("UPDATE users SET cnt=cnt+1 WHERE uid=?", (uid,))
    db.commit()

def fmt_size(b):
    if b >= 1024*1024*1024: return f"{b/1024/1024/1024:.1f}GB"
    return f"{b/1024/1024:.1f}MB"

def fmt_speed(kbs):
    if kbs >= 1024: return f"{kbs/1024:.1f}MB/s"
    return f"{kbs:.0f}KB/s"

_edit_ts = defaultdict(float)
async def safe_edit(msg, text: str, markup=None):
    if time.time() - _edit_ts[msg.message_id] < 3:
        return
    try:
        await msg.edit_text(text, reply_markup=markup)
        _edit_ts[msg.message_id] = time.time()
    except Exception:
        pass

def find_file(vid_id: str) -> str | None:
    cands = sorted(
        [f for f in DL_DIR.glob(f"{vid_id}*") if f.is_file()],
        key=lambda f: f.stat().st_size, reverse=True)
    return str(cands[0]) if cands else None

def cleanup(vid_id: str):
    for f in DL_DIR.glob(f"{vid_id}*"):
        try: f.unlink()
        except: pass

def split_video(filepath: str, max_part_size=MAX_SIZE):
    if not HAS_FFMPEG:
        return None
    base = os.path.splitext(filepath)[0]
    part_pattern = f"{base}_part%03d.mp4"
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", filepath],
            capture_output=True, text=True)
        duration = float(result.stdout.strip())
    except:
        duration = None
    if not duration:
        return None
    total_size = os.path.getsize(filepath)
    num_parts = math.ceil(total_size / max_part_size)
    segment_duration = duration / num_parts
    segment_duration = math.ceil(segment_duration) + 1

    cmd = [
        "ffmpeg", "-i", filepath, "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(segment_duration),
        "-reset_timestamps", "1",
        "-segment_format", "mp4",
        part_pattern
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    parts = sorted(Path(base).parent.glob(f"{Path(filepath).stem}_part*.mp4"))
    return [str(p) for p in parts]

# ────────────── Invidious Helper ──────────────
def get_youtube_id(url):
    patterns = [
        r'(?:v=|/)([0-9A-Za-z_-]{11})(?:[?&]|$)',
        r'youtu\.be/([0-9A-Za-z_-]{11})'
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def fetch_invidious_stream(video_id):
    """يحاول الحصول على رابط مباشر للفيديو من Invidious."""
    for instance in INVIDIOUS_INSTANCES:
        try:
            api_url = f"{instance}/api/v1/videos/{video_id}"
            req = Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            # نختار أفضل جودة (عادة adaptiveFormats أو formatStreams)
            streams = data.get("adaptiveFormats", []) + data.get("formatStreams", [])
            if not streams:
                continue
            # نختار أعلى جودة فيديو + صوت
            video_streams = [s for s in streams if s.get("type", "").startswith("video")]
            if video_streams:
                # ترتيب تنازلي حسب الجودة (quality)
                video_streams.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                return video_streams[0]["url"]
        except Exception:
            continue
    return None

# ────────────── Commands ──────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_user(update.effective_user)
    msg = (
        f"أهلاً بك في <b>{BOT_NAME}</b>! 🚀\n\n"
        f"مرحباً بك في تجربة التحميل الأسرع على تلغرام. أنا بوت <b>{BOT_FULL_NAME}</b>، "
        "المصمم خصيصاً لخدمتك بأقصى سرعة ممكنة، حتى لو كان إنترنت لديك بطيئاً جداً.\n\n"
        "<b>طريقة الاستخدام:</b>\n"
        "فقط أرسل رابط الفيديو من أي منصة (يوتيوب، تيك توك، فيسبوك، انستغرام، بنترست...) وسأقوم بالباقي.\n\n"
        "⚡ <b>الميزات:</b>\n"
        "• تحميل فوري بأفضل جودة\n"
        "• دعم الملفات الضخمة بالتقسيم التلقائي\n"
        "• يعمل على أضعف الشبكات\n\n"
        "<b>ابدأ الآن.. أرسل رابطاً!</b> ⚡👇\n\n"
        "💡 /about للمزيد من المعلومات"
    )
    if update.effective_user.id == ADMIN_ID:
        msg += "\n🛡️ /admin"
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    about_text = (
        f"🌟 <b>{BOT_NAME}</b>\n"
        f"<i>({BOT_FULL_NAME})</i>\n\n"
        "البوت الأول المصمم للسرعة القصوى حتى على أضعف الشبكات.\n\n"
        "⚡ <b>المميزات:</b>\n"
        "• تحميل فوري من جميع المنصات\n"
        "• تقسيم ذكي للملفات الكبيرة\n"
        "• دعم شامل: Pinterest, TikTok, YouTube, Instagram...\n"
        "• شريط تقدم ديناميكي\n\n"
        "<b>السرعة هويتنا.</b> 🚀"
    )
    await update.message.reply_text(about_text, parse_mode="HTML")

async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_user(update.effective_user)
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ أرسل رابطاً صالحاً.")
        return

    fmt = "bestvideo+bestaudio/best"
    status = await update.message.reply_text("⬇️ جارٍ التحميل...")
    asyncio.create_task(
        do_download(ctx, update.message.chat_id, url, fmt, "auto", status, update.effective_user.id)
    )

async def do_download(ctx, chat_id, url, fmt, qkey, status_msg, uid):
    loop = asyncio.get_running_loop()
    t0 = time.time()
    vid_id = f"v{uid}{int(t0)}"

    spd = {"kbs": 0.0, "bytes": 0, "last_b": 0, "last_t": t0}

    def hook(d):
        if d.get("status") != "downloading": return
        got = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        now = time.time()
        dt = now - spd["last_t"]
        if dt >= 1:
            spd["kbs"] = max(0, (got - spd["last_b"]) / dt / 1024)
            spd["last_b"] = got
            spd["last_t"] = now
        spd["bytes"] = got
        kbs = spd["kbs"]
        pct = f"{got/total*100:.0f}%" if total else "…"
        done = fmt_size(got)
        tot = fmt_size(total) if total else "?"
        eta = "?"
        if kbs > 0 and total:
            s = (total - got) / 1024 / kbs
            eta = f"{int(s//60)}د{int(s%60)}ث" if s >= 60 else f"{int(s)}ث"
        asyncio.run_coroutine_threadsafe(
            safe_edit(status_msg, f"⬇️ جارٍ التحميل...\n⚡ {fmt_speed(kbs)}\n📦 {done}/{tot} [{pct}]  ETA: {eta}"),
            loop)

    is_youtube = "youtube.com" in url or "youtu.be" in url
    is_pin = "pin.it" in url or "pinterest" in url.lower()

    # إذا كان يوتيوب، نحاول استخدام Invidious أولاً
    if is_youtube:
        video_id = get_youtube_id(url)
        if video_id:
            direct_url = await loop.run_in_executor(None, fetch_invidious_stream, video_id)
            if direct_url:
                await safe_edit(status_msg, "🔄 جارٍ التحميل عبر Invidious (بدون حظر)...")
                # استخدم الرابط المباشر كمصدر
                url = direct_url
                fmt = "best"  # لا نحتاج لتنسيق معين

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
    if qkey == "audio":
        del opts["merge_output_format"]
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}]

    filename = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            raw = ydl.prepare_filename(info)
        filename = _locate_file(raw, info.get("id", vid_id), qkey)
    except Exception as e:
        if "Sign in" in str(e) or "bot" in str(e):
            await status_msg.edit_text("❌ يوتيوب يطلب التحقق. جارٍ المحاولة عبر Invidious...")
            # المحاولة مرة أخرى عبر Invidious (إذا لم نكن قد استخدمناه)
            if not is_youtube:
                video_id = get_youtube_id(url)
                if video_id:
                    direct_url = await loop.run_in_executor(None, fetch_invidious_stream, video_id)
                    if direct_url:
                        url = direct_url
                        opts["format"] = "best"
                        try:
                            with yt_dlp.YoutubeDL(opts) as ydl:
                                info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
                                raw = ydl.prepare_filename(info)
                            filename = _locate_file(raw, info.get("id", vid_id), qkey)
                        except Exception as e2:
                            await status_msg.edit_text(f"❌ فشل عبر Invidious: {str(e2)[:200]}")
                            cleanup(vid_id)
                            return
                    else:
                        await status_msg.edit_text("❌ Invidious غير متاح حالياً. حاول لاحقاً.")
                        cleanup(vid_id)
                        return
                else:
                    await status_msg.edit_text("❌ تعذّر استخراج معرف الفيديو.")
                    cleanup(vid_id)
                    return
            else:
                await status_msg.edit_text(f"❌ فشل: {str(e)[:200]}")
                cleanup(vid_id)
                return
        elif "Requested format" in str(e) or "not available" in str(e):
            await safe_edit(status_msg, "🔄 إعادة المحاولة بأفضل صيغة...")
            opts["format"] = "best"
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
                    raw = ydl.prepare_filename(info)
                filename = _locate_file(raw, info.get("id", vid_id), qkey)
            except Exception as e2:
                await status_msg.edit_text(f"❌ فشل: {str(e2)[:200]}")
                cleanup(vid_id)
                return
        else:
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
            await ctx.bot.send_video(
                chat_id=chat_id,
                video=open(p, 'rb'),
                caption=f"{base_cap}📦 جزء {i}/{total} | {fmt_size(ps)}",
                parse_mode="HTML",
                read_timeout=600, write_timeout=600,
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
        kw = dict(chat_id=chat_id, caption=caption, parse_mode="HTML",
                  read_timeout=600, write_timeout=600)
        with open(filename, 'rb') as f:
            if qkey == "audio":
                await ctx.bot.send_audio(audio=f, title=title[:64],
                                         performer=info.get('uploader','YAMD'), **kw)
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
    cands = sorted(
        [f for f in DL_DIR.glob(f"{vid_id}*") if f.is_file()],
        key=lambda f: f.stat().st_size, reverse=True)
    return str(cands[0]) if cands else None

async def on_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|", 2)
    qkey = parts[1]
    url = parts[2] if len(parts) > 2 else ctx.user_data.get("url","")
    if not url:
        await q.edit_message_text("❌ انتهت الجلسة. أرسل الرابط مجدداً.")
        return

    label, fmt = QUALITY[qkey]
    await q.edit_message_text(f"⬇️ {label}...")
    asyncio.create_task(
        do_download(ctx, q.message.chat_id, url, fmt, qkey, q.message, q.from_user.id)
    )

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ للمشرف فقط."); return
    kb = [
        [InlineKeyboardButton("👥 المستخدمون",     callback_data="a|users")],
        [InlineKeyboardButton("📜 آخر التحميلات", callback_data="a|dls")],
        [InlineKeyboardButton("📊 إحصائيات",       callback_data="a|stats")],
        [InlineKeyboardButton("🗑 تنظيف مؤقت",    callback_data="a|clean")],
    ]
    await update.message.reply_text(
        f"🛡️ <b>YAMD Admin</b>\nffmpeg: {'✅' if HAS_FFMPEG else '❌'}",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def on_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("⛔", show_alert=True); return
    await q.answer()
    act = q.data.split("|")[1]

    if act == "users":
        rows = db.execute(
            "SELECT uid,username,fname,cnt,active FROM users "
            "ORDER BY active DESC LIMIT 15").fetchall()
        t = "👥 <b>آخر 15 مستخدم</b>\n\n"
        for uid,un,fn,cnt,ac in rows:
            t += f"• <code>{uid}</code> {un or fn}  ⬇️{cnt}  {(ac or '')[:10]}\n"
        await q.edit_message_text(t or "لا يوجد.", parse_mode="HTML")

    elif act == "dls":
        rows = db.execute(
            "SELECT d.uid,d.title,d.quality,d.size,d.speed,d.ts,u.username "
            "FROM downloads d LEFT JOIN users u ON d.uid=u.uid "
            "ORDER BY d.id DESC LIMIT 20").fetchall()
        t = "📜 <b>آخر 20 تحميلة</b>\n\n"
        for uid,ttl,qual,sz,spd,ts,un in rows:
            t += (f"• <b>{un or uid}</b> | {qual} | "
                  f"{fmt_size(sz) if sz else '?'} | "
                  f"{fmt_speed(spd) if spd else '?'}\n"
                  f"  {(ttl or '')[:35]}\n")
        await q.edit_message_text(t or "لا توجد.", parse_mode="HTML")

    elif act == "stats":
        uc  = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        dc  = db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        avg = db.execute("SELECT AVG(speed) FROM downloads").fetchone()[0] or 0
        await q.edit_message_text(
            f"📊 <b>YAMD Stats</b>\n\n"
            f"👥 {uc} مستخدم\n📥 {dc} تحميلة\n"
            f"⚡ متوسط: {fmt_speed(avg)}\n"
            f"ffmpeg: {'✅' if HAS_FFMPEG else '❌'}",
            parse_mode="HTML")

    elif act == "clean":
        for f in DL_DIR.iterdir():
            try: f.unlink()
            except: pass
        await q.edit_message_text("🗑 تم تنظيف المجلد المؤقت.")

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[ERROR] {ctx.error}", exc_info=ctx.error)

def main():
    logger.info(f"ffmpeg: {'✅' if HAS_FFMPEG else '❌'}")
    logger.info(f"🚀 {BOT_NAME} يعمل — تحميل فوري مع دعم Invidious لتفادي الحظر!")

    app = (Application.builder()
           .token(BOT_TOKEN)
           .connect_timeout(30)
           .read_timeout(600)
           .write_timeout(600)
           .pool_timeout(120)
           .concurrent_updates(True)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(CallbackQueryHandler(on_admin,   pattern=r"^a\|"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q\|"))
    app.add_error_handler(on_error)

    app.run_polling(
        allowed_updates=["message","callback_query"],
        drop_pending_updates=True)

if __name__ == "__main__":
    main()
