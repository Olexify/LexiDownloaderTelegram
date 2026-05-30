import os
import re
import json
import math
import shutil
import tempfile
import subprocess
import socket
import ipaddress
import time
import random
import html as html_lib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
import asyncio

from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    filters,
)
from telegram.helpers import escape_markdown

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Check your .env file.")

# Admin IDs: comma-separated list in env, e.g. ADMIN_IDS=12345,67890
ADMIN_IDS = {
    int(x) for x in (os.getenv("ADMIN_IDS") or "").replace(" ", "").split(",") if x
}

SETTINGS_FILE = Path("user_settings.json")
USERS_FILE = Path("users_db.json")

USER_URLS: dict[int, dict] = {}
USER_LAST_REQUEST: dict[int, float] = {}

# Platforms and security limits
PLATFORM_HOSTS = {
    "youtube": {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    },
    "instagram": {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
    },
    "tiktok": {
        "tiktok.com",
        "www.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    },
    "twitter": {
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
        "x.com",
        "www.x.com",
    },
    "pinterest": {
        "pinterest.com",
        "www.pinterest.com",
        "pin.it",
    },
    "spotify": {
        "spotify.com",
        "www.spotify.com",
        "open.spotify.com",
        "play.spotify.com",
    },
    "facebook": {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "fb.watch",
    },
    "reddit": {
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "v.redd.it",
        "redd.it",
        "www.redd.it",
    },
}

ALLOWED_HOSTS = set().union(*PLATFORM_HOSTS.values())
SUPPORTED_PLATFORMS_TEXT = "YouTube, Instagram, TikTok, Twitter/X, Pinterest, Facebook, Reddit"

RATE_LIMIT_SECONDS = 1
MAX_DURATION_SECONDS = 60 * 60 * 24  # 24 hours

# Telegram Bot API upload limit is 50 MB for bots (all file types) unless using a local Bot API server.[web:11][web:10]
BOT_FILE_LIMIT_BYTES = int(os.getenv("BOT_FILE_LIMIT_BYTES", str(50 * 1024 * 1024)))
MAX_FILESIZE_BYTES = BOT_FILE_LIMIT_BYTES  # global guard, do not exceed bot upload capabilities

FREE_TOKENS_PER_MONTH = 100

# More generous ffmpeg timeout (seconds)
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "900"))

DEFAULT_SETTINGS = {
    "video_format": "mp4",
    "audio_format": "mp3",
    "show_size": True,
}

VIDEO_QUALITIES = {
    "144p": "bestvideo[height<=144]+bestaudio/best[height<=144]",
    "240p": "bestvideo[height<=240]+bestaudio/best[height<=240]",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "1440p": "bestvideo[height<=1440]+bestaudio/best[height<=1440]",
    "2160p": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
}

AUDIO_QUALITIES = {
    "low": {"selector": "bestaudio[abr<=64]/bestaudio", "quality": "10", "bitrate_kbps": 64},
    "medium": {"selector": "bestaudio[abr<=128]/bestaudio", "quality": "5", "bitrate_kbps": 128},
    "high": {"selector": "bestaudio/bestaudio[abr<=320]", "quality": "0", "bitrate_kbps": 192},
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?$")

# Optional cookie files per platform (Netscape cookie format)
COOKIE_FILES = {
    "youtube": os.getenv("YOUTUBE_COOKIES_FILE") or None,
    "instagram": os.getenv("INSTAGRAM_COOKIES_FILE") or None,
    "tiktok": os.getenv("TIKTOK_COOKIES_FILE") or None,
    "twitter": os.getenv("TWITTER_COOKIES_FILE") or None,
    "pinterest": os.getenv("PINTEREST_COOKIES_FILE") or None,
    "spotify": os.getenv("SPOTIFY_COOKIES_FILE") or None,
    "facebook": os.getenv("FACEBOOK_COOKIES_FILE") or None,
    "reddit": os.getenv("REDDIT_COOKIES_FILE") or None,
}

# Token packs for Stars payments
TOKEN_PACKS = {
    "pack_50":   {"tokens": 50,    "stars": 50,    "title": "50 tokens",    "description": "50 download tokens"},
    "pack_500":  {"tokens": 500,   "stars": 490,   "title": "500 tokens",   "description": "500 download tokens"},
    "pack_1000": {"tokens": 1000,  "stars": 970,   "title": "1000 tokens",  "description": "1000 download tokens"},
    "pack_10000": {"tokens": 10000, "stars": 9500, "title": "10000 tokens", "description": "10000 download tokens"},
}

BONUS_RANGES = {
    "pack_50": (1, 10),
    "pack_500": (5, 15),
    "pack_1000": (10, 35),
    "pack_10000": (125, 500),
}


# ---------- Security helpers ----------

def is_public_ip(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
        for info in infos:
            ip = info[4][0]
            addr = ipaddress.ip_address(ip)
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_multicast
                or addr.is_reserved
                or addr.is_unspecified
            ):
                return False
        return True
    except Exception:
        return False


def classify_platform(hostname: str) -> str | None:
    for name, hosts in PLATFORM_HOSTS.items():
        if hostname in hosts:
            return name
    return None


def normalize_and_validate_url(raw_url: str) -> tuple[str | None, str | None]:
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return None, None

    if "://" not in raw_url:
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)
    if parsed.scheme != "https":
        return None, None

    hostname = (parsed.hostname or "").lower().strip(".")
    if not hostname:
        return None, None

    if hostname not in ALLOWED_HOSTS:
        return None, None

    if not is_public_ip(hostname):
        return None, None

    platform = classify_platform(hostname)
    if not platform:
        return None, None

    return raw_url, platform


def validate_extracted_info(info: dict):
    webpage_url = info.get("webpage_url") or info.get("original_url") or ""
    normalized, platform = normalize_and_validate_url(webpage_url)
    if not normalized or not platform:
        raise ValueError(f"Only direct links from {SUPPORTED_PLATFORMS_TEXT} are allowed.")

    duration = info.get("duration")
    if duration and duration > MAX_DURATION_SECONDS:
        raise ValueError("Video is too long.")

    filesize = info.get("filesize") or info.get("filesize_approx")
    # Do not allow anything > MAX_FILESIZE_BYTES for any platform.
    if filesize and filesize > MAX_FILESIZE_BYTES:
        raise ValueError(
            f"File is too large ({format_size(filesize)}). Telegram bots can only send up to "
            f"{format_size(MAX_FILESIZE_BYTES)} per file."
        )


