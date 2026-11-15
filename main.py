import os
import re
import uuid
import shutil
import hashlib
import secrets
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import BASE_DIR, STATIC_DIR, UPLOADS_DIR, OUTPUT_DIR, get_settings, save_settings
from backend.storage import (
    get_all_jobs, get_all_clips, get_clip, update_clip,
    create_job, get_job, delete_clip, delete_clips_for_job, delete_job,
    request_cancel, get_history, get_clips_per_day, get_connection_counts
)
from backend.scheduler import process_job_async, start_background_scheduler, publish_clip
from backend.oauth import (
    get_instagram_auth_url, handle_instagram_callback,
    get_tiktok_auth_url, handle_tiktok_callback,
    get_youtube_auth_url, handle_youtube_callback, get_base_url
)
from backend.maintenance import run_cleanup
from backend.posters.youtube import fetch_channel_title

app = FastAPI(title="Clipy - AI Stream & Short Video Clipper")

# Mount static and media folders
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(OUTPUT_DIR)), name="media")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ----------------- PIN security -----------------
_active_sessions = set()

SENSITIVE_KEYS = [
    "gemini_api_key", "buffer_access_token", "tiktok_session_id",
    "instagram_access_token", "tiktok_access_token", "instagram_password",
    "youtube_access_token", "youtube_refresh_token", "youtube_client_secret", "access_pin_hash",
]

def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "..." + value[-3:] if len(value) > 10 else "******"

def _is_masked(value) -> bool:
    # Masked values contain our "..." separator (real keys/tokens never contain "...").
    return isinstance(value, str) and "..." in value

class PinGuardMiddleware(BaseHTTPMiddleware):
    """When an access PIN is set, all API + media data requires an unlocked session."""
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/api") or path.startswith("/media"):
            if path in ("/api/auth/status", "/api/auth/unlock"):
                return await call_next(request)
            s = get_settings()
            if s.get("access_pin_hash") and request.cookies.get("clipy_auth") not in _active_sessions:
                return JSONResponse(status_code=401, content={"error": "Locked. Enter your PIN.", "locked": True})
        return await call_next(request)

app.add_middleware(PinGuardMiddleware)

@app.on_event("startup")
async def startup_event():
    start_background_scheduler()
    try:
        run_cleanup()
    except Exception as e:
        print(f"[Startup] Cleanup skipped: {e}")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        return templates.TemplateResponse(request=request, name="index.html")
    except TypeError:
        return templates.TemplateResponse("index.html", {"request": request})

@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

# ----------------- Auth (PIN lock) -----------------
@app.get("/api/auth/status")
async def api_auth_status():
    s = get_settings()
    return {"locked": bool(s.get("access_pin_hash"))}

