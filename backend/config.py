import os
import json
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"

for d in [DATA_DIR, UPLOADS_DIR, OUTPUT_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)

DEFAULT_SETTINGS = {
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
    "default_clips_count": 3,
    "schedule_interval_hours": 3,
    "auto_post_tiktok": False,
    "auto_post_instagram": False,
    "auto_post_youtube": False,
    "tiktok_session_id": os.getenv("TIKTOK_SESSION_ID", ""),
    "instagram_username": os.getenv("INSTAGRAM_USERNAME", ""),
    "instagram_password": os.getenv("INSTAGRAM_PASSWORD", ""),
    "buffer_access_token": os.getenv("BUFFER_ACCESS_TOKEN", ""),
    "buffer_post_youtube": True,          # also publish to YouTube channels connected in Buffer
    # --- Direct YouTube (OAuth) ---
    "youtube_client_id": os.getenv("YOUTUBE_CLIENT_ID", ""),
    "youtube_client_secret": os.getenv("YOUTUBE_CLIENT_SECRET", ""),
    "youtube_access_token": "",
    "youtube_refresh_token": "",
    "youtube_channel_title": "",
    # --- Posting behaviour ---
    "review_mode": False,                 # ask before auto-posting (scheduler holds scheduled clips)
    "default_hashtags": "",               # appended to every caption
    "notify_enabled": False,              # web notification when jobs finish
    # --- Storage / automation ---
    "keep_last_clips": 0,                 # keep only the N newest clips (0 = keep all)
    "max_clip_age_days": 0,               # delete clips older than X days (0 = never)
    "delete_source_after": False,         # remove uploaded/downloaded source video after clipping
    # --- Security ---
    "access_pin_hash": "",                # sha256 of PIN, empty = no lock
    "whisper_model": "tiny", # fast, free, local
    "layout_mode": "auto" # auto: stacks facecam or blurs 9:16
}

def get_settings():
    if not SETTINGS_FILE.exists():
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            merged = DEFAULT_SETTINGS.copy()
            merged.update(data)
            return merged
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(new_settings: dict):
    current = DEFAULT_SETTINGS.copy()
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                current.update(json.load(f))
        except Exception:
            pass
    current.update(new_settings)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    if current.get("gemini_api_key"):
        os.environ["GEMINI_API_KEY"] = current["gemini_api_key"]
    return current

