# -*- coding: utf-8 -*-
"""
GOHAR-DL PRO (NO COOKIES) — Railway Worker
Public/Allowed content only.

Platforms (best-effort via yt-dlp):
YouTube / TikTok / Instagram / Facebook / X

Features:
- Hacker-style /start menu
- Extract title/uploader/duration
- Quality selection REQUIRED (from MIN_QUALITY_P)
- Video download:
  - If chosen format is progressive (video+audio) -> direct
  - If video-only -> merge with bestaudio (ffmpeg)
- Audio download (fast): M4A (no mp3 conversion)
- Progress bar updates
- Duration limit: 3 hours
- Basic queue/concurrency limit to avoid server overload
- /cleanup deletes downloaded files
"""

import os
import re
import time
import shutil
import threading
from collections import deque

import telebot
import yt_dlp

# =======================
# ENV / SETTINGS
# =======================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if ":" not in TOKEN:
    raise ValueError("Token must contain a colon")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_DURATION_SEC = 3 * 60 * 60
MIN_QUALITY_P = 480
MAX_FILE_MB = 1800

# progress edit throttling
PROGRESS_EDIT_SECONDS = 2

# concurrency control
MAX_ACTIVE_JOBS = 2
_active_sem = threading.BoundedSemaphore(MAX_ACTIVE_JOBS)

# simple FIFO queue
_queue = deque()
_queue_lock = threading.Lock()
_queue_worker_started = False

bot = telebot.TeleBot(TOKEN, parse_mode=None)
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

# per-message state (chat_id, msg_id) -> dict
STATE = {}

SUPPORTED_HINT = "YouTube | TikTok | Instagram | Facebook | X (Public links)"

# =======================
# Helpers
# =======================
def fmt_dur(sec: int) -> str:
    if not sec:
        return "??"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def bytes_to_mb(b: int):
    if not b:
        return None
    return b / (1024 * 1024)

def fmt_mb(b: int) -> str:
    m = bytes_to_mb(b)
    return f"{m:.1f}MB" if m is not None else "??MB"

def progress_bar(pct: float) -> str:
    filled = int(round(pct / 10))
    filled = max(0, min(10, filled))
    return "▰" * filled + "▱" * (10 - filled)

def pick_bucket(height):
    if not height:
        return None
    common = [144, 240, 360, 480, 720, 1080, 1440, 2160]
    for q in common:
        if height <= q:
            return q
    return 2160

def clean_downloads():
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def estimate_filesize_bytes(fmt, duration):
    fs = fmt.get("filesize") or fmt.get("filesize_approx")
    if fs:
        return fs
    tbr = fmt.get("tbr")  # kbps
    if tbr and duration:
        return int((tbr * 1000 * duration) / 8)
    return None

def is_url(text: str) -> bool:
    return bool(text and URL_RE.search(text))

def safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except:
        pass

# =======================
# yt-dlp: extract info
# =======================
def extract_info(url: str):
    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

# =======================
# Build choices
# =======================
def build_video_choices(info):
    """
    Build quality choices from:
    1) MP4 progressive (video+audio) -> great for TikTok & many sites
    2) MP4 video-only -> great for YouTube, will merge with bestaudio
    3) WEBM/MKV video-only fallback
    """
    formats = info.get("formats") or []
    duration = info.get("duration") or 0

    q_to_fmt = {}
    q_sizes = {}

    def consider(f):
        h = f.get("height")
        q = pick_bucket(h)
        if not q:
            return
        fmt_id = f.get("format_id")
        if not fmt_id:
            return

        if q not in q_to_fmt:
            q_to_fmt[q] = fmt_id

        est = estimate_filesize_bytes(f, duration)
        if est:
            q_sizes[q] = max(q_sizes.get(q, 0) or 0, est)

    # 1) MP4 progressive (v+a)
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none":
            continue
        if f.get("acodec") == "none":
            continue
        consider(f)

    # 2) MP4 video-only
    if not q_to_fmt:
        for f in formats:
            if f.get("ext") != "mp4":
                continue
            if f.get("vcodec") == "none":
                continue
            if f.get("acodec") != "none":
                continue
            consider(f)

    # 3) fallback webm/mkv video-only
    if not q_to_fmt:
        for f in formats:
            if f.get("vcodec") == "none":
                continue
            if f.get("acodec") != "none":
                continue
            if f.get("ext") in ("webm", "mkv"):
                consider(f)

    qs = sorted(q_to_fmt.keys())
    return qs, q_to_fmt, q_sizes

def pick_audio_choice(info):
    """
    Choose a fast audio format:
    Prefer m4a/mp4 audio if available, else bestaudio.
    We'll download audio only and send as audio.
    """
    formats = info.get("formats") or []
    # prefer m4a
    for f in formats:
        if f.get("vcodec") != "none":
            continue
        if f.get("acodec") == "none":
            continue
        if f.get("ext") in ("m4a", "mp4"):
            return f.get("format_id")
    return None

