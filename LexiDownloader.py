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
import uuid
import html as html_lib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen, Request
import asyncio

from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    LabeledPrice,
    InputMediaPhoto,
    InputMediaVideo,
    CopyTextButton,
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

# Token-bound pending requests. Each keyboard we show is tied to a short token
# embedded in its callback_data, so two links sent in quick succession never
# clobber each other (fixes duplicate-download race). Tokens are kept small to
# stay well under Telegram's 64-byte callback_data limit.
PENDING_REQUESTS: dict[str, dict] = {}
_PENDING_MAX = 200


def store_request(req: dict) -> str:
    """Store a request dict and return a short token referencing it."""
    token = uuid.uuid4().hex[:10]
    PENDING_REQUESTS[token] = req
    # Bound memory: drop oldest entries beyond the cap (dicts keep insertion order).
    if len(PENDING_REQUESTS) > _PENDING_MAX:
        for old in list(PENDING_REQUESTS.keys())[:-_PENDING_MAX]:
            PENDING_REQUESTS.pop(old, None)
    return token


def get_request(token: str) -> dict | None:
    return PENDING_REQUESTS.get(token)

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
MAX_DURATION_SECONDS = 60 * 60 * 24  # 24 hours (hard sanity ceiling)

# Operational cap on the FULL source video length non-admins may download.
# This is enforced on the *whole* video duration (info["duration"]), NOT the
# trimmed segment, so a user cannot request a huge file and trim it to a few
# seconds to dodge limits or hammer the server. Admins are exempt.
MAX_OPERATE_SECONDS = int(os.getenv("MAX_OPERATE_SECONDS", str(2 * 60 * 60)))  # 2 hours

# ---------- Upload size limit ----------
# Telegram's CLOUD Bot API caps bot uploads at 50MB. A LOCAL Bot API server
# (telegram-bot-api, run on your own PC) raises this to 2000MB (2GB).
#
# To unlock 2GB on your local machine:
#   1) Get api_id + api_hash at https://my.telegram.org -> API development tools
#   2) Run a local server, e.g.:
#        telegram-bot-api --api-id=<API_ID> --api-hash=<API_HASH> --local
#      (Windows: download telegram-bot-api.exe or build via vcpkg.)
#   3) Point this bot at it and raise the limit in your .env:
#        TELEGRAM_API_BASE_URL=http://127.0.0.1:8081/bot
#        TELEGRAM_API_BASE_FILE_URL=http://127.0.0.1:8081/file/bot
#        BOT_FILE_LIMIT_BYTES=2000000000
#   The bot auto-detects the base URL below and switches to local mode.
TELEGRAM_LOCAL_LIMIT = 2000 * 1024 * 1024   # 2000MB hard ceiling for local API
TELEGRAM_CLOUD_LIMIT = 50 * 1024 * 1024     # 50MB cloud API limit

TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL") or None
TELEGRAM_API_BASE_FILE_URL = os.getenv("TELEGRAM_API_BASE_FILE_URL") or None
USING_LOCAL_BOT_API = bool(TELEGRAM_API_BASE_URL)

# Default cap: 50MB on cloud, 2GB when a local Bot API server is configured.
_default_limit = TELEGRAM_LOCAL_LIMIT if USING_LOCAL_BOT_API else TELEGRAM_CLOUD_LIMIT
BOT_FILE_LIMIT_BYTES = int(os.getenv("BOT_FILE_LIMIT_BYTES", str(_default_limit)))
# Never advertise more than the server actually allows.
_hard_ceiling = TELEGRAM_LOCAL_LIMIT if USING_LOCAL_BOT_API else TELEGRAM_CLOUD_LIMIT
if BOT_FILE_LIMIT_BYTES > _hard_ceiling:
    BOT_FILE_LIMIT_BYTES = _hard_ceiling
MAX_FILESIZE_BYTES = BOT_FILE_LIMIT_BYTES

FREE_TOKENS_PER_MONTH = 100

FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "900"))

# Auto-download supported platforms (order = display order in settings)
AUTO_DL_PLATFORMS = [
    ("youtube", "YouTube"),
    ("instagram", "Instagram"),
    ("tiktok", "TikTok"),
    ("twitter", "Twitter/X"),
    ("pinterest", "Pinterest"),
    ("facebook", "Facebook"),
    ("reddit", "Reddit"),
]

# Valid auto-download "format" choices stored per platform.
# video qualities -> ("video", "<quality>"), music -> ("audio", "<key>"), image -> ("image", None)
AUTO_DL_FORMATS = {
    "360p": ("video", "360p"),
    "720p": ("video", "720p"),
    "1080p": ("video", "1080p"),
    "music_med": ("audio", "medium"),
    "music_high": ("audio", "high"),
    "image": ("image", None),
    "imgvideo": ("imgvideo", None),
}

DEFAULT_SETTINGS = {
    "video_format": "mp4",
    "audio_format": "mp3",
    "show_size": True,
    # "off" | "low" | "medium" | "high"
    "compression": "off",
    # per-platform auto download config:
    # { "<platform>": {"on": bool, "format": "720p"} }
    "auto_download": {},
}

# Compression presets. CRF higher = smaller file / lower quality.
# scale caps the height so even 720p clips can be shrunk further.
COMPRESSION_PROFILES = {
    "low":    {"crf": 28, "preset": "veryfast", "max_height": None, "audio_kbps": 128},
    "medium": {"crf": 32, "preset": "veryfast", "max_height": 720,  "audio_kbps": 96},
    "high":   {"crf": 36, "preset": "veryfast", "max_height": 480,  "audio_kbps": 64},
}

# Up to 1080p, no 1440p/2160p
VIDEO_QUALITIES = {
    "144p": "bestvideo[height<=144]+bestaudio/best[height<=144]",
    "240p": "bestvideo[height<=240]+bestaudio/best[height<=240]",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
}

