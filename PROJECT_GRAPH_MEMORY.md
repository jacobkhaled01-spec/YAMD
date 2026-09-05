# 🧠 ذاكرة الرسم البياني للمشروع (Project Graph Memory)

## 📌 معلومات المشروع الأساسية
- **اسم المشروع:** YAMD (Yet Another Media Downloader)
- **المطور الرئيسي:** يعقوب المهاجري (@Yaaqob_Almahajeri)
- **الإصدار الحالي:** 1.2.1
- **البيئة المستهدفة:** Python 3.11+, Telegram Bot API (Async), Linux/Docker

---

## 🗺️ الرسم البياني للمكونات والعلاقات (Component Graph)

```mermaid
graph TD
    User["Telegram User"] -->|Sends Link/Command| Dispatcher["Message Dispatcher (on_link)"]
    
    subgraph Routing ["توجيه الروابط"]
        Dispatcher -->|Pinterest Link| PinHandler["download_pinterest()"]
        Dispatcher -->|TikTok Link| TikTokHandler["download_tiktok()"]
        Dispatcher -->|Other Links| FormatHandler["show_formats() & do_download()"]
    end

    subgraph TikTok_Pipeline ["مسار معالجة TikTok"]
        TikTokHandler -->|Primary Strategy| TikWM["_fetch_tikwm_sync (POST/GET)"]
        TikWM -->|Direct Stream| StreamDownloader["_download_stream_sync (urllib)"]
        TikWM -->|Photos| PhotoGroup["send_media_group()"]
        
        TikWM -.->|Fallback if failed| YTDLP_Fallback["yt_dlp.YoutubeDL (impersonate=chrome)"]
        YTDLP_Fallback -->|Target Type Safe| ImpersonateTarget["ImpersonateTarget.from_str()"]
        YTDLP_Fallback -.->|Retry on Error| YTDLP_NoImpersonate["yt_dlp without impersonate"]
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
| `bot.py` | `python-telegram-bot`, `yt-dlp`, `curl_cffi`, `ffmpeg`, `sqlite3` | تشغيل البوت وإدارة الطلبات المتزامنة |
| `_fetch_tikwm_sync` | `urllib.request`, `urllib.parse`, `json` | استخراج روابط MP4 الأصلية بدون علامة مائية |
| `download_tiktok` | `_fetch_tikwm_sync`, `yt-dlp.YoutubeDL`, `ImpersonateTarget` | معالجة وتنزيل مقاطع تيك توك بأمان وتفادي AssertionError |
| `split_video` | `ffmpeg`, `ffprobe` | تقسيم المقاطع الأكبر من 48MB لأجزاء متوافقة مع تلغرام |
| `db_log` | `sqlite3` (WAL Mode) | تسجيل عمليات التحميل وإحصائيات المستخدمين |

---

## 🕒 سجل التعديلات المحدثة (State History)
- **2026-09-05:** إصلاح انهيار `AssertionError` الناتج عن تمرير سلسلة نصية لخيار `impersonate` في `yt-dlp`، وتحديثه لـ `ImpersonateTarget.from_str("chrome")` مع آلية Fallback مرنة ودعم POST لـ TikWM.