# =======================
# Progress tracking
# =======================
class ProgressTracker:
    def __init__(self, chat_id, status_msg_id, title=""):
        self.chat_id = chat_id
        self.status_msg_id = status_msg_id
        self.last_edit = 0
        self.last_text = ""
        self.title = title[:60] if title else ""

    def hook(self, d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes") or 0
        if not total:
            return

        pct = (downloaded / total) * 100
        now = time.time()
        if now - self.last_edit < PROGRESS_EDIT_SECONDS:
            return

        bar = progress_bar(pct)
        spd = d.get("speed") or 0
        eta = d.get("eta")
        spd_txt = f"{bytes_to_mb(spd):.2f}MB/s" if spd else "?"
        eta_txt = f"{eta}s" if eta is not None else "?"

        text = (
            f"⛓️ DOWNLOADING...\n"
            f"{bar}  {pct:.1f}%\n"
            f"⚡ SPD: {spd_txt}\n"
            f"⏱️ ETA: {eta_txt}"
        )

        if text != self.last_text:
            try:
                bot.edit_message_text(text, self.chat_id, self.status_msg_id)
                self.last_text = text
                self.last_edit = now
            except:
                pass

# =======================
# Sending
# =======================
def send_with_limit(chat_id, file_path, kind):
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        bot.send_message(chat_id, f"❌ FILE TOO BIG: {size_mb:.1f}MB\n✅ اختار جودة أقل أو Audio.")
        return False

    if kind == "audio":
        with open(file_path, "rb") as f:
            bot.send_audio(chat_id, f)
    else:
        with open(file_path, "rb") as f:
            bot.send_video(chat_id, f)
    return True

# =======================
# Job queue worker
# =======================
def _ensure_queue_worker():
    global _queue_worker_started
    if _queue_worker_started:
        return
    _queue_worker_started = True
    t = threading.Thread(target=_queue_loop, daemon=True)
    t.start()

def _queue_loop():
    while True:
        job = None
        with _queue_lock:
            if _queue:
                job = _queue.popleft()

        if not job:
            time.sleep(0.2)
            continue

        _active_sem.acquire()
        try:
            job()
        finally:
            _active_sem.release()

def enqueue(job_callable):
    _ensure_queue_worker()
    with _queue_lock:
        _queue.append(job_callable)

# =======================
# Downloader worker
# =======================
def run_download(chat_id, origin_msg_id, mode, fmt_id=None):
    status = bot.send_message(chat_id, "⛓️ INIT...")
    st = STATE.get((chat_id, origin_msg_id))
    if not st:
        bot.edit_message_text("❌ SESSION EXPIRED. SEND LINK AGAIN.", chat_id, status.message_id)
        return

    url = st["url"]
    title = st.get("title", "")

    tracker = ProgressTracker(chat_id, status.message_id, title=title)
    ts = int(time.time())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"%(title).80s_{ts}.%(ext)s")

    base_opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "progress_hooks": [tracker.hook],
    }

    try:
        if mode == "audio":
            # prefer a known m4a/mp4 audio id if available; else bestaudio
            a_fmt = st.get("audio_fmt_id")
            fmt = a_fmt if a_fmt else "bestaudio/best"
            opts = {
                **base_opts,
                "format": fmt,
            }
        else:
            if not fmt_id:
                bot.edit_message_text("❌ PICK A QUALITY BUTTON FIRST.", chat_id, status.message_id)
                return

            # Try progressive format first; if needs audio merge, do fmt_id+bestaudio
            # Requires ffmpeg for merging (installed via nixpacks.toml)
            opts = {
                **base_opts,
                "format": f"{fmt_id}/{fmt_id}+bestaudio/best",
                "merge_output_format": "mp4",
            }

        bot.edit_message_text("⏳ STARTING DOWNLOAD...", chat_id, status.message_id)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        ok = send_with_limit(chat_id, file_path, "audio" if mode == "audio" else "video")
        bot.edit_message_text("✅ DONE." if ok else "⚠️ DOWNLOADED BUT NOT SENT.", chat_id, status.message_id)
        safe_remove(file_path)

    except Exception as e:
        bot.edit_message_text(f"❌ ERROR: {type(e).__name__}", chat_id, status.message_id)

