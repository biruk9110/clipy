"""Clipy - AI video clipping platform (FastAPI backend + Jinja UI)."""
import csv
import io
import os
import re
import secrets
import shutil
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from backend import maintenance
from backend.accounts import (account_status_summary, get_account, refresh_account_token,
                              remove_account, upsert_account)
from backend.auth import (COOKIE_NAME, authenticate, change_password, consume_reset_token,
                          create_reset_token, create_session, destroy_session, get_user,
                          normalize_email, register, session_user)
from backend.config import (BASE_DIR, DATA_DIR, HOST_SETTINGS, OUTPUT_DIR, STATIC_DIR,
                            TEMPLATES_DIR, UPLOADS_DIR)
from backend.crypto import is_masked
from backend.database import db, init_db
from backend.filestore import delete_file, file_exists, open_file, save_from_stream
from backend.oauth import (consume_state, get_base_url, get_instagram_auth_url,
                           get_tiktok_auth_url, get_youtube_auth_url,
                           handle_instagram_callback, handle_tiktok_callback,
                           handle_youtube_callback)
from backend.scheduler import publish_clip, start_background_scheduler
from backend.storage import (add_clip, analytics_summary, count_clips, create_job,
                             create_project, delete_clip, delete_job, delete_project,
                             get_clip, get_history, get_job, get_project, list_clips,
                             list_jobs, list_projects, list_scheduled, request_cancel,
                             schedule_post, update_clip, update_job, update_project,
                             update_scheduled, get_clips_per_day, get_connection_counts)
from backend.usersettings import get_prefs, get_secret, remove_secret, save_prefs, secret_status, set_secret
from backend.usersettings import DEFAULT_PREFS

app = FastAPI(title="Clipy - AI Video Editing Platform")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(OUTPUT_DIR)), name="media")

MAX_UPLOAD = int(HOST_SETTINGS.get("max_upload_mb", 500)) * 1024 * 1024
ALLOWED_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpg", ".mpeg"}


# ---------------------------------------------------------------------------
# Rate limiting (per-IP, in-memory, for auth + AI endpoints)
# ---------------------------------------------------------------------------
_rate_buckets: dict[str, deque] = defaultdict(lambda: deque(maxlen=64))


def rate_ok(key: str, limit: int, window_sec: int = 60) -> bool:
    now = time.time()
    bucket = _rate_buckets[key]
    while bucket and now - bucket[0] > window_sec:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else request.client.host) if request.client else "local"


# ---------------------------------------------------------------------------
# Session middleware (persistent DB sessions)
# ---------------------------------------------------------------------------
PUBLIC_API = {"/api/auth/register", "/api/auth/login", "/api/auth/forgot", "/api/auth/reset",
              "/api/auth/me", "/api/health"}
PUBLIC_PAGES = {"/", "/login", "/register", "/forgot", "/reset", "/welcome"}
AUTH_PAGES = {"/login", "/register", "/forgot", "/reset"}


