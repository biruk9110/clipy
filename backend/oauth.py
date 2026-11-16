"""OAuth flows for Instagram, TikTok and YouTube (per-user accounts).

The OAuth `state` parameter carries the initiating user's session token so the
callback can attribute the account to the right user (and reject callbacks
without a valid session).
"""
import hashlib
import os
import secrets
import urllib.parse

import requests

from .accounts import upsert_account
from .database import db
from .usersettings import get_prefs

OAUTH_STATES = {}  # state -> user_id (transient, also stored in DB for restarts)


def get_base_url() -> str:
    settings = db.one("SELECT value FROM app_meta WHERE key = ?", ("base_url",))
    return (os.getenv("RENDER_EXTERNAL_URL") or (settings or {}).get("value")
            or "https://clipy-0o5j.onrender.com").rstrip("/")


def set_base_url(url: str):
    if url:
        _upsert_meta("base_url", url.rstrip("/"))


def _upsert_meta(key: str, value: str):
    from .database import IS_PG
    if IS_PG:
        db.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)"
                   " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
    else:
        db.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)", (key, value))


def create_state(user_id: int, platform: str) -> str:
    state = secrets.token_urlsafe(24)
    _upsert_meta(f"oauth_state_{state}", f"{user_id}|{platform}|{db.now()}")
    return state


def consume_state(state: str) -> tuple[int | None, str]:
    """Returns (user_id, platform) for a valid, fresh state; then deletes it."""
    if not state:
        return None, ""
    row = db.one("SELECT value FROM app_meta WHERE key = ?", (f"oauth_state_{state}",))
    if not row:
        return None, ""
    db.execute("DELETE FROM app_meta WHERE key = ?", (f"oauth_state_{state}",))
    try:
        user_str, platform, created = (row["value"] or "").split("|")
        user_id = int(user_str)
    except Exception:
        return None, ""
    return user_id, platform


# ---------------------------------------------------------------------------
# Instagram (Instagram API with Instagram Login)
# ---------------------------------------------------------------------------
IG_SCOPES = "instagram_basic,instagram_content_publish,business_basic"


def get_instagram_auth_url(user_id: int) -> str | None:
    client_id = os.getenv("INSTAGRAM_CLIENT_ID", "")
    if not client_id:
        return None
    params = {
        "client_id": client_id,
        "redirect_uri": f"{get_base_url()}/auth/instagram/callback",
        "scope": IG_SCOPES,
        "response_type": "code",
        "state": create_state(user_id, "instagram"),
    }
    return "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode(params)


def handle_instagram_callback(code: str, user_id: int) -> dict:
    client_id = os.getenv("INSTAGRAM_CLIENT_ID", "")
    client_secret = os.getenv("INSTAGRAM_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        return {"success": False, "error": "Instagram app credentials are not configured on the server."}
    try:
        r = requests.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": f"{get_base_url()}/auth/instagram/callback",
            "code": code,
        }, timeout=20)
        data = r.json()
    except Exception as e:
        return {"success": False, "error": f"Instagram token exchange failed: {e}"}
    if "access_token" not in data:
        err = (data.get("error_message") or str(data))[:250]
        return {"success": False, "error": err}
    user_id_ig = str(data.get("user_id", ""))
    # Discover username via Graph
    display = f"IG User {user_id_ig[-4:]}"
    try:
        me = requests.get("https://graph.facebook.com/v21.0/me",
                          params={"fields": "username", "access_token": data["access_token"]},
                          timeout=15).json()
        if me.get("username"):
            display = f"@{me['username']}"
    except Exception:
        pass
    from datetime import datetime, timedelta
    upsert_account(user_id, "instagram", access_token=data["access_token"],
                   account_id=user_id_ig, display_name=display,
                   expires_at=(datetime.now() + timedelta(days=55)).isoformat(timespec="seconds"),
                   meta={"ig_user_id": user_id_ig, "token_type": data.get("token_type", "")})
    return {"success": True, "display": display}