def check_rate_limit(user_id: int):
    now = time.time()
    last = USER_LAST_REQUEST.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        raise ValueError("Too many requests. Please wait a few seconds.")
    USER_LAST_REQUEST[user_id] = now


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------- Settings persistence ----------

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_user_settings(user_id: int):
    data = load_settings()
    user_key = str(user_id)
    if user_key not in data:
        data[user_key] = DEFAULT_SETTINGS.copy()
        save_settings(data)
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data[user_key])
    return merged


def set_user_setting(user_id: int, key: str, value):
    data = load_settings()
    user_key = str(user_id)
    current = DEFAULT_SETTINGS.copy()
    current.update(data.get(user_key, {}))
    current[key] = value
    data[user_key] = current
    save_settings(data)


# ---------- User DB (tokens, stats, bans) ----------

def load_users_db() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_users_db(db: dict):
    USERS_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")


def current_month_key() -> str:
    t = time.localtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


def _recompute_total_tokens(rec: dict) -> None:
    monthly = float(rec.get("monthly_tokens", 0.0))
    purchased = float(rec.get("purchased_tokens", 0.0))
    rec["tokens"] = monthly + purchased


def ensure_user_record(tg_user) -> dict:
    db = load_users_db()
    user_id = str(tg_user.id)
    month_key = current_month_key()

    name_parts = []
    if tg_user.first_name:
        name_parts.append(tg_user.first_name)
    if tg_user.last_name:
        name_parts.append(tg_user.last_name)
    name = " ".join(name_parts) if name_parts else (tg_user.username or str(tg_user.id))

    rec = db.get(user_id)

    if not rec:
        rec = {
            "id": tg_user.id,
            "name": name,
            "monthly_tokens": float(FREE_TOKENS_PER_MONTH),
            "purchased_tokens": 0.0,
            "monthly_tokens_spent": 0.0,
            "purchased_tokens_spent": 0.0,
            "tokens_spent": 0.0,
            "tokens_purchased": 0.0,
            "downloads": 0,
            "banned": False,
            "created_at": int(time.time()),
            "last_reset_month": month_key,
            "pay_ids": [],
        }
        _recompute_total_tokens(rec)
    else:
        rec["name"] = name

        if "monthly_tokens" not in rec or "purchased_tokens" not in rec:
            old_total = float(rec.get("tokens", 0.0))
            old_purchased = float(rec.get("tokens_purchased", 0.0))
            monthly = max(0.0, old_total - old_purchased)
            rec["monthly_tokens"] = monthly
            rec["purchased_tokens"] = max(0.0, old_purchased)
            old_spent = float(rec.get("tokens_spent", 0.0))
            rec.setdefault("monthly_tokens_spent", 0.0)
            rec.setdefault("purchased_tokens_spent", old_spent)

        rec.setdefault("monthly_tokens_spent", 0.0)
        rec.setdefault("purchased_tokens_spent", 0.0)
        rec.setdefault("tokens_spent", 0.0)
        rec.setdefault("tokens_purchased", 0.0)

        if rec.get("last_reset_month") != month_key:
            rec["monthly_tokens"] = float(FREE_TOKENS_PER_MONTH)
            rec["monthly_tokens_spent"] = 0.0
            rec["last_reset_month"] = month_key

        _recompute_total_tokens(rec)

    db[user_id] = rec
    save_users_db(db)
    return rec


def consume_tokens(user_id: int, amount: float) -> dict | None:
    db = load_users_db()
    key = str(user_id)
    rec = db.get(key)
    if not rec:
        return None

    rec.setdefault("monthly_tokens", float(FREE_TOKENS_PER_MONTH))
    rec.setdefault("purchased_tokens", 0.0)
    rec.setdefault("monthly_tokens_spent", 0.0)
    rec.setdefault("purchased_tokens_spent", 0.0)
    rec.setdefault("tokens_spent", 0.0)

    monthly = float(rec.get("monthly_tokens", 0.0))
    purchased = float(rec.get("purchased_tokens", 0.0))

    amount = max(0.0, float(amount))

    from_monthly = min(monthly, amount) if monthly > 0 else 0.0
    leftover = amount - from_monthly
    from_purchased = leftover

    rec["monthly_tokens"] = monthly - from_monthly
    rec["purchased_tokens"] = purchased - from_purchased

    rec["monthly_tokens_spent"] = float(rec.get("monthly_tokens_spent", 0.0)) + from_monthly
    rec["purchased_tokens_spent"] = float(rec.get("purchased_tokens_spent", 0.0)) + from_purchased
    rec["tokens_spent"] = float(rec.get("tokens_spent", 0.0)) + amount

    _recompute_total_tokens(rec)

    db[key] = rec
    save_users_db(db)
    return rec


def update_user_after_download(user_id: int, tokens_used: float):
    db = load_users_db()
    key = str(user_id)
    rec = db.get(key)
    if not rec:
        return
    rec["downloads"] = rec.get("downloads", 0) + 1
    save_users_db(db)
    consume_tokens(user_id, tokens_used)


def grant_tokens_to_user(target_id: int, amount: float) -> dict | None:
    db = load_users_db()
    key = str(target_id)
    rec = db.get(key)
    if not rec:
        return None

    rec.setdefault("monthly_tokens", float(FREE_TOKENS_PER_MONTH))
    rec.setdefault("purchased_tokens", 0.0)
    rec.setdefault("monthly_tokens_spent", 0.0)
    rec.setdefault("purchased_tokens_spent", 0.0)
    rec.setdefault("tokens_spent", 0.0)
    rec.setdefault("tokens_purchased", 0.0)

    rec["purchased_tokens"] = float(rec.get("purchased_tokens", 0.0)) + float(amount)
    rec["tokens_purchased"] = float(rec.get("tokens_purchased", 0.0)) + float(amount)
    _recompute_total_tokens(rec)

    db[key] = rec
    save_users_db(db)
    return rec


def set_user_ban(target_id: int, banned: bool) -> dict | None:
    db = load_users_db()
    key = str(target_id)
    rec = db.get(key)
    if not rec:
        return None
    rec["banned"] = bool(banned)
    db[key] = rec
    save_users_db(db)
    return rec