def _secure_cookie(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or proto == "https"


class SessionGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        token = request.cookies.get(COOKIE_NAME)
        user = session_user(token)
        request.state.user = user
        request.state.session_token = token

        if path.startswith("/api"):
            if path in PUBLIC_API:
                return await call_next(request)
            if not user:
                return JSONResponse(status_code=401,
                                    content={"error": "Not signed in", "auth": True})
            # Light rate limiting for expensive/abusable endpoints
            if path.startswith("/api/projects") and request.method == "POST":
                if not rate_ok(f"jobs:{user['id']}", 10, 60):
                    return JSONResponse(status_code=429, content={"error": "Too many requests - wait a minute."})
            return await call_next(request)

        if path.startswith(("/static", "/media", "/health", "/manifest.json", "/sw.js",
                            "/favicon.ico", "/auth/", "/icons")):
            return await call_next(request)

        if path in PUBLIC_PAGES:
            if user and path in AUTH_PAGES:
                return RedirectResponse("/", status_code=302)
            return await call_next(request)

        if not user:
            return RedirectResponse(f"/login?next={path}", status_code=302)
        if path == "/onboarding" and user.get("onboarding_done"):
            return RedirectResponse("/", status_code=302)
        return await call_next(request)


app.add_middleware(SessionGuardMiddleware)


def require_user(request: Request) -> dict:
    user = request.state.user
    if not user:
        raise PermissionError("Not signed in")
    return user


@app.on_event("startup")
async def startup_event():
    init_db()
    start_background_scheduler()


@app.get("/health")
async def health():
    try:
        db.one("SELECT 1")
        db_ok = True
    except Exception as e:
        db_ok = False
    from backend.database import IS_PG
    return {"ok": True, "database": "postgres" if IS_PG else "sqlite", "db_ok": db_ok}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _page(request: Request, name: str, **ctx):
    base = {"user": request.state.user, "HOST_SETTINGS": HOST_SETTINGS}
    base.update(ctx)
    return templates.TemplateResponse(request=request, name=name, context=base)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not request.state.user:
        return _page(request, "welcome.html")
    return _page(request, "index.html", user=request.state.user)


@app.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request):
    if request.state.user:
        return RedirectResponse("/", status_code=302)
    return _page(request, "welcome.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _page(request, "auth.html", mode="login", next=request.query_params.get("next", ""))


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _page(request, "auth.html", mode="register")


@app.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    return _page(request, "auth.html", mode="forgot")


@app.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request):
    return _page(request, "auth.html", mode="reset", token=request.query_params.get("token", ""))


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    return _page(request, "onboarding.html", user=request.state.user)


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------
def _set_session(response: Response, request: Request, token: str):
    response.set_cookie(COOKIE_NAME, token, max_age=60 * 60 * 24 * 30, httponly=True,
                        samesite="lax", secure=_secure_cookie(request), path="/")


@app.post("/api/auth/register")
async def api_register(request: Request, data: dict):
    if not rate_ok(f"reg:{client_ip(request)}", 5, 300):
        return JSONResponse(status_code=429, content={"error": "Too many attempts - try later."})
    res = register(data.get("name", ""), data.get("email", ""), data.get("password", ""))
    if not res.get("success"):
        return JSONResponse(status_code=400, content=res)
    token = create_session(res["user_id"], request.headers.get("user-agent", ""))
    resp = JSONResponse({"success": True, "redirect": "/onboarding"})
    _set_session(resp, request, token)
    return resp


@app.post("/api/auth/login")
async def api_login(request: Request, data: dict):
    if not rate_ok(f"login:{client_ip(request)}", 10, 60):
        return JSONResponse(status_code=429, content={"error": "Too many attempts - try again in a minute."})
    res = authenticate(data.get("email", ""), data.get("password", ""))
    if not res.get("success"):
        return JSONResponse(status_code=401, content=res)
    user = res["user"]
    token = create_session(user["id"], request.headers.get("user-agent", ""))
    nxt = data.get("next") or "/"
    if not nxt.startswith("/"):
        nxt = "/"
    dest = "/onboarding" if not user.get("onboarding_done") else nxt
    resp = JSONResponse({"success": True, "redirect": dest, "user": get_user(user["id"])})
    _set_session(resp, request, token)
    return resp


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    destroy_session(request.state.session_token)
    resp = JSONResponse({"success": True, "redirect": "/login"})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/me")
async def api_me(request: Request):
    user = request.state.user
    if not user:
        return {"user": None}
    return {"user": user, "onboarding_done": bool(user.get("onboarding_done"))}


@app.post("/api/auth/forgot")
async def api_forgot(request: Request, data: dict):
    if not rate_ok(f"forgot:{client_ip(request)}", 3, 600):
        return JSONResponse(status_code=429, content={"error": "Too many reset requests - try later."})
    user_id, raw = create_reset_token(normalize_email(data.get("email", "")))
    # Never reveal whether the account exists.
    if not raw:
        return {"success": True, "message": "If that account exists, a reset link was generated."}
    reset_url = f"{get_base_url()}/reset?token={raw}"
    # Self-hosted deployments have no mail server: show the link to the requester.
    return {"success": True, "reset_url": reset_url,
            "message": "Reset link generated below (self-hosted mode: no email server configured)."}


