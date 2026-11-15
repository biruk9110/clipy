import os
from pathlib import Path
from ..config import get_settings

def post_to_tiktok(video_path: Path, caption: str, hashtags: list):
    """
    Posts a 9:16 video to TikTok.
    Supports session_id or browser cookies (free).
    If no session ID is supplied, runs in Preview/Demo mode.
    """
    settings = get_settings()
    session_id = settings.get("tiktok_session_id") or os.getenv("TIKTOK_SESSION_ID")
    
    full_caption = caption + " " + " ".join(hashtags)
    
    if not session_id:
        print("[TikTok] No TikTok Session ID configured. Operating in Demo/Preview Mode.")
        return {
            "success": True,
            "mode": "preview",
            "message": "Ready to post! Enter TikTok session ID in Settings to enable live uploads.",
            "url": "https://tiktok.com/upload"
        }
        
    try:
        from tiktok_uploader.upload import upload_video
        success = upload_video(
            str(video_path),
            description=full_caption[:150],
            cookies=session_id
        )
        return {"success": success, "message": "Uploaded to TikTok!" if success else "TikTok upload failed."}
    except Exception as e:
        return {"success": False, "error": str(e)}

