import os
import uuid
import shutil
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.config import BASE_DIR, STATIC_DIR, UPLOADS_DIR, OUTPUT_DIR, get_settings, save_settings
from backend.storage import (
    get_all_jobs, get_all_clips, get_clip, update_clip, 
    create_job, get_job
)
from backend.scheduler import process_job_async, start_background_scheduler, publish_clip
from backend.oauth import (
    get_instagram_auth_url, handle_instagram_callback,
    get_tiktok_auth_url, handle_tiktok_callback, get_base_url
)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse

app = FastAPI(title="Clipy - AI Stream & Short Video Clipper")

# Mount static and media folders
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(OUTPUT_DIR)), name="media")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Start background timer for scheduled uploads on startup
@app.on_event("startup")
async def startup_event():
    start_background_scheduler()

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

@app.get("/api/settings")
async def api_get_settings():
    s = get_settings()
    # Mask API key partially for security in UI
    masked = s.copy()
    if masked.get("gemini_api_key"):
        k = masked["gemini_api_key"]
        masked["gemini_api_key_masked"] = k[:6] + "..." + k[-4:] if len(k) > 10 else "******"
    if masked.get("buffer_access_token"):
        b = masked["buffer_access_token"]
        masked["buffer_access_token_masked"] = b[:4] + "..." + b[-3:] if len(b) > 8 else "******"
    return masked

@app.post("/api/settings")
async def api_save_settings(data: dict):
    updated = save_settings(data)
    return {"success": True, "settings": updated}

@app.get("/api/clips")
async def api_get_clips():
    clips = get_all_clips()
    return {"clips": clips}

@app.post("/api/clips/{clip_id}/post")
async def api_post_clip(clip_id: str):
    publish_clip(clip_id)
    return {"success": True, "clip": get_clip(clip_id)}

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
            
        create_job(job_id, title=file.filename, source_type="upload", source_file=str(file_path))
        process_job_async(job_id, source_type="upload", file_path=str(file_path), num_clips=num_clips, interval_hours=interval_hours)
        return {"success": True, "job_id": job_id, "message": "Uploaded and processing started!"}
        
    elif url:
        # YouTube or TikTok URL
        create_job(job_id, title=url, source_type="url", source_url=url)
        process_job_async(job_id, source_type="url", source_url=url, num_clips=num_clips, interval_hours=interval_hours)
        return {"success": True, "job_id": job_id, "message": "Link received and processing started!"}
        
    return JSONResponse(status_code=400, content={"error": "Provide either a URL or upload a video file."})

@app.get("/api/analytics")
async def api_analytics():
    clips = get_all_clips()
    total_clips = len(clips)
    posted_clips = sum(1 for c in clips if c["status"] == "posted")
    scheduled_clips = sum(1 for c in clips if c["status"] == "scheduled")
    total_views = sum(c.get("views", 0) for c in clips)
    total_likes = sum(c.get("likes", 0) for c in clips)
    
    return {
        "total_clips": total_clips,
        "posted": posted_clips,
        "scheduled": scheduled_clips,
        "total_views": total_views,
        "total_likes": total_likes
    }

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
    return {"success": True, "settings": get_settings()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