@app.post("/api/auth/reset")
async def api_reset(data: dict):
    res = consume_reset_token(data.get("token", ""), data.get("password", ""))
    if not res.get("success"):
        return JSONResponse(status_code=400, content=res)
    return res


@app.post("/api/auth/change-password")
async def api_change_password(request: Request, data: dict):
    user = require_user(request)
    res = change_password(user["id"], data.get("current", ""), data.get("new", ""))
    if not res.get("success"):
        return JSONResponse(status_code=400, content=res)
    return res


@app.post("/api/onboarding/complete")
async def api_onboarding_complete(request: Request):
    user = require_user(request)
    save_prefs(user["id"], {"onboarding_done": True})
    db.execute("UPDATE users SET onboarding_done = 1 WHERE id = ?", (user["id"],))
    return {"success": True, "redirect": "/"}


# ---------------------------------------------------------------------------
# Settings API (per-user, secrets encrypted)
# ---------------------------------------------------------------------------
@app.get("/api/settings")
async def api_get_settings(request: Request):
    user = require_user(request)
    prefs = get_prefs(user["id"])
    return {"prefs": prefs, "secrets": secret_status(user["id"]),
            "accounts": account_status_summary(user["id"]),
            "max_upload_mb": HOST_SETTINGS.get("max_upload_mb", 500)}


@app.post("/api/settings")
async def api_save_settings(request: Request, data: dict):
    user = require_user(request)
    prefs = save_prefs(user["id"], data.get("prefs", {}))
    return {"success": True, "prefs": prefs}


@app.post("/api/settings/gemini")
async def api_set_gemini(request: Request, data: dict):
    user = require_user(request)
    key = (data.get("api_key") or "").strip()
    if not key or len(key) < 20:
        return JSONResponse(status_code=400, content={"error": "That does not look like a Gemini API key."})
    if is_masked(key):
        return JSONResponse(status_code=400, content={"error": "Enter a new key (the masked value is not editable)."})
    set_secret(user["id"], "gemini_api_key", key)
    from backend.gemini_director import test_connection
    test = test_connection(user["id"])
    return {"success": True, "test": test, "secrets": secret_status(user["id"])}


@app.delete("/api/settings/gemini")
async def api_remove_gemini(request: Request):
    user = require_user(request)
    remove_secret(user["id"], "gemini_api_key")
    return {"success": True, "secrets": secret_status(user["id"])}


@app.post("/api/settings/gemini/test")
async def api_test_gemini(request: Request):
    user = require_user(request)
    from backend.gemini_director import test_connection
    return test_connection(user["id"])


