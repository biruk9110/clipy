"""Host-level configuration: directories, environment fallbacks, temp settings.

Per-user preferences and secrets live in the database (see usersettings.py);
this module only holds paths and host-level env configuration.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Windows consoles/redirected pipes default to a legacy codepage; emoji in
# video titles would crash print(). Force UTF-8 with replacement everywhere.
import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CLIPY_DATA_DIR", BASE_DIR / "data"))
UPLOADS_DIR = Path(os.getenv("CLIPY_UPLOADS_DIR", BASE_DIR / "uploads"))
OUTPUT_DIR = Path(os.getenv("CLIPY_OUTPUT_DIR", BASE_DIR / "output"))
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

for d in [DATA_DIR, UPLOADS_DIR, OUTPUT_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

# Host-level knobs (not per-user)
HOST_SETTINGS = {
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),   # optional server-wide fallback key
    "buffer_access_token": os.getenv("BUFFER_ACCESS_TOKEN", ""),
    "tiktok_session_id": os.getenv("TIKTOK_SESSION_ID", ""),  # for public reference downloads
    "youtube_cookies": os.getenv("YOUTUBE_COOKIES", ""),
    "cookies_txt": "",                                   # optional cookies.txt contents
    "max_upload_mb": int(os.getenv("CLIPY_MAX_UPLOAD_MB", "500")),
    "whisper_model": os.getenv("CLIPY_WHISPER_MODEL", "tiny"),
}


def get_settings():
    """Legacy host settings accessor (kept for modules still using it)."""
    return dict(HOST_SETTINGS)


def save_settings(new_settings: dict):
    """Legacy no-op: host settings now come from the environment only."""
    return dict(HOST_SETTINGS)