def record_successful_payment(user_id: int, charge_id: str, pack_id: str) -> dict | None:
    pack = TOKEN_PACKS.get(pack_id)
    if not pack:
        return None
    db = load_users_db()
    key = str(user_id)
    rec = db.get(key)
    if not rec:
        return None

    rec.setdefault("monthly_tokens", float(FREE_TOKENS_PER_MONTH))
    rec.setdefault("purchased_tokens", 0.0)
    rec.setdefault("monthly_tokens_spent", 0.0)
    rec.setdefault("purchased_tokens_spent", 0.0)
    rec.setdefault("tokens_spent", 0.0)
    rec.setdefault("tokens_purchased", 0.0)

    pay_ids = set(rec.get("pay_ids", []))
    if charge_id in pay_ids:
        return rec
    pay_ids.add(charge_id)
    rec["pay_ids"] = list(pay_ids)

    rec["purchased_tokens"] = float(rec.get("purchased_tokens", 0.0)) + float(pack["tokens"])
    rec["tokens_purchased"] = float(rec.get("tokens_purchased", 0.0)) + float(pack["tokens"])
    _recompute_total_tokens(rec)

    db[key] = rec
    save_users_db(db)
    return rec


def calculate_tokens_for_size(size_bytes: int, media_type: str) -> float:
    if media_type == "image":
        return 0.2

    size_mb = size_bytes / (1024 * 1024)

    if size_mb < 2:
        return 0.2
    if size_mb < 10:
        return 0.5

    if size_mb <= 8:
        return 0.0
    return float(max(1, math.ceil((size_mb - 8) / 10)))


# ---------- Utility ----------

def format_size(size_bytes: int | float | None) -> str:
    if size_bytes is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "MB":
                return f"{size:.1f}mb"
            if unit == "GB":
                return f"{size:.2f}gb"
            if unit == "KB":
                return f"{size:.0f}kb"
            return f"{size:.0f}{unit.lower()}"
        size /= 1024


def valid_time(value: str) -> bool:
    return bool(TIME_RE.match(value.strip()))


def ffmpeg_timestamp(value: str) -> str:
    parts = value.strip().split(":")
    if len(parts) == 2:
        mm, ss = parts
        return f"00:{int(mm):02d}:{ss}"
    if len(parts) == 3:
        hh, mm, ss = parts
        return f"{int(hh):02d}:{int(mm):02d}:{ss}"
    raise ValueError("Invalid time format")


def trim_args(start: str | None, end: str | None):
    args = []
    if start:
        args += ["-ss", ffmpeg_timestamp(start)]
    if end:
        args += ["-to", ffmpeg_timestamp(end)]
    return args


def parse_link_message(text: str):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    url, platform = normalize_and_validate_url(lines[0])
    if not url or not platform:
        return None

    start = None
    end = None
    mode = None
    for line in lines[1:]:
        low = line.lower()
        if low.startswith("start="):
            start = line.split("=", 1)[1].strip()
        elif low.startswith("end="):
            end = line.split("=", 1)[1].strip()
        elif low.startswith("mode="):
            mode = line.split("=", 1)[1].strip().lower()
    if start and not valid_time(start):
        raise ValueError("Invalid start time. Use mm:ss, hh:mm:ss, or with .ms")
    if end and not valid_time(end):
        raise ValueError("Invalid end time. Use mm:ss, hh:mm:ss, or with .ms")
    return {"url": url, "start": start, "end": end, "mode": mode, "platform": platform}


def parse_duration_seconds(value: str | None) -> float | None:
    if not value:
        return None
    parts = value.split(":")
    try:
        if len(parts) == 2:
            mm, ss = parts
            return int(mm) * 60 + float(ss)
        if len(parts) == 3:
            hh, mm, ss = parts
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except ValueError:
        return None
    return None


def effective_duration(info: dict, start: str | None, end: str | None) -> float | None:
    total = info.get("duration")
    if start or end:
        start_s = parse_duration_seconds(start) or 0
        end_s = parse_duration_seconds(end) if end else total
        if end_s is not None:
            return max(0, end_s - start_s)
    return total


def get_size_value(fmt: dict) -> int | None:
    return fmt.get("filesize") or fmt.get("filesize_approx")


def estimate_video_size(info: dict, selector_label: str) -> int | None:
    if not info:
        return None
    target_h = int(selector_label.replace("p", ""))
    formats = info.get("formats", [])
    video_candidates = [
        f for f in formats
        if f.get("vcodec") not in (None, "none") and (f.get("height") or 0) <= target_h
    ]
    audio_candidates = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") == "none"
    ]
    if not video_candidates:
        return None
    best_video = max(video_candidates, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))
    best_audio = max(audio_candidates, key=lambda f: (f.get("abr") or f.get("tbr") or 0), default=None)
    total = 0
    got_any = False
    for item in (best_video, best_audio):
        if not item:
            continue
        size = get_size_value(item)
        if size:
            total += size
            got_any = True
    if got_any:
        return total
    duration = info.get("duration")
    if duration:
        v_tbr = best_video.get("tbr") or 0
        a_tbr = (best_audio.get("abr") if best_audio else 0) or (best_audio.get("tbr") if best_audio else 0) or 0
        total_kbps = v_tbr + a_tbr
        if total_kbps > 0:
            return int(duration * total_kbps * 1000 / 8)
    return None


def estimate_audio_size(info: dict, quality_key: str, start: str | None, end: str | None) -> int | None:
    duration = effective_duration(info, start, end)
    if not duration:
        return None
    bitrate_kbps = AUDIO_QUALITIES[quality_key]["bitrate_kbps"]
    return int(duration * bitrate_kbps * 1000 / 8)


def build_size_suffix(size_bytes: int | None, show_size: bool) -> str:
    if not show_size:
        return ""
    if size_bytes is None:
        return " ~?"
    return f" {format_size(size_bytes)}"


# ---------- Keyboards ----------