# ---------------------------------------------------------------------------
# Projects (upload + reference)
# ---------------------------------------------------------------------------
def _safe_filename(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        ext = ".mp4"
    return f"{uuid.uuid4().hex[:12]}{ext}"


@app.get("/api/projects")
async def api_list_projects(request: Request, search: str = "", status: str = "",
                            limit: int = 30, offset: int = 0):
    user = require_user(request)
    items = list_projects(user["id"], limit=min(100, limit), offset=max(0, offset),
                          search=search[:80], status=status[:20])
    for p in items:
        p["source_available"] = bool(p.get("source_file")) and file_exists(p["source_file"], user["id"])
        p["reference_available"] = bool(p.get("reference_file")) and file_exists(p["reference_file"], user["id"])
        try:
            p["reference_profile_obj"] = ( __import__("json").loads(p["reference_profile"])
                                           if p.get("reference_profile") else None)
        except Exception:
            p["reference_profile_obj"] = None
    return {"projects": items, "total": len(items)}


@app.post("/api/projects")
async def api_create_project(
    request: Request,
    file: UploadFile = File(None),
    source_url: str = Form(""),
    name: str = Form(""),
    reference_url: str = Form(""),
    reference_file: UploadFile = File(None),
):
    user = require_user(request)
    if not rate_ok(f"proj:{user['id']}", 8, 60):
        return JSONResponse(status_code=429, content={"error": "Too many uploads - wait a minute."})

    src_type, src_path, src_size = "upload", "", 0
    if file and file.filename:
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return JSONResponse(status_code=400,
                                content={"error": f"Unsupported video format '{ext}'. Use MP4, MOV, WEBM or MKV."})
        fname = _safe_filename(file.filename)
        dest = UPLOADS_DIR / fname
        res = save_from_stream(dest, file.file, user_id=user["id"],
                               mime=file.content_type or "video/mp4", max_bytes=MAX_UPLOAD)
        if res["truncated"]:
            delete_file(dest)
            return JSONResponse(status_code=413, content={
                "error": f"File is larger than the {HOST_SETTINGS.get('max_upload_mb', 500)} MB limit."})
        src_type, src_path, src_size = "upload", str(dest), res["size"]
    elif source_url.strip():
        src_type = "url"
        if not re.match(r"^https?://", source_url.strip(), re.I):
            return JSONResponse(status_code=400, content={"error": "Enter a valid http(s) video URL."})
    else:
        return JSONResponse(status_code=400,
                            content={"error": "Upload a video file or paste a video link."})

    from backend.video_processor import probe_video
    duration, width, height = 0, 0, 0
    if src_path and Path(src_path).exists():
        info = probe_video(Path(src_path))
        duration, width, height = info["duration"], info["width"], info["height"]
        if duration <= 0:
            duration = 0

    project_name = (name or "").strip()
    if not project_name:
        project_name = (file.filename if file and file.filename
                        else (source_url[:60] if source_url else "Untitled project"))
    project_id = create_project(
        user["id"], project_name, source_type=src_type,
        source_url=source_url.strip() if src_type == "url" else "",
        source_file=src_path, source_duration=duration, source_width=width,
        source_height=height, source_size=src_size,
        source_mime=(file.content_type or "video/mp4") if file else "",
        reference_url=(reference_url or "").strip())

    # Reference handling: queue an analysis job (survives page navigation)
    ref_job_id = None
    if reference_url.strip() or (reference_file and reference_file.filename):
        ref_job_id = _start_reference_analysis(user, project_id, reference_url.strip(),
                                               reference_file)
    return {"success": True, "project_id": project_id, "reference_job_id": ref_job_id,
            "duration": duration}


def _start_reference_analysis(user: dict, project_id: str, ref_url: str,
                              ref_file: UploadFile | None) -> str | None:
    """Queues a reference-analysis job (kind='reference')."""
    ref_path = ""
    if ref_file and ref_file.filename:
        ext = Path(ref_file.filename).suffix.lower()
        if ext in ALLOWED_EXT:
            fname = _safe_filename("ref" + ext)
            dest = UPLOADS_DIR / fname
            try:
                res = save_from_stream(dest, ref_file.file, user_id=user["id"],
                                       mime=ref_file.content_type or "video/mp4",
                                       max_bytes=MAX_UPLOAD)
                ref_path = str(dest) if not res["truncated"] else ""
            except Exception:
                ref_path = ""
    job_id = create_job(user["id"], project_id, kind="reference")
    update_project(project_id, reference_url=ref_url,
                   reference_file=ref_path,
                   reference_platform=_platform_of(ref_url))
    return job_id


def _platform_of(url: str) -> str:
    from backend.reference import detect_platform
    return detect_platform(url or "")


@app.get("/api/projects/{project_id}")
async def api_get_project(request: Request, project_id: str):
    user = require_user(request)
    project = get_project(project_id, user["id"])
    if not project:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    import json as _json
    if project.get("reference_profile"):
        try:
            project["reference_profile_obj"] = _json.loads(project["reference_profile"])
        except Exception:
            project["reference_profile_obj"] = None
    project["source_available"] = bool(project.get("source_file")) and file_exists(project["source_file"], user["id"])
    project["reference_available"] = bool(project.get("reference_file")) and file_exists(project["reference_file"], user["id"])
    jobs = [j for j in list_jobs(user["id"], limit=10) if j.get("project_id") == project_id]
    project["jobs"] = jobs
    clips = [c for c in list_clips(user["id"], limit=100) if c.get("project_id") == project_id]
    for c in clips:
        c["file_available"] = bool(c.get("file_path")) and file_exists(c["file_path"], user["id"])
    project["clips"] = clips
    return {"project": project}


@app.post("/api/projects/{project_id}/reference")
async def api_project_reference(request: Request, project_id: str,
                                file: UploadFile = File(None),
                                reference_url: str = Form("")):
    user = require_user(request)
    project = get_project(project_id, user["id"])
    if not project:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    job_id = _start_reference_analysis(user, project_id, (reference_url or "").strip(), file)
    return {"success": True, "reference_job_id": job_id}


@app.delete("/api/projects/{project_id}")
async def api_delete_project(request: Request, project_id: str):
    user = require_user(request)
    project = get_project(project_id, user["id"])
    if not project:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    job = get_job(project.get("last_job_id") or "", user["id"]) if project.get("last_job_id") else None
    if job and job["status"] in ("queued", "processing"):
        return JSONResponse(status_code=400, content={"error": "Cancel the running job first."})
    delete_project(project_id, user["id"])
    return {"success": True}


# ---------------------------------------------------------------------------
# Editing jobs
# ---------------------------------------------------------------------------
@app.post("/api/projects/{project_id}/edit")
async def api_start_edit(request: Request, project_id: str,
                         num_clips: int = Form(3), clip_duration: int = Form(45),
                         aspect_ratio: str = Form("9:16"), resolution: str = Form("720"),
                         caption_style: str = Form("karaoke"),
                         editing_intensity: str = Form("balanced"),
                         trim_silence: bool = Form(True)):
    user = require_user(request)
    project = get_project(project_id, user["id"])
    if not project:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    prefs = get_prefs(user["id"])
    if not (project.get("source_file") or project.get("source_url")):
        return JSONResponse(status_code=400, content={"error": "This project has no source video."})
    if aspect_ratio not in ("9:16", "16:9", "1:1", "4:5"):
        aspect_ratio = "9:16"
    if str(resolution) not in ("720", "1080"):
        resolution = "720"
    job_id = create_job(
        user["id"], project_id, num_clips=max(1, min(10, num_clips or prefs.get("default_num_clips", 3))),
        clip_duration=max(15, min(90, clip_duration or prefs.get("default_clip_duration", 45))),
        aspect_ratio=aspect_ratio, resolution=resolution,
        caption_style=caption_style or "karaoke",
        editing_intensity=editing_intensity or "balanced",
        trim_silence=bool(trim_silence),
        interval_hours=float(prefs.get("schedule_interval_hours", 3)))
    update_project(project_id, last_job_id=job_id, status="queued", error=None)
    return {"success": True, "job_id": job_id}


@app.get("/api/jobs")
async def api_list_jobs(request: Request, limit: int = 30):
    user = require_user(request)
    jobs = list_jobs(user["id"], limit=min(100, limit))
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
async def api_get_job_status(request: Request, job_id: str):
    user = require_user(request)
    job = get_job(job_id, user["id"])
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return {"job": job}


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(request: Request, job_id: str):
    user = require_user(request)
    job = get_job(job_id, user["id"])
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] not in ("queued", "processing"):
        return JSONResponse(status_code=400, content={"error": "Job is not running"})
    request_cancel(job_id)
    if job["status"] == "queued":
        update_job(job_id, status="cancelled", step="Cancelled by user", progress=0,
                   error="Cancelled by user")
        if job.get("project_id"):
            update_project(job["project_id"], status="cancelled", error="Cancelled by user")
    return {"success": True}