@app.post("/api/auth/unlock")
async def api_auth_unlock(data: dict):
    s = get_settings()
    pin = (data.get("pin") or "").strip()
    if not s.get("access_pin_hash"):
        return {"success": True, "locked": False}
    if _hash_pin(pin) != s["access_pin_hash"]:
        return JSONResponse(status_code=403, content={"success": False, "error": "Wrong PIN"})
    token = secrets.token_hex(32)
    _active_sessions.add(token)
    resp = JSONResponse({"success": True, "locked": False})
    resp.set_cookie("clipy_auth", token, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return resp

@app.post("/api/auth/lock")
async def api_auth_lock():
    _active_sessions.clear()
    resp = JSONResponse({"success": True})
    resp.delete_cookie("clipy_auth")
    return resp

# ----------------- Settings -----------------
@app.get("/api/settings")
async def api_get_settings():
    s = get_settings()
    masked = {}
    for k, v in s.items():
        if k in SENSITIVE_KEYS:
            masked[k + "_masked"] = _mask(str(v)) if v else ""
        else:
            masked[k] = v
    masked["has_access_pin"] = bool(s.get("access_pin_hash"))
    masked["connections"] = {
        "buffer": bool(s.get("buffer_access_token")),
        "instagram": bool(s.get("instagram_access_token")),
        "tiktok": bool(s.get("tiktok_access_token") or s.get("tiktok_session_id")),
        "youtube": bool(s.get("youtube_access_token") or s.get("youtube_refresh_token")),
    }
    return masked

@app.post("/api/settings")
async def api_save_settings(data: dict):
    clean = {}
    for k, v in data.items():
        if k in SENSITIVE_KEYS:
            # Ignore values the client echoed back in masked form
            if v == "" or _is_masked(v):
                continue
        clean[k] = v
    # Plain PIN -> store hashed
    if "access_pin" in clean:
        pin = str(clean.pop("access_pin")).strip()
        clean["access_pin_hash"] = _hash_pin(pin) if pin else ""
    updated = save_settings(clean)
    return {"success": True, "settings": {k: v for k, v in updated.items() if k not in SENSITIVE_KEYS}}

@app.post("/api/settings/pin")
async def api_set_pin(data: dict):
    pin = str(data.get("pin") or "").strip()
    if pin and (len(pin) < 4 or not re.fullmatch(r"\d{4,8}", pin)):
        return JSONResponse(status_code=400, content={"success": False, "error": "PIN must be 4-8 digits"})
    save_settings({"access_pin_hash": _hash_pin(pin) if pin else ""})
    return {"success": True, "has_pin": bool(pin)}

# ----------------- Clips -----------------
@app.get("/api/clips")
async def api_get_clips():
    clips = get_all_clips()
    # Tell the UI which clips still have a playable file on disk
    for c in clips:
        p = c.get("file_path")
        c["file_exists"] = bool(p) and Path(p).exists()
    return {"clips": clips}

@app.patch("/api/clips/{clip_id}")
async def api_edit_clip(clip_id: str, data: dict):
    allowed = {k: v for k, v in data.items() if k in ("hook_text", "caption", "hashtags", "title")}
    if not allowed:
        return JSONResponse(status_code=400, content={"error": "Nothing to update"})
    update_clip(clip_id, **allowed)
    return {"success": True, "clip": get_clip(clip_id)}

@app.delete("/api/clips/{clip_id}")
async def api_delete_clip(clip_id: str):
    if not delete_clip(clip_id):
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    return {"success": True}

@app.post("/api/clips/{clip_id}/post")
async def api_post_clip(clip_id: str):
    publish_clip(clip_id)
    return {"success": True, "clip": get_clip(clip_id)}

@app.get("/api/clips/{clip_id}/video")
async def api_clip_video(clip_id: str):
    """Streams the clip's video for playback / 'Save to Device' with a friendly filename."""
    clip = get_clip(clip_id)
    if not clip:
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    p = Path(clip.get("file_path") or "")
    if not p.exists():
        matches = list(OUTPUT_DIR.glob(f"**/{clip_id}.mp4"))
        if matches:
            p = matches[0]
        else:
            return JSONResponse(status_code=404, content={"error": "Video file missing on server"})
    safe_name = re.sub(r"[^A-Za-z0-9 _-]", "", (clip.get("hook_text") or clip.get("title") or "clipy-clip"))[:40].strip() or "clipy-clip"
    return FileResponse(
        str(p),
        media_type="video/mp4",
        headers={"Content-Disposition": f'inline; filename="{safe_name}.mp4"'},
    )

# ----------------- Jobs -----------------
@app.get("/api/jobs")
async def api_get_jobs():
    jobs = get_all_jobs()
    return {"jobs": jobs}

@app.post("/api/jobs")
async def api_create_job(
    url: str = Form(None),
    file: UploadFile = File(None),
    num_clips: int = Form(3),
    interval_hours: float = Form(3.0)
):
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    
    if file and file.filename:
        # File upload from phone or PC
        filename = f"{job_id}_{file.filename}"
        file_path = UPLOADS_DIR / filename
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
            
        create_job(job_id, title=file.filename, source_type="upload", source_file=str(file_path),
                   num_clips=num_clips, interval_hours=interval_hours)
        process_job_async(job_id, source_type="upload", file_path=str(file_path), num_clips=num_clips, interval_hours=interval_hours)
        return {"success": True, "job_id": job_id, "message": "Uploaded and processing started!"}
        
    elif url:
        # YouTube or TikTok URL
        create_job(job_id, title=url, source_type="url", source_url=url,
                   num_clips=num_clips, interval_hours=interval_hours)
        process_job_async(job_id, source_type="url", source_url=url, num_clips=num_clips, interval_hours=interval_hours)
        return {"success": True, "job_id": job_id, "message": "Link received and processing started!"}
        
    return JSONResponse(status_code=400, content={"error": "Provide either a URL or upload a video file."})

@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] not in ("processing", "pending"):
        return JSONResponse(status_code=400, content={"error": "Job is not running"})
    request_cancel(job_id)
    if job["status"] == "pending":
        delete_clips_for_job(job_id)
        update_job(job_id, status="cancelled", step="🚫 Cancelled by user", progress=0, error="Cancelled by user")
    return {"success": True}

@app.post("/api/jobs/{job_id}/retry")
async def api_retry_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] in ("processing", "pending"):
        return JSONResponse(status_code=400, content={"error": "Job is still running"})
    if job.get("source_type") == "upload":
        src = job.get("source_file")
        if not src or not Path(src).exists():
            return JSONResponse(status_code=400, content={"error": "The uploaded source file was deleted - upload the video again."})
    delete_clips_for_job(job_id)
    s = get_settings()
    update_job(job_id, status="pending", step="🔄 Queued for retry...", progress=5,
               error=None, cancel_requested=0, completed_at=None)
    process_job_async(
        job_id,
        source_type=job.get("source_type", "url"),
        source_url=job.get("source_url", ""),
        file_path=job.get("source_file", ""),
        num_clips=int(job.get("num_clips") or s.get("default_clips_count", 3)),
        interval_hours=float(job.get("interval_hours") or s.get("schedule_interval_hours", 3.0)),
    )
    return {"success": True}