def all_options_keyboard(user_id: int, info: dict | None = None, start: str | None = None, end: str | None = None):
    settings = get_user_settings(user_id)
    show_size = settings.get("show_size", True)

    def v(label: str):
        estimate = estimate_video_size(info, label) if info else None
        return InlineKeyboardButton(f"🎬 {label}{build_size_suffix(estimate, show_size)}", callback_data=f"video:{label}")

    def a(label: str, key: str):
        estimate = estimate_audio_size(info, key, start, end) if info else None
        return InlineKeyboardButton(f"🎵 {label}{build_size_suffix(estimate, show_size)}", callback_data=f"audio:{key}")

    return InlineKeyboardMarkup([
        [v("144p"), v("240p"), v("360p")],
        [v("480p"), v("720p"), v("1080p")],
        [v("1440p"), v("2160p")],
        [a("Low", "low"), a("Med", "medium"), a("High", "high")],
        [
            InlineKeyboardButton("⭕ Circle", callback_data="extra:circle"),
            InlineKeyboardButton("🎙️ Voice", callback_data="extra:voice"),
            InlineKeyboardButton("🖼 Image", callback_data="extra:image"),
        ],
        [InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings")],
        [InlineKeyboardButton("📋 Copy trim template", callback_data="template:trim")],
    ])


def settings_keyboard(user_id: int):
    s = get_user_settings(user_id)
    size_state = "✅ On" if s.get("show_size", True) else "❌ Off"
    toggle_target = "false" if s.get("show_size", True) else "true"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Video format: {s['video_format']}", callback_data="noop")],
        [InlineKeyboardButton("📼 MP4", callback_data="apply:video_format:mp4"),
         InlineKeyboardButton("📦 MKV", callback_data="apply:video_format:mkv")],
        [InlineKeyboardButton(f"🎵 Audio format: {s['audio_format']}", callback_data="noop")],
        [InlineKeyboardButton("🎧 MP3", callback_data="apply:audio_format:mp3"),
         InlineKeyboardButton("🎶 M4A", callback_data="apply:audio_format:m4a"),
         InlineKeyboardButton("🗣️ OGG", callback_data="apply:audio_format:ogg")],
        [InlineKeyboardButton(f"📏 Show size: {size_state}", callback_data=f"apply:show_size:{toggle_target}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:root")],
    ])


def leaderboard_view(page: int, page_size: int) -> tuple[str, InlineKeyboardMarkup]:
    db = load_users_db()
    users = list(db.values())
    users.sort(key=lambda u: u.get("downloads", 0), reverse=True)
    total = len(users)
    if total == 0:
        text = "🏆 Leaderboard\n\nNo users yet."
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️", callback_data="lb:0:15"),
            InlineKeyboardButton("15", callback_data="lb:0:15"),
            InlineKeyboardButton("25", callback_data="lb:0:25"),
            InlineKeyboardButton("50", callback_data="lb:0:50"),
            InlineKeyboardButton("100", callback_data="lb:0:100"),
            InlineKeyboardButton("➡️", callback_data="lb:0:15"),
        ]])
        return text, kb

    page_size = max(1, min(100, page_size))
    max_page = max(0, (total - 1) // page_size)
    page = max(0, min(page, max_page))

    start = page * page_size
    end = min(start + page_size, total)
    slice_users = users[start:end]

    lines = [
        f"🏆 Leaderboard (page {page + 1}/{max_page + 1})",
        f"Showing users {start + 1}–{end} of {total}",
        "",
    ]
    for i, u in enumerate(slice_users, start=start + 1):
        lines.append(
            f"{i}. 👤 {u.get('name')} - 📥 {u.get('downloads', 0)} downloads"
        )

    left_page = page - 1 if page > 0 else page
    right_page = page + 1 if page < max_page else page

    nav_row = [
        InlineKeyboardButton("⬅️", callback_data=f"lb:{left_page}:{page_size}"),
        InlineKeyboardButton("15", callback_data="lb:0:15"),
        InlineKeyboardButton("25", callback_data="lb:0:25"),
        InlineKeyboardButton("50", callback_data="lb:0:50"),
        InlineKeyboardButton("100", callback_data="lb:0:100"),
        InlineKeyboardButton("➡️", callback_data=f"lb:{right_page}:{page_size}"),
    ]

    kb = InlineKeyboardMarkup([nav_row])
    return "\n".join(lines), kb


def admin_users_view(page: int, page_size: int) -> tuple[str, InlineKeyboardMarkup]:
    db = load_users_db()
    users = list(db.values())
    users.sort(key=lambda u: u.get("created_at", 0))
    total = len(users)
    if total == 0:
        text = "👷 Admin • Users\n\nNo users in DB yet."
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️", callback_data="adminlist:0:15"),
            InlineKeyboardButton("15", callback_data="adminlist:0:15"),
            InlineKeyboardButton("25", callback_data="adminlist:0:25"),
            InlineKeyboardButton("50", callback_data="adminlist:0:50"),
            InlineKeyboardButton("100", callback_data="adminlist:0:100"),
            InlineKeyboardButton("➡️", callback_data="adminlist:0:15"),
        ]])
        return text, kb

    page_size = max(1, min(100, page_size))
    max_page = max(0, (total - 1) // page_size)
    page = max(0, min(page, max_page))

    start = page * page_size
    end = min(start + page_size, total)
    slice_users = users[start:end]

    lines = [
        f"👷 Admin • Users (page {page + 1}/{max_page + 1})",
        f"Showing users {start + 1}–{end} of {total}",
        "",
    ]
    for u in slice_users:
        lines.append(
            f"👤 {u.get('name')} (id={u['id']})\n"
            f"   💰 total={u.get('tokens', 0.0):.1f} "
            f"| 🗓 monthly={u.get('monthly_tokens', 0.0):.1f} "
            f"| 🎁 purchased={u.get('purchased_tokens', 0.0):.1f}\n"
            f"   🧾 spent={u.get('tokens_spent', 0.0):.1f} "
            f"| 📥 downloads={u.get('downloads', 0)} "
            f"| 🚫 banned={u.get('banned', False)}"
        )

    left_page = page - 1 if page > 0 else page
    right_page = page + 1 if page < max_page else page

    nav_row = [
        InlineKeyboardButton("⬅️", callback_data=f"adminlist:{left_page}:{page_size}"),
        InlineKeyboardButton("15", callback_data="adminlist:0:15"),
        InlineKeyboardButton("25", callback_data="adminlist:0:25"),
        InlineKeyboardButton("50", callback_data="adminlist:0:50"),
        InlineKeyboardButton("100", callback_data="adminlist:0:100"),
        InlineKeyboardButton("➡️", callback_data=f"adminlist:{right_page}:{page_size}"),
    ]

    kb = InlineKeyboardMarkup([nav_row])
    return "\n".join(lines), kb


# ---------- yt-dlp + ffmpeg ----------

def extract_final_file(outdir: Path) -> Path:
    files = [p for p in outdir.iterdir() if p.is_file()]
    media_files = [
        p for p in files
        if p.suffix.lower() not in {'.part', '.ytdl', '.json', '.jpg', '.jpeg', '.png', '.webp', '.txt'}
    ]
    if not media_files:
        raise RuntimeError("No file produced.")
    return max(media_files, key=lambda p: p.stat().st_size)


def base_ydl_opts_for_url(url: str) -> tuple[dict, str]:
    normalized, platform = normalize_and_validate_url(url)
    if not normalized or not platform:
        raise ValueError(f"Unsupported or unsafe URL. Only {SUPPORTED_PLATFORMS_TEXT} are allowed.")
    parsed = urlparse(normalized)

    if platform == "spotify":
        raise ValueError("Spotify audio is DRM protected and not supported by this bot.")

    if platform == "instagram" and "stories" in (parsed.path or "").lower():
        if not COOKIE_FILES.get("instagram"):
            raise ValueError(
                "Instagram stories require login cookies. "
                "Set INSTAGRAM_COOKIES_FILE env pointing to a Netscape cookie file."
            )

    ydl_opts = {
        "noplaylist": True,
        "quiet": True,
        "socket_timeout": 15,
        "retries": 2,
    }
    cookiefile = COOKIE_FILES.get(platform)
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    return ydl_opts, platform


def fetch_info(url: str):
    ydl_opts, _platform = base_ydl_opts_for_url(url)
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    validate_extracted_info(info)
    return info


def remux_for_circle(src: Path, dst: Path):
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", "scale=512:512:force_original_aspect_ratio=increase,crop=512:512",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-t", "60",
        str(dst),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT)