AUDIO_QUALITIES = {
    "low": {"selector": "bestaudio[abr<=64]/bestaudio/best", "quality": "10", "bitrate_kbps": 64},
    "medium": {"selector": "bestaudio[abr<=128]/bestaudio/best", "quality": "5", "bitrate_kbps": 128},
    "high": {"selector": "bestaudio/best", "quality": "0", "bitrate_kbps": 192},
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?$")

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


def _default_settings_copy() -> dict:
    import copy
    return copy.deepcopy(DEFAULT_SETTINGS)


def get_user_settings(user_id: int):
    data = load_settings()
    user_key = str(user_id)
    if user_key not in data:
        data[user_key] = _default_settings_copy()
        save_settings(data)
    merged = _default_settings_copy()
    stored = data[user_key]
    merged.update(stored)
    # Re-merge nested auto_download so missing platforms fall back to defaults
    if not isinstance(merged.get("auto_download"), dict):
        merged["auto_download"] = {}
    return merged


def get_auto_download_for_platform(user_id: int, platform: str) -> dict:
    """Return {'on': bool, 'format': str} for a platform with sane defaults."""
    s = get_user_settings(user_id)
    cfg = (s.get("auto_download") or {}).get(platform, {})
    return {
        "on": bool(cfg.get("on", False)),
        "format": cfg.get("format", "720p"),
    }


def set_auto_download(user_id: int, platform: str, *, on: bool | None = None, fmt: str | None = None):
    data = load_settings()
    user_key = str(user_id)
    current = _default_settings_copy()
    current.update(data.get(user_key, {}))
    auto = current.get("auto_download")
    if not isinstance(auto, dict):
        auto = {}
    entry = dict(auto.get(platform, {}))
    if "on" not in entry:
        entry["on"] = False
    if "format" not in entry:
        entry["format"] = "720p"
    if on is not None:
        entry["on"] = bool(on)
    if fmt is not None:
        entry["format"] = fmt
    auto[platform] = entry
    current["auto_download"] = auto
    data[user_key] = current
    save_settings(data)


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
    db[key] = rec
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
    """
    First line must contain URL + optional trim/mode lines.
    """
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


def parse_trim_settings_only(text: str):
    """
    Parse messages that only contain start/end/mode (no URL).
    Used to update settings for last URL.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    start = None
    end = None
    mode = None
    for line in lines:
        low = line.lower()
        if low.startswith("start="):
            start = line.split("=", 1)[1].strip()
        elif low.startswith("end="):
            end = line.split("=", 1)[1].strip()
        elif low.startswith("mode="):
            mode = line.split("=", 1)[1].strip().lower()

    if not (start or end or mode):
        return None

    if start and not valid_time(start):
        raise ValueError("Invalid start time. Use mm:ss, hh:mm:ss, or with .ms")
    if end and not valid_time(end):
        raise ValueError("Invalid end time. Use mm:ss, hh:mm:ss, or with .ms")

    return {"start": start, "end": end, "mode": mode}


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


def format_hms(seconds: float | int | None) -> str:
    """Render a seconds count as H:MM:SS (or M:SS when under an hour)."""
    if not seconds or seconds <= 0:
        return "0:00"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class DurationCapError(ValueError):
    """Raised when a non-admin requests a video longer than MAX_OPERATE_SECONDS."""


def enforce_duration_cap(info: dict | None, user_id: int) -> None:
    """Reject FULL videos longer than the operational cap for non-admins.

    Checks the *whole* source duration (not the trimmed segment) so a user
    cannot request a 10GB / multi-hour video and trim it to a few seconds to
    dodge billing or overload the server. Admins are exempt.
    """
    if is_admin(user_id):
        return
    if not info:
        return
    duration = info.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration and duration > MAX_OPERATE_SECONDS:
        raise DurationCapError(
            f"This video is {format_hms(duration)} long, which is over the "
            f"{format_hms(MAX_OPERATE_SECONDS)} limit per download. "
            "Trimming does not bypass this — the whole video still has to be "
            "downloaded first. Please pick a shorter video."
        )


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

def all_options_keyboard(user_id: int, info: dict | None = None, start: str | None = None, end: str | None = None, token: str | None = None):
    settings = get_user_settings(user_id)
    show_size = settings.get("show_size", True)
    # Bind every action button to a specific request token so two links sent in
    # quick succession can't be confused for one another.
    sfx = f":{token}" if token else ""

    def v(label: str):
        estimate = estimate_video_size(info, label) if info else None
        return InlineKeyboardButton(f"🎬 {label}{build_size_suffix(estimate, show_size)}", callback_data=f"video:{label}{sfx}")

    def a(label: str, key: str):
        estimate = estimate_audio_size(info, key, start, end) if info else None
        return InlineKeyboardButton(f"🎵 {label}{build_size_suffix(estimate, show_size)}", callback_data=f"audio:{key}{sfx}")

    return InlineKeyboardMarkup([
        [v("144p"), v("240p"), v("360p")],
        [v("480p"), v("720p"), v("1080p")],
        [a("Low", "low"), a("Med", "medium"), a("High", "high")],
        [
            InlineKeyboardButton("⭕ Circle", callback_data=f"extra:circle{sfx}"),
            InlineKeyboardButton("🎙️ Voice", callback_data=f"extra:voice{sfx}"),
            InlineKeyboardButton("🖼 Image", callback_data=f"extra:image{sfx}"),
            InlineKeyboardButton("🖼🎵 Img+Music", callback_data=f"extra:imgvideo{sfx}"),
        ],
        [InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings")],
        [InlineKeyboardButton("📋 Copy trim template", callback_data="template:trim")],
    ])


def image_choice_keyboard(token: str | None = None):
    """Shown when an image-only post also has music: pick plain image or
    image+music (slideshow video)."""
    sfx = f":{token}" if token else ""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F5BC Image", callback_data=f"imgchoice:image{sfx}"),
        InlineKeyboardButton("\U0001F5BC\U0001F3B5 Image+Music", callback_data=f"imgchoice:imgvideo{sfx}"),
    ]])


def settings_keyboard(user_id: int):
    s = get_user_settings(user_id)
    size_state = "✅ On" if s.get("show_size", True) else "❌ Off"
    toggle_target = "false" if s.get("show_size", True) else "true"
    comp = s.get("compression", "off")
    comp_label = {"off": "Off", "low": "Low", "medium": "Medium", "high": "High"}.get(comp, "Off")

    def comp_btn(label: str, value: str):
        mark = "✅ " if comp == value else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"apply:compression:{value}")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Video format: {s['video_format']}", callback_data="noop")],
        [InlineKeyboardButton("📼 MP4", callback_data="apply:video_format:mp4"),
         InlineKeyboardButton("📦 MKV", callback_data="apply:video_format:mkv")],
        [InlineKeyboardButton(f"🎵 Audio format: {s['audio_format']}", callback_data="noop")],
        [InlineKeyboardButton("🎧 MP3", callback_data="apply:audio_format:mp3"),
         InlineKeyboardButton("🎶 M4A", callback_data="apply:audio_format:m4a"),
         InlineKeyboardButton("🗣️ OGG", callback_data="apply:audio_format:ogg")],
        [InlineKeyboardButton(f"🗜 Compression: {comp_label}", callback_data="noop")],
        [comp_btn("🚫 Off", "off"), comp_btn("🟢 Low", "low"),
         comp_btn("🟡 Medium", "medium"), comp_btn("🔴 High", "high")],
        [InlineKeyboardButton(f"📏 Show size: {size_state}", callback_data=f"apply:show_size:{toggle_target}")],
        [InlineKeyboardButton("⚡ Auto download", callback_data="menu:autodl")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:root")],
    ])


def autodownload_keyboard(user_id: int):
    """Compact per-platform auto-download config.

    For each platform:
      Row 1: <Platform name>  | toggle (🟢 On / ❌ Off)
      Row 2: 360p | 720p | 1080p   (selected one marked)
      Row 3: 🎵Med | 🎵High | 🖼 Image | 🖼🎵 Img+Music
    """
    rows: list[list[InlineKeyboardButton]] = []

    for platform, label in AUTO_DL_PLATFORMS:
        cfg = get_auto_download_for_platform(user_id, platform)
        on = cfg["on"]
        sel = cfg["format"]

        toggle_text = "🟢 On" if on else "❌ Off"
        toggle_target = "off" if on else "on"
        rows.append([
            InlineKeyboardButton(label, callback_data="noop"),
            InlineKeyboardButton(toggle_text, callback_data=f"autotoggle:{platform}:{toggle_target}"),
        ])

        def fmt_btn(text: str, fmt_key: str):
            mark = "✅ " if sel == fmt_key else ""
            return InlineKeyboardButton(f"{mark}{text}", callback_data=f"autofmt:{platform}:{fmt_key}")

        rows.append([
            fmt_btn("360p", "360p"),
            fmt_btn("720p", "720p"),
            fmt_btn("1080p", "1080p"),
        ])
        rows.append([
            fmt_btn("🎵 Med", "music_med"),
            fmt_btn("🎵 High", "music_high"),
            fmt_btn("🖼 Image", "image"),
            fmt_btn("🖼🎵 Img+Music", "imgvideo"),
        ])

    rows.append([InlineKeyboardButton("⬅️ Back to settings", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)


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
        "noplaylist": True,    # override to False where we need full IG galleries
        "quiet": True,
        "no_warnings": True,
        "verbose": False,
        # Long videos / slow CDNs need a more forgiving network profile.
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "extractor_retries": 3,
        "retry_sleep_functions": {"http": lambda n: min(5 * (n + 1), 30)},
        # Download m3u8/DASH fragments in parallel -> big speedup for long YT videos.
        "concurrent_fragment_downloads": 5,
        "http_chunk_size": 10 * 1024 * 1024,
        "nocheckcertificate": True,
        "ignoreerrors": False,
    }
    if platform == "youtube":
        # Prefer the Android/web clients which avoid many throttling / SABR issues
        # and are more reliable for long videos.
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "web"],
            }
        }
    cookiefile = COOKIE_FILES.get(platform)
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    return ydl_opts, platform


# Error fragments that indicate "this post has no video, but may have images".
NO_VIDEO_ERROR_HINTS = (
    "no video in this post",
    "no video could be found",
    "there is no video",
    "no video formats found",
    "unable to extract video",
    "requested format is not available",
)


def _looks_like_no_video_error(err: str) -> bool:
    low = (err or "").lower()
    return any(h in low for h in NO_VIDEO_ERROR_HINTS)


def fetch_info(url: str, *, allow_no_video: bool = False):
    """Fetch metadata. If allow_no_video and the post has no video (e.g. an
    Instagram photo), return a minimal info dict instead of raising."""
    ydl_opts, platform = base_ydl_opts_for_url(url)
    print(f"[fetch_info] url={url} platform={platform} allow_no_video={allow_no_video}")
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        validate_extracted_info(info)
        return info
    except Exception as e:
        if allow_no_video and _looks_like_no_video_error(str(e)):
            print(f"[fetch_info] no-video post, returning minimal info: {e}")
            return {
                "_no_video": True,
                "title": f"{platform.title()} post",
                "webpage_url": url,
                "original_url": url,
                "formats": [],
                "thumbnails": [],
            }
        raise


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
    print("[ffmpeg circle]", " ".join(cmd))
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
    print("[ffmpeg voice]", " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT)


def compress_video(src: Path, dst: Path, level: str) -> Path:
    """Re-encode a video to make it smaller. Returns dst on success, else src.
    level: 'low' | 'medium' | 'high'. Higher = smaller file.
    """
    profile = COMPRESSION_PROFILES.get(level)
    if not profile:
        return src

    cmd = ["ffmpeg", "-y", "-i", str(src)]
    max_h = profile.get("max_height")
    if max_h:
        # Only scale down (never up); keep even dimensions for yuv420p.
        cmd += ["-vf", f"scale=-2:'min({max_h},ih)'"]
    cmd += [
        "-c:v", "libx264",
        "-preset", profile["preset"],
        "-crf", str(profile["crf"]),
        "-c:a", "aac",
        "-b:a", f"{profile['audio_kbps']}k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        str(dst),
    ]
    print("[ffmpeg compress]", " ".join(cmd))
    try:
        subprocess.run(
            cmd, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=FFMPEG_TIMEOUT,
        )
    except Exception as e:
        print("[compress error]", e)
        return src
    # Only keep the compressed file if it is actually smaller.
    try:
        if dst.exists() and dst.stat().st_size > 0 and dst.stat().st_size < src.stat().st_size:
            return dst
    except Exception:
        pass
    return src


def maybe_compress_files(file_paths: list[Path], final_mode: str, user_id: int, temp_dir: Path) -> list[Path]:
    """Apply user's compression setting to video-type outputs."""
    settings = get_user_settings(user_id)
    level = settings.get("compression", "off")
    if level == "off" or level not in COMPRESSION_PROFILES:
        return file_paths
    if final_mode not in ("video",):
        return file_paths
    out: list[Path] = []
    for idx, p in enumerate(file_paths):
        dst = temp_dir / f"compressed_{idx:03d}.mp4"
        out.append(compress_video(p, dst, level))
    return out


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


def post_has_audio(info: dict) -> bool:
    """True if the (image-only) post still carries an audio track.

    Instagram photo posts/Reels can attach music; in that case we let the user
    choose between a plain image and an image+music slideshow video.
    """
    if not info:
        return False
    for f in info.get("formats", []) or []:
        acodec = f.get("acodec")
        if acodec and acodec != "none":
            return True
    # Some extractors expose audio hints at the top level.
    if info.get("acodec") and info.get("acodec") != "none":
        return True
    return False


def instagram_post_has_audio(url: str) -> bool:
    """Best-effort: probe whether an Instagram image post also has music.

    Parses the page HTML for audio markers (clips_music / has_audio / audio).
    Returns False on any failure so we degrade to plain-image behaviour.
    """
    try:
        html = _http_get_text(url)
    except Exception:
        return False
    markers = ('"has_audio":true', '"clips_music_attribution_info"',
               '"music_metadata"', '"original_sound_info"', '"audio_ranking_info"')
    return any(m in html for m in markers)


# ---------- HTML og:image fallback ----------

def extract_og_image_from_html(html: str) -> str | None:
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return html_lib.unescape(m.group(1))
    return None


def download_social_image_direct(url: str, platform: str) -> tuple[Path, str, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="lexi_img_"))
    print(f"[social-image] url={url} platform={platform}")
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


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _http_get_bytes(url: str, timeout: int = 60) -> bytes:
    req = Request(url, headers=_BROWSER_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_text(url: str, timeout: int = 60) -> str:
    req = Request(url, headers=_BROWSER_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# Instagram CDN URLs embed resize directives like ``/s640x640/`` or
# ``/c0.135.1080.1080a/`` and query params like ``?stp=...e15...``. Removing the
# resize path segments yields the largest stored variant. We keep the query
# string but drop the ``stp`` resize hint so the CDN serves the original.
_IG_RESIZE_SEG_RE = re.compile(r"/(?:[sc]\d+x\d+(?:_[a-z0-9]+)?|c\d[\d.]*\d+a?)(?=/)", re.IGNORECASE)


def _upgrade_instagram_image_url(img_url: str) -> str:
    """Return the highest-resolution variant of an Instagram CDN image URL."""
    if not img_url:
        return img_url
    upgraded = img_url
    # Drop path resize segments (e.g. /s640x640/, /c0.135.1080.1080a/).
    try:
        upgraded = _IG_RESIZE_SEG_RE.sub("", upgraded)
    except Exception:
        upgraded = img_url
    # Drop the ``stp`` query hint that forces a downscaled/cropped render.
    upgraded = re.sub(r"([?&])stp=[^&]*&?", r"\1", upgraded)
    upgraded = re.sub(r"[?&]$", "", upgraded)
    return upgraded


def extract_instagram_image_urls(html: str) -> list[str]:
    """Parse full-resolution image URLs from an Instagram page's embedded JSON.

    Looks for ``display_url`` plus ``display_resources`` (carousel-safe) and
    falls back to ``og:image``. Returns de-duplicated, resolution-upgraded URLs
    in document order (carousel order).
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: str):
        if not u:
            return
        u = u.replace("\\u0026", "&").replace("\\/", "/")
        u = _upgrade_instagram_image_url(u)
        if u not in seen and u.startswith("http"):
            seen.add(u)
            urls.append(u)

    # Prefer display_resources blocks: each carousel node lists multiple sizes;
    # the last entry is the largest. Capture the widest "src" per block.
    for block in re.finditer(r'"display_resources"\s*:\s*\[(.*?)\]', html, re.DOTALL):
        srcs = re.findall(r'"src"\s*:\s*"([^"]+)"', block.group(1))
        widths = re.findall(r'"config_width"\s*:\s*(\d+)', block.group(1))
        if srcs:
            if widths and len(widths) == len(srcs):
                best = max(zip(srcs, (int(w) for w in widths)), key=lambda t: t[1])[0]
            else:
                best = srcs[-1]
            _add(best)

    # display_url entries (top-level + each carousel node).
    for m in re.finditer(r'"display_url"\s*:\s*"([^"]+)"', html):
        _add(m.group(1))

    # As a last resort, og:image (already cropped, but better than nothing).
    if not urls:
        og = extract_og_image_from_html(html)
        if og:
            _add(og)

    return urls


def ffmpeg_trim_file(src: Path, dst: Path, start: str | None, end: str | None,
                     reencode: bool = True) -> Path:
    """Trim ``src`` to [start, end] with a standalone ffmpeg pass.

    This is the robust Windows-safe replacement for yt-dlp's
    ``external_downloader=ffmpeg`` + ``ffmpeg_i`` seeking, which fails with exit
    code -106 (4294967158) when combined with separate-stream merges.
    Output-side ``-ss/-to`` (after ``-i``) re-encodes for frame-accurate cuts.
    """
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if start:
        cmd += ["-ss", ffmpeg_timestamp(start)]
    if end:
        cmd += ["-to", ffmpeg_timestamp(end)]
    if reencode:
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-pix_fmt", "yuv420p",
        ]
    else:
        cmd += ["-c", "copy"]
    cmd.append(str(dst))
    print("[ffmpeg trim]", " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT)
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    return src


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}


def _img_ext_for(u: str) -> str:
    low = (u or "").lower()
    if ".png" in low:
        return ".png"
    if ".webp" in low:
        return ".webp"
    return ".jpg"


def _entry_is_video(entry: dict) -> bool:
    """Decide whether a yt-dlp Instagram entry is a video (vs a still image)."""
    if not entry:
        return False
    if entry.get("_type") == "url" and entry.get("ie_key"):
        # Unresolved nested entry; treat conservatively as possibly video.
        return True
    vcodec = entry.get("vcodec")
    if vcodec and vcodec != "none":
        return True
    if entry.get("duration"):
        return True
    for f in entry.get("formats", []) or []:
        if f.get("vcodec") and f.get("vcodec") != "none":
            return True
    ext = (entry.get("ext") or "").lower()
    if ext and f".{ext}" in _VIDEO_EXTS:
        return True
    return False


def _best_image_url_from_entry(entry: dict) -> str | None:
    """Pull the highest-resolution still-image URL from a yt-dlp IG entry."""
    if not entry:
        return None
    u = entry.get("display_url")
    if u:
        return _upgrade_instagram_image_url(u)
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        best = max(
            thumbs,
            key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0),
        )
        u = best.get("url") or best.get("url_https")
        if u:
            return _upgrade_instagram_image_url(u)
    u = entry.get("thumbnail")
    if u:
        return _upgrade_instagram_image_url(u)
    u = entry.get("url")
    if u and any(seg in u.lower() for seg in (".jpg", ".jpeg", ".png", ".webp")):
        return _upgrade_instagram_image_url(u)
    return None


def _download_image_url(img_url: str, dst: Path) -> bool:
    """Download a single image URL to dst using browser-like headers."""
    try:
        data = _http_get_bytes(img_url)
    except Exception as e:
        print(f"[ig-media img dl error] {e}")
        return False
    if not data:
        return False
    with open(dst, "wb") as fh:
        fh.write(data)
    return True


# Instagram internal web API. Public posts can often be read without login via
# the same JSON endpoints the website itself calls. Instagram rotates these, so
# we try several shapes and degrade gracefully. App id is the public web app id.[web:scrapfly]
_IG_APP_ID = "936619743392459"
_IG_SHORTCODE_RE = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def _ig_shortcode_from_url(url: str) -> str | None:
    m = _IG_SHORTCODE_RE.search(url or "")
    return m.group(1) if m else None


def _ig_api_headers() -> dict:
    return {
        **_BROWSER_HEADERS,
        "X-IG-App-ID": _IG_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-ASBD-ID": "129477",
        "Accept": "*/*",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    }


def _http_get_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = Request(url, headers=headers or _ig_api_headers())
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset, errors="replace")
    return json.loads(raw)


def _http_post_json(url: str, data: bytes, headers: dict | None = None, timeout: int = 30):
    req = Request(url, data=data, headers=headers or _ig_api_headers(), method="POST")
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset, errors="replace")
    return json.loads(raw)


def _ig_best_image_from_node(node: dict) -> str | None:
    """Largest still-image URL from an IG media node (web-API JSON shape)."""
    if not node:
        return None
    iv2 = node.get("image_versions2") or {}
    candidates = iv2.get("candidates") or []
    if candidates:
        best = max(
            candidates,
            key=lambda c: (c.get("width", 0) or 0) * (c.get("height", 0) or 0),
        )
        u = best.get("url")
        if u:
            return _upgrade_instagram_image_url(u)
    # Older GraphQL shape: display_resources / display_url
    dr = node.get("display_resources") or []
    if dr:
        best = max(dr, key=lambda d: (d.get("config_width", 0) or 0))
        u = best.get("src")
        if u:
            return _upgrade_instagram_image_url(u)
    u = node.get("display_url")
    if u:
        return _upgrade_instagram_image_url(u)
    return None


def _ig_best_video_from_node(node: dict) -> str | None:
    """Direct video URL from an IG media node (web-API JSON shape)."""
    if not node:
        return None
    vv = node.get("video_versions") or []
    if vv:
        best = max(vv, key=lambda v: (v.get("width", 0) or 0) * (v.get("height", 0) or 0))
        u = best.get("url")
        if u:
            return u
    u = node.get("video_url")  # older GraphQL shape
    if u:
        return u
    return None


def _ig_node_is_video(node: dict) -> bool:
    if not node:
        return False
    mt = node.get("media_type")
    if mt == 2:  # 1=image, 2=video, 8=carousel
        return True
    if node.get("is_video") is True:
        return True
    if node.get("video_versions") or node.get("video_url"):
        return True
    return False


def _ig_iter_media_nodes(media: dict):
    """Yield each leaf media node (handles single + carousel shapes)."""
    if not media:
        return
    # Carousel (REST shape): carousel_media list
    car = media.get("carousel_media")
    if isinstance(car, list) and car:
        for child in car:
            yield child
        return
    # Carousel (GraphQL shape): edge_sidecar_to_children.edges[].node
    sidecar = (media.get("edge_sidecar_to_children") or {}).get("edges")
    if isinstance(sidecar, list) and sidecar:
        for edge in sidecar:
            node = edge.get("node") if isinstance(edge, dict) else None
            if node:
                yield node
        return
    # Single media
    yield media


def _ig_fetch_media_json(shortcode: str) -> dict | None:
    """Return the media dict for a shortcode via IG's internal web endpoints.

    Tries (1) the public ?__a=1 JSON, (2) the GraphQL query endpoint with a
    couple of known doc_ids. Returns the inner media dict or None.
    """
    headers = _ig_api_headers()

    # (1) Legacy/public JSON endpoint. Often still served for public posts.
    for suffix in ("?__a=1&__d=dis", "?__a=1"):
        try:
            j = _http_get_json(
                f"https://www.instagram.com/p/{shortcode}/{suffix}",
                headers=headers,
            )
        except Exception as e:
            print(f"[ig-api] __a=1 ({suffix}) error: {e}")
            continue
        media = (
            (j.get("graphql") or {}).get("shortcode_media")
            or (j.get("items") or [None])[0]
            or j.get("shortcode_media")
        )
        if media:
            print("[ig-api] got media via __a=1")
            return media

    # (2) GraphQL query endpoint. doc_id values rotate; try several.[web:scrapfly]
    from urllib.parse import urlencode
    doc_ids = ["8845758582119845", "10015901848480474", "9510064595728286"]
    for doc_id in doc_ids:
        try:
            payload = urlencode(
                {
                    "doc_id": doc_id,
                    "variables": json.dumps(
                        {
                            "shortcode": shortcode,
                            "fetch_comment_count": 0,
                            "fetch_related_profile_media_count": 0,
                            "parent_comment_count": 0,
                            "child_comment_count": 0,
                            "fetch_like_count": 0,
                            "fetch_tagged_user_count": None,
                            "fetch_preview_comment_count": 0,
                            "has_threaded_comments": False,
                            "hoisted_comment_id": None,
                            "hoisted_reply_id": None,
                        }
                    ),
                }
            ).encode("utf-8")
            h = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
            j = _http_post_json(
                "https://www.instagram.com/graphql/query", payload, headers=h
            )
        except Exception as e:
            print(f"[ig-api] graphql doc_id={doc_id} error: {e}")
            continue
        media = (
            ((j.get("data") or {}).get("xdt_shortcode_media"))
            or ((j.get("data") or {}).get("shortcode_media"))
        )
        if media:
            print(f"[ig-api] got media via graphql doc_id={doc_id}")
            return media

    return None


def fetch_instagram_via_web_api(url: str, temp_dir: Path) -> tuple[list[Path], list[Path]]:
    """Download IG images/videos via the internal web API (no login).

    Handles single image, carousel (images, videos, or mixed). Returns
    (image_paths, video_paths). Either may be empty if extraction fails.
    """
    image_paths: list[Path] = []
    video_paths: list[Path] = []
    shortcode = _ig_shortcode_from_url(url)
    if not shortcode:
        print("[ig-api] could not parse shortcode from url")
        return image_paths, video_paths

    print(f"[ig-api] shortcode={shortcode}")
    media = _ig_fetch_media_json(shortcode)
    if not media:
        print("[ig-api] no media json")
        return image_paths, video_paths

    nodes = list(_ig_iter_media_nodes(media))
    print(f"[ig-api] {len(nodes)} media node(s)")
    for idx, node in enumerate(nodes, start=1):
        if _ig_node_is_video(node):
            vurl = _ig_best_video_from_node(node)
            if not vurl:
                continue
            dst = temp_dir / f"{idx:03d}_vid.mp4"
            try:
                data = _http_get_bytes(vurl)
                if data:
                    with open(dst, "wb") as fh:
                        fh.write(data)
                    video_paths.append(dst)
            except Exception as e:
                print(f"[ig-api] video node {idx} dl error: {e}")
        else:
            iurl = _ig_best_image_from_node(node)
            if not iurl:
                continue
            dst = temp_dir / f"{idx:03d}_ig{_img_ext_for(iurl)}"
            if _download_image_url(iurl, dst):
                image_paths.append(dst)

    image_paths.sort(key=lambda p: p.name)
    video_paths.sort(key=lambda p: p.name)
    print(f"[ig-api] result: {len(image_paths)} image(s), {len(video_paths)} video(s)")
    return image_paths, video_paths



def fetch_instagram_media(url: str, temp_dir: Path) -> tuple[list[Path], list[Path]]:
    """Download all media from an Instagram post/carousel, handling every shape.

    Covers: single image, image + IG music, collage (multi-image), collage with
    music, mixed collage (images AND videos), and reels/stories (videos).

    Primary path uses yt-dlp's metadata extraction (which works for Instagram
    even when bare HTTP gets login-walled / blocked with WinError 10054), then
    downloads each still image from its native display_url (full res) and each
    video entry via yt-dlp. Falls back to yt-dlp bulk download, then HTML JSON
    parse, then og:image.

    Returns (image_paths, video_paths) (either may be empty).
    """
    base_opts, _platform = base_ydl_opts_for_url(url)
    image_paths: list[Path] = []
    video_paths: list[Path] = []

    def _collect(exts: set[str]) -> list[Path]:
        found = [
            p for p in temp_dir.iterdir()
            if p.is_file() and p.suffix.lower() in exts
        ]
        found.sort(key=lambda p: p.name)
        return found

    # --- Primary: yt-dlp metadata extraction, then per-entry download ---
    info = None
    try:
        meta_opts = {
            **base_opts,
            "noplaylist": False,
            "ignoreerrors": True,
            "skip_download": True,
        }
        print("[ig-media] yt-dlp metadata extract")
        with YoutubeDL(meta_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print("[ig-media meta error]", e)
        info = None

    if info:
        entries = info.get("entries")
        if entries is None:
            entries = [info]
        entries = [e for e in entries if e]
        print(f"[ig-media] {len(entries)} entry/entries")

        video_entry_urls: list[tuple[int, str]] = []
        for idx, entry in enumerate(entries, start=1):
            if _entry_is_video(entry):
                ent_url = (
                    entry.get("webpage_url")
                    or entry.get("original_url")
                    or entry.get("url")
                )
                if ent_url:
                    video_entry_urls.append((idx, ent_url))
                continue
            img_url = _best_image_url_from_entry(entry)
            if not img_url:
                continue
            dst = temp_dir / f"{idx:03d}_ig{_img_ext_for(img_url)}"
            if _download_image_url(img_url, dst):
                image_paths.append(dst)

        for idx, ent_url in video_entry_urls:
            vdir = temp_dir / f"vid_{idx:03d}"
            vdir.mkdir(exist_ok=True)
            try:
                vopts = {
                    **base_opts,
                    "noplaylist": True,
                    "ignoreerrors": True,
                    "format": "bestvideo+bestaudio/best",
                    "merge_output_format": "mp4",
                    "outtmpl": str(vdir / "%(id)s.%(ext)s"),
                }
                with YoutubeDL(vopts) as ydl:
                    ydl.extract_info(ent_url, download=True)
                vids = [p for p in vdir.iterdir()
                        if p.is_file() and p.suffix.lower() in _VIDEO_EXTS]
                if vids:
                    biggest = max(vids, key=lambda p: p.stat().st_size)
                    target = temp_dir / f"{idx:03d}_vid{biggest.suffix.lower()}"
                    shutil.move(str(biggest), str(target))
                    video_paths.append(target)
            except Exception as e:
                print(f"[ig-media video entry {idx} error]", e)

    if image_paths or video_paths:
        image_paths.sort(key=lambda p: p.name)
        video_paths.sort(key=lambda p: p.name)
        print(f"[ig-media] primary: {len(image_paths)} image(s), "
              f"{len(video_paths)} video(s)")
        return image_paths, video_paths

    # --- Fallback A: Instagram internal web API (no login) ---
    # This is the key path for image posts/carousels: yt-dlp's IG extractor
    # refuses image-only posts ("There is no video in this post") and returns
    # zero entries for logged-out carousels, so we hit IG's own JSON endpoints.
    try:
        api_imgs, api_vids = fetch_instagram_via_web_api(url, temp_dir)
    except Exception as e:
        print("[ig-media] fallback A (web API) error:", e)
        api_imgs, api_vids = [], []
    if api_imgs or api_vids:
        print(f"[ig-media] fallback A (web API): {len(api_imgs)} image(s), "
              f"{len(api_vids)} video(s)")
        return api_imgs, api_vids

    # --- Fallback B0: yt-dlp bulk download (writes media straight to temp_dir) ---
    try:
        ydl_opts = {
            **base_opts,
            "noplaylist": False,
            "ignoreerrors": True,
            "outtmpl": str(temp_dir / "%(autonumber)03d_%(id)s.%(ext)s"),
        }
        print("[ig-media] fallback A: yt-dlp bulk download")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        print("[ig-media fallback A error]", e)

    imgs = _collect(_IMAGE_EXTS)
    vids = _collect(_VIDEO_EXTS)
    if imgs or vids:
        print(f"[ig-media] fallback A: {len(imgs)} image(s), {len(vids)} video(s)")
        return imgs, vids

    # --- Fallback B: HTML embedded-JSON full-res image URLs ---
    try:
        print("[ig-media] fallback B: HTML/JSON parse")
        html = _http_get_text(url)
        img_urls = extract_instagram_image_urls(html)
        print(f"[ig-media] fallback B found {len(img_urls)} candidate url(s)")
        for idx, img_url in enumerate(img_urls, start=1):
            dst = temp_dir / f"{idx:03d}_ig{_img_ext_for(img_url)}"
            _download_image_url(img_url, dst)
    except Exception as e:
        print("[ig-media fallback B error]", e)

    imgs = _collect(_IMAGE_EXTS)
    if imgs:
        print(f"[ig-media] fallback B: {len(imgs)} image(s)")
        return imgs, []

    # --- Fallback C: og:image (cropped) last resort ---
    try:
        print("[ig-media] fallback C: og:image")
        html = _http_get_text(url)
        img_url = extract_og_image_from_html(html)
        if img_url:
            img_url = _upgrade_instagram_image_url(img_url)
            dst = temp_dir / f"og_image{_img_ext_for(img_url)}"
            _download_image_url(img_url, dst)
    except Exception as e:
        print("[ig-media fallback C error]", e)

    return _collect(_IMAGE_EXTS), _collect(_VIDEO_EXTS)


def fetch_instagram_images_robust(url: str, temp_dir: Path) -> list[Path]:
    """Backwards-compatible wrapper: return just the image paths from a post.

    Uses fetch_instagram_media. If the post is purely a video (reel), no images
    are returned (the caller handles the video path separately).
    """
    images, _videos = fetch_instagram_media(url, temp_dir)
    return images


def build_slideshow_with_audio(images: list[Path], audio: Path | None, dst: Path, temp_dir: Path):
    """
    Build a simple slideshow MP4 from a list of images and optional audio.
    Shows each image for ~3 seconds; audio is trimmed/looped via -shortest.
    """
    if not images:
        raise RuntimeError("No images for slideshow.")

    slides_dir = temp_dir / "slides"
    slides_dir.mkdir(exist_ok=True)

    slide_paths: list[Path] = []
    for idx, img in enumerate(images, start=1):
        ext = img.suffix.lower() or ".jpg"
        slide_path = slides_dir / f"slide_{idx:03d}{ext}"
        shutil.copy(img, slide_path)
        slide_paths.append(slide_path)

    slides_list = temp_dir / "slides.txt"
    with open(slides_list, "w", encoding="utf-8") as f:
        for p in slide_paths:
            f.write(f"file '{p.as_posix()}'\n")
            f.write("duration 3\n")
        f.write(f"file '{slide_paths[-1].as_posix()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(slides_list),
    ]
    if audio is not None:
        cmd += ["-i", str(audio)]
    cmd += [
        "-vsync", "vfr",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
    ]
    if audio is not None:
        cmd += [
            "-c:a", "aac",
            "-shortest",
        ]
    cmd.append(str(dst))
    print("[ffmpeg slideshow]", " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT)


def download_media(url: str, mode: str, quality: str | None, user_id: int, start: str | None, end: str | None):
    settings = get_user_settings(user_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="lexi_"))

    base_opts, platform = base_ydl_opts_for_url(url)
    common_opts = {
        **base_opts,
        "outtmpl": str(temp_dir / "%(title).120s.%(ext)s"),
    }

    print(f"[download_media] platform={platform} mode={mode} quality={quality} user={user_id} temp={temp_dir}")

    if platform == "spotify" and mode in ("video", "circle", "image", "imgvideo"):
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ValueError("Spotify links are DRM-protected; only supported as external streaming, not downloads.")

    # ---- IMAGE-FIRST BRANCHING ----
    # For image / imgvideo modes we must NOT call the video-oriented fetch_info
    # first, because single-image Instagram posts raise
    # "There is no video in this post" and kill the whole download.

    # IMAGE MODE
    if mode == "image":
        if platform == "instagram":
            image_files, video_files = fetch_instagram_media(url, temp_dir)
            # Title is best-effort and must never crash on photo posts.
            info = fetch_info(url, allow_no_video=True)
            # A mixed carousel (images + clips) or a reel routed here -> send the
            # videos too so nothing is silently dropped.
            if video_files:
                title = info.get("title") or "Instagram post"
                full_size = sum(p.stat().st_size for p in (image_files + video_files))
                if image_files:
                    # Mixed carousel: images AND clips. "mixed" mode tells the
                    # sender to dispatch photos as photos and videos as videos
                    # (classified by file extension).
                    return temp_dir, title, image_files + video_files, "mixed", full_size
                return temp_dir, title, video_files, "video", full_size
            if not image_files:
                raise RuntimeError("No images downloaded for image mode.")
            title = info.get("title") or "Instagram Image"
            full_size = sum(p.stat().st_size for p in image_files)
            return temp_dir, title, image_files, "image", full_size

        # Non-Instagram: try thumbnails via metadata (safe, allow no video).
        info = fetch_info(url, allow_no_video=True)
        title = info.get("title") or "Untitled"
        thumb_url = None
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumbs_sorted = sorted(
                thumbs,
                key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0),
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
                return temp_dir, title, [img_path], "image", img_path.stat().st_size
            except Exception as e:
                print("[image thumb error]", e)

        # Last resort: direct social image extractor (og:image scrape).
        try:
            with urlopen(url) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                html = resp.read().decode(charset, errors="replace")
            img_url = extract_og_image_from_html(html)
            if img_url:
                with urlopen(img_url) as resp:
                    data = resp.read()
                ext = ".jpg"
                low = img_url.lower()
                if ".png" in low:
                    ext = ".png"
                elif ".webp" in low:
                    ext = ".webp"
                img_path = temp_dir / f"og_image{ext}"
                with open(img_path, "wb") as f:
                    f.write(data)
                return temp_dir, title, [img_path], "image", img_path.stat().st_size
        except Exception as e:
            print("[image direct error]", e)

        raise RuntimeError("No images downloaded for image mode.")

    # IMG+MUSIC MODE: Instagram photos -> slideshow video with the post audio.
    if mode == "imgvideo":
        if platform != "instagram":
            # For non-IG, just fall back to normal video download.
            mode = "video"
        else:
            image_files, _carousel_videos = fetch_instagram_media(url, temp_dir)
            if not image_files:
                raise RuntimeError("No images downloaded for Img+Music mode.")

            info = fetch_info(url, allow_no_video=True)
            title = info.get("title") or "Instagram Reel"
            images_bytes = sum(p.stat().st_size for p in image_files)

            audio_dir = temp_dir / "audio_src"
            audio_dir.mkdir(exist_ok=True)
            audio_opts = {
                **base_opts,
                "noplaylist": False,
                "ignoreerrors": True,
                "outtmpl": str(audio_dir / "%(title).120s.%(ext)s"),
                "format": "bestaudio/best",
            }

            audio_file = None
            try:
                print("[imgvideo audio_opts]", audio_opts)
                with YoutubeDL(audio_opts) as ydl:
                    ydl.extract_info(url, download=True)
                try:
                    audio_file = extract_final_file(audio_dir)
                except Exception:
                    audio_file = None
                # Trim the audio AFTER download (robust; avoids ffmpeg seek+merge bug).
                if audio_file and (start or end):
                    trimmed = audio_dir / "trimmed_audio.m4a"
                    audio_file = ffmpeg_trim_file(audio_file, trimmed, start, end, reencode=True)
            except Exception as e:
                print("[imgvideo audio error]", e)
                audio_file = None  # silent slideshow

            slideshow_file = temp_dir / "slideshow.mp4"
            build_slideshow_with_audio(image_files, audio_file, slideshow_file, temp_dir)
            # Bill on the REAL downloaded bytes (images + source audio), not the
            # rendered slideshow size, so this can't be gamed.
            audio_bytes = audio_file.stat().st_size if audio_file and audio_file.exists() else 0
            full_size = images_bytes + audio_bytes
            return temp_dir, title, [slideshow_file], "video", full_size

    # ---- VIDEO / AUDIO METADATA ----
    # For all remaining (video-bearing) modes we fetch info normally. If a user
    # somehow requested video on a photo-only post, surface a clean no-video
    # error so handle_url can fall back to image sending.
    info = fetch_info(url)
    title = info.get("title", "Untitled")

    # Operational length cap on the FULL source video (non-admins). Checked on
    # the whole duration so trimming to a few seconds cannot dodge it.
    enforce_duration_cap(info, user_id)

    # NON-IMAGE MODES
    if mode == "circle":
        ydl_opts = {
            **common_opts,
            "format": "bestvideo[height<=720]+bestaudio/best[height<=720]" if platform == "youtube" else "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
        }
        print("[circle ydl_opts]", ydl_opts)
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        base_file = extract_final_file(temp_dir)
        # Capture the FULL downloaded size BEFORE trim/remux for fair billing.
        full_size = base_file.stat().st_size
        # Trim AFTER download (robust) before remuxing to a circle note.
        if start or end:
            trimmed = temp_dir / "circle_trim.mp4"
            base_file = ffmpeg_trim_file(base_file, trimmed, start, end, reencode=True)
        circle_file = temp_dir / "circle_note.mp4"
        remux_for_circle(base_file, circle_file)
        return temp_dir, title, [circle_file], "circle", full_size

    if mode == "voice":
        ydl_opts = {
            **common_opts,
            "format": "bestaudio/best",
        }
        print("[voice ydl_opts]", ydl_opts)
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        base_file = extract_final_file(temp_dir)
        full_size = base_file.stat().st_size
        voice_file = temp_dir / "voice_message.ogg"
        convert_for_voice(base_file, voice_file, start, end)
        return temp_dir, title, [voice_file], "voice", full_size

    if mode == "video":
        if platform == "youtube":
            base_fmt = VIDEO_QUALITIES[quality]
            # Add progressive fallbacks so long videos / odd format trees still
            # resolve: capped merge -> capped progressive -> best merge -> best.
            height_cap = base_fmt.split("height<=")[1].split("]")[0] if "height<=" in base_fmt else None
            if height_cap:
                fmt = (
                    f"bestvideo[height<={height_cap}]+bestaudio/"
                    f"best[height<={height_cap}]/"
                    f"bestvideo+bestaudio/best"
                )
            else:
                fmt = base_fmt + "/bestvideo+bestaudio/best"
            ydl_opts = {
                **common_opts,
                "format": fmt,
                "merge_output_format": settings["video_format"],
            }
        elif platform == "instagram":
            # IG collages: download full playlist
            ydl_opts = {
                **common_opts,
                "noplaylist": False,
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": settings["video_format"],
            }
        else:
            ydl_opts = {
                **common_opts,
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": settings["video_format"],
            }

        print("[video ydl_opts]", ydl_opts)
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as e:
            # Long YouTube videos can fail on the first format tree (throttling,
            # SABR, missing merged format). Retry once with a fully relaxed
            # format and the broadest set of player clients.
            print("[video first attempt failed, retrying with fallback]", e)
            if platform == "youtube":
                ydl_opts = {
                    **common_opts,
                    "format": "best/bestvideo+bestaudio",
                    "merge_output_format": settings["video_format"],
                    "extractor_args": {
                        "youtube": {"player_client": ["android", "ios", "web", "tv"]},
                    },
                }
                print("[video retry ydl_opts]", ydl_opts)
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)
            else:
                raise

        # ---- Trim AFTER the full download (robust on Windows) ----
        # yt-dlp's ffmpeg external downloader cannot input-seek (-ss/-to) while
        # merging bestvideo+bestaudio -> it dies with code -106 (4294967158).
        # We download the whole file, then run a standalone ffmpeg trim pass.
        def _trim_video(p: Path, idx: int = 0) -> Path:
            if not (start or end):
                return p
            out = temp_dir / f"trimmed_{idx:03d}{p.suffix or '.mp4'}"
            return ffmpeg_trim_file(p, out, start, end, reencode=True)

        if platform == "instagram":
            # Multi-video support (collages)
            files = [p for p in temp_dir.iterdir() if p.is_file()]
            video_exts = {".mp4", ".mkv", ".webm", ".mov"}
            video_files = [p for p in files if p.suffix.lower() in video_exts]
            if video_files:
                video_files.sort(key=lambda p: p.name)
                # FULL downloaded size BEFORE any trim (fair billing).
                full_size = sum(p.stat().st_size for p in video_files)
                if start or end:
                    video_files = [_trim_video(p, i) for i, p in enumerate(video_files)]
                print(f"[download_media] instagram videos={len(video_files)}")
                return temp_dir, title, video_files, "video", full_size

        final_file = extract_final_file(temp_dir)
        # FULL downloaded size BEFORE trim (so trimming can't reduce the bill).
        full_size = final_file.stat().st_size
        final_file = _trim_video(final_file, 0)
        return temp_dir, title, [final_file], "video", full_size

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
    print("[audio ydl_opts]", ydl_opts)
    with YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)
    final_file = extract_final_file(temp_dir)
    # FULL extracted size BEFORE trim (fair billing).
    full_size = final_file.stat().st_size
    # Trim AFTER extraction (robust). Keep the same container/codec via copy
    # when possible; fall back to re-encode if copy produces nothing.
    if start or end:
        trimmed = temp_dir / f"trimmed_audio{final_file.suffix or '.mp3'}"
        cmd = ["ffmpeg", "-y", "-i", str(final_file)]
        if start:
            cmd += ["-ss", ffmpeg_timestamp(start)]
        if end:
            cmd += ["-to", ffmpeg_timestamp(end)]
        cmd += ["-vn", str(trimmed)]
        print("[ffmpeg audio trim]", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT)
            if trimmed.exists() and trimmed.stat().st_size > 0:
                final_file = trimmed
        except Exception as e:
            print("[audio trim error]", e)
    return temp_dir, title, [final_file], "audio", full_size


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = ensure_user_record(update.effective_user)
    await update.message.reply_text(
        "🎧 Lexi Downloader is ready.\n\n"
        f"Send a link from: {SUPPORTED_PLATFORMS_TEXT}.\n\n"
        "✂️ Optional trim (always outputs video). Send the link, then add the\n"
        "times below it — or tap \"📋 Copy trim template\":\n"
        "YouTube URL\n"
        "start=01:23.500\n"
        "end=02:10.000\n\n"
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
        "• /leaderboard – top downloaders\n"
        "• /limits – upload size cap & how to raise to 2GB\n"
        "• /getMyUserID – show your numeric user id (private only)\n\n"
        "✂️ Trim (always video): send a link, then on the next lines add\n"
        "start=00:00.000 and end=00:00.000 — or tap 📋 Copy trim template.\n\n"
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


async def limits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current upload size cap and how to raise it to 2GB."""
    if USING_LOCAL_BOT_API:
        body = (
            "\U0001F4E6 Upload limit\n\n"
            f"\u2705 Local Bot API server: ON\n"
            f"Current cap: {format_size(MAX_FILESIZE_BYTES)} "
            f"(max {format_size(TELEGRAM_LOCAL_LIMIT)}).\n\n"
            "To change it, set BOT_FILE_LIMIT_BYTES in your .env and restart."
        )
    else:
        body = (
            "\U0001F4E6 Upload limit\n\n"
            f"Current cap: {format_size(MAX_FILESIZE_BYTES)} (cloud Bot API).\n\n"
            "To send files up to 2GB, run a LOCAL Bot API server on this PC:\n"
            "1) Get api_id + api_hash at my.telegram.org (API development tools).\n"
            "2) Run: telegram-bot-api --api-id=<ID> --api-hash=<HASH> --local\n"
            "3) In .env set:\n"
            "   TELEGRAM_API_BASE_URL=http://127.0.0.1:8081/bot\n"
            "   TELEGRAM_API_BASE_FILE_URL=http://127.0.0.1:8081/file/bot\n"
            "   BOT_FILE_LIMIT_BYTES=2000000000\n"
            "4) Restart the bot. It auto-switches to local mode (2GB)."
        )
    body += (
        f"\n\n\u23F1 Max video length: {format_hms(MAX_OPERATE_SECONDS)} per download "
        "(applies to the FULL video, not the trimmed part). Admins are exempt.\n"
        "\U0001F4B3 Billing is on the FULL downloaded size — trimming or "
        "compressing afterwards does not lower the token cost."
    )
    await update.message.reply_text(body)


async def get_my_user_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return
    uid = update.effective_user.id
    await update.message.reply_text(f"Your user id: `{uid}`", parse_mode="MarkdownV2")


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

    user_id = update.effective_user.id

    # First try full URL+settings
    try:
        parsed_full = parse_link_message(text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    if parsed_full:
        # Remember the message that contained the URL so results reply to it.
        parsed_full["request_message_id"] = update.message.message_id
        USER_URLS[user_id] = parsed_full
        url = parsed_full["url"]
        start = parsed_full.get("start")
        end = parsed_full.get("end")
        platform_name = parsed_full.get("platform", "unknown")
        request_msg_id = update.message.message_id

        # ---- Auto-download: if enabled for this platform, skip the menu ----
        auto_cfg = get_auto_download_for_platform(user_id, platform_name)
        if auto_cfg["on"]:
            fmt_key = auto_cfg.get("format", "720p")
            mapping = AUTO_DL_FORMATS.get(fmt_key)
            if mapping:
                auto_mode, auto_quality = mapping
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    reply_to_message_id=request_msg_id,
                    text=f"⚡ Auto-download ({fmt_key}) — downloading...",
                )
                await process_and_send_media(
                    context=context,
                    chat_id=update.effective_chat.id,
                    user_id=user_id,
                    effective_user=update.effective_user,
                    url=url,
                    mode=auto_mode,
                    quality=auto_quality,
                    start=start,
                    end=end,
                    reply_to_message_id=request_msg_id,
                    status_edit=None,
                )
                return

        info = None
        try:
            info = await asyncio.to_thread(fetch_info, url)
        except Exception as e:
            err = str(e)
            if platform_name in ("twitter", "reddit", "instagram", "facebook") and _looks_like_no_video_error(err):
                # If an Instagram image post also has music, let the user pick
                # plain image vs image+music (slideshow) instead of auto-DL.
                if platform_name == "instagram" and await asyncio.to_thread(instagram_post_has_audio, url):
                    USER_URLS[user_id] = parsed_full
                    choice_token = store_request(dict(parsed_full))
                    await update.message.reply_text(
                        "🖼🎵 This image post also has music. How do you want it?",
                        reply_markup=image_choice_keyboard(token=choice_token),
                    )
                    return
                await update.message.reply_text("🖼 Detected image-only post, downloading image...")
                temp_dir = None
                # Instagram: route through the shared pipeline so single images,
                # collages, mixed image+video carousels and reels are all handled
                # (and billed on the real downloaded size) consistently.
                if platform_name == "instagram":
                    await process_and_send_media(
                        context=context,
                        chat_id=update.effective_chat.id,
                        user_id=user_id,
                        effective_user=update.effective_user,
                        url=url,
                        mode="image",
                        quality=None,
                        start=start,
                        end=end,
                        reply_to_message_id=request_msg_id,
                        status_edit=None,
                    )
                    return
                # Non-Instagram still posts: og:image direct downloader.
                try:
                    temp_dir, title, single_path = await asyncio.to_thread(
                        download_social_image_direct,
                        url,
                        platform_name,
                    )
                    image_paths = [single_path]

                    # Filter out anything too big to send.
                    sendable = [p for p in image_paths if p.stat().st_size <= MAX_FILESIZE_BYTES]
                    if not sendable:
                        await update.message.reply_text(
                            f"Image(s) exceed the maximum size "
                            f"{format_size(MAX_FILESIZE_BYTES)} this bot can send."
                        )
                        return

                    size_bytes = sum(p.stat().st_size for p in sendable)
                    safe_caption = f"`{escape_markdown(title[:1000], version=2)}`"
                    for idx, p in enumerate(sendable):
                        with open(p, "rb") as f:
                            await context.bot.send_photo(
                                chat_id=update.effective_chat.id,
                                reply_to_message_id=request_msg_id,
                                photo=f,
                                caption=safe_caption if idx == 0 else None,
                                parse_mode="MarkdownV2" if idx == 0 else None,
                            )
                    tokens_used = calculate_tokens_for_size(size_bytes, "image")
                    update_user_after_download(user_id, tokens_used)
                    rec2 = ensure_user_record(update.effective_user)
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        reply_to_message_id=request_msg_id,
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
            info = None

        # Auto image-only send if metadata clearly has thumbnails but no video
        if info and platform_name in ("twitter", "reddit", "instagram", "facebook") and not has_video_format(info) and has_any_thumbnail(info):
            # Instagram image post with music -> offer image vs image+music.
            if platform_name == "instagram" and (
                post_has_audio(info)
                or await asyncio.to_thread(instagram_post_has_audio, url)
            ):
                USER_URLS[user_id] = parsed_full
                choice_token = store_request(dict(parsed_full))
                await update.message.reply_text(
                    "🖼🎵 This image post also has music. How do you want it?",
                    reply_markup=image_choice_keyboard(token=choice_token),
                )
                return
            await update.message.reply_text("🖼 Detected image-only post, downloading image...")
            # Route through the shared pipeline: handles single image, collage,
            # mixed image+video carousel, and reels uniformly, billing on the
            # real downloaded size.
            await process_and_send_media(
                context=context,
                chat_id=update.effective_chat.id,
                user_id=user_id,
                effective_user=update.effective_user,
                url=url,
                mode="image",
                quality=None,
                start=start,
                end=end,
                reply_to_message_id=request_msg_id,
                status_edit=None,
            )
            return

        # Early operational length cap (non-admins): reject too-long videos
        # before showing the menu. The whole-video duration is what matters,
        # so trimming cannot get around it.
        try:
            enforce_duration_cap(info, user_id)
        except DurationCapError as e:
            await update.message.reply_text(f"⛔ {e}")
            return

        platform_label = platform_name.title()
        req_token = store_request(dict(parsed_full))
        await update.message.reply_text(
            f"🔗 Detected platform: {platform_label}\n"
            f"💰 Your total tokens: {rec.get('tokens', 0.0):.1f}\n"
            f"🗓 Monthly left: {rec.get('monthly_tokens', 0.0):.1f} | "
            f"🎁 Purchased left: {rec.get('purchased_tokens', 0.0):.1f}\n\n"
            "Choose format or extra mode:",
            reply_markup=all_options_keyboard(
                user_id,
                info,
                start,
                end,
                token=req_token,
            ),
        )
        return

    # No URL in message: maybe trim settings only
    try:
        trim_settings = parse_trim_settings_only(text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    if not trim_settings:
        await update.message.reply_text(f"Send a valid HTTPS link from {SUPPORTED_PLATFORMS_TEXT}, or trim settings after a URL.")
        return

    current = USER_URLS.get(user_id)
    if not current:
        await update.message.reply_text("Send a URL first, then you can send start/end/mode lines to update trim for that URL.")
        return

    for key in ("start", "end", "mode"):
        val = trim_settings.get(key)
        if val is not None:
            current[key] = val
    USER_URLS[user_id] = current

    await update.message.reply_text(
        "✂️ Trim settings updated for current URL:\n"
        f"start={current.get('start') or 'not set'}\n"
        f"end={current.get('end') or 'not set'}\n"
        f"mode={current.get('mode') or 'video'}"
    )

    info = None
    try:
        info = await asyncio.to_thread(fetch_info, current["url"])
    except Exception:
        info = None

    await update.message.reply_text(
        "Now choose format / mode:",
        reply_markup=all_options_keyboard(
            user_id,
            info,
            current.get("start"),
            current.get("end"),
        ),
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


async def process_and_send_media(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    effective_user,
    url: str,
    mode: str,
    quality: str | None,
    start: str | None,
    end: str | None,
    reply_to_message_id: int | None,
    status_edit=None,
):
    """Download, optionally compress, send media as a reply, and bill tokens.

    Shared by the manual button flow and the auto-download flow.
    All sent media use reply_to_message_id so the result is a reply to the
    user's original link request.
    Returns True on success, False on failure.
    """
    temp_dir = None
    try:
        temp_dir, title, file_paths, final_mode, full_size = await asyncio.to_thread(
            download_media, url, mode, quality, user_id, start, end,
        )

        # Apply user compression setting to video outputs.
        file_paths = maybe_compress_files(file_paths, final_mode, user_id, temp_dir)

        total_size = sum(p.stat().st_size for p in file_paths)
        # Bill on the REAL downloaded size (before trim/compression). This stops
        # abuse where a user downloads a huge file then trims it to 1s to pay
        # almost nothing / hammer the server. Never bill less than what we sent.
        billed_size = max(full_size or 0, total_size)
        if any(p.stat().st_size > MAX_FILESIZE_BYTES for p in file_paths):
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                text=(
                    f"Result file is {format_size(total_size)} which is above "
                    f"the maximum size {format_size(MAX_FILESIZE_BYTES)} this bot can send.\n"
                    "Try a lower quality, stronger compression, or trim a shorter segment.\n"
                    + (
                        "To raise the cap to 2GB, see /limits."
                        if not USING_LOCAL_BOT_API else
                        "Raise BOT_FILE_LIMIT_BYTES in .env (max 2GB on local API)."
                    )
                ),
            )
            return False

        safe_caption = f"`{escape_markdown(title[:1000], version=2)}`"

        if final_mode == "video":
            if len(file_paths) == 1:
                with open(file_paths[0], "rb") as f:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        reply_to_message_id=reply_to_message_id,
                        video=f,
                        caption=safe_caption,
                        parse_mode="MarkdownV2",
                        supports_streaming=True,
                    )
            else:
                for i in range(0, len(file_paths), 10):
                    batch = file_paths[i:i + 10]
                    files = [open(p, "rb") for p in batch]
                    try:
                        media = []
                        for idx, f in enumerate(files):
                            if i == 0 and idx == 0:
                                media.append(InputMediaVideo(f, caption=safe_caption, parse_mode="MarkdownV2"))
                            else:
                                media.append(InputMediaVideo(f))
                        await context.bot.send_media_group(
                            chat_id=chat_id,
                            reply_to_message_id=reply_to_message_id,
                            media=media,
                        )
                    finally:
                        for f in files:
                            f.close()

        elif final_mode == "audio":
            with open(file_paths[0], "rb") as f:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    reply_to_message_id=reply_to_message_id,
                    audio=f,
                    title=title,
                    caption=safe_caption,
                    parse_mode="MarkdownV2",
                )
        elif final_mode == "voice":
            with open(file_paths[0], "rb") as f:
                await context.bot.send_voice(
                    chat_id=chat_id,
                    reply_to_message_id=reply_to_message_id,
                    voice=f,
                    caption=safe_caption,
                    parse_mode="MarkdownV2",
                )
        elif final_mode == "circle":
            with open(file_paths[0], "rb") as f:
                await context.bot.send_video_note(
                    chat_id=chat_id,
                    reply_to_message_id=reply_to_message_id,
                    video_note=f,
                    length=512,
                )
        elif final_mode == "image":
            if len(file_paths) == 1:
                with open(file_paths[0], "rb") as f:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        reply_to_message_id=reply_to_message_id,
                        photo=f,
                        caption=safe_caption,
                        parse_mode="MarkdownV2",
                    )
            else:
                for i in range(0, len(file_paths), 10):
                    batch = file_paths[i:i + 10]
                    files = [open(p, "rb") for p in batch]
                    try:
                        media = []
                        for idx, f in enumerate(files):
                            if i == 0 and idx == 0:
                                media.append(InputMediaPhoto(f, caption=safe_caption, parse_mode="MarkdownV2"))
                            else:
                                media.append(InputMediaPhoto(f))
                        await context.bot.send_media_group(
                            chat_id=chat_id,
                            reply_to_message_id=reply_to_message_id,
                            media=media,
                        )
                    finally:
                        for f in files:
                            f.close()
        elif final_mode == "mixed":
            # Instagram carousel containing BOTH images and clips. Send each
            # piece with the right method (photos as photos, videos as videos),
            # batched into albums of up to 10 mixed items.
            image_exts = {".jpg", ".jpeg", ".png", ".webp"}
            first = True
            for i in range(0, len(file_paths), 10):
                batch = file_paths[i:i + 10]
                files = [open(p, "rb") for p in batch]
                try:
                    media = []
                    for f, p in zip(files, batch):
                        is_img = p.suffix.lower() in image_exts
                        if first:
                            cap = {"caption": safe_caption, "parse_mode": "MarkdownV2"}
                            first = False
                        else:
                            cap = {}
                        if is_img:
                            media.append(InputMediaPhoto(f, **cap))
                        else:
                            media.append(InputMediaVideo(f, **cap))
                    await context.bot.send_media_group(
                        chat_id=chat_id,
                        reply_to_message_id=reply_to_message_id,
                        media=media,
                    )
                finally:
                    for f in files:
                        f.close()
        else:
            raise RuntimeError("Unsupported send mode.")

        # Bill on the REAL downloaded size. "image" is a flat per-image rate, but
        # any size-bearing mode (video/audio/circle/voice/mixed) bills by bytes
        # so trimming/compression can never undercut the true download cost.
        billing_mode = "image" if final_mode == "image" else "video"
        tokens_used = calculate_tokens_for_size(billed_size, billing_mode)
        update_user_after_download(user_id, tokens_used)
        rec2 = ensure_user_record(effective_user)

        # Show the sent size, and (when different) the billed full size so the
        # charge is transparent.
        if billed_size > total_size:
            size_line = (
                f"\U0001F4E6 Sent: {format_size(total_size)} "
                f"(billed on full download {format_size(billed_size)})\n"
            )
        else:
            size_line = f"\U0001F4E6 Size: {format_size(total_size)}\n"

        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            text=(
                size_line
                + f"\u26A1 Tokens used: {tokens_used:.1f}\n"
                f"\U0001F4B0 Total remaining: {rec2.get('tokens', 0.0):.1f} "
                f"(monthly {rec2.get('monthly_tokens', 0.0):.1f}, purchased {rec2.get('purchased_tokens', 0.0):.1f})"
            ),
        )
        return True

    except subprocess.TimeoutExpired:
        msg = "\u23F1 Processing took too long and was aborted."
        if status_edit is not None:
            await status_edit(msg)
        else:
            await context.bot.send_message(chat_id=chat_id, reply_to_message_id=reply_to_message_id, text=msg)
        return False
    except subprocess.CalledProcessError as e:
        msg = f"Download failed: process exited with code {e.returncode}"
        if status_edit is not None:
            await status_edit(msg)
        else:
            await context.bot.send_message(chat_id=chat_id, reply_to_message_id=reply_to_message_id, text=msg)
        return False
    except Exception as e:
        msg = f"Failed: {e}"
        if status_edit is not None:
            await status_edit(msg)
        else:
            await context.bot.send_message(chat_id=chat_id, reply_to_message_id=reply_to_message_id, text=msg)
        return False
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


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
            chat_id=query.message.chat.id,
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
        template = "start=00:00.000\nend=00:00.000"
        escaped = escape_markdown(template, version=2)
        prose = escape_markdown(
            "\u2702\ufe0f Trim template \u2014 tap the block to copy, edit the "
            "times, then send it right after your link (or as a reply to set "
            "trim on the last link). Trim always outputs video.",
            version=2,
        )
        msg = f"{prose}\n\n```\n{escaped}\n```"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "\U0001F4CB Copy to clipboard",
                copy_text=CopyTextButton(text=template),
            )
        ]])
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=msg,
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )
        return

    rec = ensure_user_record(update.effective_user)
    if rec.get("banned"):
        await query.edit_message_text("🚫 You are banned from using this bot.")
        return

    # Image-vs-image+music choice for image posts that also carry audio.
    if data.startswith("imgchoice:"):
        parts = data.split(":")
        choice = parts[1]                       # "image" | "imgvideo"
        tok = parts[2] if len(parts) > 2 else None
        link_data = get_request(tok) if tok else None
        if not link_data:
            link_data = USER_URLS.get(user_id)
        if not link_data:
            await query.edit_message_text("Send a supported link first.")
            return
        request_msg_id = link_data.get("request_message_id")
        label = "image" if choice == "image" else "image + music"
        await query.edit_message_text(f"⏬ Downloading as {label}...")

        async def _imgchoice_status(msg):
            try:
                await query.edit_message_text(msg)
            except Exception:
                await context.bot.send_message(
                    chat_id=query.message.chat.id,
                    reply_to_message_id=request_msg_id,
                    text=msg,
                )

        await process_and_send_media(
            context=context,
            chat_id=query.message.chat.id,
            user_id=user_id,
            effective_user=update.effective_user,
            url=link_data["url"],
            mode=choice,
            quality=None,
            start=link_data.get("start"),
            end=link_data.get("end"),
            reply_to_message_id=request_msg_id,
            status_edit=_imgchoice_status,
        )
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

    if data == "menu:autodl":
        await query.edit_message_text(
            "⚡ Auto download\n\n"
            "Turn it on per platform. When on, just send a link and it downloads "
            "automatically in your chosen format — no menu.\n"
            "Pick one format per platform (a video size, music quality, image, or img+music).",
            reply_markup=autodownload_keyboard(user_id),
        )
        return

    if data.startswith("autotoggle:"):
        _, platform, target = data.split(":", 2)
        set_auto_download(user_id, platform, on=(target == "on"))
        await query.edit_message_reply_markup(reply_markup=autodownload_keyboard(user_id))
        return

    if data.startswith("autofmt:"):
        _, platform, fmt_key = data.split(":", 2)
        if fmt_key in AUTO_DL_FORMATS:
            set_auto_download(user_id, platform, fmt=fmt_key)
        await query.edit_message_reply_markup(reply_markup=autodownload_keyboard(user_id))
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

    # Action callbacks are "<kind>:<value>[:<token>]". Pull the token (if any)
    # so each button click resolves to the exact request it was created for,
    # even if the user sent another link in between (no more duplicates).
    parts = data.split(":")
    kind = parts[0]
    value = parts[1] if len(parts) > 1 else ""
    tok = parts[2] if len(parts) > 2 else None

    link_data = get_request(tok) if tok else None
    if not link_data:
        link_data = USER_URLS.get(user_id)
    if not link_data:
        await query.edit_message_text("Send a supported link first.")
        return

    mode = None
    quality = None
    if kind == "video":
        mode = "video"
        quality = value
    elif kind == "audio":
        mode = "audio"
        quality = value
    elif kind == "extra" and value == "circle":
        mode = "circle"
    elif kind == "extra" and value == "voice":
        mode = "voice"
    elif kind == "extra" and value == "image":
        mode = "image"
    elif kind == "extra" and value == "imgvideo":
        mode = "imgvideo"
    else:
        await query.edit_message_text("Unknown action.")
        return

    request_msg_id = link_data.get("request_message_id")

    await query.edit_message_text("⏬ Downloading, processing and uploading...")

    async def _status_edit(msg):
        try:
            await query.edit_message_text(msg)
        except Exception:
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                reply_to_message_id=request_msg_id,
                text=msg,
            )

    await process_and_send_media(
        context=context,
        chat_id=query.message.chat.id,
        user_id=user_id,
        effective_user=update.effective_user,
        url=link_data["url"],
        mode=mode,
        quality=quality,
        start=link_data.get("start"),
        end=link_data.get("end"),
        reply_to_message_id=request_msg_id,
        status_edit=_status_edit,
    )


def main():
    builder = Application.builder().token(BOT_TOKEN).concurrent_updates(True)
    # When a local Bot API server is configured, route through it to unlock
    # 2GB uploads. read/write/connect timeouts are raised for big files.
    if USING_LOCAL_BOT_API:
        builder = builder.base_url(TELEGRAM_API_BASE_URL).local_mode(True)
        if TELEGRAM_API_BASE_FILE_URL:
            builder = builder.base_file_url(TELEGRAM_API_BASE_FILE_URL)
        builder = (
            builder
            .read_timeout(900)
            .write_timeout(900)
            .connect_timeout(120)
            .media_write_timeout(900)
        )
        print(f"[main] Local Bot API mode ON -> {TELEGRAM_API_BASE_URL} "
              f"(upload cap {format_size(MAX_FILESIZE_BYTES)})")
    else:
        print(f"[main] Cloud Bot API mode (upload cap {format_size(MAX_FILESIZE_BYTES)}). "
              f"Set TELEGRAM_API_BASE_URL to enable 2GB uploads.")
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("tokens", tokens_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("limits", limits_cmd))
    app.add_handler(CommandHandler("getMyUserID", get_my_user_id_cmd))

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