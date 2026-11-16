"""Instagram Reels publishing via the official Graph API.

Flow: create a REELS media container with a public video URL, wait for the
container to finish processing, then publish it. No simulated success.
"""
import os
import time

import requests
from pathlib import Path

GRAPH = "https://graph.facebook.com/v21.0"
CONTAINER_TIMEOUT = 300


def refresh_long_lived_token(access_token: str) -> str:
    """Instagram long-lived tokens can be refreshed when >24h old."""
    if not access_token:
        return ""
    try:
        r = requests.get(f"{GRAPH}/refresh_access_token",
                         params={"grant_type": "ig_refresh_token",
                                 "access_token": access_token},
                         timeout=15)
        data = r.json()
        if data.get("access_token"):
            return data["access_token"]
    except Exception:
        pass
    return ""


def _container_status(container_id: str, access_token: str) -> str:
    try:
        r = requests.get(f"{GRAPH}/{container_id}",
                         params={"fields": "status_code",
                                 "access_token": access_token},
                         timeout=15)
        return r.json().get("status_code", "")
    except Exception:
        return ""


def post_to_instagram(acc: dict, video_url: str, video_path: str, caption: str,
                      hashtags: list) -> dict:
    """acc: connected account dict with access_token + meta.ig_user_id"""
    access_token = (acc or {}).get("access_token") or ""
    ig_user_id = ((acc or {}).get("meta") or {}).get("ig_user_id") or (acc or {}).get("account_id") or ""
    if not access_token or not ig_user_id:
        return {"success": False, "error": "Instagram not connected (token or user id missing)"}
    if not video_url or video_url.startswith("file://"):
        return {"success": False,
                "error": "Instagram publishing needs a public video URL (deployed host required)"}

    full_caption = caption + "\n\n" + " ".join(hashtags or [])
    try:
        init = requests.post(f"{GRAPH}/{ig_user_id}/media", data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": full_caption[:2200],
            "share_to_feed": "true",
            "access_token": access_token,
        }, timeout=30)
        res = init.json()
        container_id = res.get("id")
        if not container_id:
            err = res.get("error", {}).get("message") or str(res)[:250]
            return {"success": False, "error": f"Container init failed: {err}"}

        # Wait for the container to finish processing (status: FINISHED)
        waited = 0
        status = ""
        while waited < CONTAINER_TIMEOUT:
            status = _container_status(container_id, access_token)
            if status in ("FINISHED", "ERROR"):
                break
            time.sleep(5)
            waited += 5
        if status != "FINISHED":
            return {"success": False,
                    "error": f"Instagram container processing did not finish (status: {status or 'unknown'})"}

        publish = requests.post(f"{GRAPH}/{ig_user_id}/media_publish", data={
            "creation_id": container_id,
            "access_token": access_token,
        }, timeout=30)
        pub = publish.json()
        post_id = pub.get("id")
        if not post_id:
            err = pub.get("error", {}).get("message") or str(pub)[:250]
            return {"success": False, "error": f"Publish failed: {err}"}
        return {"success": True, "post_id": post_id,
                "url": f"https://instagram.com/reel/{post_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)[:250]}