def convert_for_voice(src: Path, dst: Path, start: str | None, end: str | None):
    cmd = ["ffmpeg", "-y"]
    if start:
        cmd += ["-ss", ffmpeg_timestamp(start)]
    if end:
        cmd += ["-to", ffmpeg_timestamp(end)]
    cmd += [
        "-i", str(src),
        "-vn",
        "-c:a", "libopus",
        "-b:a", "48k",
        str(dst),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT)


def has_video_format(info: dict) -> bool:
    for f in info.get("formats", []):
        vcodec = f.get("vcodec")
        if vcodec not in (None, "none"):
            return True
    return False


def has_any_thumbnail(info: dict) -> bool:
    if info.get("thumbnail"):
        return True
    thumbs = info.get("thumbnails") or []
    return len(thumbs) > 0


# ---------- HTML og:image fallback for social images ----------

def extract_og_image_from_html(html: str) -> str | None:
    patterns = [
        r'<meta[^>]+property["\']og:image["\'][^>]+content["\']([^"\']+)["\']',
        r'<meta[^>]+property["\']og:image:secure_url["\'][^>]+content["\']([^"\']+)["\']',
        r'<meta[^>]+name["\']twitter:image["\'][^>]+content["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return html_lib.unescape(m.group(1))
    return None


def download_social_image_direct(url: str, platform: str) -> tuple[Path, str, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="lexi_img_"))
    try:
        with urlopen(url) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to fetch page HTML: {e}")

    img_url = extract_og_image_from_html(html)
    if not img_url:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("Could not find image in page metadata.")

    try:
        with urlopen(img_url) as resp:
            data = resp.read()
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to download image: {e}")

    ext = ".jpg"
    lower_url = img_url.lower()
    if ".png" in lower_url:
        ext = ".png"
    elif ".webp" in lower_url:
        ext = ".webp"

    img_path = temp_dir / f"image{ext}"
    with open(img_path, "wb") as f:
        f.write(data)

    title = f"{platform.title()} image"
    return temp_dir, title, img_path


