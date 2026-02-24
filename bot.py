# -*- coding: utf-8 -*-
"""
GOHAR-DL // HACKER STYLE (FINAL) — NO COOKIES — Public/Allowed content only

Supports (best-effort via yt-dlp):
YouTube / TikTok / Instagram / Facebook / X (public links)

Fixes:
- TikTok video now shows video qualities (not audio-only) by accepting MP4 progressive
- Size shown even when filesize missing (estimates via bitrate * duration)
- YouTube video-only streams are merged with best audio automatically
- If chosen format is progressive, download it directly (no merge needed)

Features:
- Hacker /start menu (buttons)
- Title + uploader + duration + size per quality (real or estimated)
- Quality selection REQUIRED (no auto-best)
- MIN quality start: 480p (change via /setminq)
- Audio-only MP3
- Progress bar (edits throttled)
- Duration limit: 3 hours
- /cleanup deletes downloaded files

Notes:
- Without cookies: private/age-restricted/login-required links may fail.
"""

import os
import re
import time
import threading
import shutil

import telebot
import yt_dlp

# =======================
# SETTINGS
# =======================
TOKEN = "PUT_YOUR_TOKEN_HERE"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_DURATION_SEC = 3 * 60 * 60      # 3 hours
MAX_FILE_MB = 1800                  # safety limit for sending
MIN_QUALITY_P = 480                 # show qualities from 480p+
PROGRESS_EDIT_SECONDS = 2           # progress message edit throttle
COOKIES_FILE = None                 # keep None (no cookies)

bot = telebot.TeleBot(TOKEN)
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

STATE = {}  # (chat_id, msg_id) -> {"url":..., "q_to_fmt":..., "q_sizes":..., "title":..., "duration":...}

# =======================
# HELPERS
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

def is_url(text: str) -> bool:
    return bool(text and URL_RE.search(text))

def estimate_filesize_bytes(f, duration):
    """
    If filesize is missing, estimate from total bitrate (tbr in kbps) * duration.
    """
    fs = f.get("filesize") or f.get("filesize_approx")
    if fs:
        return fs

    tbr = f.get("tbr")  # kbps
    if tbr and duration:
        return int((tbr * 1000 * duration) / 8)  # bits -> bytes

    return None


# =======================
# yt-dlp INFO
# =======================
def extract_info(url: str):
    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# =======================