@app.post("/api/jobs/{job_id}/retry")
async def api_retry_job(request: Request, job_id: str):
    user = require_user(request)
    job = get_job(job_id, user["id"])
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] in ("queued", "processing"):
        return JSONResponse(status_code=400, content={"error": "Job is still running"})
    project = get_project(job.get("project_id") or "", user["id"])
    if not project:
        return JSONResponse(status_code=400, content={"error": "Project no longer exists"})
    if project.get("source_type") == "upload":
        src = project.get("source_file")
        if not src or not file_exists(src, user["id"]):
            return JSONResponse(status_code=400, content={
                "error": "The uploaded source file is gone - open the project and re-upload the video."})
    db.execute("DELETE FROM clips WHERE job_id = ?", (job_id,))
    update_job(job_id, status="queued", step="Queued for retry", progress=2, error=None,
               cancel_requested=0, attempts=int(job.get("attempts") or 0) + 1,
               completed_at=None, started_at=None)
    update_project(project["id"], status="queued", error=None)
    return {"success": True}


@app.delete("/api/jobs/{job_id}")
async def api_delete_job(request: Request, job_id: str):
    user = require_user(request)
    job = get_job(job_id, user["id"])
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] in ("queued", "processing"):
        return JSONResponse(status_code=400, content={"error": "Cancel the job before deleting it"})
    delete_job(job_id, user["id"])
    return {"success": True}


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------
@app.get("/api/clips")
async def api_list_clips(request: Request, limit: int = 100, status: str = "", search: str = ""):
    user = require_user(request)
    clips = list_clips(user["id"], limit=min(300, limit), status=status[:20], search=search[:80])
    for c in clips:
        c["file_available"] = bool(c.get("file_path")) and file_exists(c["file_path"], user["id"])
    return {"clips": clips}