# ---------------------------------------------------------------------------
# TikTok (Content Posting API)
# ---------------------------------------------------------------------------
TIKTOK_SCOPES = "user.info.basic,video.upload,video.publish"


def get_tiktok_auth_url(user_id: int) -> str | None:
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "")
    if not client_key:
        return None
    verifier = secrets.token_urlsafe(48)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    _upsert_meta(f"pkce_{user_id}", verifier)
    params = {
        "client_key": client_key,
        "redirect_uri": f"{get_base_url()}/auth/tiktok/callback",
        "scope": TIKTOK_SCOPES,
        "response_type": "code",
        "state": create_state(user_id, "tiktok"),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)


def handle_tiktok_callback(code: str, user_id: int) -> dict:
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "")
    if not (client_key and client_secret):
        return {"success": False, "error": "TikTok app credentials are not configured on the server."}
    verifier = ""
    row = db.one("SELECT value FROM app_meta WHERE key = ?", (f"pkce_{user_id}",))
    if row:
        verifier = row["value"]
        db.execute("DELETE FROM app_meta WHERE key = ?", (f"pkce_{user_id}",))
    try:
        payload = {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"{get_base_url()}/auth/tiktok/callback",
        }
        if verifier:
            payload["code_verifier"] = verifier
        r = requests.post("https://open.tiktokapis.com/v2/oauth/token/",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data=payload, timeout=20)
        data = r.json().get("data") or {}
    except Exception as e:
        return {"success": False, "error": f"TikTok token exchange failed: {e}"}
    if not data.get("access_token"):
        return {"success": False, "error": str(data)[:250] or "TikTok rejected the authorization"}

    display = "TikTok Account"
    try:
        me = requests.get("https://open.tiktokapis.com/v2/user/info/",
                          params={"fields": "open_id,display_name"},
                          headers={"Authorization": f"Bearer {data['access_token']}"},
                          timeout=15).json()
        u = (me.get("data") or {}).get("user") or {}
        display = u.get("display_name") or display
    except Exception:
        pass
    from datetime import datetime, timedelta
    expires_in = int(data.get("expires_in") or 86400)
    upsert_account(user_id, "tiktok", access_token=data["access_token"],
                   refresh_token=data.get("refresh_token", ""),
                   account_id=data.get("open_id", ""), display_name=display,
                   expires_at=(datetime.now() + timedelta(seconds=expires_in)).isoformat(timespec="seconds"),
                   meta={"client_key": client_key, "client_secret": client_secret,
                         "scopes": TIKTOK_SCOPES})
    return {"success": True, "display": display}


# ---------------------------------------------------------------------------
# YouTube (Data API v3 - upload + manage)
# ---------------------------------------------------------------------------
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def get_youtube_auth_url(user_id: int) -> str | None:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "")
    if not client_id:
        return None
    params = {
        "client_id": client_id,
        "redirect_uri": f"{get_base_url()}/auth/youtube/callback",
        "response_type": "code",
        "scope": YOUTUBE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": create_state(user_id, "youtube"),
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def handle_youtube_callback(code: str, user_id: int) -> dict:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        return {"success": False, "error": "YouTube app credentials are not configured on the server."}
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"{get_base_url()}/auth/youtube/callback",
        }, timeout=20)
        data = r.json()
    except Exception as e:
        return {"success": False, "error": f"YouTube token exchange failed: {e}"}
    if "access_token" not in data:
        return {"success": False, "error": str(data)[:250]}

    from .posters.youtube import fetch_channel_title
    channel = fetch_channel_title(data["access_token"]) or "YouTube Channel"
    from datetime import datetime, timedelta
    expires_in = int(data.get("expires_in") or 3600)
    upsert_account(user_id, "youtube", access_token=data["access_token"],
                   refresh_token=data.get("refresh_token", ""),
                   account_id="", display_name=channel,
                   expires_at=(datetime.now() + timedelta(seconds=expires_in)).isoformat(timespec="seconds"),
                   meta={"client_id": client_id, "client_secret": client_secret})
    return {"success": True, "display": channel}
