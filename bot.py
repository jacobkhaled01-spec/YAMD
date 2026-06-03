import os, logging, time, asyncio, sqlite3
from pathlib import Path
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, CallbackQueryHandler, ContextTypes
)
import yt_dlp

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN  = "ضع_التوكن_هنا"
ADMIN_ID   = 123456789

MAX_SIZE     = 50 * 1024 * 1024
DOWNLOAD_DIR = Path.home() / "yamd_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

BOT_NAME      = "YAMD"
BOT_FULL_NAME = "YAAQOB ALMAHAJERI MEDIA DOWNLOADER"

# ── جودات ──────────────────────────────────────────────────────────────────────
QUALITY_OPTIONS = {
    "direct": ("⚡ مباشر (أسرع)",   "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best"),
    "144":    ("🐢 144p",           "bestvideo[height<=144]+bestaudio/best[height<=144]"),
    "240":    ("🐢 240p",           "bestvideo[height<=240]+bestaudio/best[height<=240]"),
    "360":    ("🎥 360p",           "bestvideo[height<=360]+bestaudio/best[height<=360]"),
    "480":    ("🎥 480p",           "bestvideo[height<=480]+bestaudio/best[height<=480]"),
    "audio":  ("🔊 MP3",            "bestaudio/best"),
}