# =======================
# UI: Hacker Menu
# =======================
def hacker_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("🕶️ HOW TO", callback_data="menu_help"),
        telebot.types.InlineKeyboardButton("⚙ SETTINGS", callback_data="menu_settings"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("🎬 VIDEO", callback_data="menu_video"),
        telebot.types.InlineKeyboardButton("🎧 AUDIO", callback_data="menu_audio"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("🧹 CLEANUP", callback_data="menu_cleanup"),
    )
    return kb

@bot.message_handler(commands=["start"])
def cmd_start(message):
    text = (
        "🟢 GOHAR-DL // ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 Supported: {SUPPORTED_HINT}\n"
        f"⏱️ Max duration: {MAX_DURATION_SEC//3600}h\n"
        f"🎚️ Min quality: {MIN_QUALITY_P}p\n"
        "🍪 No cookies: private/login links may fail\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Send a link ↓"
    )
    bot.send_message(message.chat.id, text, reply_markup=hacker_menu())

@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(
        message,
        "🕶️ HOW TO:\n"
        "1) Send a link\n"
        "2) Choose quality button (Video) OR Audio button\n\n"
        "Commands:\n"
        "/start - menu\n"
        "/help - guide\n"
        "/cleanup - delete files\n"
        "/setminq 360|480|720 - change min quality"
    )

@bot.message_handler(commands=["cleanup"])
def cmd_cleanup(message):
    clean_downloads()
    bot.reply_to(message, "🧹 CLEANED. downloads/ is empty.")

@bot.message_handler(commands=["setminq"])
def cmd_setminq(message):
    global MIN_QUALITY_P
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /setminq 360  (or 480 / 720)")
        return
    v = int(parts[1])
    if v < 144 or v > 2160:
        bot.reply_to(message, "Pick: 360 or 480 or 720 (etc).")
        return
    MIN_QUALITY_P = v
    bot.reply_to(message, f"✅ MIN QUALITY SET TO {MIN_QUALITY_P}p")

@bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
def menu_cb(call):
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    if call.data == "menu_help":
        bot.send_message(cid, "Send link → choose quality → download.\nUse /setminq to save space.")
    elif call.data == "menu_settings":
        bot.send_message(cid, "⚙ SETTINGS:\n/setminq 360 (save data)\n/setminq 480 (default)\n/setminq 720 (higher)")
    elif call.data == "menu_video":
        bot.send_message(cid, "🎬 Send a video link now. I will show quality buttons.")
    elif call.data == "menu_audio":
        bot.send_message(cid, "🎧 Send a link, then choose AUDIO button.")
    elif call.data == "menu_cleanup":
        clean_downloads()
        bot.send_message(cid, "🧹 CLEANUP DONE.")

# =======================
# Link handler
# =======================
@bot.message_handler(func=lambda m: is_url(m.text))
def on_url(message):
    url = URL_RE.search(message.text).group(1).strip()
    bot.reply_to(message, "🔎 SCANNING...")

    try:
        info = extract_info(url)

        title = info.get("title", "NO_TITLE")
        duration = info.get("duration") or 0
        uploader = info.get("uploader") or info.get("channel") or "UNKNOWN"

        if duration and duration > MAX_DURATION_SEC:
            bot.reply_to(message, f"❌ TOO LONG: {fmt_dur(duration)} (MAX 3h)")
            return

        qs, q_to_fmt, q_sizes = build_video_choices(info)
        qs_shown = [q for q in qs if q >= MIN_QUALITY_P]

        audio_fmt_id = pick_audio_choice(info)

        text_lines = [
            "🧾 TARGET LOCKED:",
            f"• TITLE: {title}",
            f"• SRC: {uploader}",
            f"• DUR: {fmt_dur(duration)}",
            "",
            f"🎬 PICK QUALITY (>= {MIN_QUALITY_P}p):"
        ]

        kb = telebot.types.InlineKeyboardMarkup(row_width=1)

        if qs_shown:
            for q in qs_shown:
                kb.add(
                    telebot.types.InlineKeyboardButton(
                        f"🎥 {q}p  [{fmt_mb(q_sizes.get(q))}]",
                        callback_data=f"v|{message.chat.id}|{message.message_id}|{q}"
                    )
                )
        else:
            text_lines.append("⚠️ NO QUALITIES FOUND FOR THIS LINK.")
            text_lines.append("Try another link or use AUDIO.")

        kb.add(
            telebot.types.InlineKeyboardButton(
                "🎧 AUDIO (M4A)",
                callback_data=f"a|{message.chat.id}|{message.message_id}|0"
            )
        )

        bot.send_message(message.chat.id, "\n".join(text_lines), reply_markup=kb)

        STATE[(message.chat.id, message.message_id)] = {
            "url": url,
            "title": title,
            "duration": duration,
            "q_to_fmt": q_to_fmt,
            "q_sizes": q_sizes,
            "audio_fmt_id": audio_fmt_id,
        }

    except Exception as e:
        bot.reply_to(message, f"❌ SCAN FAILED: {type(e).__name__}\nMake sure the link is public.")

# =======================
# Download buttons
# =======================
@bot.callback_query_handler(func=lambda call: call.data.startswith(("v|", "a|")))
def dl_cb(call):
    try:
        parts = call.data.split("|")
        kind = parts[0]  # v / a
        chat_id = int(parts[1])
        msg_id = int(parts[2])
        q = int(parts[3])

        st = STATE.get((chat_id, msg_id))
        if not st:
            bot.answer_callback_query(call.id, "SESSION EXPIRED. SEND LINK AGAIN.")
            return

        bot.answer_callback_query(call.id, "⏳ QUEUED...")

        if kind == "a":
            enqueue(lambda: run_download(chat_id, msg_id, "audio", None))
            return

        if kind == "v":
            fmt_id = st["q_to_fmt"].get(q)
            if not fmt_id:
                bot.answer_callback_query(call.id, "QUALITY NOT AVAILABLE. SEND LINK AGAIN.")
                return
            enqueue(lambda: run_download(chat_id, msg_id, "video", fmt_id))
            return

    except:
        try:
            bot.answer_callback_query(call.id, "ERROR")
        except:
            pass

print("Bot running...", flush=True)
bot.infinity_polling()