@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] in ("processing", "pending"):
        return JSONResponse(status_code=400, content={"error": "Cancel the job before deleting it"})
    delete_job(job_id)
    return {"success": True}

@app.get("/api/history")
async def api_history():
    return {"history": get_history()}

# ----------------- Analytics -----------------
@app.get("/api/analytics")
async def api_analytics():
    clips = get_all_clips()
    total_clips = len(clips)
    posted = sum(1 for c in clips if c["status"] == "posted")
    scheduled = sum(1 for c in clips if c["status"] == "scheduled")
    ready = sum(1 for c in clips if c["status"] == "ready")
    failed = sum(1 for c in clips if c["status"] == "failed")
    scores = [c.get("score") or 0 for c in clips if c.get("score")]
    avg_score = round(sum(scores) / len(scores)) if scores else 0
    denom = posted + failed
    success_rate = round(posted / denom * 100) if denom else None
    connections = get_settings()
    return {
        "total_clips": total_clips,
        "posted": posted,
        "scheduled": scheduled,
        "ready": ready,
        "failed": failed,
        "avg_score": avg_score,
        "success_rate": success_rate,
        "per_day": get_clips_per_day(7),
        "platform_posts": get_connection_counts(),
        "connections": {
            "buffer": bool(connections.get("buffer_access_token")),
            "instagram": bool(connections.get("instagram_access_token")),
            "tiktok": bool(connections.get("tiktok_access_token") or connections.get("tiktok_session_id")),
            "youtube": bool(connections.get("youtube_access_token") or connections.get("youtube_refresh_token")),
        },
    }

@app.post("/api/cleanup")
async def api_cleanup():
    removed = run_cleanup()
    return {"success": True, "removed": removed}

@app.get("/api/export/csv")
async def api_export_csv():
    """Exports all clips (captions, hashtags, status) as CSV - a backup for ephemeral hosts."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "status", "hook_text", "caption", "hashtags", "score", "scheduled_time", "source"])
    for c in get_all_clips():
        tags = " ".join(c["hashtags"]) if isinstance(c["hashtags"], list) else (c["hashtags"] or "")
        writer.writerow([
            c["id"], c.get("created_at", ""), c.get("status", ""),
            c.get("hook_text", ""), c.get("caption", ""), tags,
            c.get("score", ""), c.get("scheduled_time", ""),
            c.get("job_title", "") or c.get("title", ""),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=clipy_clips_export.csv"},
    )

# ----------------- Social OAuth -----------------
@app.get("/auth/instagram/login")
async def auth_instagram_login():
    url = get_instagram_auth_url()
    if not url:
        return RedirectResponse("/?tab=settings&modal=instagram")
    return RedirectResponse(url)

@app.get("/auth/instagram/callback")
async def auth_instagram_callback(code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?tab=settings&error=instagram_auth_failed")
    res = handle_instagram_callback(code)
    if res.get("success"):
        return RedirectResponse("/?tab=settings&connected=instagram")
    return RedirectResponse("/?tab=settings&error=instagram_token_failed")

@app.get("/auth/tiktok/login")
async def auth_tiktok_login():
    url = get_tiktok_auth_url()
    if not url:
        return RedirectResponse("/?tab=settings&modal=tiktok")
    return RedirectResponse(url)

@app.get("/auth/tiktok/callback")
async def auth_tiktok_callback(code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?tab=settings&error=tiktok_auth_failed")
    res = handle_tiktok_callback(code)
    if res.get("success"):
        return RedirectResponse("/?tab=settings&connected=tiktok")
    return RedirectResponse("/?tab=settings&error=tiktok_token_failed")

@app.get("/auth/youtube/login")
async def auth_youtube_login():
    url = get_youtube_auth_url()
    if not url:
        return RedirectResponse("/?tab=settings&modal=youtube")
    return RedirectResponse(url)

@app.get("/auth/youtube/callback")
async def auth_youtube_callback(code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?tab=settings&error=youtube_auth_failed")
    res = handle_youtube_callback(code)
    if res.get("success"):
        return RedirectResponse("/?tab=settings&connected=youtube")
    return RedirectResponse("/?tab=settings&error=youtube_token_failed")

@app.post("/auth/disconnect/{platform}")
async def auth_disconnect(platform: str):
    if platform == "instagram":
        save_settings({
            "instagram_access_token": "",
            "instagram_user_id": "",
            "instagram_username": "",
            "auto_post_instagram": False
        })
    elif platform == "tiktok":
        save_settings({
            "tiktok_access_token": "",
            "tiktok_open_id": "",
            "tiktok_username": "",
            "auto_post_tiktok": False
        })
    elif platform == "buffer":
        save_settings({
            "buffer_access_token": ""
        })
    elif platform == "youtube":
        save_settings({
            "youtube_access_token": "",
            "youtube_refresh_token": "",
            "youtube_channel_title": "",
            "auto_post_youtube": False
        })
    return {"success": True, "settings": get_settings()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
