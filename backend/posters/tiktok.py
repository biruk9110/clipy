"""TikTok publishing via the official Content Posting API (OAuth).

Unaudited apps can only post privately (SELF_ONLY) - set env
TIKTOK_AUDITED=true once TikTok approves direct-post access to publish
publicly. Success is only reported when TikTok returns a publish id.
"""
import os

import requests
from pathlib import Path

API = "https://open.tiktokapis.com/v2"


def refresh_tiktok_token(refresh_token: str, meta: dict) -> str:
    client_key = (meta or {}).get("client_key") or os.getenv("TIKTOK_CLIENT_KEY", "")
    client_secret = (meta or {}).get("client_secret") or os.getenv("TIKTOK_CLIENT_SECRET", "")
    if not (refresh_token and client_key and client_secret):
        return ""
    try:
        r = requests.post(f"{API}/oauth/token/", headers={
            "Content-Type": "application/x-www-form-urlencoded",
        }, data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=20)
        data = r.json().get("data") or {}
        if data.get("access_token"):
            return data["access_token"]
    except Exception:
        pass
    return ""


def post_to_tiktok(acc: dict, video_path: str, caption: str, hashtags: list) -> dict:
    acc = acc or {}
    access_token = acc.get("access_token") or ""
    meta = acc.get("meta") or {}
    if not access_token:
        return {"success": False, "error": "TikTok not connected (OAuth token missing)"}

    video = Path(video_path)
    if not video.exists():
        return {"success": False, "error": "Video file missing"}
    size = video.stat().st_size
    if size > 64 * 1024 * 1024:
        return {"success": False, "error": "Video exceeds TikTok's 64MB chunk limit - render at 720p"}

    full_caption = (caption + " " + " ".join(hashtags or [])).strip()[:2200]
    audited = str(os.getenv("TIKTOK_AUDITED", "")).lower() in ("1", "true", "yes")
    privacy = "PUBLIC_TO_EVERYONE" if audited else "SELF_ONLY"

    try:
        init = requests.post(f"{API}/post/publish/video/init/", headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }, json={
            "post_info": {
                "title": full_caption,
                "privacy_level": privacy,
                "disable_duet": not audited,
                "disable_comment": False,
                "disable_stitch": not audited,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        }, timeout=30)
        res = init.json()
        data = res.get("data") or {}
        if res.get("error") and res["error"].get("code") not in ("ok", ""):
            code = res["error"].get("code")
            msg = res["error"].get("message", "")
            hint = (" (TikTok requires app approval for public direct posts - "
                    "posting privately to your account instead is available with an approved app)"
                    if code == "access_token_invalid" else "")
            if code == "invalid_params" and not audited:
                return {"success": False,
                        "error": "TikTok rejected direct posting for this app (needs audit). Set TIKTOK_AUDITED=true only after TikTok approves, or publish via Buffer."}
            return {"success": False, "error": f"TikTok init failed [{code}]: {msg}{hint}"}

        upload_url = data.get("upload_url")
        publish_id = data.get("publish_id")
        if not upload_url:
            return {"success": False, "error": "TikTok did not return an upload URL"}

        with open(video, "rb") as f:
            put = requests.put(upload_url, data=f, headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            }, timeout=600)
        if put.status_code not in (200, 201):
            return {"success": False,
                    "error": f"TikTok upload failed (HTTP {put.status_code}): {put.text[:200]}"}

        # Upload accepted -> TikTok processes asynchronously; a publish_id means queued
        if publish_id:
            note = "" if audited else " (private draft - visible only to you until TikTok approves the app)"
            return {"success": True, "post_id": publish_id,
                    "url": "https://www.tiktok.com/t/me" + note}
        return {"success": False, "error": "TikTok accepted upload but returned no publish id"}
    except Exception as e:
        return {"success": False, "error": str(e)[:250]}