# ── قاعدة البيانات ─────────────────────────────────────────────────────────────
conn = sqlite3.connect("yamd.db", check_same_thread=False)
cur  = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
    first_seen TEXT, last_active TEXT, downloads_count INTEGER DEFAULT 0)""")
cur.execute("""CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, url TEXT, title TEXT,
    quality TEXT, file_size INTEGER, downloaded_at TEXT)""")
conn.commit()

def register_user(user):
    now = datetime.now().isoformat()
    cur.execute(
        "INSERT INTO users (user_id,username,first_name,last_name,first_seen,last_active)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(user_id) DO UPDATE SET last_active=?,username=COALESCE(?,username),"
        "first_name=COALESCE(?,first_name),last_name=COALESCE(?,last_name)",
        (user.id,user.username,user.first_name,user.last_name,now,now,
         now,user.username,user.first_name,user.last_name))
    conn.commit()

def log_download(user_id, url, title, quality, size):
    now = datetime.now().isoformat()
    cur.execute(
        "INSERT INTO downloads(user_id,url,title,quality,file_size,downloaded_at)"
        " VALUES(?,?,?,?,?,?)", (user_id,url,title,quality,size,now))
    cur.execute("UPDATE users SET downloads_count=downloads_count+1 WHERE user_id=?", (user_id,))
    conn.commit()

# ── خيارات yt-dlp مُضبوطة للسرعة القصوى ───────────────────────────────────────
def build_ydl_opts(quality_key: str) -> dict:
    fmt = QUALITY_OPTIONS[quality_key][1]
    opts = {
        # ── المسار ──
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),

        # ── صامت ──
        'quiet': True,
        'no_warnings': True,

        # ── الشبكة ──
        'socket_timeout': 15,          # يفشل بسرعة ويُعيد المحاولة
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'no_check_certificate': True,

        # ── التحميل المتوازي (أهم شيء) ──
        'concurrent_fragment_downloads': 16,  # 16 شريحة معًا
        'http_chunk_size': 10 * 1024 * 1024,  # 10 MB لكل طلب

        # ── تسريع إضافي ──
        'buffersize': 1024 * 1024,     # 1 MB buffer
        'no_mtime': True,
        'no_playlist': True,
        'extractor_retries': 3,
        'format': fmt,

        # ── أداة دمج سريعة ──
        'prefer_ffmpeg': True,
    }

    if quality_key == "audio":
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }]
    elif quality_key != "direct":
        opts['merge_output_format'] = 'mp4'

    return opts

# ── أداة الرفع السريع (بدون انتظار progress) ───────────────────────────────────
async def send_file_fast(context, chat_id, filename, quality_key, info, caption):
    """يرفع الملف مباشرة بأعلى مهل ممكنة."""
    kwargs = dict(
        chat_id=chat_id,
        caption=caption,
        read_timeout=300,
        write_timeout=300,
        connect_timeout=60,
        pool_timeout=60,
    )
    if quality_key == "audio":
        with open(filename, 'rb') as f:
            await context.bot.send_audio(
                audio=f,
                title=(info.get('title','')[:64]),
                performer=info.get('uploader', BOT_NAME),
                **kwargs
            )
    else:
        with open(filename, 'rb') as f:
            await context.bot.send_video(
                video=f,
                supports_streaming=True,
                width=info.get('width'),
                height=info.get('height'),
                duration=info.get('duration'),
                **kwargs
            )

# ── Admin ──────────────────────────────────────────────────────────────────────
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ للمشرف فقط.")
        return
    kb = [
        [InlineKeyboardButton("👥 المستخدمون",     callback_data="admin_users")],
        [InlineKeyboardButton("📜 آخر التحميلات", callback_data="admin_downloads")],
        [InlineKeyboardButton("📊 إحصائيات",       callback_data="admin_stats")],
    ]
    await update.message.reply_text("🛡️ لوحة تحكم YAMD:", reply_markup=InlineKeyboardMarkup(kb))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("⛔ غير مصرح", show_alert=True); return
    await q.answer()

    if q.data == "admin_users":
        cur.execute("SELECT user_id,username,first_name,downloads_count,last_active"
                    " FROM users ORDER BY last_active DESC LIMIT 15")
        rows = cur.fetchall()
        text = "👥 <b>المستخدمون (آخر 15)</b>:\n\n"
        for uid,uname,fname,cnt,last in rows:
            name = uname or fname or str(uid)
            text += f"• <code>{uid}</code> {name} | ⬇️{cnt} | {last[:10]}\n"
        await q.edit_message_text(text or "لا يوجد.", parse_mode="HTML")

    elif q.data == "admin_downloads":
        cur.execute(
            "SELECT d.user_id,d.title,d.quality,d.file_size,d.downloaded_at,u.username"
            " FROM downloads d LEFT JOIN users u ON d.user_id=u.user_id"
            " ORDER BY d.id DESC LIMIT 20")
        rows = cur.fetchall()
        text = "📜 <b>آخر 20 تحميلة</b>:\n\n"
        for uid,title,qual,size,date,uname in rows:
            name = uname or str(uid)
            size_mb = (size//(1024*1024)) if size else 0
            t = (title[:45]+"..") if title and len(title)>45 else title
            text += f"• <b>{name}</b> | {qual} | {size_mb}MB\n  {t} | {date[:10]}\n"
        await q.edit_message_text(text or "لا توجد.", parse_mode="HTML")

    elif q.data == "admin_stats":
        users_count = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        dl_count    = cur.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        await q.edit_message_text(
            f"📊 <b>إحصائيات YAMD</b>\n\n"
            f"👥 المستخدمون: {users_count}\n"
            f"📥 التحميلات:  {dl_count}",
            parse_mode="HTML"
        )

# ── /start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    is_admin = update.effective_user.id == ADMIN_ID
    admin_note = "\n🛡️ /admin" if is_admin else ""
    await update.message.reply_text(
        f"🎬 <b>{BOT_NAME}</b> – <i>{BOT_FULL_NAME}</i>\n\n"
        f"⚡ أرسل رابط الفيديو واختر الجودة{admin_note}",
        parse_mode="HTML"
    )

# ── استقبال الرابط ─────────────────────────────────────────────────────────────
async def link_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ أرسل رابطاً صالحاً."); return

    msg = await update.message.reply_text("🔍 ...")
    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL({'quiet':True,'no_warnings':True,'socket_timeout':15}) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:300]}"); return

    context.user_data['url']  = url
    context.user_data['info'] = info

    kb = [[InlineKeyboardButton(label, callback_data=f"q|{key}")]
          for key, (label, _) in QUALITY_OPTIONS.items()]
    dur = info.get('duration', 0)
    dur_str = f"{dur//60}:{dur%60:02d}" if dur else "?"
    await msg.edit_text(
        f"🎞️ <b>{info.get('title','')[:180]}</b>\n"
        f"⏱ {dur_str}  👤 {info.get('uploader','?')}\n\n"
        "اختر الجودة:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML"
    )

# ── اختيار الجودة والتحميل ────────────────────────────────────────────────────
async def quality_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    qkey   = query.data.split('|')[1]
    url    = context.user_data.get('url')
    info   = context.user_data.get('info')
    if not url:
        await query.edit_message_text("❌ الجلسة منتهية."); return

    qlabel  = QUALITY_OPTIONS[qkey][0]
    status  = await query.edit_message_text(f"⬇️ {qlabel} ...")
    chat_id = query.message.chat_id
    ydl_opts = build_ydl_opts(qkey)
    loop     = asyncio.get_event_loop()
    t0       = time.time()

    try:
        # ── تحميل في خيط منفصل ──────────────────────────────────────────
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            dl_info  = await loop.run_in_executor(
                None, lambda: ydl.extract_info(url, download=True))
            filename = ydl.prepare_filename(dl_info)

        # ── تصحيح الامتداد ───────────────────────────────────────────────
        base = os.path.splitext(filename)[0]
        if qkey == "audio":
            filename = base + ".mp3"
        elif qkey == "direct":
            # yt-dlp يختار الامتداد — ابحث عن الملف كما هو
            if not os.path.exists(filename):
                candidates = list(DOWNLOAD_DIR.glob(f"{dl_info['id']}.*"))
                if candidates:
                    filename = str(candidates[0])
        else:
            if not filename.endswith('.mp4'):
                filename = base + ".mp4"

        if not os.path.exists(filename):
            cands = list(DOWNLOAD_DIR.glob(f"{dl_info['id']}.*"))
            if not cands:
                await status.edit_text("❌ الملف غير موجود."); return
            filename = str(cands[0])

        size = os.path.getsize(filename)

        # ── ضغط طارئ (فقط إذا لزم) ─────────────────────────────────────
        if size > MAX_SIZE:
            if qkey == "direct":
                await status.edit_text(
                    f"❌ الملف {size//1024//1024}MB > 50MB. جرب 360p أو 240p.")
                os.remove(filename); return

            await status.edit_text("📦 ضغط سريع...")
            compressed = str(DOWNLOAD_DIR / f"{dl_info['id']}_c.mp4")
            ret = await loop.run_in_executor(None, lambda: os.system(
                f"ffmpeg -i '{filename}' -vcodec libx264 -crf 35 -preset ultrafast "
                f"-acodec aac -b:a 32k -movflags +faststart '{compressed}' -y -loglevel quiet"
            ))
            if ret == 0 and os.path.exists(compressed):
                os.remove(filename)
                filename = compressed
                size = os.path.getsize(filename)
            if size > MAX_SIZE:
                await status.edit_text(f"❌ لا يزال كبيراً ({size//1024//1024}MB).")
                if os.path.exists(filename): os.remove(filename)
                return

        # ── رفع ─────────────────────────────────────────────────────────
        elapsed = int(time.time() - t0)
        await status.edit_text(f"📤 رفع {size//1024//1024}MB (تحميل: {elapsed}ث)...")

        title   = dl_info.get('title','')[:200]
        caption = f"<b>{title}</b>\n👤 {dl_info.get('uploader','')}\n\n📥 {BOT_NAME}"

        await send_file_fast(context, chat_id, filename, qkey, dl_info, caption)

        log_download(query.from_user.id, url, title, qkey, size)
        total = int(time.time() - t0)
        await status.edit_text(f"✅ تم في {total}ث ({size//1024//1024}MB)", parse_mode="HTML")

    except Exception as e:
        logger.error(str(e))
        await status.edit_text(f"❌ {str(e)[:300]}\n💡 جرب ⚡ مباشر أو 144p.")
    finally:
        # حذف الملف المحلي دائمًا لتوفير المساحة
        for f in DOWNLOAD_DIR.glob(f"{info.get('id','x')}*"):
            try: os.remove(f)
            except: pass

# ── تشغيل ─────────────────────────────────────────────────────────────────────
def main():
    app = (Application.builder()
           .token(BOT_TOKEN)
           .connect_timeout(30)
           .read_timeout(300)
           .write_timeout(300)
           .pool_timeout(60)
           .concurrent_updates(True)   # معالجة طلبات متعددة بالتوازي
           .build())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_received))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(quality_selected, pattern="^q\\|"))

    logger.info("🚀 YAMD يعمل بأقصى سرعة!")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,   # تجاهل الطلبات القديمة عند الإعادة
    )

if __name__ == "__main__":
    main()

