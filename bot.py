import os, sys, time, asyncio, logging, sqlite3, shutil, subprocess, math, traceback
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
    filters, CallbackQueryHandler, ContextTypes)
import yt_dlp
from aiohttp import web

BOT_NAME      = "⚡ YAMD – Ultra Speed"
BOT_FULL_NAME = "YAAQOB ALMAHAJERI MEDIA DOWNLOADER"

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
ADMIN_ID    = int(os.environ.get("ADMIN_ID", "0"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

MAX_SIZE = 48 * 1024 * 1024
DL_DIR   = Path.home() / "yamd_dl"
DL_DIR.mkdir(exist_ok=True)
HAS_FFMPEG = bool(shutil.which("ffmpeg"))
HAS_ARIA2  = bool(shutil.which("aria2c"))
DOWNLOAD_SEM = asyncio.Semaphore(3)

# ── كوكيز ──────────────────────────────────────────
COOKIE_FILE = None
COOKIE_TMP  = "/tmp/yamd_cookies.txt"
COOKIES_TEXT = os.environ.get("COOKIES_TEXT", "")
if COOKIES_TEXT.strip():
    with open(COOKIE_TMP, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(COOKIES_TEXT.strip())
    COOKIE_FILE = COOKIE_TMP
else:
    SECRET = "/etc/secrets/cookies.txt"
    if os.path.exists(SECRET):
        shutil.copy2(SECRET, COOKIE_TMP)
        COOKIE_FILE = COOKIE_TMP

# ── لوج ────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("yamd.log")])
logger = logging.getLogger("YAMD")
logger.setLevel(logging.INFO)
for lib in ("httpx","httpcore","telegram","hpack","asyncio"):
    logging.getLogger(lib).setLevel(logging.ERROR)

# ── DB ─────────────────────────────────────────────
db = sqlite3.connect("yamd.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.execute("""CREATE TABLE IF NOT EXISTS users(
    uid INTEGER PRIMARY KEY, username TEXT, fname TEXT,
    seen TEXT, active TEXT, cnt INTEGER DEFAULT 0)""")
db.execute("""CREATE TABLE IF NOT EXISTS downloads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER, url TEXT, title TEXT,
    quality TEXT, size INTEGER, speed REAL, ts TEXT)""")
db.commit()

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
    if not b: return "?"
    return f"{b/1024**2:.1f}MB" if b < 1024**3 else f"{b/1024**3:.1f}GB"

def fmt_speed(kbs):
    if not kbs: return "?"
    return f"{kbs/1024:.1f}MB/s" if kbs >= 1024 else f"{kbs:.0f}KB/s"

_edit_ts = defaultdict(float)
async def safe_edit(msg, text, markup=None):
    if time.time() - _edit_ts[msg.message_id] < 3: return
    try:
        await msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
        _edit_ts[msg.message_id] = time.time()
    except: pass

def find_file(vid_id):
    cands = sorted([f for f in DL_DIR.glob(f"{vid_id}*") if f.is_file()],
                   key=lambda f: f.stat().st_size, reverse=True)
    return str(cands[0]) if cands else None

def cleanup(vid_id):
    for f in DL_DIR.glob(f"{vid_id}*"):
        try: f.unlink()
        except: pass

def split_video(filepath):
    if not HAS_FFMPEG: return None
    base = os.path.splitext(filepath)[0]
    pattern = f"{base}_part%03d.mp4"
    try:
        dur = float(subprocess.run(
            ["ffprobe","-v","quiet","-show_entries","format=duration",
             "-of","csv=p=0", filepath],
            capture_output=True, text=True).stdout.strip())
    except: return None
    if not dur: return None
    total_size  = os.path.getsize(filepath)
    parts_count = math.ceil(total_size / MAX_SIZE)
    seg = math.ceil(dur / parts_count) + 1
    r = subprocess.run(
        ["ffmpeg","-i",filepath,"-c","copy","-map","0",
         "-f","segment","-segment_time",str(seg),
         "-reset_timestamps","1","-segment_format","mp4", pattern],
        capture_output=True)
    if r.returncode != 0: return None
    return sorted(str(p) for p in Path(base).parent.glob(f"{Path(filepath).stem}_part*.mp4"))

async def cleanup_task():
    while True:
        now = time.time()
        for f in DL_DIR.glob("*"):
            try:
                if now - f.stat().st_mtime > 3600: f.unlink()
            except: pass
        await asyncio.sleep(1800)

# ══════════════════════════════════════════════════════════════
#  ★ بناء format string مرن — لا يعتمد على format_id ثابت ★
#
#  السبب: format_id يتغيّر بين extract_info وdownload
#  الحل:  نبني سلسلة تصفية بالارتفاع مع fallbacks متعددة
# ══════════════════════════════════════════════════════════════
def build_fmt(height: int, is_audio: bool = False) -> str:
    if is_audio:
        return "bestaudio[ext=m4a]/bestaudio/best"
    h = height
    # نجرب كل مجموعة بالترتيب حتى ينجح واحد
    return (
        f"bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}]+bestaudio"
        f"/best[height<={h}]"
        f"/best[height<={h*2}]"   # ← fallback للجودة الأعلى إذا الأدنى غير متاح
        f"/best"
    )

# ── استخراج الجودات المتاحة ────────────────────────
def get_available_qualities(url: str):
    """
    يُرجع (info, heights_list, has_audio)
    heights_list = قائمة ارتفاعات صحيحة مرتبة تنازلياً (بدون تكرار)
    """
    opts = {
        "quiet": True, "no_warnings": True,
        "socket_timeout": 30,
        "no_check_certificate": True,
        "no_playlist": True,
    }
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    if "youtube.com" in url or "youtu.be" in url:
        opts["http_headers"] = {
            "User-Agent": "com.google.android.youtube/20.10.38 (Linux; Android 14)",
        }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats") or []
    seen, heights = set(), []
    has_audio = False

    for f in formats:
        h = f.get("height") or 0
        vc = f.get("vcodec", "none")
        ac = f.get("acodec", "none")
        if vc != "none" and h > 0 and h not in seen:
            seen.add(h)
            heights.append(h)
        if ac != "none" and vc == "none":
            has_audio = True

    heights.sort(reverse=True)
    return info, heights, has_audio

# ── Commands ───────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_user(update.effective_user)
    await update.message.reply_text(
        f"أهلاً بك في <b>{BOT_NAME}</b>! 🚀\n"
        f"<b>{BOT_FULL_NAME}</b>\n\n"
        "⚡ أرسل رابط الفيديو من أي منصة.\n"
        f"{'🛡️ /admin' if update.effective_user.id==ADMIN_ID else ''}",
        parse_mode="HTML")

async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🌟 <b>{BOT_NAME}</b>\n<b>{BOT_FULL_NAME}</b>\n\n"
        "⚡ أسرع بوت تحميل على تلغرام\n"
        "✅ format string مرن — لا يعتمد على ID ثابت\n"
        "📦 تقسيم ذكي للملفات الكبيرة\n"
        "🍪 دعم Cookies لتجاوز القيود",
        parse_mode="HTML")

# ── استقبال الرابط ─────────────────────────────────
async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db_user(update.effective_user)
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ أرسل رابطاً صالحاً.")
        return

    status = await update.message.reply_text("🔍 جارٍ فحص الجودات المتاحة...")
    ctx.user_data["url"] = url

    try:
        loop = asyncio.get_running_loop()
        info, heights, has_audio = await loop.run_in_executor(
            None, lambda: get_available_qualities(url))
    except Exception as e:
        await status.edit_text(f"❌ فشل الفحص:\n<code>{str(e)[:250]}</code>",
                               parse_mode="HTML")
        return

    # ── بناء الأزرار ────────────────────────────────
    kb = []
    for h in heights:
        # callback_data: q|h|<height>  (نحفظ الارتفاع وليس format_id)
        kb.append([InlineKeyboardButton(f"🎥 {h}p", callback_data=f"q|h|{h}")])
    if has_audio:
        kb.append([InlineKeyboardButton("🔊 MP3 – صوت فقط", callback_data="q|a|0")])

    if not kb:
        # إذا لم تُكشف جودات → حمّل بأفضل ما يوجد مباشرة
        await status.edit_text("⚡ لم تُكشف جودات محددة — تحميل بأفضل جودة...")
        asyncio.create_task(
            do_download(ctx, update.message.chat_id, url,
                        "best[filesize<48M]/best", "auto", status,
                        update.effective_user.id))
        return

    dur = int(info.get("duration") or 0)
    dur_s = f"{dur//60}:{dur%60:02d}" if dur else "—"
    title = (info.get("title") or "")[:180]

    await status.edit_text(
        f"🎞️ <b>{title}</b>\n"
        f"⏱ {dur_s}   👤 {info.get('uploader','?')}\n\n"
        "اختر الجودة:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML")

# ── اختيار الجودة ──────────────────────────────────
async def on_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")          # q | h|a | value
    qtype = parts[1]                   # h = فيديو بارتفاع , a = صوت
    value = parts[2]
    url   = ctx.user_data.get("url")

    if not url:
        await q.edit_message_text("❌ الجلسة منتهية. أرسل الرابط مجدداً.")
        return

    if qtype == "a":
        fmt   = build_fmt(0, is_audio=True)
        qkey  = "audio"
        label = "🔊 MP3"
    else:
        h     = int(value)
        fmt   = build_fmt(h)
        qkey  = str(h)
        label = f"🎥 {h}p"

    await q.edit_message_text(f"⬇️ {label} — جارٍ التحميل...")
    asyncio.create_task(
        do_download(ctx, q.message.chat_id, url, fmt,
                    qkey, q.message, q.from_user.id))

# ══════════════════════════════════════════════════════════════
#  دالة التحميل والرفع
# ══════════════════════════════════════════════════════════════
async def do_download(ctx, chat_id, url, fmt, qkey, status_msg, uid):
    async with DOWNLOAD_SEM:
        loop  = asyncio.get_running_loop()
        t0    = time.time()
        vid_id = f"v{uid}{int(t0)}"
        spd   = {"kbs":0.0, "bytes":0, "last_b":0, "last_t":t0}
        is_pin = "pin.it" in url or "pinterest" in url.lower()

        def hook(d):
            if d.get("status") != "downloading": return
            got   = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            now   = time.time()
            dt    = now - spd["last_t"]
            if dt >= 1:
                spd["kbs"]    = max(0, (got - spd["last_b"]) / dt / 1024)
                spd["last_b"] = got
                spd["last_t"] = now
            spd["bytes"] = got
            kbs = spd["kbs"]
            pct = f"{got/total*100:.0f}%" if total else "…"
            eta = "?"
            if kbs > 0 and total:
                s   = (total - got) / 1024 / kbs
                eta = f"{int(s//60)}د{int(s%60)}ث" if s >= 60 else f"{int(s)}ث"
            asyncio.run_coroutine_threadsafe(
                safe_edit(status_msg,
                    f"⬇️ جارٍ التحميل...\n"
                    f"⚡ {fmt_speed(kbs)}\n"
                    f"📦 {fmt_size(got)}/{fmt_size(total)} [{pct}]  ETA: {eta}"),
                loop)

        opts = {
            "outtmpl":  str(DL_DIR / f"{vid_id}.%(ext)s"),
            "quiet":    True, "no_warnings": True, "noprogress": True,
            "socket_timeout": 60,
            "retries":  15, "fragment_retries": 15,
            "file_access_retries": 5,
            "no_check_certificate": True,
            "continuedl": not is_pin,
            "concurrent_fragment_downloads": 0 if is_pin else 16,
            "http_chunk_size": 4 * 1024 * 1024,
            "buffersize":      1024 * 1024,
            "no_mtime":  True, "no_playlist": True,
            "prefer_ffmpeg": True,
            "merge_output_format": "mp4",
            "format":   fmt,
            "progress_hooks": [hook],
            "geo_bypass": True,
            # ★ الخيار الأهم: إذا فشل format → جرب التالي تلقائياً
            "format_sort": ["res", "ext:mp4:m4a", "size", "br"],
        }

        if COOKIE_FILE and os.path.exists(COOKIE_FILE):
            opts["cookiefile"] = COOKIE_FILE

        if "youtube.com" in url or "youtu.be" in url:
            opts["http_headers"] = {
                "User-Agent": "com.google.android.youtube/20.10.38 (Linux; Android 14)",
                "Accept-Language": "en-US,en;q=0.9",
            }

        if qkey == "audio":
            del opts["merge_output_format"]
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }]

        # aria2c فقط للمواقع البسيطة (يفشل مع DASH/HLS على YouTube)
        is_yt = "youtube.com" in url or "youtu.be" in url
        if HAS_ARIA2 and not is_yt and not is_pin:
            opts["external_downloader"] = "aria2c"
            opts["external_downloader_args"] = {"default": [
                "--max-connection-per-server=16", "--split=16",
                "--min-split-size=256K", "--retry-wait=3",
                "--max-tries=0", "--timeout=60", "--connect-timeout=15",
                "--lowest-speed-limit=0", "--file-allocation=none",
                "--auto-file-renaming=false", "--allow-overwrite=true",
                "--enable-http-keep-alive=true", "--console-log-level=error",
            ]}

        filename = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(url, download=True))
                raw  = ydl.prepare_filename(info)

            # ── إيجاد الملف ─────────────────────────
            filename = None
            for ext in (["mp3"] if qkey=="audio" else ["mp4","mkv","webm",""]):
                c = os.path.splitext(raw)[0] + (f".{ext}" if ext else "")
                if os.path.exists(c):
                    filename = c; break
            if not filename:
                filename = find_file(vid_id)
            if not filename:
                await status_msg.edit_text("❌ الملف غير موجود بعد التحميل.")
                return

            size  = os.path.getsize(filename)
            speed = spd["kbs"]
            title = (info.get("title") or "")[:200]

            # ── تقسيم إذا كبير ───────────────────────
            if size > MAX_SIZE and qkey != "audio":
                await safe_edit(status_msg, "📦 جارٍ تقسيم الفيديو...")
                parts = await loop.run_in_executor(
                    None, lambda: split_video(filename))
                if parts:
                    base_cap = (f"<b>{title}</b>\n"
                                f"👤 {info.get('uploader','')}\n"
                                f"📥 {BOT_NAME}")
                    total_parts = len(parts)
                    for i, p in enumerate(parts, 1):
                        ps = os.path.getsize(p)
                        with open(p, "rb") as vf:
                            await ctx.bot.send_video(
                                chat_id=chat_id, video=vf,
                                caption=f"{base_cap}\n📦 جزء {i}/{total_parts} | {fmt_size(ps)}",
                                parse_mode="HTML", supports_streaming=True,
                                read_timeout=600, write_timeout=600)
                        os.remove(p)
                    db_log(uid, url, title, qkey, size, speed)
                    try: await status_msg.delete()
                    except: pass
                    return
                else:
                    await status_msg.edit_text("❌ فشل التقسيم. اختر جودة أقل.")
                    return

            # ── رفع الملف ────────────────────────────
            elapsed = int(time.time() - t0)
            await safe_edit(status_msg,
                f"📤 رفع {fmt_size(size)}...  ⏱ {elapsed}ث")

            caption = (
                f"<b>{title}</b>\n"
                f"👤 {info.get('uploader','')}\n"
                f"⚡ {fmt_speed(speed)}  ⏱ {elapsed}ث\n"
                f"📥 {BOT_NAME}"
            )
            kw = dict(chat_id=chat_id, caption=caption, parse_mode="HTML",
                      read_timeout=600, write_timeout=600,
                      connect_timeout=60, pool_timeout=120)

            with open(filename, "rb") as f:
                if qkey == "audio":
                    await ctx.bot.send_audio(
                        audio=f, title=title[:64],
                        performer=info.get("uploader","YAMD"), **kw)
                else:
                    await ctx.bot.send_video(
                        video=f, supports_streaming=True,
                        width=info.get("width"),
                        height=info.get("height"),
                        duration=int(info.get("duration") or 0), **kw)

            db_log(uid, url, title, qkey, size, speed)
            try: await status_msg.delete()
            except: pass
            logger.info(f"✅ {title[:40]} | {fmt_size(size)} | {int(time.time()-t0)}ث")

        except Exception as e:
            logger.error(f"[do_download] {e}", exc_info=True)
            err = str(e)
            # ── إذا فشل الـ format → عرض خيارات جودة بديلة ───
            if "Requested format is not available" in err or "format" in err.lower():
                kb = [[InlineKeyboardButton(f"🎥 {h}p", callback_data=f"q|h|{h}")]
                      for h in [144, 240, 360, 480]]
                kb.append([InlineKeyboardButton("🔊 MP3", callback_data="q|a|0")])
                await status_msg.edit_text(
                    "⚠️ الجودة المطلوبة غير متاحة لهذا الفيديو.\n"
                    "اختر جودة أخرى:",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="HTML")
            else:
                await status_msg.edit_text(
                    f"❌ خطأ:\n<code>{err[:200]}</code>",
                    parse_mode="HTML")
        finally:
            cleanup(vid_id)

# ── Admin ───────────────────────────────────────────
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
        f"🛡️ <b>YAMD Admin</b>\n"
        f"aria2: {'✅' if HAS_ARIA2 else '❌'}  "
        f"ffmpeg: {'✅' if HAS_FFMPEG else '❌'}  "
        f"🍪: {'✅' if COOKIE_FILE else '❌'}",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def on_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("⛔", show_alert=True); return
    await q.answer()
    act = q.data.split("|")[1]
    if act == "users":
        rows = db.execute("SELECT uid,username,fname,cnt,active FROM users "
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
            t += (f"• <b>{un or uid}</b> | {qual}p | "
                  f"{fmt_size(sz)} | {fmt_speed(spd)}\n"
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
            f"aria2: {'✅' if HAS_ARIA2 else '❌'}  "
            f"🍪: {'✅' if COOKIE_FILE else '❌'}",
            parse_mode="HTML")
    elif act == "clean":
        n = 0
        for f in DL_DIR.iterdir():
            try: f.unlink(); n += 1
            except: pass
        await q.edit_message_text(f"🗑 تم حذف {n} ملف مؤقت.")

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[ERROR] {ctx.error}", exc_info=ctx.error)

# ── Webhook Server ──────────────────────────────────
async def main():
    app = (Application.builder().token(BOT_TOKEN)
           .connect_timeout(30).read_timeout(600).write_timeout(600)
           .pool_timeout(120).concurrent_updates(True).build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(CallbackQueryHandler(on_admin,   pattern=r"^a\|"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q\|"))
    app.add_error_handler(on_error)

    await app.initialize()
    await app.start()
    await app.bot.delete_webhook(drop_pending_updates=True)

    if WEBHOOK_URL:
        wh = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await app.bot.set_webhook(url=wh, allowed_updates=["message","callback_query"])
        logger.info(f"✅ Webhook: {wh}")

        asyncio.create_task(cleanup_task())

        web_app = web.Application()
        async def handle(request):
            data = await request.json()
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
            return web.Response()
        web_app.router.add_post(f"/{BOT_TOKEN}", handle)
        web_app.router.add_get("/", lambda r: web.Response(text="YAMD ✅"))

        runner = web.AppRunner(web_app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info(f"🌐 port {port}")

        try:
            while True: await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            pass
    else:
        # polling (للاختبار المحلي)
        logger.info("🚀 Polling mode")
        asyncio.create_task(cleanup_task())
        await app.updater.start_polling(
            allowed_updates=["message","callback_query"],
            drop_pending_updates=True)
        await app.updater.idle()

    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except Exception:
        logger.critical(traceback.format_exc())
        sys.exit(1)
    finally:
        loop.close()