def download_media(url: str, mode: str, quality: str | None, user_id: int, start: str | None, end: str | None):
    settings = get_user_settings(user_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="lexi_"))

    base_opts, platform = base_ydl_opts_for_url(url)
    common_opts = {
        **base_opts,
        "outtmpl": str(temp_dir / "%(title).120s.%(ext)s"),
    }

    if platform == "spotify" and mode in ("video", "circle", "image"):
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ValueError("Spotify links are DRM-protected; only supported as external streaming, not downloads.")

    info = fetch_info(url)
    title = info.get("title", "Untitled")

    # IMAGE MODE via thumbnails or full download fallback
    if mode == "image":
        thumb_url = None
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumbs_sorted = sorted(
                thumbs,
                key=lambda t: (t.get("width", 0) * t.get("height", 0)),
                reverse=True,
            )
            thumb_url = thumbs_sorted[0].get("url") or thumbs_sorted[0].get("url_https")
        if not thumb_url:
            thumb_url = info.get("thumbnail")

        if thumb_url:
            try:
                with urlopen(thumb_url) as resp:
                    data = resp.read()
                ext = ".jpg"
                lower_url = thumb_url.lower()
                if ".png" in lower_url:
                    ext = ".png"
                elif ".webp" in lower_url:
                    ext = ".webp"
                img_path = temp_dir / f"image{ext}"
                with open(img_path, "wb") as f:
                    f.write(data)
                return temp_dir, title, img_path, "image"
            except Exception:
                pass

        ydl_opts = {
            **common_opts,
        }
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        files = [p for p in temp_dir.iterdir() if p.is_file()]
        image_files = [
            p for p in files if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        if image_files:
            final_file = max(image_files, key=lambda p: p.stat().st_size)
        else:
            final_file = extract_final_file(temp_dir)

        return temp_dir, title, final_file, "image"

    # NON-IMAGE MODES
    if mode == "circle":
        ydl_opts = {
            **common_opts,
            "format": "bestvideo[height<=720]+bestaudio/best[height<=720]" if platform == "youtube" else "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
        }
        if start or end:
            ydl_opts["external_downloader"] = "ffmpeg"
            ydl_opts["external_downloader_args"] = {"ffmpeg_i": trim_args(start, end)}
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        base_file = extract_final_file(temp_dir)
        circle_file = temp_dir / "circle_note.mp4"
        remux_for_circle(base_file, circle_file)
        return temp_dir, title, circle_file, "circle"

    if mode == "voice":
        ydl_opts = {
            **common_opts,
            "format": "bestaudio/best",
        }
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        base_file = extract_final_file(temp_dir)
        voice_file = temp_dir / "voice_message.ogg"
        convert_for_voice(base_file, voice_file, start, end)
        return temp_dir, title, voice_file, "voice"

    if mode == "video":
        if platform == "youtube":
            fmt = VIDEO_QUALITIES[quality]
        else:
            fmt = "bestvideo+bestaudio/best"
        ext = settings["video_format"]
        ydl_opts = {
            **common_opts,
            "format": fmt,
            "merge_output_format": ext,
        }
        if start or end:
            ydl_opts["external_downloader"] = "ffmpeg"
            ydl_opts["external_downloader_args"] = {"ffmpeg_i": trim_args(start, end)}
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        final_file = extract_final_file(temp_dir)
        return temp_dir, title, final_file, "video"

    # Audio
    audio_profile = AUDIO_QUALITIES[quality]
    audio_format = settings["audio_format"]
    ydl_opts = {
        **common_opts,
        "format": audio_profile["selector"],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": audio_profile["quality"],
        }],
    }
    if start or end:
        ydl_opts["external_downloader"] = "ffmpeg"
        ydl_opts["external_downloader_args"] = {"ffmpeg_i": trim_args(start, end)}
    with YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)
    final_file = extract_final_file(temp_dir)
    return temp_dir, title, final_file, "audio"


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = ensure_user_record(update.effective_user)
    await update.message.reply_text(
        "🎧 Lexi Downloader is ready.\n\n"
        f"Send a link from: {SUPPORTED_PLATFORMS_TEXT}.\n\n"
        "✂️ Optional trim (you can also tap \"📋 Copy trim template\"):\n"
        "Youtube URL\n"
        "start=01:23.500\n"
        "end=02:10.000\n"
        "mode=video or mode=audio\n\n"
        f"💰 Your total tokens: {rec['tokens']:.1f}",
        reply_markup=all_options_keyboard(update.effective_user.id),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base = (
        "ℹ️ Help\n\n"
        "User commands:\n"
        "• /start – open bot\n"
        "• /help – show help\n"
        "• /settings – user settings\n"
        "• /tokens – view & buy tokens\n"
        "• /leaderboard – top downloaders\n\n"
        "Supported platforms (no Spotify DRM):\n"
        f"{SUPPORTED_PLATFORMS_TEXT}\n"
    )

    if is_admin(update.effective_user.id):
        base += (
            "\nAdmin commands:\n"
            "• /admin – admin help\n"
            "• /grant <user_id> <amount>\n"
            "• /ban <user_id>\n"
            "• /unban <user_id>\n"
            "• /listusers [limit]\n"
            "• /find <user_id or name>\n"
        )

    await update.message.reply_text(base)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_record(update.effective_user)
    await update.message.reply_text("⚙️ Your settings:", reply_markup=settings_keyboard(update.effective_user.id))


async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = ensure_user_record(update.effective_user)
    text = (
        "💰 Your tokens\n\n"
        f"• Total available: {rec.get('tokens', 0.0):.1f}\n"
        f"• Monthly pool left: {rec.get('monthly_tokens', 0.0):.1f}\n"
        f"• Purchased left: {rec.get('purchased_tokens', 0.0):.1f}\n"
        f"• Monthly spent this month: {rec.get('monthly_tokens_spent', 0.0):.1f}\n"
        f"• Purchased spent total: {rec.get('purchased_tokens_spent', 0.0):.1f}\n\n"
        "📏 1 token ≈ 10 MB with 8 MB grace.\n"
        "   Example: 27 MB → 2 tokens, 29 MB → 3 tokens.\n"
        "   Tiny files: <2 MB → 0.2 token, 2–9.9 MB → 0.5 token.\n"
        "   Images: 0.2 token per image.\n\n"
        f"🗓 Every month you get {FREE_TOKENS_PER_MONTH} free tokens (monthly pool refilled, not stacked).\n\n"
        "⭐ Pricing (Stars):\n"
        "• 50 tokens = 50⭐\n"
        "• 500 tokens = 490⭐\n"
        "• 1000 tokens = 970⭐\n"
        "• 10000 tokens = 9500⭐\n"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 50 tokens", callback_data="buy:pack_50"),
            InlineKeyboardButton("⭐ 500 tokens", callback_data="buy:pack_500"),
        ],
        [
            InlineKeyboardButton("⭐ 1000 tokens", callback_data="buy:pack_1000"),
            InlineKeyboardButton("⭐ 10000 tokens", callback_data="buy:pack_10000"),
        ],
    ])
    await update.message.reply_text(text, reply_markup=kb)


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page_size = 15
    if context.args:
        try:
            page_size = max(1, min(100, int(context.args[0])))
        except ValueError:
            pass
    text, kb = leaderboard_view(page=0, page_size=page_size)
    await update.message.reply_text(text, reply_markup=kb)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = (
        "👷 Admin panel\n\n"
        "Commands:\n"
        "• /grant <user_id> <amount> – 🎁 grant tokens\n"
        "• /ban <user_id> – 🚫 ban user\n"
        "• /unban <user_id> – ✅ unban user\n"
        "• /listusers [limit] – 📋 list users\n"
        "• /find <user_id or name> – 🔍 search users"
    )
    await update.message.reply_text(text)


async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /grant <user_id> <amount>")
        return
    try:
        target_id = int(args[0])
        amount = float(args[1])
    except ValueError:
        await update.message.reply_text("Invalid arguments.")
        return
    rec = grant_tokens_to_user(target_id, amount)
    if not rec:
        await update.message.reply_text("User not found in DB.")
        return
    await update.message.reply_text(
        f"🎁 Granted {amount:.1f} tokens to {rec['name']} (id={rec['id']}).\n"
        f"New total: {rec['tokens']:.1f} (monthly {rec.get('monthly_tokens', 0.0):.1f}, "
        f"purchased {rec.get('purchased_tokens', 0.0):.1f})"
    )


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user_id.")
        return
    rec = set_user_ban(target_id, True)
    if not rec:
        await update.message.reply_text("User not found in DB.")
        return
    await update.message.reply_text(f"🚫 User {rec['name']} (id={rec['id']}) banned.")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user_id.")
        return
    rec = set_user_ban(target_id, False)
    if not rec:
        await update.message.reply_text("User not found in DB.")
        return
    await update.message.reply_text(f"✅ User {rec['name']} (id={rec['id']}) unbanned.")


