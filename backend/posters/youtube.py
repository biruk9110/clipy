import os
import requests
from pathlib import Path
from ..config import get_settings, save_settings

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"


def _refresh_access_token() -> str:
    """Exchanges the stored refresh token for a fresh access token."""
    settings = get_settings()
    refresh_token = settings.get("youtube_refresh_token")
    if not refresh_token:
        return ""
    try:
        r = requests.post(TOKEN_URL, data={
            "client_id": settings.get("youtube_client_id") or os.getenv("YOUTUBE_CLIENT_ID", ""),
            "client_secret": settings.get("youtube_client_secret") or os.getenv("YOUTUBE_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=15)
        data = r.json()
        if "access_token" in data:
            save_settings({"youtube_access_token": data["access_token"]})
            return data["access_token"]
        print(f"[YouTube] Token refresh failed: {data}")
    except Exception as e:
        print(f"[YouTube] Token refresh error: {e}")
    return ""


def _auth_headers():
    token = get_settings().get("youtube_access_token")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def post_to_youtube(video_path, caption: str, hashtags: list, title: str = ""):
    """
    Uploads a clip as a YouTube Short (public) using the stored OAuth token.
    Handles automatic token refresh when the access token has expired.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        return {"success": False, "error": "Video file missing"}

    settings = get_settings()
    if not (settings.get("youtube_access_token") or settings.get("youtube_refresh_token")):
        return {"success": False, "error": "YouTube not connected"}

    headers = _auth_headers()
    if not headers:
        return {"success": False, "error": "No YouTube access token"}

    tags = [h.lstrip("#") for h in hashtags if isinstance(hashtags, list)] if hashtags else []
    video_title = (title or "Clip").strip()[:95]
    description = (caption or "")[:4900]

    meta = {
        "snippet": {
            "title": video_title,
            "description": description,
            "tags": tags,
            "categoryId": "20",  # Gaming
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    try:
        # Step 1: initiate a resumable upload session
        init = requests.post(
            f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
            headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
            json=meta,
            timeout=20,
        )
        if init.status_code == 401:
            # Token expired -> refresh once and retry
            new_token = _refresh_access_token()
            if not new_token:
                return {"success": False, "error": "YouTube token expired and refresh failed"}
            headers = {"Authorization": f"Bearer {new_token}"}
            init = requests.post(
                f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
                headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
                json=meta,
                timeout=20,
            )
        if init.status_code not in (200, 201):
            return {"success": False, "error": f"YouTube init failed: {init.text[:300]}"}

        upload_url = init.headers.get("Location")
        if not upload_url:
            return {"success": False, "error": "YouTube did not return an upload URL"}

        # Step 2: upload the actual video bytes
        with open(video_path, "rb") as f:
            size = video_path.stat().st_size
            upload = requests.put(
                upload_url,
                headers={**headers, "Content-Length": str(size), "Content-Type": "video/mp4"},
                data=f,
                timeout=600,
            )

        if upload.status_code in (200, 201):
            data = upload.json()
            video_id = data.get("id", "")
            return {
                "success": True,
                "url": f"https://youtube.com/shorts/{video_id}" if video_id else "",
                "id": video_id,
            }
        return {"success": False, "error": f"YouTube upload failed ({upload.status_code}): {upload.text[:300]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def fetch_channel_title(access_token: str) -> str:
    try:
        r = requests.get(
            f"{API_URL}/channels?part=snippet&mine=true",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        data = r.json()
        items = data.get("items", [])
        if items:
            return items[0]["snippet"]["title"]
    except Exception as e:
        print(f"[YouTube] Channel fetch error: {e}")
    return ""