# BUILD QUALITY CHOICES
# =======================
def build_video_choices(info):
    """
    Build quality buttons from:
    1) MP4 progressive (video+audio) — good for TikTok and many sites
    2) MP4 video-only — good for YouTube (we merge with best audio)
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

    # (1) MP4 progressive: v+a (fixes TikTok video buttons)
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none":
            continue
        if f.get("acodec") == "none":
            continue
        consider(f)

    # (2) MP4 video-only (YouTube)
    if not q_to_fmt:
        for f in formats:
            if f.get("ext") != "mp4":
                continue
            if f.get("vcodec") == "none":
                continue
            if f.get("acodec") != "none":
                continue
            consider(f)

    # (3) WEBM/MKV video-only fallback
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


# =======================
# PROGRESS TRACKER
# =======================
class ProgressTracker:
    def __init__(self, chat_id, status_msg_id):
        self.chat_id = chat_id
        self.status_msg_id = status_msg_id
        self.last_edit = 0
        self.last_text = ""

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

        text = f"⛓️  DOWNLOADING...\n{bar}  {pct:.1f}%\n⚡ SPD: {spd_txt}\n⏱️ ETA: {eta_txt}"
        if text != self.last_text:
            try:
                bot.edit_message_text(text, self.chat_id, self.status_msg_id)
                self.last_text = text
                self.last_edit = now
            except:
                pass


# =======================
# SEND + DELETE
# =======================
def safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except:
        pass

def send_with_limit(chat_id, file_path, mode):
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        bot.send_message(chat_id, f"❌ FILE TOO BIG: {size_mb:.1f}MB\n✅ اختر جودة أقل أو MP3.")
        return False

    if mode == "audio":
        with open(file_path, "rb") as f:
            bot.send_audio(chat_id, f)
    else:
        with open(file_path, "rb") as f:
            bot.send_video(chat_id, f)
    return True


# =======================
# DOWNLOAD WORKER
# =======================
def run_download(chat_id, origin_msg_id, mode, fmt_id=None):
    status = bot.send_message(chat_id, "⛓️ INIT...")
    tracker = ProgressTracker(chat_id, status.message_id)

    ts = int(time.time())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"%(title).80s_{ts}.%(ext)s")

    base_opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "progress_hooks": [tracker.hook],
    }

    try:
        st = STATE.get((chat_id, origin_msg_id))
        if not st:
            bot.edit_message_text("❌ SESSION EXPIRED. SEND LINK AGAIN.", chat_id, status.message_id)
            return

        url = st["url"]

        if mode == "audio":
            opts = {
                **base_opts,
                "format": "bestaudio/best",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                ],
            }
        else:
            if not fmt_id:
                bot.edit_message_text("❌ PICK A QUALITY BUTTON FIRST.", chat_id, status.message_id)
                return

            # Try progressive fmt_id first; if it needs audio merge, do fmt_id+bestaudio
            # This fixes TikTok progressive + YouTube video-only with merge.
            opts = {
                **base_opts,
                "format": f"{fmt_id}/{fmt_id}+bestaudio/best",
                "merge_output_format": "mp4",
            }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        # audio final file name
        if mode == "audio":
            base, _ = os.path.splitext(file_path)
            mp3 = base + ".mp3"
            if os.path.exists(mp3):
                file_path = mp3

        ok = send_with_limit(chat_id, file_path, mode)
        bot.edit_message_text("✅ DONE." if ok else "⚠️ DOWNLOADED BUT NOT SENT.", chat_id, status.message_id)
        safe_remove(file_path)

    except Exception as e:
        bot.edit_message_text(f"❌ ERROR: {type(e).__name__}", chat_id, status.message_id)


# =======================
# HACKER MENU (START)
# =======================
def hacker_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("🕶️ HOW TO", callback_data="menu_help"),
        telebot.types.InlineKeyboardButton("⚙ SETTINGS", callback_data="menu_settings"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("🎬 VIDEO", callback_data="menu_video"),
        telebot.types.InlineKeyboardButton("🎧 MP3", callback_data="menu_mp3"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("🧹 CLEANUP", callback_data="menu_cleanup"),
    )
    return kb


@bot.message_handler(commands=["start"])
def cmd_start(message):
    text = (
        "🟢  GOHAR-DL  //  ONLINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📌 Send a PUBLIC link:\n"
        "YouTube | TikTok | Instagram | Facebook | X\n\n"
        "🧩 Rules:\n"
        f"- Max Duration: {MAX_DURATION_SEC//3600}h\n"
        f"- Choose Quality (>= {MIN_QUALITY_P}p)\n"
        "- No cookies (private links may fail)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Select an option ↓"
    )
    bot.send_message(message.chat.id, text, reply_markup=hacker_menu())


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(
        message,
        "🕶️ HOW TO USE:\n"
        "1) Send a link\n"
        "2) Bot shows title/duration + quality buttons\n"
        "3) Pick quality OR MP3\n\n"
        "Commands:\n"
        "/start - menu\n"
        "/help - guide\n"
        "/cleanup - delete downloaded files\n"
        "/setminq 360|480|720 - set minimum quality buttons"
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
        bot.send_message(cid, "🕶️ Send link → choose quality button → download.\nUse /setminq to save space.")
    elif call.data == "menu_settings":
        bot.send_message(cid, "⚙ SETTINGS:\n/setminq 360  (save data)\n/setminq 480  (default)\n/setminq 720  (higher)")
    elif call.data == "menu_video":
        bot.send_message(cid, "🎬 Send a video link now. I will show quality buttons.")
    elif call.data == "menu_mp3":
        bot.send_message(cid, "🎧 Send a link, then choose MP3 button.")
    elif call.data == "menu_cleanup":
        clean_downloads()
        bot.send_message(cid, "🧹 CLEANUP DONE.")


# =======================
# URL HANDLER
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
            text_lines.append("Try another link or use MP3.")

        kb.add(
            telebot.types.InlineKeyboardButton(
                "🎧 MP3 ONLY",
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
        }

    except Exception as e:
        bot.reply_to(message, f"❌ SCAN FAILED: {type(e).__name__}")


# =======================
# DOWNLOAD BUTTONS
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

        bot.answer_callback_query(call.id, "⏳ STARTING...")

        if kind == "a":
            t = threading.Thread(target=run_download, args=(chat_id, msg_id, "audio", None), daemon=True)
            t.start()
            return

        if kind == "v":
            fmt_id = st["q_to_fmt"].get(q)
            if not fmt_id:
                bot.answer_callback_query(call.id, "QUALITY NOT AVAILABLE. SEND LINK AGAIN.")
                return
            t = threading.Thread(target=run_download, args=(chat_id, msg_id, "video", fmt_id), daemon=True)
            t.start()
            return

    except:
        try:
            bot.answer_callback_query(call.id, "ERROR")
        except:
            pass


print("Bot running...")
bot.infinity_polling()