@app.patch("/api/clips/{clip_id}")
async def api_edit_clip(request: Request, clip_id: str, data: dict):
    user = require_user(request)
    if not get_clip(clip_id, user["id"]):
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    allowed = {k: v for k, v in data.items() if k in ("hook_text", "caption", "hashtags", "title")}
    if not allowed:
        return JSONResponse(status_code=400, content={"error": "Nothing to update"})
    update_clip(clip_id, **allowed)
    return {"success": True, "clip": get_clip(clip_id, user["id"])}


@app.delete("/api/clips/{clip_id}")
async def api_delete_clip(request: Request, clip_id: str):
    user = require_user(request)
    if not delete_clip(clip_id, user["id"]):
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    return {"success": True}


@app.get("/api/clips/{clip_id}/video")
async def api_clip_video(request: Request, clip_id: str, download: int = 0):
    user = require_user(request)
    clip = get_clip(clip_id, user["id"])
    if not clip:
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    p = open_file(clip.get("file_path") or "", user["id"])
    if not p:
        update_clip(clip_id, file_missing=1)
        return JSONResponse(status_code=404, content={
            "error": "Video file missing on server. Use 'Re-render' or re-upload the source.",
            "missing": True})
    safe_name = re.sub(r"[^A-Za-z0-9 _-]", "",
                       (clip.get("hook_text") or clip.get("title") or "clipy-clip"))[:40].strip() or "clipy-clip"
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}.mp4"'
    else:
        headers["Content-Disposition"] = f'inline; filename="{safe_name}.mp4"'
    return FileResponse(str(p), media_type="video/mp4", headers=headers)


@app.get("/api/clips/{clip_id}/thumb")
async def api_clip_thumb(request: Request, clip_id: str):
    user = require_user(request)
    clip = get_clip(clip_id, user["id"])
    if not clip or not clip.get("thumb_path"):
        return JSONResponse(status_code=404, content={"error": "No thumbnail"})
    p = open_file(clip["thumb_path"], user["id"])
    if not p:
        return JSONResponse(status_code=404, content={"error": "No thumbnail"})
    return FileResponse(str(p), media_type="image/jpeg")


