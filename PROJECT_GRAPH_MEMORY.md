# 🧠 ذاكرة الرسم البياني للمشروع (Project Graph Memory)

## 📌 معلومات المشروع الأساسية
- **اسم المشروع:** YAMD (Yet Another Media Downloader)
- **المطور الرئيسي:** يعقوب المهاجري (@Yaaqob_Almahajeri)
- **الإصدار الحالي:** 1.2.2
- **البيئة المستهدفة:** Python 3.11+, Telegram Bot API (Async), Linux/Docker
- **بيئة الاستضافة المدعومة:** Render.com (Web Service - Hybrid Polling/Webhook)

---

## 🗺️ الرسم البياني للمكونات والعلاقات (Component Graph)

```mermaid
graph TD
    User["Telegram User"] -->|Sends Link/Command| Dispatcher["Message Dispatcher (on_link)"]
    
    subgraph Hosting ["بنية الاستضافة وشبكة الويب"]
        Render["Render.com Web Service"] -->|Health Check GET /| WebServer["aiohttp Web Server (:PORT)"]
        WebServer -->|If WEBHOOK_URL Set| WebhookRouter["POST /{BOT_TOKEN} Handler"]
        WebhookRouter --> Dispatcher
        WebServer -->|If No WEBHOOK_URL| PollingEngine["Application.updater Long Polling"]
        PollingEngine --> Dispatcher
    end

    subgraph Routing ["توجيه الروابط"]
        Dispatcher -->|Pinterest Link| PinHandler["download_pinterest()"]
        Dispatcher -->|TikTok Link| TikTokHandler["download_tiktok()"]
        Dispatcher -->|Other Links| FormatHandler["show_formats() & do_download()"]
    end

    subgraph TikTok_Pipeline ["مسار معالجة TikTok"]
        TikTokHandler -->|Primary Strategy| TikWM["_fetch_tikwm_sync (POST/GET)"]
        TikWM -->|Direct Stream| StreamDownloader["_download_stream_sync (urllib)"]
        TikWM -->|Photos| PhotoGroup["send_media_group()"]
        
        TikWM -.->|Fallback if failed| YTDLP_Fallback["yt_dlp.YoutubeDL (clean headers, no impersonate)"]
    end

    subgraph Media_Delivery ["تسليم الوسائط والمعالجة"]
        StreamDownloader --> SizeCheck{"حجم الملف > 48MB؟"}
        YTDLP_Fallback --> SizeCheck
        SizeCheck -->|Yes| Splitter["split_video() via FFmpeg"]
        SizeCheck -->|No| DirectUpload["send_video()"]
        Splitter --> PartUpload["Send Split Parts"]
    end

    subgraph Persistence ["قاعدة البيانات والتنظيف"]
        DirectUpload --> DB["SQLite WAL (db_log)"]
        PartUpload --> DB
        DB --> Cleanup["cleanup(vid_id)"]
    end
```

---

## 🔗 سجل العلاقات بين الملفات والمكتبات (Node Dependencies)

| العقدة (Node) | الاعتماديات الأساسية (Dependencies) | المخرجات / التأثير |
| :--- | :--- | :--- |
| `bot.py` | `python-telegram-bot`, `yt-dlp`, `curl_cffi`, `ffmpeg`, `sqlite3`, `aiohttp` | تشغيل البوت وإدارة الطلبات المتزامنة وخدمة المنفذ للبيئة السحابية |
| `main` (Server) | `aiohttp.web`, `Application.updater` | فتح المنفذ الفوري لـ Render مع دعم Webhook أو Polling تلقائياً |
| `_fetch_tikwm_sync` | `urllib.request`, `urllib.parse`, `json` | استخراج روابط MP4 الأصلية بدون علامة مائية |
| `download_tiktok` | `_fetch_tikwm_sync`, `yt-dlp.YoutubeDL` | معالجة وتنزيل مقاطع تيك توك بأمان وتفادي AssertionError |
| `split_video` | `ffmpeg`, `ffprobe` | تقسيم المقاطع الأكبر من 48MB لأجزاء متوافقة مع تلغرام |
| `db_log` | `sqlite3` (WAL Mode) | تسجيل عمليات التحميل وإحصائيات المستخدمين |

---

## 🕒 سجل التعديلات المحدثة (State History)
- **2026-09-05 (v1.2.2):** حل مشكلة `Port scan timeout` على Render.com بربط خادم الويب فورياً على `$PORT` وتوفير دعم هجين (Hybrid) لـ Webhook و Polling ليعمل بسلاسة ومجاناً كـ Web Service.
- **2026-09-05 (v1.2.1):** إصلاح انهيار `AssertionError` في تيك توك عبر إزالة `impersonate` من الـ fallback وتعزيز TikWM بطلبات POST المباشرة.
