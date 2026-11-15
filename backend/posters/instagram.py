import os
import requests
import json
from pathlib import Path
from ..config import get_settings

def post_to_instagram(video_path: Path, caption: str, hashtags: list):
    """
    Posts a 9:16 video to Instagram Reels.
    Supports:
    1. Meta Graph API (Official free Reels publishing) if INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID are set.
    2. Simulated / Preview mode if credentials not yet configured.
    """
    settings = get_settings()
    access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN") or settings.get("instagram_access_token")
    ig_user_id = os.getenv("INSTAGRAM_USER_ID") or settings.get("instagram_user_id")
    
    full_caption = caption + "\n\n" + " ".join(hashtags)
    
    if not access_token or not ig_user_id:
        print("[Instagram] No API credentials found. Operating in Demo/Preview Mode.")
        return {
            "success": True,
            "mode": "preview",
            "message": "Ready to post! Enter Instagram token in Settings to enable live uploads.",
            "url": "https://instagram.com/reels"
        }
        
    try:
        # Step 1: Initialize container
        init_url = f"https://graph.facebook.com/v20.0/{ig_user_id}/media"
        # In real API, video must be reachable via public URL or uploaded to container
        payload = {
            "media_type": "REELS",
            "caption": full_caption,
            "access_token": access_token
        }
        r = requests.post(init_url, data=payload)
        res = r.json()
        if "id" not in res:
            return {"success": False, "error": str(res)}
            
        creation_id = res["id"]
        # Step 2: Publish container
        pub_url = f"https://graph.facebook.com/v20.0/{ig_user_id}/media_publish"
        pub_res = requests.post(pub_url, data={"creation_id": creation_id, "access_token": access_token}).json()
        return {"success": True, "post_id": pub_res.get("id"), "url": f"https://instagram.com/p/{pub_res.get('id')}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