async def listusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    page_size = 15
    if context.args:
        try:
            page_size = max(1, min(100, int(context.args[0])))
        except ValueError:
            pass
    text, kb = admin_users_view(page=0, page_size=page_size)
    await update.message.reply_text(text, reply_markup=kb)


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /find <user_id or name substring>")
        return
    query = " ".join(context.args).lower()
    db = load_users_db()
    results = []
    for u in db.values():
        if query.isdigit() and int(query) == u.get("id"):
            results.append(u)
        elif query in str(u.get("name", "")).lower():
            results.append(u)
    if not results:
        await update.message.reply_text("🔍 No users found.")
        return
    lines = ["🔍 Search results:"]
    for u in results[:25]:
        lines.append(
            f"👤 {u.get('name')} (id={u['id']}) "
            f"💰 total={u.get('tokens', 0.0):.1f} "
            f"🗓 monthly={u.get('monthly_tokens', 0.0):.1f} "
            f"🎁 purchased={u.get('purchased_tokens', 0.0):.1f} "
            f"📥 downloads={u.get('downloads', 0)} "
            f"🚫 banned={u.get('banned', False)}"
        )
    await update.message.reply_text("\n".join(lines))


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    rec = ensure_user_record(update.effective_user)

    if rec.get("banned"):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    try:
        check_rate_limit(update.effective_user.id)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    try:
        parsed = parse_link_message(text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    if not parsed:
        await update.message.reply_text(f"Send a valid HTTPS link from {SUPPORTED_PLATFORMS_TEXT}.")
        return

    user_id = update.effective_user.id
    USER_URLS[user_id] = parsed

    platform_name = parsed.get("platform", "unknown")
    url = parsed["url"]
    start = parsed.get("start")
    end = parsed.get("end")

    info = None
    try:
        info = await asyncio.to_thread(fetch_info, url)
    except Exception as e:
        err = str(e)
        # Social image-only fallback when yt-dlp refuses (e.g. "No video could be found")
        if platform_name in ("twitter", "reddit", "instagram", "facebook") and "No video could be found" in err:
            await update.message.reply_text("🖼 Detected image-only post, downloading image...")
            temp_dir = None
            try:
                temp_dir, title, file_path = await asyncio.to_thread(
                    download_social_image_direct,
                    url,
                    platform_name,
                )
                size_bytes = file_path.stat().st_size
                if size_bytes > MAX_FILESIZE_BYTES:
                    await update.message.reply_text(
                        f"Image is {format_size(size_bytes)}, which is above the maximum size "
                        f"{format_size(MAX_FILESIZE_BYTES)} this bot can send."
                    )
                    return

                safe_caption = f"`{escape_markdown(title[:1000], version=2)}`"
                with open(file_path, "rb") as f:
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=f,
                        caption=safe_caption,
                        parse_mode="MarkdownV2",
                    )
                tokens_used = calculate_tokens_for_size(size_bytes, "image")
                update_user_after_download(user_id, tokens_used)
                rec2 = ensure_user_record(update.effective_user)
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=(
                        f"📦 Size: {format_size(size_bytes)}\n"
                        f"⚡ Tokens used: {tokens_used:.1f}\n"
                        f"💰 Total remaining: {rec2.get('tokens', 0.0):.1f} "
                        f"(monthly {rec2.get('monthly_tokens', 0.0):.1f}, "
                        f"purchased {rec2.get('purchased_tokens', 0.0):.1f})"
                    )
                )
            except Exception as e2:
                await update.message.reply_text(f"Failed to download image: {e2}")
            finally:
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)
            return

        if "Spotify audio is DRM protected" in err or "Instagram stories require login cookies" in err or "DRM" in err:
            await update.message.reply_text(f"Failed: {err}")
            return
        await update.message.reply_text(f"Note: could not prefetch metadata ({err}). You can still choose a mode.")

    # Auto image-only send if metadata clearly has thumbnails but no video
    if info and platform_name in ("twitter", "reddit", "instagram", "facebook") and not has_video_format(info) and has_any_thumbnail(info):
        await update.message.reply_text("🖼 Detected image-only post, downloading image...")
        temp_dir = None
        try:
            temp_dir, title, file_path, final_mode = await asyncio.to_thread(
                download_media,
                url,
                "image",
                None,
                user_id,
                start,
                end,
            )
            size_bytes = file_path.stat().st_size
            if size_bytes > MAX_FILESIZE_BYTES:
                await update.message.reply_text(
                    f"Result image is {format_size(size_bytes)}, which is above the maximum size "
                    f"{format_size(MAX_FILESIZE_BYTES)} this bot can send."
                )
                return

            safe_caption = f"`{escape_markdown(title[:1000], version=2)}`"
            with open(file_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=f,
                    caption=safe_caption,
                    parse_mode="MarkdownV2",
                )
            tokens_used = calculate_tokens_for_size(size_bytes, final_mode)
            update_user_after_download(user_id, tokens_used)
            rec2 = ensure_user_record(update.effective_user)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"📦 Size: {format_size(size_bytes)}\n"
                    f"⚡ Tokens used: {tokens_used:.1f}\n"
                    f"💰 Total remaining: {rec2.get('tokens', 0.0):.1f} "
                    f"(monthly {rec2.get('monthly_tokens', 0.0):.1f}, "
                    f"purchased {rec2.get('purchased_tokens', 0.0):.1f})"
                )
            )
        except Exception as e2:
            await update.message.reply_text(f"Failed to download image: {e2}")
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
        return

    platform_label = platform_name.title()
    await update.message.reply_text(
        f"🔗 Detected platform: {platform_label}\n"
        f"💰 Your total tokens: {rec.get('tokens', 0.0):.1f}\n"
        f"🗓 Monthly left: {rec.get('monthly_tokens', 0.0):.1f} | "
        f"🎁 Purchased left: {rec.get('purchased_tokens', 0.0):.1f}\n\n"
        "Choose format or extra mode:",
        reply_markup=all_options_keyboard(user_id, info, start, end)
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user = update.effective_user
    payload = payment.invoice_payload
    pack = TOKEN_PACKS.get(payload)
    if not pack:
        return
    charge_id = payment.telegram_payment_charge_id

    rec = record_successful_payment(user.id, charge_id, payload)
    if not rec:
        rec = ensure_user_record(user)

    bonus_min, bonus_max = BONUS_RANGES.get(payload, (0, 0))
    bonus = random.randint(bonus_min, bonus_max) if bonus_max > 0 else 0
    if bonus > 0:
        rec = grant_tokens_to_user(user.id, bonus) or ensure_user_record(user)

    await update.message.reply_text(
        "⭐ Payment received!\n"
        f"🎁 You bought {pack['tokens']} tokens"
        + (f" and won an extra {bonus} gift tokens!\n" if bonus > 0 else "!\n")
        + f"💰 New total: {rec.get('tokens', 0.0):.1f} "
          f"(monthly {rec.get('monthly_tokens', 0.0):.1f}, "
          f"purchased {rec.get('purchased_tokens', 0.0):.1f})"
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data.startswith("lb:"):
        _, page_str, size_str = data.split(":", 2)
        page = int(page_str)
        page_size = int(size_str)
        text, kb = leaderboard_view(page, page_size)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("adminlist:"):
        if not is_admin(user_id):
            return
        _, page_str, size_str = data.split(":", 2)
        page = int(page_str)
        page_size = int(size_str)
        text, kb = admin_users_view(page, page_size)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("buy:"):
        pack_id = data.split(":", 1)[1]
        pack = TOKEN_PACKS.get(pack_id)
        if not pack:
            await query.edit_message_text("Unknown pack.")
            return
        prices = [LabeledPrice(label=pack["title"], amount=pack["stars"])]
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=pack["title"],
            description=pack["description"],
            payload=pack_id,
            provider_token=None,
            currency="XTR",
            prices=prices,
        )
        await query.edit_message_text(
            f"Opening Telegram Stars purchase panel for {pack['tokens']} tokens ({pack['stars']}⭐)..."
        )
        return

    if data == "template:trim":
        template = "Youtube URL\nstart=00:00.000\nend=00:00.000\nmode=video"
        escaped = escape_markdown(template, version=2)
        msg = f"Trim template:\n\n`{escaped}`"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=msg,
            parse_mode="MarkdownV2",
        )
        return

    rec = ensure_user_record(update.effective_user)
    if rec.get("banned"):
        await query.edit_message_text("🚫 You are banned from using this bot.")
        return

    if data == "menu:root":
        link_data = USER_URLS.get(user_id)
        info = None
        try:
            info = await asyncio.to_thread(fetch_info, link_data["url"]) if link_data else None
        except Exception:
            info = None
        await query.edit_message_text(
            "Choose format or extra mode.",
            reply_markup=all_options_keyboard(
                user_id,
                info,
                link_data.get("start") if link_data else None,
                link_data.get("end") if link_data else None,
            )
        )
        return
    if data == "menu:settings":
        await query.edit_message_text("⚙️ Your settings:", reply_markup=settings_keyboard(user_id))
        return

    if data == "noop":
        return
    if data.startswith("apply:"):
        _, key, value = data.split(":", 2)
        if key == "show_size":
            set_user_setting(user_id, key, value.lower() == "true")
        else:
            set_user_setting(user_id, key, value)
        await query.edit_message_text("Settings updated.", reply_markup=settings_keyboard(user_id))
        return

    link_data = USER_URLS.get(user_id)
    if not link_data:
        await query.edit_message_text("Send a supported link first.")
        return

    mode = None
    quality = None
    if data.startswith("video:"):
        mode = "video"
        quality = data.split(":", 1)[1]
    elif data.startswith("audio:"):
        mode = "audio"
        quality = data.split(":", 1)[1]
    elif data == "extra:circle":
        mode = "circle"
    elif data == "extra:voice":
        mode = "voice"
    elif data == "extra:image":
        mode = "image"
    else:
        await query.edit_message_text("Unknown action.")
        return

    await query.edit_message_text("⏬ Downloading, processing and uploading...")
    temp_dir = None
    try:
        # run heavy yt_dlp/ffmpeg work off the event loop
        temp_dir, title, file_path, final_mode = await asyncio.to_thread(
            download_media,
            link_data["url"],
            mode,
            quality,
            user_id,
            link_data.get("start"),
            link_data.get("end"),
        )

        size_bytes = file_path.stat().st_size
        if size_bytes > MAX_FILESIZE_BYTES:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    f"Result file is {format_size(size_bytes)} which is above "
                    f"the maximum size {format_size(MAX_FILESIZE_BYTES)} this bot can send.\n"
                    "Try a lower quality or trim a shorter segment.\n"
                    "If you need bigger files, run a local Bot API server and raise BOT_FILE_LIMIT_BYTES."
                ),
            )
            return

        safe_caption = f"`{escape_markdown(title[:1000], version=2)}`"

        with open(file_path, "rb") as f:
            if final_mode == "video":
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    caption=safe_caption,
                    parse_mode="MarkdownV2",
                    supports_streaming=True,
                )
            elif final_mode == "audio":
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    title=title,
                    caption=safe_caption,
                    parse_mode="MarkdownV2",
                )
            elif final_mode == "voice":
                await context.bot.send_voice(
                    chat_id=query.message.chat_id,
                    voice=f,
                    caption=safe_caption,
                    parse_mode="MarkdownV2",
                )
            elif final_mode == "circle":
                await context.bot.send_video_note(
                    chat_id=query.message.chat_id,
                    video_note=f,
                    length=512,
                )
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=safe_caption,
                    parse_mode="MarkdownV2",
                )
            elif final_mode == "image":
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=f,
                    caption=safe_caption,
                    parse_mode="MarkdownV2",
                )
            else:
                raise RuntimeError("Unsupported send mode.")

        tokens_used = calculate_tokens_for_size(size_bytes, final_mode)
        update_user_after_download(user_id, tokens_used)
        rec2 = ensure_user_record(update.effective_user)

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"📦 Size: {format_size(size_bytes)}\n"
                f"⚡ Tokens used: {tokens_used:.1f}\n"
                f"💰 Total remaining: {rec2.get('tokens', 0.0):.1f} "
                f"(monthly {rec2.get('monthly_tokens', 0.0):.1f}, purchased {rec2.get('purchased_tokens', 0.0):.1f})"
            )
        )

    except subprocess.TimeoutExpired:
        await query.edit_message_text("⏱ Processing took too long and was aborted.")
    except subprocess.CalledProcessError as e:
        await query.edit_message_text(f"Download failed: process exited with code {e.returncode}")
    except Exception as e:
        await query.edit_message_text(f"Failed: {e}")
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    # concurrent_updates(True) => process multiple updates (users) in parallel
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("tokens", tokens_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("grant", grant_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("listusers", listusers_cmd))
    app.add_handler(CommandHandler("find", find_cmd))

    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()


if __name__ == "__main__":
    main()