@app.post("/api/clips/{clip_id}/post")
async def api_post_clip(request: Request, clip_id: str):
    user = require_user(request)
    if not get_clip(clip_id, user["id"]):
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    res = publish_clip(clip_id, user["id"], platform="all", manual=True)
    return {"success": bool(res.get("ok")), **res, "clip": get_clip(clip_id, user["id"])}


@app.post("/api/clips/{clip_id}/schedule")
async def api_schedule_clip(request: Request, clip_id: str, data: dict):
    user = require_user(request)
    clip = get_clip(clip_id, user["id"])
    if not clip:
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    platform = (data.get("platform") or "all").strip()
    when = (data.get("scheduled_at") or "").strip()
    try:
        from datetime import datetime
        datetime.fromisoformat(when)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Provide a valid scheduled_at ISO datetime."})
    post_id = schedule_post(user["id"], clip_id, platform, when)
    update_clip(clip_id, status="scheduled", scheduled_time=when)
    return {"success": True, "scheduled_id": post_id}


@app.get("/api/scheduled")
async def api_list_scheduled(request: Request):
    user = require_user(request)
    return {"scheduled": list_scheduled(user["id"])}


@app.delete("/api/scheduled/{post_id}")
async def api_cancel_scheduled(request: Request, post_id: int):
    user = require_user(request)
    rows = list_scheduled(user["id"])
    match = next((s for s in rows if s["id"] == post_id), None)
    if not match:
        return JSONResponse(status_code=404, content={"error": "Scheduled post not found"})
    update_scheduled(post_id, status="cancelled")
    if match.get("clip_id"):
        update_clip(match["clip_id"], status="ready")
    return {"success": True}


# ---------------------------------------------------------------------------
# History & analytics
# ---------------------------------------------------------------------------
@app.get("/api/history")
async def api_history(request: Request, search: str = "", status: str = "",
                      days: int = 0, limit: int = 20, offset: int = 0):
    user = require_user(request)
    return get_history(user["id"], search=search[:80], status=status[:20],
                       days=max(0, min(365, days)), limit=min(50, max(1, limit)),
                       offset=max(0, offset))


@app.get("/api/analytics")
async def api_analytics(request: Request):
    user = require_user(request)
    summary = analytics_summary(user["id"])
    summary["per_day"] = get_clips_per_day(user["id"], 7)
    summary["platform_posts"] = get_connection_counts(user["id"])
    summary["connections"] = account_status_summary(user["id"])
    return summary


# ---------------------------------------------------------------------------
# Connected accounts
# ---------------------------------------------------------------------------
@app.get("/api/accounts")
async def api_accounts(request: Request):
    user = require_user(request)
    return {"accounts": account_status_summary(user["id"]),
            "oauth_configured": {
                "instagram": bool(os.getenv("INSTAGRAM_CLIENT_ID")),
                "tiktok": bool(os.getenv("TIKTOK_CLIENT_KEY")),
                "youtube": bool(os.getenv("YOUTUBE_CLIENT_ID")),
            }}


@app.post("/api/accounts/buffer")
async def api_connect_buffer(request: Request, data: dict):
    user = require_user(request)
    token = (data.get("access_token") or "").strip()
    if not token or len(token) < 10:
        return JSONResponse(status_code=400, content={"error": "Enter your Buffer access token."})
    from backend.posters.buffer import get_buffer_profiles
    profiles, err = get_buffer_profiles(token)
    if err and not profiles:
        return JSONResponse(status_code=400, content={"error": err})
    upsert_account(user["id"], "buffer", access_token=token,
                   display_name=f"Buffer ({len(profiles)} channels)",
                   meta={"channels": profiles})
    remove_secret(user["id"], "buffer_access_token")  # migrated to encrypted account store
    return {"success": True, "channels": profiles, "accounts": account_status_summary(user["id"])}


