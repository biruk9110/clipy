import os
import requests
from pathlib import Path
from ..config import get_settings

def get_buffer_profiles(access_token: str):
    """Fetches all social channels connected to the user's Buffer account."""
    url = f"https://api.bufferapp.com/1/profiles.json?access_token={access_token}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception as e:
        print(f"[Buffer] Error getting profiles: {e}")
        return []

def post_to_buffer(video_url: str, caption: str, hashtags: list, schedule_timestamp: int = None):
    """
    Schedules/Posts a video clip to all connected channels on Buffer (Instagram, TikTok, etc.).
    """
    settings = get_settings()
    access_token = settings.get("buffer_access_token") or os.getenv("BUFFER_ACCESS_TOKEN")
    if not access_token:
        return {"success": False, "error": "No Buffer access token configured"}
        
    profiles = get_buffer_profiles(access_token)
    if not profiles:
        return {"success": False, "error": "No connected channels found on your Buffer account"}
        
    profile_ids = [p["id"] for p in profiles]
    full_text = caption + "\n\n" + (" ".join(hashtags) if isinstance(hashtags, list) else hashtags)
    
    endpoint = "https://api.bufferapp.com/1/updates/create.json"
    payload = {
        "access_token": access_token,
        "profile_ids[]": profile_ids,
        "text": full_text,
        "media[video]": video_url,
        "now": "true" if not schedule_timestamp else "false"
    }
    if schedule_timestamp:
        payload["scheduled_at"] = schedule_timestamp
        
    try:
        r = requests.post(endpoint, data=payload, timeout=20)
        res = r.json()
        if res.get("success"):
            return {
                "success": True,
                "message": f"Successfully queued to {len(profile_ids)} channels on Buffer!",
                "updates": res.get("updates", [])
            }
        return {"success": False, "error": str(res)}
    except Exception as e:
        return {"success": False, "error": str(e)}
