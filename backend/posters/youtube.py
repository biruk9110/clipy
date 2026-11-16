"""YouTube Shorts publishing via the official Data API (OAuth, user-scoped)."""
import os
import requests
from pathlib import Path

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"


def refresh_access_token(refresh_token: str, client_id: str = "", client_secret: str = "") -> str:
    client_id = client_id or os.getenv("YOUTUBE_CLIENT_ID", "")
    client_secret = client_secret or os.getenv("YOUTUBE_CLIENT_SECRET", "")
    if not (refresh_token and client_id and client_secret):
        return ""
    try:
        r = requests.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=15)
        data = r.json()
        if data.get("access_token"):
            return data["access_token"]
        print(f"[YouTube] Token refresh failed: {data}")
    except Exception as e:
        print(f"[YouTube] Token refresh error: {e}")
    return ""


def fetch_channel_title(access_token: str) -> str:
    try:
        r = requests.get(f"{API_URL}/channels?part=snippet&mine=true",
                         headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        items = r.json().get("items", [])
        if items:
            return items[0]["snippet"]["title"]
    except Exception as e:
        print(f"[YouTube] Channel fetch error: {e}")
    return ""


def post_to_youtube(acc: dict, video_path: str, caption: str, hashtags: list,
                    title: str = "") -> dict:
    acc = acc or {}
    access_token = acc.get("access_token") or ""
    meta = acc.get("meta") or {}
    if not access_token:
        if acc.get("refresh_token"):
            access_token = refresh_access_token(acc["refresh_token"],
                                                meta.get("client_id", ""),
                                                meta.get("client_secret", ""))
        if not access_token:
            return {"success": False, "error": "YouTube not connected (token missing)"}

    video = Path(video_path)
    if not video.exists():
        return {"success": False, "error": "Video file missing"}

    tags = [h.lstrip("#") for h in (hashtags or []) if isinstance(hashtags, list)]
    video_title = (title or "Clip").strip()[:95]
    description = (caption or "")[:4900]
    body = {
        "snippet": {"title": video_title, "description": description,
                    "tags": tags, "categoryId": "22"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }

    try:
        init = requests.post(f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
                             headers={"Authorization": f"Bearer {access_token}",
                                      "Content-Type": "application/json; charset=UTF-8"},
                             json=body, timeout=30)
        if init.status_code == 401 and acc.get("refresh_token"):
            new_token = refresh_access_token(acc["refresh_token"],
                                             meta.get("client_id", ""),
                                             meta.get("client_secret", ""))
            if new_token:
                access_token = new_token
                init = requests.post(f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
                                     headers={"Authorization": f"Bearer {access_token}",
                                              "Content-Type": "application/json; charset=UTF-8"},
                                     json=body, timeout=30)
        if init.status_code not in (200, 201):
            return {"success": False, "error": f"YouTube init failed: {init.text[:300]}"}

        upload_url = init.headers.get("Location")
        if not upload_url:
            return {"success": False, "error": "YouTube did not return an upload URL"}

        with open(video, "rb") as f:
            size = video.stat().st_size
            upload = requests.put(upload_url,
                                  headers={"Authorization": f"Bearer {access_token}",
                                           "Content-Length": str(size),
                                           "Content-Type": "video/mp4"},
                                  data=f, timeout=900)
        if upload.status_code in (200, 201):
            video_id = upload.json().get("id", "")
            return {"success": True, "id": video_id,
                    "url": f"https://youtube.com/shorts/{video_id}" if video_id else ""}
        return {"success": False,
                "error": f"YouTube upload failed ({upload.status_code}): {upload.text[:300]}"}
    except Exception as e:
        return {"success": False, "error": str(e)[:250]}