@app.get("/api/accounts/buffer/channels")
async def api_buffer_channels(request: Request):
    user = require_user(request)
    acc = get_account(user["id"], "buffer")
    if not acc:
        return {"connected": False, "channels": [], "error": "Buffer not connected"}
    acc = refresh_account_token(acc)
    from backend.posters.buffer import get_buffer_profiles
    profiles, err = get_buffer_profiles(acc.get("access_token", ""))
    return {"connected": bool(profiles), "channels": profiles, "error": err}


@app.post("/api/accounts/{platform}/disconnect")
async def api_disconnect(request: Request, platform: str):
    user = require_user(request)
    if platform not in ("tiktok", "instagram", "youtube", "buffer"):
        return JSONResponse(status_code=400, content={"error": "Unknown platform"})
    remove_account(user["id"], platform)
    return {"success": True, "accounts": account_status_summary(user["id"])}


@app.post("/api/accounts/{platform}/refresh")
async def api_refresh_account(request: Request, platform: str):
    user = require_user(request)
    acc = get_account(user["id"], platform)
    if not acc:
        return JSONResponse(status_code=404, content={"error": "Not connected"})
    acc = refresh_account_token(acc)
    return {"success": True, "accounts": account_status_summary(user["id"])}


@app.get("/auth/{platform}/login")
async def auth_login_redirect(request: Request, platform: str):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=302)
    builder = {"instagram": get_instagram_auth_url, "tiktok": get_tiktok_auth_url,
               "youtube": get_youtube_auth_url}.get(platform)
    if not builder:
        return RedirectResponse("/?error=unknown_platform", status_code=302)
    url = builder(user["id"])
    if not url:
        return RedirectResponse("/?tab=accounts&error=oauth_not_configured", status_code=302)
    return RedirectResponse(url, status_code=302)


@app.get("/auth/{platform}/callback")
async def auth_callback(platform: str, code: str = None, error: str = None,
                        state: str = None):
    user_id, state_platform = consume_state(state or "")
    if not user_id or state_platform != platform:
        return RedirectResponse("/?tab=accounts&error=oauth_state_invalid", status_code=302)
    if error or not code:
        return RedirectResponse(f"/?tab=accounts&error={platform}_auth_failed", status_code=302)
    handler = {"instagram": handle_instagram_callback, "tiktok": handle_tiktok_callback,
               "youtube": handle_youtube_callback}.get(platform)
    if not handler:
        return RedirectResponse("/?tab=accounts&error=unknown_platform", status_code=302)
    res = handler(code, user_id)
    if res.get("success"):
        return RedirectResponse("/?tab=accounts&connected=" + platform, status_code=302)
    return RedirectResponse(f"/?tab=accounts&error={platform}_token_failed", status_code=302)


# ---------------------------------------------------------------------------
# Maintenance & export
# ---------------------------------------------------------------------------
@app.get("/api/storage")
async def api_storage(request: Request):
    user = require_user(request)
    usage = maintenance.storage_usage(user["id"])
    missing = maintenance.detect_missing_files(user["id"])
    usage["missing_clips"] = missing[:20]
    return usage


@app.post("/api/cleanup")
async def api_cleanup(request: Request):
    user = require_user(request)
    removed = maintenance.run_cleanup(user["id"])
    orphans = maintenance.remove_orphans(older_than_hours=24)
    return {"success": True, "removed": removed, "orphans_removed": orphans}


@app.get("/api/export/csv")
async def api_export_csv(request: Request):
    user = require_user(request)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "status", "hook_text", "caption", "hashtags",
                     "score", "scheduled_time", "project"])
    for c in list_clips(user["id"], limit=1000):
        tags = " ".join(c["hashtags"]) if isinstance(c["hashtags"], list) else (c["hashtags"] or "")
        writer.writerow([c["id"], c.get("created_at", ""), c.get("status", ""),
                         c.get("hook_text", ""), c.get("caption", ""), tags,
                         c.get("score", ""), c.get("scheduled_time", ""), c.get("title", "")])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=clipy_clips_export.csv"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
