import os
import requests
import urllib.parse
from .config import get_settings, save_settings

# Base domain for OAuth redirects
def get_base_url():
    settings = get_settings()
    # Read custom domain or Render public URL
    return os.getenv("RENDER_EXTERNAL_URL") or settings.get("base_url") or "https://clipy-0o5j.onrender.com"

# ----------------- INSTAGRAM OAUTH -----------------
def get_instagram_auth_url():
    settings = get_settings()
    client_id = os.getenv("INSTAGRAM_CLIENT_ID") or settings.get("instagram_client_id")
    if not client_id:
        return None
    redirect_uri = f"{get_base_url()}/auth/instagram/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "instagram_basic,instagram_content_publish",
        "response_type": "code"
    }
    return f"https://api.instagram.com/oauth/authorize?{urllib.parse.urlencode(params)}"

def handle_instagram_callback(code: str):
    settings = get_settings()
    client_id = os.getenv("INSTAGRAM_CLIENT_ID") or settings.get("instagram_client_id")
    client_secret = os.getenv("INSTAGRAM_CLIENT_SECRET") or settings.get("instagram_client_secret")
    redirect_uri = f"{get_base_url()}/auth/instagram/callback"
    
    token_url = "https://api.instagram.com/oauth/access_token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code
    }
    
    r = requests.post(token_url, data=payload)
    data = r.json()
    if "access_token" in data:
        user_id = data.get("user_id")
        save_settings({
            "instagram_access_token": data["access_token"],
            "instagram_user_id": user_id,
            "instagram_username": f"User_{str(user_id)[-4:]}",
            "auto_post_instagram": True
        })
        return {"success": True, "user_id": user_id}
    return {"success": False, "error": str(data)}

# ----------------- TIKTOK OAUTH -----------------
def get_tiktok_auth_url():
    settings = get_settings()
    client_key = os.getenv("TIKTOK_CLIENT_KEY") or settings.get("tiktok_client_key")
    if not client_key:
        return None
    redirect_uri = f"{get_base_url()}/auth/tiktok/callback"
    params = {
        "client_key": client_key,
        "redirect_uri": redirect_uri,
        "scope": "user.info.basic,video.upload,video.publish",
        "response_type": "code"
    }
    return f"https://www.tiktok.com/v2/auth/authorize/?{urllib.parse.urlencode(params)}"

def handle_tiktok_callback(code: str):
    settings = get_settings()
    client_key = os.getenv("TIKTOK_CLIENT_KEY") or settings.get("tiktok_client_key")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET") or settings.get("tiktok_client_secret")
    redirect_uri = f"{get_base_url()}/auth/tiktok/callback"
    
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri
    }
    
    r = requests.post(token_url, headers=headers, data=payload)
    data = r.json()
    if "data" in data and "access_token" in data["data"]:
        token = data["data"]["access_token"]
        open_id = data["data"].get("open_id")
        save_settings({
            "tiktok_access_token": token,
            "tiktok_open_id": open_id,
            "tiktok_username": f"TikTok_{str(open_id)[-4:]}",
            "auto_post_tiktok": True
        })
        return {"success": True, "open_id": open_id}
    return {"success": False, "error": str(data)}
